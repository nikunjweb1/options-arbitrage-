"""
Phase 6 -- Step 1: real-data backtest replay.

Takes every candidate pair that backtest/audit_coverage.py confirmed is
fully backtestable (real entry snapshot + real short-leg near-settlement
snapshot + real long-leg near-settlement snapshot, all present in
`market_data`) and computes REALIZED P&L from those actual recorded rows --
not a live theoretical estimate, and not a Black-Scholes repricing of the
long leg. Per docs/architecture.md Section G.1's non-negotiable #2 ("no
synthetic/manufactured historical data"), every number in this report
traces back to an actual row in `market_data`; a candidate missing any of
the three required rows is skipped and counted, never estimated.

WHAT "REALIZED P&L" MEANS HERE, PRECISELY:
  1. Entry: short_bid and long_ask taken from the market_data row closest to
     (at or before) `short_expiry - min_entry_lead_hours`, scaled by each
     leg's own contract_multiplier -- identical premium-scaling treatment to
     pricing/ev_engine.py's Bug #2 fix (raw quotes are in per-1-BTC terms,
     not pre-scaled to one contract's notional).
  2. Short-leg settlement: short_payoff computed via
     black_scholes.settlement_payoff() using the REAL index_price recorded
     closest to the short leg's expiry_ts (within
     --near-settlement-tolerance-min) -- not a scenario-grid guess.
  3. Long-leg exit: valued at the REAL best_bid recorded closest to that same
     settlement window -- i.e. what the long leg could actually have been
     sold for, not a Black-Scholes theoretical repricing. This is the one
     place this backtest is MORE honest than the live EV engine, which has
     no choice but to theoretically reprice the long leg since it evaluates
     BEFORE settlement happens and no real exit quote exists yet.

KNOWN LIMITATION -- FEES: market_data doesn't carry a historical fee
schedule (Delta's fee schedule isn't tick data, and Phase 2's collector
never captured it point-in-time). This uses TODAY's live fee schedule
(fetched once per run via DeltaAdapter.get_fees()) as a proxy for whatever
fee rate actually applied during the backtest window. Fee schedules are
documented to change rarely, so this is a reasonable proxy -- but it is a
proxy, flagged here rather than silently assumed exact.

KNOWN LIMITATION -- SLIPPAGE: entry/exit prices are the recorded best_bid/
best_ask closest to the target time, exactly as if a market order filled
instantly at the top of book with zero market impact and no partial fills.
Real fills -- especially the long-leg exit near the short leg's own
settlement window, when liquidity may be thin -- could be worse than this.
Slippage modeling is the flagged gap carried over from Phase 5; this
backtest does not close it. Do not read a positive mean realized P&L here
as "this survives real execution costs" without accounting for that gap.

SAMPLE SIZE: report this honestly. If audit_coverage.py's backtestable
count is small (its own threshold for "say so explicitly" is <20), any
go/no-go read on this backtest's aggregate stats carries correspondingly
low statistical confidence -- this script prints the N alongside every
aggregate figure specifically so it can't be read out of context.

Usage:
    python -m backtest.replay --underlying BTC
    python -m backtest.replay --underlying BTC --near-settlement-tolerance-min 30 --min-entry-lead-hours 1.0
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from config.settings import DB
from exchange_adapters.delta import DeltaAdapter, DeltaAdapterError
from normalization.schemas import FeeSchedule
from pricing.black_scholes import OptionKind, settlement_payoff

logger = logging.getLogger("backtest.replay")

# Only Delta is wired end-to-end as of Phase 5/6 -- same single-adapter map
# as pricing/run_pricing.py, for the same documented reason (see that
# module's _ADAPTERS comment).
_ADAPTERS = {
    "delta_india": DeltaAdapter(),
}


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


@dataclass(frozen=True)
class ReplayResult:
    pair_id: str
    classification: str
    net_entry_cost: Decimal
    short_payoff: Decimal
    long_exit_value: Decimal
    exit_fee: Decimal
    realized_pnl: Decimal
    short_expiry: datetime
    entry_ts: datetime
    short_settlement_ts: datetime
    long_exit_ts: datetime


def _closest_entry_row(
    conn: sqlite3.Connection, exchange: str, instrument_id: str, price_field: str, at_or_before: datetime
) -> sqlite3.Row | None:
    """Most recent market_data row at or before `at_or_before` that has a
    non-null `price_field` -- the real quote that would have been executable
    at entry time."""
    return conn.execute(
        f"""
        SELECT * FROM market_data
        WHERE exchange = ? AND instrument_id = ? AND ts <= ? AND {price_field} IS NOT NULL
        ORDER BY ts DESC LIMIT 1
        """,
        (exchange, instrument_id, at_or_before.isoformat()),
    ).fetchone()


def _closest_settlement_row(
    conn: sqlite3.Connection,
    exchange: str,
    instrument_id: str,
    price_field: str,
    near_ts: datetime,
    tolerance: timedelta,
) -> sqlite3.Row | None:
    """market_data row with a non-null `price_field`, within `tolerance` of
    `near_ts`, closest to it in absolute time -- the real quote nearest the
    short leg's settlement moment."""
    window_start = (near_ts - tolerance).isoformat()
    window_end = (near_ts + tolerance).isoformat()
    return conn.execute(
        f"""
        SELECT *, ABS(julianday(ts) - julianday(?)) AS dist FROM market_data
        WHERE exchange = ? AND instrument_id = ? AND ts BETWEEN ? AND ? AND {price_field} IS NOT NULL
        ORDER BY dist ASC LIMIT 1
        """,
        (near_ts.isoformat(), exchange, instrument_id, window_start, window_end),
    ).fetchone()


