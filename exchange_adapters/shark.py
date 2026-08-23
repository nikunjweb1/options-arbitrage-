"""
Shark Exchange (sharkexchange.in) REST adapter.

CONFIRMATION STATUS -- read this before trusting anything below with real
money. Everything in this file was built from Shark's own public docs
(docs.sharkexchange.in) and legal Trading Policy (sharkexchange.in/legal/
trading-policy), not from guessing -- but those two sources don't cover the
same product, and that gap matters:

CONFIRMED (from docs.sharkexchange.in, retrieved 2026-08-23):
  - Auth scheme: HMAC-SHA256 over the query string (GET) or JSON body
    (POST/PUT/PATCH/DELETE), sent as `api-key` + `signature` headers.
  - Base URL: https://api.sharkexchange.in/
  - FUTURES endpoints: place-order, edit-order, delete-order, open-orders,
    order-history, positions, trade-history, transaction-history,
    update/leverage, update/preference, add-margin, reduce-margin --
    all confirmed with exact request/response shapes, all using
    `contractType: "PERPETUAL"` and futures-style symbols (e.g. BTCINR,
    BTCUSDT).
  - Public (no-auth) endpoints: /v1/market/klines, /v1/market/depth,
    /v1/market/aggTrade, /v1/market/ticker24Hr.
  - Rate limits: place-order 20/1s, delete-order 30/1m, everything else
    60/1m.

NOT CONFIRMED -- genuinely unknown, not guessed:
  - Shark's public API reference has NO Options section at all, despite
    Options being a live, documented product (with its own legal Trading
    Policy, its own settlement-price formula, its own pages at
    sharkexchange.in/options/btcusdt) -- the REST reference only documents
    Futures. Every field name, symbol format, and required parameter for
    placing an OPTIONS order via this API is unverified.
  - `/v1/exchange/exchangeInfo` is referenced only in the docs changelog
    (as an error-code entry), never documented with a request/response
    shape. This is the most promising lead for discovering whether options
    are exposed via this API at all, and if so what their instrument
    listing looks like -- see get_option_chain()'s docstring for the
    concrete next step to resolve this.
  - get_fees() below is NOT wired to a real endpoint -- no fee-schedule API
    endpoint was found in the docs; sharkexchange.in/fee-structure is a
    webpage, not confirmed as an API. This raises NotImplementedError
    rather than silently returning a guessed/hardcoded fee, per this
    codebase's fail-closed principle (see pricing/ev_engine.py's
    InsufficientDataError for the same pattern elsewhere).

DO NOT place a real options order through place_order() until
get_option_chain() has been run against real credentials and its output
manually inspected -- see that method's docstring for exactly what to look
for.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests

from config.settings import DELTA  # noqa: F401 -- not used yet; SharkConfig follows the same env-var pattern below
from exchange_adapters.base import (
    Balance,
    OrderBookSnapshot,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    TickerSnapshot,
)
from normalization.schemas import ContractSpec, FeeSchedule, MarketSnapshot, OptionContract

logger = logging.getLogger("exchange_adapters.shark")

_BASE_URL = "https://api.sharkexchange.in"
_REQUEST_TIMEOUT_SEC = 10


class SharkAdapterError(RuntimeError):
    """Raised on any Shark API failure -- HTTP error, malformed response, or
    (for options-specific calls) confirmed-unsupported-by-this-API-surface."""


class SharkAdapter:
    """
    Implements exchange_adapters.base.ExchangeAdapter for Shark Exchange.

    Constructor takes api_key/api_secret directly rather than reading env
    vars itself (unlike DeltaAdapter's use of the DELTA config singleton) --
    Shark credential env-var names haven't been standardized in
    config/settings.py yet since this adapter is new; wire that up once this
    adapter is confirmed working end-to-end rather than guessing a
    SHARK_API_KEY/SHARK_API_SECRET naming convention now and having to
    migrate it later.
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._session = requests.Session()

    # -- signing --------------------------------------------------------------

    def _sign(self, data_to_sign: str) -> str:
        return hmac.new(
            self._api_secret.encode("utf-8"), data_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _timestamp_ms(self) -> str:
        return str(int(time.time() * 1000))

    def _get(self, path: str, params: dict | None = None, public: bool = False) -> Any:
        params = dict(params or {})
        if not public:
            params["timestamp"] = self._timestamp_ms()
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{_BASE_URL}{path}"
        headers = {}
        if not public:
            headers = {"api-key": self._api_key, "signature": self._sign(query_string)}
        try:
            resp = self._session.get(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise SharkAdapterError(f"Shark API GET {path} failed: {exc}") from exc

    def _post(self, path: str, params: dict | None = None) -> Any:
        params = dict(params or {})
        params["timestamp"] = self._timestamp_ms()
        data_to_sign = json.dumps(params, separators=(",", ":"))
        headers = {
            "api-key": self._api_key,
            "signature": self._sign(data_to_sign),
            "Content-Type": "application/json",
        }
        url = f"{_BASE_URL}{path}"
        try:
            resp = self._session.post(url, json=params, headers=headers, timeout=_REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise SharkAdapterError(f"Shark API POST {path} failed: {exc}") from exc

    def _delete(self, path: str, params: dict | None = None) -> Any:
        params = dict(params or {})
        params["timestamp"] = self._timestamp_ms()
        data_to_sign = json.dumps(params, separators=(",", ":"))
        headers = {"api-key": self._api_key, "signature": self._sign(data_to_sign)}
        url = f"{_BASE_URL}{path}"
        try:
            resp = self._session.delete(url, json=params, headers=headers, timeout=_REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise SharkAdapterError(f"Shark API DELETE {path} failed: {exc}") from exc

    # -- confirmed: exchangeInfo probe -----------------------------------------

    def probe_exchange_info(self) -> Any:
        """
        Calls the undocumented-but-referenced /v1/exchange/exchangeInfo
        endpoint and returns the raw response, unparsed. This is the
        diagnostic entry point for discovering whether Options are exposed
        via this REST API and what their instrument spec looks like --
        run this FIRST, inspect the raw output for anything resembling
        strike/optionType/expiry fields, and only then decide how (or
        whether) to implement get_option_chain() for real. Deliberately not
        parsed into OptionContract here, since the shape is unknown.
        """
        return self._get("/v1/exchange/exchangeInfo")

    # -- ExchangeAdapter protocol: confirmed (futures-shaped) methods ---------

    def get_positions(self, position_status: str = "OPEN") -> list[Position]:
        raw = self._get(f"/v1/positions/{position_status}")
        positions = raw if isinstance(raw, list) else [raw]
        return [
            Position(
                instrument_id=p["contractPair"],
                quantity=Decimal(str(p["positionAmount"])),
                entry_price=Decimal(str(p["entryPrice"])),
                unrealized_pnl=Decimal(str(p.get("realizedProfit") or 0)),
            )
            for p in positions
        ]

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        return self._get("/v1/order/open-orders", params)

    def place_order(self, order: OrderRequest, extra_params: dict | None = None) -> OrderResult:
        """
        CONFIRMED SHAPE IS FOR FUTURES ONLY. Do not call this for an options
        instrument_id until probe_exchange_info() (or direct empirical
        testing with a single minimal-size order) has confirmed what
        `symbol` format and `contractType`/equivalent options need. Passing
        `extra_params` lets a caller inject option-specific fields once
        they're known, without this method having to guess them.
        """
        params = {
            "placeType": "ORDER_FORM",
            "quantity": float(order.quantity),
            "side": order.side.upper(),
            "symbol": order.instrument_id,
            "reduceOnly": False,
            "marginAsset": "INR",
            "type": order.order_type.upper(),
        }
        if order.order_type.lower() == "limit":
            params["price"] = float(order.limit_price)
        if extra_params:
            params.update(extra_params)

        resp = self._post("/v1/order/place-order", params)
        return OrderResult(
            order_id=resp["clientOrderId"],
            status="NEW" if resp.get("filledAmount", 0) == 0 else "PARTIALLY_FILLED",
            filled_quantity=Decimal(str(resp.get("filledAmount", 0))),
            avg_fill_price=None,
        )

    def cancel_order(self, order_id: str) -> bool:
        resp = self._delete("/v1/order/delete-order", {"clientOrderId": order_id})
        return bool(resp)

    def get_order_status(self, order_id: str) -> OrderStatus:
        resp = self._get("/v1/order", public=False).get(order_id, {})  # placeholder path shape; see note below
        # NOTE: docs show GET /v1/order/{clientOrderId} as a path parameter,
        # not a query param -- fix call site once wired: self._get(f"/v1/order/{order_id}")
        return OrderStatus(
            order_id=order_id,
            status=resp.get("status", "UNKNOWN"),
            filled_quantity=Decimal(str(resp.get("filledQty", 0))),
            remaining_quantity=Decimal(str(resp.get("quantity", 0))) - Decimal(str(resp.get("filledQty", 0))),
        )

    def get_ticker_24hr(self, symbol: str) -> dict:
        """Confirmed public endpoint. Futures symbols only, per docs -- see
        module docstring's NOT CONFIRMED section for options."""
        return self._get("/v1/market/ticker24Hr", {"symbol": symbol}, public=True)

    # -- ExchangeAdapter protocol: NOT YET IMPLEMENTABLE with confidence -----

    def get_instruments(self) -> list[OptionContract]:
        raise NotImplementedError(
            "Shark's public API docs don't cover options instrument listing. "
            "Call probe_exchange_info() first and inspect the raw response for "
            "strike/expiry/optionType-shaped fields before implementing this."
        )

    def get_option_chain(self, underlying: str, expiry: datetime | None = None) -> list[OptionContract]:
        raise NotImplementedError(
            "Same gap as get_instruments() -- see that method's docstring and "
            "probe_exchange_info()."
        )

    def get_orderbook(self, instrument_id: str, depth: int = 5) -> OrderBookSnapshot:
        raw = self._get("/v1/market/depth", {"symbol": instrument_id, "limit": depth}, public=True)
        # Response shape for /v1/market/depth was not retrieved in this
        # research pass (docs truncated before the Public Endpoints section
        # rendered response examples) -- this is a best-effort parse assuming
        # a conventional {"bids": [[price, size], ...], "asks": [...]}, shape
        # and MUST be verified against a real response before trusting it.
        bids = [(Decimal(str(p)), Decimal(str(s))) for p, s in raw.get("bids", [])]
        asks = [(Decimal(str(p)), Decimal(str(s))) for p, s in raw.get("asks", [])]
        return OrderBookSnapshot(
            instrument_id=instrument_id, timestamp=datetime.now(timezone.utc), bids=bids, asks=asks
        )

    def get_ticker(self, instrument_id: str) -> TickerSnapshot:
        raw = self.get_ticker_24hr(instrument_id)
        # Same caveat as get_orderbook: field names below are a best-effort
        # guess at a conventional 24hr-ticker shape, NOT confirmed against a
        # real Shark response (docs truncated before showing one). Verify
        # against actual output before trusting any field here, especially
        # for options symbols which aren't confirmed to work at all.
        snapshot = MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id=instrument_id,
            best_bid=Decimal(str(raw["bidPrice"])) if "bidPrice" in raw else None,
            best_ask=Decimal(str(raw["askPrice"])) if "askPrice" in raw else None,
            bid_size=None,
            ask_size=None,
            last_price=Decimal(str(raw["lastPrice"])) if "lastPrice" in raw else None,
            mark_price=None,
            index_price=None,
            iv=None,
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            open_interest=None,
            volume_24h=Decimal(str(raw["volume"])) if "volume" in raw else None,
            underlying_spot=None,
            underlying_index=None,
            underlying_futures=None,
            funding_rate=None,
        )
        return TickerSnapshot(instrument_id=instrument_id, timestamp=snapshot.timestamp, snapshot=snapshot)

    def get_balance(self) -> Balance:
        raise NotImplementedError(
            "No confirmed balance/wallet endpoint found in docs.sharkexchange.in's "
            "documented sections -- 'Get futures wallet details' and 'Get funding "
            "wallet details' are listed in the sidebar nav but their request/response "
            "shapes weren't retrieved in this research pass. Fetch and confirm before implementing."
        )

    def modify_order(self, order_id: str, changes: dict) -> OrderResult:
        params = {"clientOrderId": order_id, **changes}
        # docs use PATCH /v1/order/edit-order -- this adapter's _post/_get/_delete
        # helpers don't currently have a _patch variant; add one mirroring
        # _post's signing (JSON body) before wiring this up for real.
        raise NotImplementedError("PATCH helper not yet implemented -- see inline note.")

    def get_fees(self) -> FeeSchedule:
        raise NotImplementedError(
            "No confirmed fee-schedule API endpoint found. sharkexchange.in/fee-structure "
            "is a webpage, not confirmed as an API response. Per this codebase's "
            "fail-closed principle (see pricing/ev_engine.py InsufficientDataError), "
            "this does not return a guessed/hardcoded fee rate."
        )

    def get_contract_specification(self, instrument_id: str) -> ContractSpec:
        raise NotImplementedError(
            "Depends on get_option_chain()/get_instruments() being implemented first -- "
            "see those methods' docstrings."
        )
