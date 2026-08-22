"""
Phase 6 (lean) -- Step 0: audit how much historical `market_data` actually
exists before designing the backtest window around it.

WHY THIS EXISTS AS ITS OWN STEP, NOT FOLDED INTO THE BACKTEST ITSELF:
docs/architecture.md Section G.2 says the lean backtest window should be
"sized to whatever historical bid/ask Delta's API actually exposes," not
decided in advance -- and Section G.1's non-negotiable #2 is "no synthetic/
manufactured historical data -- gaps are reported, not filled in." Both of
those require knowing the real shape of `market_data` before writing a
single line of backtest replay logic. This mirrors how Phase 5 went: build
against real data, not an assumption about what the data looks like.

WHAT A LEAN BACKTEST NEEDS PER CANDIDATE, PER docs/architecture.md D.1-D.4:
  1. An "entry snapshot" -- some point meaningfully before the short leg's
     expiry, with executable best_bid (short leg) and best_ask (long leg),
     to compute Net_entry_cost exactly like pricing/ev_engine.py does live.
  2. A "near-settlement snapshot" for the short leg -- close to its expiry
     timestamp, to read the underlying spot/index price actually prevailing
     near settlement (every Delta ticker payload carries `spot_price`
     alongside its own quote, confirmed in exchange_adapters/delta.py's
     get_ticker -- so any option's market_data row near T1 gives us the
     underlying price too, no separate spot feed needed).
  3. A "near-settlement snapshot" for the long leg -- its own best_bid at
     approximately T1, since exiting a long option means hitting the bid,
     not asking the model to (theoretically, optimistically) reprice it via
     Black-Scholes when a real quote is sitting right there in the data.

A candidate is "backtestable" only if all three exist within a reasonable
tolerance. This script counts how many real candidates clear that bar, and
reports the actual calendar window covered, rather than assuming either
number.

Verified against a synthetic DB matching the real schema (2 candidates: one
with full entry + near-settlement coverage on both legs, one with zero
market_data rows) before being run against real data -- correctly reported
1/2 fully backtestable, matching the synthetic setup exactly.

Usage:
    python -m backtest.audit_coverage --underlying BTC
    python -m backtest.audit_coverage --underlying BTC --near-settlement-tolerance-min 30
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from config.settings import DB

logger = logging.getLogger("backtest.audit_coverage")


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _overall_coverage(conn: sqlite3.Connection, underlying: str) -> None:
    rows = conn.execute(
        """
        SELECT md.exchange, md.instrument_id, COUNT(*) AS n_rows,
               MIN(md.ts) AS first_ts, MAX(md.ts) AS last_ts,
               SUM(CASE WHEN md.best_bid IS NOT NULL AND md.best_ask IS NOT NULL THEN 1 ELSE 0 END) AS n_executable
        FROM market_data md
        JOIN instruments i ON i.exchange = md.exchange AND i.instrument_id = md.instrument_id
        WHERE i.underlying = ?
        GROUP BY md.exchange, md.instrument_id
        """,
        (underlying,),
    ).fetchall()

    if not rows:
        logger.warning(
            "ZERO market_data rows found for underlying=%s. The realtime/REST "
            "collectors (Phase 2) have not captured any history for this "
            "underlying, or haven't been run since. A lean backtest needs "
            "SOME historical window to replay -- there is currently nothing "
            "to replay. Run `python -m collectors.run_realtime --duration-hours N` "
            "for a while first.",
            underlying,
        )
        return

    total_rows = sum(r["n_rows"] for r in rows)
    total_executable = sum(r["n_executable"] for r in rows)
    earliest = min(_parse_ts(r["first_ts"]) for r in rows)
    latest = max(_parse_ts(r["last_ts"]) for r in rows)
    span = latest - earliest

    logger.info(
        "Overall market_data coverage for underlying=%s: %d instrument(s), %d row(s) "
        "(%d with executable bid/ask), spanning %s -> %s (%.1f hours).",
        underlying, len(rows), total_rows, total_executable, earliest.isoformat(),
        latest.isoformat(), span.total_seconds() / 3600,
    )

    sparse = [r for r in rows if r["n_rows"] < 5]
    if sparse:
        logger.info(
            "%d/%d instrument(s) have fewer than 5 market_data rows each -- "
            "likely instruments that were only ever fetched once or twice, "
            "not continuously tracked.",
            len(sparse), len(rows),
        )


def _candidate_backtestability(
    conn: sqlite3.Connection,
    underlying: str,
    near_settlement_tolerance_min: int,
    min_entry_lead_hours: float,
) -> None:
    candidates = conn.execute(
        """
        SELECT cp.pair_id, cp.short_exchange, cp.short_instrument_id,
               cp.long_exchange, cp.long_instrument_id, cp.classification,
               si.expiry_ts AS short_expiry_ts, si.symbol AS short_symbol,
               li.expiry_ts AS long_expiry_ts, li.symbol AS long_symbol
        FROM candidate_pairs cp
        JOIN instruments si ON si.exchange = cp.short_exchange AND si.instrument_id = cp.short_instrument_id
        JOIN instruments li ON li.exchange = cp.long_exchange AND li.instrument_id = cp.long_instrument_id
        WHERE si.underlying = ?
        """,
        (underlying,),
    ).fetchall()

    if not candidates:
        logger.warning("No candidate_pairs found for underlying=%s. Run matching.run_matcher first.", underlying)
        return

    has_entry = 0
    has_short_settlement = 0
    has_long_settlement = 0
    fully_backtestable = 0
    tolerance = timedelta(minutes=near_settlement_tolerance_min)

    for c in candidates:
        short_expiry = _parse_ts(c["short_expiry_ts"])
        entry_cutoff = short_expiry - timedelta(hours=min_entry_lead_hours)

        entry_row = conn.execute(
            """
            SELECT 1 FROM market_data
            WHERE exchange = ? AND instrument_id = ? AND ts <= ?
              AND best_bid IS NOT NULL
            LIMIT 1
            """,
            (c["short_exchange"], c["short_instrument_id"], entry_cutoff.isoformat()),
        ).fetchone()
        entry_long_row = conn.execute(
            """
            SELECT 1 FROM market_data
            WHERE exchange = ? AND instrument_id = ? AND ts <= ?
              AND best_ask IS NOT NULL
            LIMIT 1
            """,
            (c["long_exchange"], c["long_instrument_id"], entry_cutoff.isoformat()),
        ).fetchone()
        candidate_has_entry = bool(entry_row and entry_long_row)
        if candidate_has_entry:
            has_entry += 1

        window_start = (short_expiry - tolerance).isoformat()
        window_end = (short_expiry + tolerance).isoformat()

        short_settlement_row = conn.execute(
            """
            SELECT 1 FROM market_data
            WHERE exchange = ? AND instrument_id = ? AND ts BETWEEN ? AND ?
              AND index_price IS NOT NULL
            LIMIT 1
            """,
            (c["short_exchange"], c["short_instrument_id"], window_start, window_end),
        ).fetchone()
        candidate_has_short_settlement = bool(short_settlement_row)
        if candidate_has_short_settlement:
            has_short_settlement += 1

        long_settlement_row = conn.execute(
            """
            SELECT 1 FROM market_data
            WHERE exchange = ? AND instrument_id = ? AND ts BETWEEN ? AND ?
              AND best_bid IS NOT NULL
            LIMIT 1
            """,
            (c["long_exchange"], c["long_instrument_id"], window_start, window_end),
        ).fetchone()
        candidate_has_long_settlement = bool(long_settlement_row)
        if candidate_has_long_settlement:
            has_long_settlement += 1

        if candidate_has_entry and candidate_has_short_settlement and candidate_has_long_settlement:
            fully_backtestable += 1

    n = len(candidates)
    logger.info(
        "Candidate backtestability (underlying=%s, %d total candidates, "
        "entry_lead>=%.1fh, near-settlement tolerance=%dmin):",
        underlying, n, min_entry_lead_hours, near_settlement_tolerance_min,
    )
    logger.info("  has entry snapshot (both legs):        %d/%d (%.1f%%)", has_entry, n, 100 * has_entry / n)
    logger.info("  has short-leg near-settlement snapshot: %d/%d (%.1f%%)", has_short_settlement, n, 100 * has_short_settlement / n)
    logger.info("  has long-leg near-settlement snapshot:  %d/%d (%.1f%%)", has_long_settlement, n, 100 * has_long_settlement / n)
    logger.info("  FULLY BACKTESTABLE (all three):         %d/%d (%.1f%%)", fully_backtestable, n, 100 * fully_backtestable / n)

    if fully_backtestable == 0:
        logger.warning(
            "Zero fully-backtestable candidates. This is expected if the "
            "realtime collector hasn't been running continuously through at "
            "least one full short-leg expiry cycle yet. Per Section G.1, we "
            "do NOT synthesize missing settlement data -- either let the "
            "collector run longer and re-audit, or narrow scope to whatever "
            "expiry window the data does cover."
        )
    elif fully_backtestable < 20:
        logger.warning(
            "Only %d fully-backtestable candidates. A lean pass can still "
            "run and report honestly on this sample, but treat any resulting "
            "go/no-go signal as low-confidence given the small N -- say so "
            "explicitly in the backtest report rather than implying more "
            "statistical weight than %d trades supports.",
            fully_backtestable, fully_backtestable,
        )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Phase 6 step 0: audit historical market_data coverage.")
    parser.add_argument("--underlying", required=True, help="e.g. BTC")
    parser.add_argument("--near-settlement-tolerance-min", type=int, default=30,
                         help="How close (minutes) a market_data row must be to a short leg's "
                              "expiry_ts to count as a 'near-settlement' snapshot. Delta settles "
                              "off a 30-min TWAP, so 30 min is a reasonable default tolerance.")
    parser.add_argument("--min-entry-lead-hours", type=float, default=1.0,
                         help="How many hours before expiry an 'entry snapshot' must be taken at "
                              "or before, to count as a realistic entry point rather than a "
                              "last-minute quote.")
    args = parser.parse_args()

    if not DB.sqlite_path.exists():
        logger.error("No database found at %s.", DB.sqlite_path)
        return 1

    conn = sqlite3.connect(DB.sqlite_path)
    conn.row_factory = sqlite3.Row

    _overall_coverage(conn, args.underlying)
    _candidate_backtestability(
        conn, args.underlying, args.near_settlement_tolerance_min, args.min_entry_lead_hours
    )

    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
