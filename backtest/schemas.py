"""
Phase 6 (lean backtester) schemas.

Per docs/architecture.md Section G.2 ("lean test matrix"): one window
(whatever historical bid/ask the DB actually has), one underlying, one entry
rule -- not the fuller v1 matrix (segmented windows, threshold sweeps,
walk-forward). This module defines the shapes that plan produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class HistoricalTick:
    """
    One row of `market_data`, exactly as stored -- not a live TickerSnapshot.
    Deliberately narrower than normalization.schemas.MarketSnapshot: the
    backtester only ever reads best_bid/best_ask (per architecture.md
    Section A.1's executable-price-only rule) and index_price (the only
    underlying-price field actually persisted to `market_data` -- see
    collectors/market_data_collector.py; MarketSnapshot.underlying_spot/
    underlying_index exist on the live snapshot but were never written to a
    column, so historical underlying price must come from index_price).
    """

    ts: datetime
    best_bid: Decimal | None
    best_ask: Decimal | None
    index_price: Decimal | None


@dataclass(frozen=True)
class BacktestTradeResult:
    """
    Outcome of simulating one candidate pair against historical ticks.

    `status` is the load-bearing field -- per architecture.md Section G.1
    ("gaps are reported, not filled in"), every trade that couldn't be fully
    resolved from real historical data is labeled with *why*, never silently
    dropped or silently scored as a loss/win. Only status == "completed"
    trades should ever be aggregated into a win-rate or average P&L.
    """

    pair_id: str
    status: str  # "completed" | "not_yet_settled" | "gap_no_entry_data" |
                 # "gap_no_settlement_data" | "gap_no_exit_data" | "legging_failed"
    entry_ts: datetime | None = None
    net_entry_cost: Decimal | None = None
    settlement_index_price: Decimal | None = None
    short_payoff: Decimal | None = None
    settlement_fee: Decimal | None = None
    long_exit_price: Decimal | None = None
    long_exit_fee: Decimal | None = None
    realized_pnl: Decimal | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BacktestReport:
    """
    One honest report, per the Phase 6 (lean) exit criterion in
    docs/architecture.md Section I: "one honest report, whatever window the
    data supports, explicitly labeled 'lean pass'". Every count below is
    reported, not just the ones that make the strategy look good --
    completed vs. each gap reason vs. legging failures are kept separate on
    purpose so a reader can see exactly how much of the candidate pool this
    report actually says something about.
    """

    underlying: str
    generated_at: datetime
    window_start: datetime | None
    window_end: datetime | None
    total_candidates_considered: int
    not_yet_settled_count: int
    gap_no_entry_data_count: int
    gap_no_settlement_data_count: int
    gap_no_exit_data_count: int
    legging_failed_count: int
    completed_trades: tuple[BacktestTradeResult, ...]
    win_count: int
    loss_count: int
    win_rate: Decimal | None  # None if zero completed trades -- never reported as 0%
    total_realized_pnl: Decimal | None
    avg_pnl_per_trade: Decimal | None
    model_notes: tuple[str, ...] = field(default_factory=lambda: (
        "LEAN PASS -- per docs/architecture.md Section G.2/L.3: one window "
        "(whatever historical bid/ask the DB actually has), one underlying, "
        "one entry rule (earliest tick where both legs are simultaneously "
        "executable). Not the full v1 matrix (segmented windows, threshold "
        "sweep, walk-forward, in-sample/out-of-sample split) -- see Section "
        "G.3 for what's deferred, not cancelled.",
        "Legging-failure simulation (Section G.1 item 4) uses a fixed, "
        "documented failure rate and a fixed assumed extra-slippage cost, "
        "deterministically applied per pair_id for reproducibility -- this "
        "is NOT fitted to any measured historical fill-time data (Phase 2's "
        "collectors don't capture per-order fill latency), so treat the "
        "legging-failure numbers as a stress assumption, not a measurement.",
        "Settlement price is a TWAP of index_price ticks in the 30 minutes "
        "before the short leg's settlement_timestamp, approximating Delta's "
        "documented 30-min-TWAP-of-index settlement formula from whatever "
        "ticks the collector actually captured in that window -- not a "
        "guaranteed match to Delta's own internal TWAP calculation.",
    ))
