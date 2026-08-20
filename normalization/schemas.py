"""
Exchange-agnostic normalized schemas.

Every exchange adapter's normalizer must produce these, and only these,
shapes. Nothing downstream of the normalization layer (matching engine,
pricing engine, scanner) is allowed to import an exchange-specific type.

See docs/architecture.md Section E and Section C for the rationale behind
every field -- several of these fields exist specifically to catch the
false-positive-arbitrage failure modes documented there (settlement clock,
settlement price basis, contract multiplier, option variant, etc).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class OptionVariant(str, Enum):
    """
    Per docs/architecture.md Section C.5: Delta offers exotic knockout ("Turbo")
    options alongside vanilla European options. A matching engine that doesn't
    check this field could pair a vanilla contract against a barrier contract
    that merely has a similar-looking symbol. Never assume "vanilla" by default.
    """

    VANILLA = "vanilla"
    TURBO = "turbo"
    SPREAD = "spread"
    OTHER = "other"


class SettlementMethod(str, Enum):
    CASH = "cash"
    PHYSICAL = "physical"


@dataclass(frozen=True)
class OptionContract:
    """
    Normalized representation of a single option instrument on a single exchange.
    One of these per (exchange, instrument_id) pair -- never merge two exchanges'
    contracts into one record even if strike/expiry/underlying look identical.
    """

    exchange: str
    underlying: str
    base_asset: str
    quote_asset: str
    option_type: OptionType
    option_variant: OptionVariant
    strike: Decimal
    expiry_timestamp: datetime  # UTC, timezone-aware, always
    settlement_timestamp: datetime  # may differ from expiry_timestamp
    settlement_method: SettlementMethod
    settlement_price_formula: str  # e.g. "30min_twap_index" -- human-readable, used by Section C checks
    contract_multiplier: Decimal
    lot_size: Decimal
    tick_size: Decimal
    quote_currency: str
    settlement_currency: str
    contract_symbol: str
    instrument_id: str
    is_european: bool

    def __post_init__(self) -> None:
        if self.expiry_timestamp.tzinfo is None:
            raise ValueError(
                f"{self.instrument_id}: expiry_timestamp must be timezone-aware "
                "(UTC). Naive datetimes are a common source of cross-exchange "
                "settlement-clock bugs -- see architecture.md Section C.1."
            )
        if self.settlement_timestamp.tzinfo is None:
            raise ValueError(
                f"{self.instrument_id}: settlement_timestamp must be timezone-aware (UTC)."
            )
        if self.contract_multiplier <= 0:
            raise ValueError(f"{self.instrument_id}: contract_multiplier must be > 0")
        if self.lot_size <= 0:
            raise ValueError(f"{self.instrument_id}: lot_size must be > 0")


@dataclass(frozen=True)
class MarketSnapshot:
    """
    Normalized point-in-time market data for one instrument on one exchange.

    IMPORTANT: best_bid/best_ask are the only fields that may be used for
    entry/exit P&L calculations (see architecture.md Section A.1 -- "executable
    price only"). mark_price and index_price exist for margin/risk calculations
    only. Do not substitute mark_price for best_bid/best_ask "when the book is
    thin" -- if the book is thin, that's a liquidity-risk signal, not a reason
    to use a different price source.
    """

    timestamp: datetime
    exchange: str
    instrument_id: str

    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    book_levels: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # [(price, size), ...]

    last_price: Decimal | None = None
    mark_price: Decimal | None = None
    index_price: Decimal | None = None

    iv: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None

    open_interest: Decimal | None = None
    volume_24h: Decimal | None = None

    underlying_spot: Decimal | None = None
    underlying_index: Decimal | None = None
    underlying_futures: Decimal | None = None
    funding_rate: Decimal | None = None

    ingested_at: datetime = field(default_factory=datetime.utcnow)

    def is_executable(self) -> bool:
        """
        Fail-closed check per architecture.md Section A.1: a snapshot with no
        bid or no ask cannot be used to compute an executable entry/exit price.
        Callers in the scanner/matching engine must check this before using
        best_bid/best_ask in any P&L formula.
        """
        return self.best_bid is not None and self.best_ask is not None


@dataclass(frozen=True)
class FeeSchedule:
    """Per-exchange fee schedule, as documented (never guessed)."""

    exchange: str
    maker_fee_pct: Decimal
    taker_fee_pct: Decimal
    settlement_fee_pct: Decimal | None  # None if not applicable/not documented
    fee_cap_pct_of_premium: Decimal | None  # e.g. Delta caps options fees at ~7.5-12.5% of premium
    zero_fee_on_otm_settlement: bool  # Delta-specific: True means OTM expiry incurs no settlement fee
    additional_tax_pct: Decimal | None  # e.g. 18% GST on Delta India accounts
    source_url: str  # where this schedule was documented -- never leave this blank


@dataclass(frozen=True)
class ContractSpec:
    """
    Full contract specification for a single instrument, used by the matching
    engine's Section C structural checks. Distinct from OptionContract in that
    this is meant to be re-fetched and diffed on every match attempt, not
    cached indefinitely (see architecture.md H: "Contract-spec risk").
    """

    instrument_id: str
    exchange: str
    contract_multiplier: Decimal
    lot_size: Decimal
    tick_size: Decimal
    settlement_method: SettlementMethod
    settlement_price_formula: str
    option_variant: OptionVariant
    is_european: bool
    fetched_at: datetime
