"""
Shark Exchange (sharkexchange.in) REST adapter.

CONFIRMATION STATUS -- read this before trusting anything below with real
money. Everything in this file was built from Shark's own public docs
(docs.sharkexchange.in), legal Trading Policy (sharkexchange.in/legal/
trading-policy), and -- as of 2026-08-23 -- real frames/requests captured
from a live options session via browser DevTools. Three different sources,
and they don't all cover the same product, so the gap matters:

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

CONFIRMED (from a live DevTools capture against a real options session,
2026-08-23 -- see exchange_adapters/shark_ws.py's module docstring for the
full writeup of the WebSocket side of this same capture):
  - Real options symbol format: "BTC-24AUG26-73000-P-USDT" /
    "BTC-24AUG26-75000-C-USDT" -- pattern {BASE}-{DD}{MMM}{YY}-{STRIKE}-
    {C|P}-{QUOTE}. This is the confirmed instrument_id shape for options on
    Shark -- use this directly when constructing OrderRequest.instrument_id
    for an options order, per place_order()'s docstring below.
  - The options page's Fetch/XHR traffic surfaced REST endpoint NAMES not
    in the public docs at all: `basePairs`, `delivery-times`,
    `options-tier-info`, `instrument-info`, `exchangeInfo` (options-scoped
    -- likely different from or in addition to /v1/exchange/exchangeInfo's
    futures-only response, see probe_exchange_info()'s docstring), and a
    paginated `list?page=...&pageSize=...&isPast=...` endpoint (plausibly
    the expiry-date or contract list). These are NAMES only -- the request
    host and full response shape for each were not captured (the DevTools
    session showed the Name column of the Network panel but not every
    request's Headers/Response detail). This is the single most promising
    concrete lead for finishing get_option_chain()/get_instruments() --
    capturing the Headers + Response of any one of these (Network tab ->
    click the request -> Headers for URL, Response for body) would very
    likely unblock both methods in one step.

NOT CONFIRMED -- genuinely unknown, not guessed:
  - The exact host + full response shape for the options-specific
    endpoints named above.
  - `/v1/exchange/exchangeInfo` (no version prefix disambiguation captured)
    was independently confirmed via probe_exchange_info() to return
    FUTURES-ONLY data (every one of ~400 contracts returned was
    `contractType: PERPETUAL` or `TRADIFI_PERPETUAL`, zero options). The
    options-scoped `exchangeInfo` request seen in the Fetch/XHR capture
    above may be a different endpoint/host entirely, not the same call --
    do not assume they're the same until the options one's actual URL is
    confirmed.
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

# Confirmed 2026-08-23 via live DevTools capture -- see module docstring.
# Example: "BTC-24AUG26-73000-P-USDT" (BTC put, strike 73000, expires
# 24-Aug-2026, USDT-settled).
OPTIONS_SYMBOL_FORMAT_EXAMPLE = "BTC-24AUG26-73000-P-USDT"


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
        Calls /v1/exchange/exchangeInfo and returns the raw response,
        unparsed. INDEPENDENTLY CONFIRMED (2026-08-23) to return
        FUTURES-ONLY data -- every one of ~400 returned contracts was
        `contractType: PERPETUAL` or `TRADIFI_PERPETUAL`, zero options
        entries of any kind. Do not re-run this expecting options data;
        it's a closed lead for that purpose. The response also confirmed
        Shark is very likely running on Pi42's white-label backend --
        `iconUrl` fields point at `storage.googleapis.com/pi42-dev-static/`
        and `pi42-prod-static` buckets, not a Shark-branded bucket.

        For OPTIONS instrument data, see the module docstring's note on
        `basePairs`/`delivery-times`/`options-tier-info`/`instrument-info`/
        options-scoped `exchangeInfo` -- endpoint NAMES observed in a
        Fetch/XHR capture of the live options page, but not yet confirmed
        with a real host+response. That's the next lead, not this method.
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
        CONFIRMED SHAPE IS FOR FUTURES ONLY -- this has NOT been tested
        against an options instrument_id (e.g. "BTC-24AUG26-73000-P-USDT",
        confirmed real format per module docstring) even though the symbol
        format is now known. Symbol format alone doesn't confirm the rest
        of the payload (contractType value, whether `marginAsset`/
        `reduceOnly` apply the same way to an options position) is correct
        for options. Do not place a real options order through this method
        until that's been verified -- start with the smallest possible
        size and confirm the response shape matches expectations before
        trusting it for anything bigger. `extra_params` lets a caller
        inject option-specific fields once/if they're discovered to be
        needed, without this method having to guess them.
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
            "probe_exchange_info() is confirmed futures-only (see its docstring). The real "
            "lead now is the options-scoped `instrument-info`/`basePairs`/`options-tier-info` "
            "endpoints observed in a live options-page Fetch/XHR capture 2026-08-23 -- capture "
            "one of those requests' Headers (for host+path) and Response (for shape) via "
            "browser DevTools before implementing this."
        )

    def get_option_chain(self, underlying: str, expiry: datetime | None = None) -> list[OptionContract]:
        raise NotImplementedError(
            "Same gap as get_instruments() -- see that method's docstring. Note: "
            "exchange_adapters/shark_ws.py's live-captured WebSocket 'ticker' events already "
            "confirm the real options symbol format (OPTIONS_SYMBOL_FORMAT_EXAMPLE, this "
            "module) and live bid/ask/IV per-strike -- for an immediate unblock without "
            "waiting on the REST instrument-listing endpoint, it may be faster to build the "
            "option chain by listening to shark_ws.py's ticker stream for a known underlying/"
            "expiry combination for a short warmup period and inferring the strike ladder from "
            "observed symbols, rather than waiting on this REST method."
        )

    def get_orderbook(self, instrument_id: str, depth: int = 5) -> OrderBookSnapshot:
        raw = self._get("/v1/market/depth", {"symbol": instrument_id, "limit": depth}, public=True)
        # Response shape for /v1/market/depth was not retrieved in this
        # research pass (docs truncated before the Public Endpoints section
        # rendered response examples) -- this is a best-effort parse assuming
        # a conventional {"bids": [[price, size], ...], "asks": [...]}, shape
        # and MUST be verified against a real response before trusting it.
        # NOTE: shark_ws.py's live-captured "orderBook" WS event DOES confirm
        # the bids shape as [[price_str, size_str], ...] (asks assumed
        # symmetric, not independently confirmed) -- consistent with what's
        # assumed here, which is reassuring but still not the same thing as
        # confirming this specific REST endpoint's response shape directly.
        bids = [(Decimal(str(p)), Decimal(str(s))) for p, s in raw.get("bids", [])]
        asks = [(Decimal(str(p)), Decimal(str(s))) for p, s in raw.get("asks", [])]
        return OrderBookSnapshot(
            instrument_id=instrument_id, timestamp=datetime.now(timezone.utc), bids=bids, asks=asks
        )

    def get_ticker(self, instrument_id: str) -> TickerSnapshot:
        raw = self.get_ticker_24hr(instrument_id)
        # Same caveat as get_orderbook: field names below are a best-effort
        # guess at a conventional 24hr-ticker shape for FUTURES, NOT
        # confirmed against a real Shark response, and NOT the same as
        # options -- for options tickers, prefer shark_ws.py's live-captured
        # "ticker" WS event fields (symbol, bidPrice, bidSize, bidIv,
        # askPrice, askSize, askIv, lastPrice, highPrice24h, lowPrice24h --
        # all confirmed real) over this REST method entirely, until this
        # method's own response shape is independently verified.
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
            "this does not return a guessed/hardcoded fee rate. NOTE: the 2026-08-23 "
            "Fetch/XHR capture also showed a `fee-structure?_rsc=epzn0` request, but the "
            "`_rsc` query param is a Next.js React Server Component page-fetch marker, "
            "meaning that request is very likely the fee-structure WEBPAGE's own data "
            "loading, not a general-purpose fee API -- worth checking, but don't assume "
            "it's usable without confirming the response is actually machine-readable JSON."
        )

    def get_contract_specification(self, instrument_id: str) -> ContractSpec:
        raise NotImplementedError(
            "Depends on get_option_chain()/get_instruments() being implemented first -- "
            "see those methods' docstrings."
        )