def _replay_one(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    fee_schedule: FeeSchedule,
    tolerance: timedelta,
    min_entry_lead_hours: float,
) -> ReplayResult | None:
    short_expiry = _parse_ts(row["short_expiry_ts"])
    entry_cutoff = short_expiry - timedelta(hours=min_entry_lead_hours)

    short_entry = _closest_entry_row(conn, row["short_exchange"], row["short_instrument_id"], "best_bid", entry_cutoff)
    long_entry = _closest_entry_row(conn, row["long_exchange"], row["long_instrument_id"], "best_ask", entry_cutoff)
    if short_entry is None or long_entry is None:
        return None

    short_settlement = _closest_settlement_row(
        conn, row["short_exchange"], row["short_instrument_id"], "index_price", short_expiry, tolerance
    )
    long_exit = _closest_settlement_row(
        conn, row["long_exchange"], row["long_instrument_id"], "best_bid", short_expiry, tolerance
    )
    if short_settlement is None or long_exit is None:
        return None

    short_mult = Decimal(row["short_contract_multiplier"])
    long_mult = Decimal(row["long_contract_multiplier"])

    # Entry economics -- same premium-scaling treatment as
    # pricing/ev_engine.py's Bug #2 fix: raw quotes are per-1-BTC, scale by
    # each leg's own contract_multiplier before combining.
    short_bid = Decimal(short_entry["best_bid"]) * short_mult
    long_ask = Decimal(long_entry["best_ask"]) * long_mult
    gross_entry_credit = short_bid - long_ask
    short_entry_fee = short_bid * fee_schedule.taker_fee_pct
    long_entry_fee = long_ask * fee_schedule.taker_fee_pct
    net_entry_cost = gross_entry_credit - short_entry_fee - long_entry_fee

    # Short-leg real settlement payoff, from the real recorded index_price.
    kind = OptionKind.CALL if row["short_option_type"] == "call" else OptionKind.PUT
    strike = Decimal(row["short_strike"])
    index_price_at_settlement = Decimal(short_settlement["index_price"])
    short_payoff = settlement_payoff(index_price_at_settlement, strike, kind) * short_mult

    # Long-leg real exit value -- the real recorded best_bid near the same
    # settlement window, NOT a Black-Scholes repricing.
    long_exit_value = Decimal(long_exit["best_bid"]) * long_mult
    exit_fee = long_exit_value * fee_schedule.taker_fee_pct

    realized_pnl = net_entry_cost - short_payoff + long_exit_value - exit_fee

    return ReplayResult(
        pair_id=row["pair_id"],
        classification=row["classification"],
        net_entry_cost=net_entry_cost,
        short_payoff=short_payoff,
        long_exit_value=long_exit_value,
        exit_fee=exit_fee,
        realized_pnl=realized_pnl,
        short_expiry=short_expiry,
        entry_ts=_parse_ts(short_entry["ts"]),
        short_settlement_ts=_parse_ts(short_settlement["ts"]),
        long_exit_ts=_parse_ts(long_exit["ts"]),
    )


