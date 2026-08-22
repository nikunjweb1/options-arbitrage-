"""
Phase 6 (lean) backtest engine.

Per docs/architecture.md Section G.1 (non-negotiables, unchanged from v1):
  1. Historical bid/ask, not last price, if the data exists.
  2. No synthetic/manufactured historical data -- gaps are reported, not
     filled in.
  3. Fees and settlement rules match documented formulas, including the
     OTM-zero-settlement-fee rule.
  4. Legging failure is simulated for a meaningful fraction of trades, not
     ignored.

This module is pure logic -- no DB access, no network. It takes a
MatchCandidate plus the historical ticks already read for both legs and
returns a BacktestTradeResult. backtest/run_backtest.py is the CLI that
reads real `market_data` rows and drives this.

BUG FOUND + FIXED (2026-08-23): the first real backtest run against 4,574
candidates produced avg_pnl_per_trade=$2848.87 and total_realized_pnl=$1.58M
at a 59% win rate -- implausible for 0.001-BTC-multiplier contracts, where
real per-trade P&L should be single-to-low-double-digit dollars. Root cause:
this module's docstring previously claimed to have "reused the
contract_multiplier fix from pricing/ev_engine.py," but only actually
applied it to settlement_payoff -- it never applied it to
entry_short.best_bid / entry_long.best_ask (net_entry_cost), the
legging-failure loss calculation, or the long leg's exit price. Those raw
ticks, per the diagnose_pair.py finding documented in ev_engine.py's own
"BUG #2" note, are quoted in raw per-1-BTC terms (same scale as spot/strike),
not per-contract -- so net_entry_cost and long_exit_price were running
roughly 1/contract_multiplier too large, the exact same bug class as Bug #2
in the EV engine, just never ported over here. Fixed by scaling every
premium (entry bid/ask, exit bid, legging-failure loss basis, settlement-fee
cap basis) by its own leg's contract_multiplier, consistently with
short_payoff. See tests/test_backtest_engine.py::TestPremiumScalingUnitConsistency.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from decimal import Decimal

from backtest.schemas import BacktestTradeResult, HistoricalTick
from matching.schemas import MatchCandidate
from normalization.schemas import OptionType
from pricing.black_scholes import OptionKind, settlement_payoff

# How close (in seconds) a long-leg tick must be to a short-leg tick to be
# treated as "the same moment" for entry purposes. Real-time collection is
# sub-1-second (per collectors/market_data_collector.py), so this is
# generous, not tight -- lean-plan simplification, not a claim that entries
# were truly simultaneous to the millisecond.
_ENTRY_MATCH_TOLERANCE_SEC = 60

# Delta's documented settlement formula is a 30-minute TWAP of the index
# price ending at the settlement clock time -- see exchange_adapters/delta.py
# module docstring. This window is used to average whatever index_price
# ticks the collector actually captured in that 30-minute span.
_SETTLEMENT_TWAP_WINDOW_SEC = 30 * 60

# How close (in seconds) a long-leg tick must be to the short leg's
# settlement_timestamp to be used as the "exit" price for closing the long
# leg out at T1. Wider than the entry tolerance because ticker polling
# intervals (not sub-second WS ticks) may apply depending on which collector
# ran during that period.
_EXIT_MATCH_TOLERANCE_SEC = 15 * 60

# Fixed, documented legging-failure assumption (Section G.1 item 4). NOT
# fitted from measured fill-time data -- Phase 2's collectors don't record
# per-order fill latency, so there's nothing to fit this to yet. Treat as a
# stress assumption: "what if 1 trade in 20 doesn't get both legs filled
# together."
_DEFAULT_LEGGING_FAILURE_RATE = Decimal("0.05")

# If legging fails, assume the loss is a fixed fraction of the entry
# premium's magnitude (a conservative stand-in for "price moved against the
# second leg before it filled"), rather than modeling a full alternate P&L
# path -- also a documented stress assumption, not a measurement.
_DEFAULT_LEGGING_FAILURE_COST_PCT = Decimal("0.5")


def _deterministic_unit_interval(pair_id: str) -> Decimal:
    """
    Deterministic pseudo-random value in [0, 1) derived from pair_id, so
    re-running the backtest on the same data always produces the same
    legging-failure decisions (reproducibility) instead of a different
    outcome every run.
    """
    digest = hashlib.sha256(pair_id.encode("utf-8")).hexdigest()
    as_int = int(digest[:13], 16)
    max_int = 16**13
    return Decimal(as_int) / Decimal(max_int)


class LeanBacktester:
    def __init__(
        self,
        short_taker_fee_pct: Decimal,
        long_taker_fee_pct: Decimal,
        settlement_fee_pct: Decimal,
        fee_cap_pct_of_premium: Decimal | None,
        zero_fee_on_otm_settlement: bool,
        legging_failure_rate: Decimal = _DEFAULT_LEGGING_FAILURE_RATE,
        legging_failure_cost_pct: Decimal = _DEFAULT_LEGGING_FAILURE_COST_PCT,
    ) -> None:
        self._short_fee_pct = short_taker_fee_pct
        self._long_fee_pct = long_taker_fee_pct
        self._settlement_fee_pct = settlement_fee_pct
        self._fee_cap_pct_of_premium = fee_cap_pct_of_premium
        self._zero_fee_on_otm_settlement = zero_fee_on_otm_settlement
        self._legging_failure_rate = legging_failure_rate
        self._legging_failure_cost_pct = legging_failure_cost_pct

    def simulate_pair(
        self,
        candidate: MatchCandidate,
        short_ticks: list[HistoricalTick],
        long_ticks: list[HistoricalTick],
        now: datetime,
    ) -> BacktestTradeResult:
        """
        short_ticks/long_ticks: all historical `market_data` rows for each
        leg, in any order (sorted here). Returns exactly one
        BacktestTradeResult -- always a status, so a caller never has to
        guess why a trade isn't in the completed set.
        """
        short = candidate.short_contract
        long_ = candidate.long_contract

        if short.settlement_timestamp > now:
            return BacktestTradeResult(
                pair_id=candidate.pair_id,
                status="not_yet_settled",
                notes=(f"Short leg settles at {short.settlement_timestamp}, which is in the "
                       f"future relative to now={now} -- no real outcome exists yet to backtest.",),
            )

        short_sorted = sorted(short_ticks, key=lambda t: t.ts)
        long_sorted = sorted(long_ticks, key=lambda t: t.ts)

        # -- Entry: earliest tick where BOTH legs are simultaneously executable --
        entry_short, entry_long = self._find_entry(short_sorted, long_sorted)
        if entry_short is None or entry_long is None:
            return BacktestTradeResult(
                pair_id=candidate.pair_id,
                status="gap_no_entry_data",
                notes=("No historical tick pair found where both legs had an executable "
                       f"bid/ask within {_ENTRY_MATCH_TOLERANCE_SEC}s of each other -- per "
                       "Section G.1 item 2, this trade is reported as unresolvable, not "
                       "assumed to have entered at some other price.",),
            )

        # BUG FIX (2026-08-23): scale raw quoted premiums by each leg's OWN
        # contract_multiplier -- see module docstring. Applied once here so
        # every downstream use (legging-failure loss, entry economics) is
        # consistent, rather than scaling in multiple places and risking one
        # getting missed again.
        entry_short_bid_scaled = entry_short.best_bid * short.contract_multiplier
        entry_long_ask_scaled = entry_long.best_ask * long_.contract_multiplier

        # -- Legging-failure simulation (Section G.1 item 4) --------------------
        draw = _deterministic_unit_interval(candidate.pair_id)
        if draw < self._legging_failure_rate:
            gross_entry_credit = entry_short_bid_scaled - entry_long_ask_scaled
            assumed_loss = abs(gross_entry_credit) * self._legging_failure_cost_pct
            return BacktestTradeResult(
                pair_id=candidate.pair_id,
                status="legging_failed",
                entry_ts=entry_short.ts,
                realized_pnl=-assumed_loss,
                notes=(f"Simulated legging failure (fixed {self._legging_failure_rate:.0%} "
                       "assumption, not measured -- see engine module docstring). Modeled "
                       f"as a loss of {self._legging_failure_cost_pct:.0%} of the gross entry "
                       "premium magnitude, not a full alternate-path simulation.",),
            )

        # -- Entry economics (same formula as pricing/ev_engine.py Section D.2) --
        short_bid = entry_short_bid_scaled
        long_ask = entry_long_ask_scaled
        gross_entry_credit = short_bid - long_ask
        short_entry_fee = short_bid * self._short_fee_pct
        long_entry_fee = long_ask * self._long_fee_pct
        net_entry_cost = gross_entry_credit - short_entry_fee - long_entry_fee

        # -- Settlement: TWAP of index_price ticks in the 30 min before settlement --
        settlement_index_price = self._settlement_twap(short_sorted, long_sorted, short.settlement_timestamp)
        if settlement_index_price is None:
            return BacktestTradeResult(
                pair_id=candidate.pair_id,
                status="gap_no_settlement_data",
                entry_ts=entry_short.ts,
                net_entry_cost=net_entry_cost,
                notes=(f"No index_price ticks found within {_SETTLEMENT_TWAP_WINDOW_SEC // 60} "
                       f"minutes before settlement_timestamp={short.settlement_timestamp} -- "
                       "per Section G.1 item 2, no synthetic settlement price is substituted.",),
            )

        short_kind = OptionKind.CALL if short.option_type == OptionType.CALL else OptionKind.PUT
        short_payoff_raw = settlement_payoff(settlement_index_price, short.strike, short_kind)
        short_payoff = short_payoff_raw * short.contract_multiplier

        # Section G.1 item 3, non-negotiable: zero settlement fee if OTM.
        if short_payoff_raw == 0 and self._zero_fee_on_otm_settlement:
            settlement_fee = Decimal("0")
        else:
            settlement_fee = settlement_index_price * self._settlement_fee_pct * short.contract_multiplier
            if self._fee_cap_pct_of_premium is not None:
                # BUG FIX: cap basis must be the scaled (per-contract) short
                # premium, matching everything else in this method now.
                cap = abs(short_bid) * self._fee_cap_pct_of_premium
                settlement_fee = min(settlement_fee, cap)

        # -- Exit: long leg's best_bid nearest to settlement_timestamp -----------
        exit_tick = self._nearest_tick_with_bid(long_sorted, short.settlement_timestamp, _EXIT_MATCH_TOLERANCE_SEC)
        if exit_tick is None:
            return BacktestTradeResult(
                pair_id=candidate.pair_id,
                status="gap_no_exit_data",
                entry_ts=entry_short.ts,
                net_entry_cost=net_entry_cost,
                settlement_index_price=settlement_index_price,
                short_payoff=short_payoff,
                settlement_fee=settlement_fee,
                notes=(f"No long-leg best_bid tick found within {_EXIT_MATCH_TOLERANCE_SEC // 60} "
                       f"minutes of settlement_timestamp={short.settlement_timestamp} to close "
                       "the long leg out -- per Section G.1 item 2, no synthetic exit price is "
                       "substituted.",),
            )

        # BUG FIX: scale the exit price by the long leg's contract_multiplier too.
        long_exit_price = exit_tick.best_bid * long_.contract_multiplier
        long_exit_fee = long_exit_price * self._long_fee_pct

        realized_pnl = net_entry_cost - short_payoff - settlement_fee + long_exit_price - long_exit_fee

        return BacktestTradeResult(
            pair_id=candidate.pair_id,
            status="completed",
            entry_ts=entry_short.ts,
            net_entry_cost=net_entry_cost,
            settlement_index_price=settlement_index_price,
            short_payoff=short_payoff,
            settlement_fee=settlement_fee,
            long_exit_price=long_exit_price,
            long_exit_fee=long_exit_fee,
            realized_pnl=realized_pnl,
        )

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _find_entry(
        short_sorted: list[HistoricalTick], long_sorted: list[HistoricalTick]
    ) -> tuple[HistoricalTick | None, HistoricalTick | None]:
        """
        Earliest short-leg tick with an executable best_bid that has a
        matching long-leg tick (executable best_ask) within
        _ENTRY_MATCH_TOLERANCE_SEC.

        O(n log m) via bisect (both lists pre-sorted by the caller) --
        see tests/test_backtest_engine.py::TestFindEntryPerformance.
        """
        short_executable = [s for s in short_sorted if s.best_bid is not None]
        long_executable = [l for l in long_sorted if l.best_ask is not None]
        if not short_executable or not long_executable:
            return None, None

        long_timestamps = [l.ts for l in long_executable]
        tolerance = timedelta(seconds=_ENTRY_MATCH_TOLERANCE_SEC)

        for s in short_executable:
            window_start = s.ts - tolerance
            window_end = s.ts + tolerance
            lo = bisect_left(long_timestamps, window_start)
            hi = bisect_right(long_timestamps, window_end)
            if lo < hi:
                return s, long_executable[lo]

        return None, None

    @staticmethod
    def _settlement_twap(
        short_sorted: list[HistoricalTick],
        long_sorted: list[HistoricalTick],
        settlement_ts: datetime,
    ) -> Decimal | None:
        """
        Average of index_price across both legs' ticks (same underlying
        index regardless of which leg reported it) in the 30 minutes before
        settlement_ts. Returns None (a gap) if zero ticks with a non-null
        index_price fall in that window -- never fabricates a settlement
        price from ticks outside the window.
        """
        window_start = settlement_ts - timedelta(seconds=_SETTLEMENT_TWAP_WINDOW_SEC)
        prices: list[Decimal] = []
        for tick in short_sorted + long_sorted:
            if tick.index_price is None:
                continue
            if window_start <= tick.ts <= settlement_ts:
                prices.append(tick.index_price)
        if not prices:
            return None
        return sum(prices) / Decimal(len(prices))

    @staticmethod
    def _nearest_tick_with_bid(
        ticks_sorted: list[HistoricalTick], target_ts: datetime, tolerance_sec: int
    ) -> HistoricalTick | None:
        best: HistoricalTick | None = None
        best_delta: float | None = None
        for t in ticks_sorted:
            if t.best_bid is None:
                continue
            delta = abs((t.ts - target_ts).total_seconds())
            if delta > tolerance_sec:
                continue
            if best_delta is None or delta < best_delta:
                best, best_delta = t, delta
        return best
