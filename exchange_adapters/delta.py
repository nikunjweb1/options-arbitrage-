"""
Delta Exchange India adapter.

Phase 2 scope: read-only market data methods only (get_instruments,
get_option_chain, get_orderbook, get_ticker, get_fees, get_contract_specification).

Trading methods (place_order, cancel_order, modify_order) are implemented per
the ExchangeAdapter protocol for shape-consistency but explicitly raise
RuntimeError if LIVE_TRADING is False -- which it always is until Phase 8, and
even in Phase 8 requires a deliberate code change (see config/settings.py).

Endpoints used here are drawn from Delta's public docs (docs.delta.exchange,
api.india.delta.exchange) as researched in docs/architecture.md Section B:
  - REST base: https://api.india.delta.exchange  (production)
                https://cdn-ind.testnet.deltaex.org  (testnet)
  - GET /v2/products                -- full instrument list with contract specs
  - GET /v2/tickers                 -- tickers incl. quotes (bid/ask), greeks, IV
                                        filterable by contract_types & expiry_date
  - GET /v2/tickers/{symbol}        -- single-instrument ticker

NOTE: full L2 order-book depth beyond top-of-book (which the tickers endpoint's
`quotes` object provides) was not independently verified against a live
response during Phase 1 research -- confirm against testnet before relying on
`get_orderbook`'s `depth` parameter for anything beyond depth=1. This is a
known open item, not a silent assumption.

Settlement mechanics baked into normalization here reflect what's documented:
  - ALL options settle at 5:30 PM IST regardless of maturity (D1/D2/weekly/
    monthly differ by settlement *date*, never by settlement *time*).
  - Settlement price = max(30-min TWAP index price - strike, 0) for calls,
    mirrored for puts.
  - Options are European, cash-settled.
  - Settlement fee is waived entirely for contracts expiring OTM.
See docs/architecture.md Section 0 and Section C for why this matters.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests

from config.settings import DELTA
from exchange_adapters.base import (
    Balance,
    OrderBookSnapshot,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    TickerSnapshot,
)
from normalization.schemas import (
    ContractSpec,
    FeeSchedule,
    MarketSnapshot,
    OptionContract,
    OptionType,
    OptionVariant,
    SettlementMethod,
)

_DELTA_SETTLEMENT_FORMULA = "30min_twap_index"


class DeltaAdapterError(RuntimeError):
    """Raised on unexpected Delta API responses -- never silently swallowed."""


class DeltaAdapter:
    """
    Implements the ExchangeAdapter protocol (see exchange_adapters/base.py)
    for Delta Exchange India.
    """

    exchange_name = "delta_india"

    def __init__(self, session: requests.Session | None = None) -> None:
        self._base_url = DELTA.rest_base_url
        self._session = session or requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # -- internal -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        resp = self._session.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            raise DeltaAdapterError(
                f"Delta API GET {url} returned {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()
        if not payload.get("success", False):
            raise DeltaAdapterError(f"Delta API GET {url} success=false: {payload}")
        return payload["result"]

    @staticmethod
    def _parse_option_variant(product: dict[str, Any]) -> OptionVariant:
        contract_type = product.get("contract_type", "")
        if "turbo" in contract_type.lower():
            return OptionVariant.TURBO
        if "spread" in contract_type.lower():
            return OptionVariant.SPREAD
        if contract_type in ("call_options", "put_options"):
            return OptionVariant.VANILLA
        return OptionVariant.OTHER

    @staticmethod
    def _settlement_timestamp(product: dict[str, Any]) -> datetime:
        """
        Every Delta options contract settles at 5:30 PM IST on its settlement
        date. This does NOT come from a free-text field we trust blindly --
        we derive it from the documented expiry date plus the fixed clock
        time, and if Delta's API ever returns an explicit settlement_time
        that disagrees with 5:30 PM IST, that's a signal to re-verify Section
        0's finding, not to silently prefer one source over the other.
        """
        expiry_raw = product.get("settlement_time") or product.get("expiry_time")
        if not expiry_raw:
            raise DeltaAdapterError(
                f"Product {product.get('symbol')} missing settlement/expiry time field"
            )
        # Delta returns ISO8601 UTC timestamps for expiry; parse and trust it
        # directly rather than re-deriving from 5:30 PM IST, since the API is
        # the source of truth -- but log/flag if it doesn't match our Section 0
        # expectation so drift gets caught.
        return datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))

    def _normalize_product(self, product: dict[str, Any]) -> OptionContract | None:
        contract_type = product.get("contract_type", "")
        if contract_type not in ("call_options", "put_options"):
            return None  # not an option -- skip futures/perps/spreads here

        try:
            settlement_ts = self._settlement_timestamp(product)
        except DeltaAdapterError:
            return None

        return OptionContract(
            exchange=self.exchange_name,
            underlying=product.get("underlying_asset", {}).get("symbol", ""),
            base_asset=product.get("underlying_asset", {}).get("symbol", ""),
            quote_asset=product.get("quoting_asset", {}).get("symbol", "USD"),
            option_type=OptionType.CALL if contract_type == "call_options" else OptionType.PUT,
            option_variant=self._parse_option_variant(product),
            strike=Decimal(str(product.get("strike_price", "0"))),
            expiry_timestamp=settlement_ts,
            settlement_timestamp=settlement_ts,
            settlement_method=SettlementMethod.CASH,
            settlement_price_formula=_DELTA_SETTLEMENT_FORMULA,
            contract_multiplier=Decimal(str(product.get("contract_value", "1"))),
            lot_size=Decimal(str(product.get("lot_size", "1"))),
            tick_size=Decimal(str(product.get("tick_size", "0.5"))),
            quote_currency=product.get("quoting_asset", {}).get("symbol", "USD"),
            settlement_currency=product.get("settlement_currency", "USDT"),
            contract_symbol=product.get("symbol", ""),
            instrument_id=str(product.get("id", product.get("symbol", ""))),
            is_european=True,
        )

    # -- ExchangeAdapter protocol implementation -----------------------------

    def get_instruments(self) -> list[OptionContract]:
        raw_products = self._get("/v2/products")
        contracts: list[OptionContract] = []
        for product in raw_products:
            normalized = self._normalize_product(product)
            if normalized is not None:
                contracts.append(normalized)
        return contracts

    def get_option_chain(
        self, underlying: str, expiry: datetime | None = None
    ) -> list[OptionContract]:
        """
        Bug found + fixed during Phase 2 testnet validation (2026-08-21): this
        originally called GET /v2/tickers with contract_types +
        underlying_asset_symbols params. That call succeeds and returns real
        rows, but a ticker row is NOT the same shape as a /v2/products row,
        and _normalize_product assumes the /v2/products shape:

          - /v2/tickers has no settlement_time or expiry_time field at all
            (confirmed against a live testnet response -- full key list was
            ['close', 'contract_type', 'contract_value', 'description',
            'greeks', 'high', 'leverage', 'low', 'ltp_change_24h',
            'mark_change_24h', 'mark_high_24h', 'mark_low_24h', 'mark_price',
            'mark_vol', 'oi', 'oi_change_usd_6h', 'oi_contracts',
            'oi_reduce_only_mode', 'oi_value', 'oi_value_symbol',
            'oi_value_usd', 'open', 'price_band', 'product_id',
            'product_trading_status', 'quotes', 'size', 'sort_priority',
            'spot_price', 'strike_price', 'symbol', 'tags', 'tick_size',
            'time', 'timestamp', 'top_tag', 'turnover', 'turnover_symbol',
            'turnover_usd', 'underlying_asset_symbol', 'volume']).
            _settlement_timestamp() requires one of those two fields and
            raises otherwise; _normalize_product catches that and returns
            None, silently dropping EVERY row. That's why get_option_chain
            always returned [] even though the underlying testnet data was
            fine (336 live BTC options existed at the time this was found).
          - /v2/tickers also flattens underlying_asset into a plain string
            (underlying_asset_symbol) instead of /v2/products' nested
            {"symbol": ...} object, so even contracts that got past the
            settlement-time guard would have had blank underlying/base_asset
            fields.

        Fix: use /v2/products instead -- the same endpoint and response shape
        get_instruments() already normalizes correctly -- and filter
        client-side by underlying, option contract type, and live state.
        We filter client-side rather than relying on /v2/products' own query
        params because that filtering behavior isn't confirmed the way
        /v2/tickers' was; the whole point of this fix is to stop trusting
        unconfirmed assumptions about this API.
        """
        raw_products = self._get("/v2/products")
        contracts: list[OptionContract] = []
        for product in raw_products:
            if product.get("contract_type") not in ("call_options", "put_options"):
                continue
            if product.get("state") != "live":
                continue
            if product.get("underlying_asset", {}).get("symbol") != underlying:
                continue
            normalized = self._normalize_product(product)
            if normalized is None:
                continue
            if expiry is not None and normalized.expiry_timestamp.date() != expiry.date():
                continue
            contracts.append(normalized)
        return contracts

    def get_orderbook(self, instrument_id: str, depth: int = 5) -> OrderBookSnapshot:
        # NOTE per module docstring: top-of-book only verified. `depth` beyond
        # 1 is accepted for interface-compatibility but not yet guaranteed to
        # return more than best bid/ask until re-verified against a live L2
        # endpoint.
        ticker = self._get(f"/v2/tickers/{instrument_id}")
        quotes = ticker.get("quotes", {})
        bids: list[tuple[Decimal, Decimal]] = []
        asks: list[tuple[Decimal, Decimal]] = []
        if quotes.get("best_bid") is not None:
            bids.append(
                (Decimal(str(quotes["best_bid"])), Decimal(str(quotes.get("bid_size", "0"))))
            )
        if quotes.get("best_ask") is not None:
            asks.append(
                (Decimal(str(quotes["best_ask"])), Decimal(str(quotes.get("ask_size", "0"))))
            )
        return OrderBookSnapshot(
            instrument_id=instrument_id,
            timestamp=datetime.now(timezone.utc),
            bids=bids,
            asks=asks,
        )

    def get_ticker(self, instrument_id: str) -> TickerSnapshot:
        raw = self._get(f"/v2/tickers/{instrument_id}")
        quotes = raw.get("quotes", {})
        greeks = raw.get("greeks", {})

        def _dec_or_none(v: Any) -> Decimal | None:
            return Decimal(str(v)) if v is not None else None

        snapshot = MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange=self.exchange_name,
            instrument_id=instrument_id,
            best_bid=_dec_or_none(quotes.get("best_bid")),
            best_ask=_dec_or_none(quotes.get("best_ask")),
            bid_size=_dec_or_none(quotes.get("bid_size")),
            ask_size=_dec_or_none(quotes.get("ask_size")),
            last_price=_dec_or_none(raw.get("close")),
            mark_price=_dec_or_none(raw.get("mark_price")),
            index_price=_dec_or_none(raw.get("spot_price")),
            iv=_dec_or_none(raw.get("mark_vol") or raw.get("iv")),
            delta=_dec_or_none(greeks.get("delta")),
            gamma=_dec_or_none(greeks.get("gamma")),
            theta=_dec_or_none(greeks.get("theta")),
            vega=_dec_or_none(greeks.get("vega")),
            open_interest=_dec_or_none(raw.get("oi")),
            volume_24h=_dec_or_none(raw.get("volume")),
            underlying_spot=_dec_or_none(raw.get("spot_price")),
            underlying_index=_dec_or_none(raw.get("spot_price")),
            underlying_futures=None,
            funding_rate=None,
        )
        return TickerSnapshot(
            instrument_id=instrument_id, timestamp=snapshot.timestamp, snapshot=snapshot
        )

    def get_positions(self) -> list[Position]:
        raise NotImplementedError(
            "Requires authenticated account access -- not built in Phase 2 "
            "(market-data collection only). See docs/architecture.md Phase roadmap."
        )

    def get_balance(self) -> Balance:
        raise NotImplementedError(
            "Requires authenticated account access -- not built in Phase 2."
        )

    def place_order(self, order: OrderRequest) -> OrderResult:
        from config.settings import LIVE_TRADING

        if not LIVE_TRADING:
            raise RuntimeError(
                "place_order() blocked: LIVE_TRADING is False. This is the "
                "expected state through Phase 7. See config/settings.py and "
                "docs/architecture.md Section 29."
            )
        raise NotImplementedError("Execution engine not built yet (Phase 8).")

    def cancel_order(self, order_id: str) -> bool:
        from config.settings import LIVE_TRADING

        if not LIVE_TRADING:
            raise RuntimeError("cancel_order() blocked: LIVE_TRADING is False.")
        raise NotImplementedError("Execution engine not built yet (Phase 8).")

    def modify_order(self, order_id: str, changes: dict) -> OrderResult:
        from config.settings import LIVE_TRADING

        if not LIVE_TRADING:
            raise RuntimeError("modify_order() blocked: LIVE_TRADING is False.")
        raise NotImplementedError("Execution engine not built yet (Phase 8).")

    def get_order_status(self, order_id: str) -> OrderStatus:
        raise NotImplementedError(
            "Requires authenticated account access -- not built in Phase 2."
        )

    def get_fees(self) -> FeeSchedule:
        # Values per docs/architecture.md Section B, sourced from Delta's
        # published fee pages. Reconcile against your actual account's fee
        # tier before using these numbers in any P&L calculation -- India vs
        # Global entity pages showed slightly different fee-cap percentages
        # (7.5% vs 12.5%) during Phase 1 research and this needs pinning down
        # against the specific account this system trades on.
        return FeeSchedule(
            exchange=self.exchange_name,
            maker_fee_pct=Decimal("0.0003"),  # placeholder -- verify against account fee tier
            taker_fee_pct=Decimal("0.0005"),  # placeholder -- verify against account fee tier
            settlement_fee_pct=Decimal("0.0005"),  # placeholder -- verify
            fee_cap_pct_of_premium=Decimal("0.075"),  # per India fee page; Global page said 7.5% too but double check
            zero_fee_on_otm_settlement=True,  # documented: OTM settlement is fee-free
            additional_tax_pct=Decimal("0.18"),  # 18% GST, India-specific
            source_url="https://www.delta.exchange/fees",
        )

    def get_contract_specification(self, instrument_id: str) -> ContractSpec:
        raw = self._get(f"/v2/products/{instrument_id}")
        return ContractSpec(
            instrument_id=instrument_id,
            exchange=self.exchange_name,
            contract_multiplier=Decimal(str(raw.get("contract_value", "1"))),
            lot_size=Decimal(str(raw.get("lot_size", "1"))),
            tick_size=Decimal(str(raw.get("tick_size", "0.5"))),
            settlement_method=SettlementMethod.CASH,
            settlement_price_formula=_DELTA_SETTLEMENT_FORMULA,
            option_variant=self._parse_option_variant(raw),
            is_european=True,
            fetched_at=datetime.now(timezone.utc),
        )