def _load_candidate_rows(conn: sqlite3.Connection, underlying: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT cp.pair_id, cp.classification,
               cp.short_exchange, cp.short_instrument_id,
               cp.long_exchange, cp.long_instrument_id,
               si.expiry_ts AS short_expiry_ts, si.strike AS short_strike,
               si.option_type AS short_option_type,
               si.contract_multiplier AS short_contract_multiplier,
               li.contract_multiplier AS long_contract_multiplier
        FROM candidate_pairs cp
        JOIN instruments si ON si.exchange = cp.short_exchange AND si.instrument_id = cp.short_instrument_id
        JOIN instruments li ON li.exchange = cp.long_exchange AND li.instrument_id = cp.long_instrument_id
        WHERE si.underlying = ?
        """,
        (underlying,),
    ).fetchall()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Phase 6 step 1: replay real historical market_data to compute REALIZED P&L "
                    "(not a live theoretical estimate) for every fully-backtestable candidate."
    )
    parser.add_argument("--underlying", required=True, help="e.g. BTC")
    parser.add_argument("--near-settlement-tolerance-min", type=int, default=30,
                         help="Same default/rationale as backtest.audit_coverage: Delta settles off a "
                              "30-min TWAP, so 30 min is a reasonable tolerance for a 'near-settlement' row.")
    parser.add_argument("--min-entry-lead-hours", type=float, default=1.0,
                         help="Same as backtest.audit_coverage -- how far before expiry an entry snapshot "
                              "must be taken at or before to count as a realistic entry point.")
    parser.add_argument("--top", type=int, default=20, help="How many best/worst realized results to print.")
    args = parser.parse_args()

    if not DB.sqlite_path.exists():
        logger.error("No database found at %s.", DB.sqlite_path)
        return 1

    conn = sqlite3.connect(DB.sqlite_path)
    conn.row_factory = sqlite3.Row

    candidate_rows = _load_candidate_rows(conn, args.underlying)
    if not candidate_rows:
        logger.error("No candidate_pairs found for underlying=%s. Run matching.run_matcher first.", args.underlying)
        conn.close()
        return 1

    adapter = _ADAPTERS.get("delta_india")
    try:
        fee_schedule = adapter.get_fees()
    except DeltaAdapterError as exc:
        logger.error(
            "Could not load a live fee schedule to use as this backtest's fee-rate proxy: %s. "
            "Cannot proceed without SOME fee rate -- per Section G.1, this script does not "
            "silently assume a zero-fee or guessed fee rate.", exc,
        )
        conn.close()
        return 1
    logger.warning(
        "Using TODAY's live fee schedule (taker=%.4f%%) as a proxy for the backtest window's fee "
        "rate -- market_data has no historical fee-schedule record. See module docstring's "
        "'KNOWN LIMITATION -- FEES' note.", fee_schedule.taker_fee_pct * 100,
    )

    tolerance = timedelta(minutes=args.near_settlement_tolerance_min)
    results: list[ReplayResult] = []
    skipped_missing_data = 0

    for row in candidate_rows:
        result = _replay_one(conn, row, fee_schedule, tolerance, args.min_entry_lead_hours)
        if result is None:
            skipped_missing_data += 1
            continue
        results.append(result)

    conn.close()

    n = len(results)
    logger.info(
        "Replayed %d/%d candidate(s) with full real-data coverage (%d skipped: missing entry or "
        "settlement snapshot within tolerance).", n, len(candidate_rows), skipped_missing_data,
    )

    if n == 0:
        logger.warning(
            "Zero replayable candidates. Run backtest.audit_coverage first to confirm data coverage "
            "before expecting this script to produce results."
        )
        return 0

    if n < 20:
        logger.warning(
            "Only %d replayed candidate(s). Treat every aggregate figure below as LOW-CONFIDENCE -- "
            "this is not enough of a sample to draw a go/no-go conclusion from.", n,
        )

    winners = [r for r in results if r.realized_pnl > 0]
    win_rate = len(winners) / n
    total_pnl = sum((r.realized_pnl for r in results), Decimal("0"))
    mean_pnl = total_pnl / n
    worst = min(results, key=lambda r: r.realized_pnl)
    best = max(results, key=lambda r: r.realized_pnl)

    logger.info(
        "REALIZED (not theoretical) results over %d trade(s): win_rate=%.1f%% (%d/%d), "
        "mean_pnl=%s, total_pnl=%s, worst_case=%s (%s), best_case=%s (%s).",
        n, win_rate * 100, len(winners), n, mean_pnl, total_pnl,
        worst.realized_pnl, worst.pair_id, best.realized_pnl, best.pair_id,
    )
    logger.warning(
        "This does NOT account for slippage on entry/exit fills -- see module docstring's "
        "'KNOWN LIMITATION -- SLIPPAGE' note. A positive mean_pnl here is not yet evidence this "
        "survives real execution costs."
    )

    ranked = sorted(results, key=lambda r: r.realized_pnl, reverse=True)
    logger.info("Top %d realized results --", min(args.top, n))
    for r in ranked[: args.top]:
        logger.info(
            "  realized_pnl=%s  net_entry=%s  short_payoff=%s  long_exit=%s  %s  short_expiry=%s",
            r.realized_pnl, r.net_entry_cost, r.short_payoff, r.long_exit_value,
            r.pair_id, r.short_expiry.isoformat(),
        )
    logger.info("Bottom %d realized results --", min(args.top, n))
    for r in ranked[-args.top:]:
        logger.info(
            "  realized_pnl=%s  net_entry=%s  short_payoff=%s  long_exit=%s  %s  short_expiry=%s",
            r.realized_pnl, r.net_entry_cost, r.short_payoff, r.long_exit_value,
            r.pair_id, r.short_expiry.isoformat(),
        )

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
