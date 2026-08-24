"""
CLI: runs LeanEVEngine (pricing/ev_engine.py) against every candidate_pairs
row that has real captured market data for both legs, and writes results
into `signals`.

This is Phase 5's actual entry point -- ev_engine.py's evaluate() method
works against fixtures/unit tests; this script is what proves it works
against whatever the collector actually captured into `instruments` and
`market_data`. Mirrors matching/run_matcher.py's structure and conventions
deliberately, since this is the same kind of "prove it against real data"
script one phase later in the pipeline.

RECONSTRUCTED 2026-08-24: this file was found as an 11-byte placeholder stub
despite docs/architecture.md and README.md describing it in detail as done,
tested, and run against live data (674 candidates loaded, 404 priced, 292
positive-EV). That console-output description could not have come from this
file as found. Rebuilt from: ev_engine.py's real (non-placeholder, fully
documented) LeanEVEngine.evaluate() signature, db/loaders.py's real
get_contract()/get_candidate_pairs() helpers, db/schema.sql's real `signals`
table definition (including the entry_eligible column added for Section
M.2), and matching/run_matcher.py's established CLI/DB-connection
conventions. If the parallel Claude Code session has a different real
version of this file locally with different numbers already run against
real market data, diff before assuming this reconstruction's first run is
the "real" 674/404/292 result -- those specific numbers cannot be verified
from this reconstruction alone, only the logic that would have produced
some numbers can be.

Usage:
    python -m pricing.run_pricing --underlying BTC
    python -m pricing.run_pricing --underlying BTC --min-confidence 0.8
    python -m pricing.run_pricing --underlying BTC --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from config.settings import DB, DELTA, RISK
from db.loaders import get_candidate_pairs, get_contract
from matching.schemas import MatchCandidate, Classification
from normalization.schemas import MarketSnapshot
from pricing.ev_engine import EVResult, InsufficientDataError, LeanEVEngine

logger = logging.getLogger("pricing.run_pricing")


def _row_to_snapshot(row: sqlite3.Row) -> MarketSnapshot | None:
    """Parses the most recent `market_data` row for one instrument into a
    MarketSnapshot. Returns None (caller decides whether to log) on
    malformed data -- one bad row should not abort a whole pricing run,
    matching db/loaders.py's row_to_contract() convention."""
    def _dec(col: str) -> Decimal | None:
        val = row[col]
        if val is None:
            return None
        try:
            return Decimal(val)
        except InvalidOperation:
            return None

    try:
        return MarketSnapshot(
            timestamp=datetime.fromisoformat(row["ts"]),
            exchange=row["exchange"],
            instrument_id=row["instrument_id"],
            best_bid=_dec("best_bid"),
            best_ask=_dec("best_ask"),
            bid_size=_dec("bid_size"),
            ask_size=_dec("ask_size"),
            last_price=_dec("last_price") if "last_price" in row.keys() else None,
            mark_price=_dec("mark_price"),
            index_price=_dec("index_price"),
            iv=_dec("iv"),
            delta=_dec("delta"),
            gamma=_dec("gamma"),
            theta=_dec("theta"),
            vega=_dec("vega"),
            open_interest=_dec("open_interest"),
            volume_24h=_dec("volume_24h"),
            # Section E: market_data has no dedicated underlying_spot column
            # today -- mark_price is the closest available proxy for a
            # short-dated option's underlying reference. This is a real gap,
            # not a silent assumption: ev_engine.py's evaluate() will raise
            # InsufficientDataError (not substitute a guess) for any
            # snapshot where this ends up None, per Section A.1.
            underlying_spot=_dec("index_price") or _dec("mark_price"),
            underlying_index=_dec("index_price"),
            ingested_at=datetime.fromisoformat(row["ingested_at"]),
        )
    except (ValueError, InvalidOperation, TypeError) as exc:
        logger.warning(
            "Skipping malformed market_data row for %s:%s: %s",
            row["exchange"], row["instrument_id"], exc,
        )
        return None


def _load_latest_snapshot(
    conn: sqlite3.Connection, exchange: str, instrument_id: str
) -> MarketSnapshot | None:
    """Most recent market_data row for one instrument. `market_data` is
    append-only (schema.sql's own comment: "Never overwritten") so "latest"
    means MAX(ts), not a single mutable row -- this is O(log n) via the
    idx_market_data_instrument_ts index, not a full table scan."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT * FROM market_data
        WHERE exchange = ? AND instrument_id = ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (exchange, instrument_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_snapshot(row)


def _row_to_candidate(conn: sqlite3.Connection, row: sqlite3.Row) -> MatchCandidate | None:
    """Reassembles a MatchCandidate from a candidate_pairs row -- this table
    only stores the pair's identity/classification (Phase 3 output), not the
    full OptionContract objects, so both legs' full contract specs are
    re-fetched from `instruments` via get_contract() (Section H: contract
    specs are meant to be re-fetched, not cached indefinitely)."""
    short = get_contract(conn, row["short_exchange"], row["short_instrument_id"])
    long_ = get_contract(conn, row["long_exchange"], row["long_instrument_id"])
    if short is None or long_ is None:
        logger.warning(
            "Skipping pair %s: missing instrument row for %s or %s",
            row["pair_id"], row["short_instrument_id"], row["long_instrument_id"],
        )
        return None
    try:
        return MatchCandidate(
            pair_id=row["pair_id"],
            short_contract=short,
            long_contract=long_,
            match_confidence=Decimal(row["match_confidence"]),
            classification=Classification(row["classification"]),
            strike_diff=abs(short.strike - long_.strike),
            expiry_gap=long_.expiry_timestamp - short.expiry_timestamp,
            same_exchange=(short.exchange == long_.exchange),
        )
    except (ValueError, InvalidOperation, TypeError) as exc:
        logger.warning("Skipping malformed candidate_pairs row %s: %s", row["pair_id"], exc)
        return None


def _fee_pct_for(exchange: str) -> Decimal:
    """Per-exchange taker fee, from config.settings. Only delta_india has a
    real (if unverified -- see config/settings.py's DeltaConfig docstring)
    fee schedule wired up today, since CoinSwitch/Shark have no working
    options execution adapter yet (Shark: confirmed futures-only per
    architecture.md Section M.7; CoinSwitch: access requested, not yet
    granted). Any other exchange name gets a conservative fallback, logged
    loudly rather than silently assumed -- see the warning below."""
    if exchange == "delta_india":
        return DELTA.fee_schedule.taker_fee_pct
    logger.warning(
        "No documented fee schedule for exchange=%r -- using a conservative "
        "0.001 (0.1%%) fallback. This number is NOT verified against that "
        "exchange's actual published fees; do not trust net_entry_cost for "
        "pairs involving this exchange until a real FeeSchedule exists for it.",
        exchange,
    )
    return Decimal("0.001")


def _persist_signal(conn: sqlite3.Connection, pair_id: str, result: EVResult, entry_eligible: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    # score: expected_value scaled by probability_of_profit -- a simple,
    # documented ranking heuristic (favors candidates that are both
    # profitable AND likely to realize that profit), not itself part of the
    # EV/probability math from ev_engine.py. Kept deliberately simple rather
    # than inventing a more elaborate scoring formula not specified anywhere
    # in docs/architecture.md.
    score = result.expected_value * result.probability_of_profit
    conn.execute(
        """
        INSERT INTO signals (
            signal_id, ts, pair_id, net_entry_cost, expected_value,
            expected_profit, prob_of_profit, score, entry_eligible
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            now,
            pair_id,
            str(result.net_entry_cost),
            str(result.expected_value),
            str(result.best_case_pnl),  # expected_profit column: best-case realized profit if short leg expires worthless
            str(result.probability_of_profit),
            str(score),
            1 if entry_eligible else 0,
        ),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Price every candidate_pairs row with real market data on both legs, write signals."
    )
    parser.add_argument("--underlying", default=None,
                         help="Optional filter (matches candidate_pairs' underlying via its instruments). "
                              "Omit to price every candidate pair in the DB.")
    parser.add_argument("--min-confidence", type=float, default=0.5,
                         help="Skip candidate_pairs rows below this match_confidence (default 0.5).")
    parser.add_argument("--min-liquidity", type=str, default=None,
                         help="Override RISK.min_liquidity (contracts) for this run. "
                              "Defaults to config.settings.RISK.min_liquidity.")
    parser.add_argument("--min-net-credit", type=str, default=None,
                         help="Override RISK.min_net_credit for entry_eligible. "
                              "Defaults to config.settings.RISK.min_net_credit.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print results without writing to signals.")
    args = parser.parse_args()

    if not DB.sqlite_path.exists():
        logger.error(
            "No database found at %s. Run db/init_db.py, then a collector, "
            "then matching/run_matcher.py before pricing.", DB.sqlite_path,
        )
        return 1

    min_liquidity = Decimal(args.min_liquidity) if args.min_liquidity else RISK.min_liquidity
    min_net_credit = Decimal(args.min_net_credit) if args.min_net_credit else RISK.min_net_credit

    conn = sqlite3.connect(DB.sqlite_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row

    candidate_rows = get_candidate_pairs(conn, min_confidence=args.min_confidence)
    if args.underlying:
        # candidate_pairs itself has no underlying column (it's derived from
        # instruments) -- filter by re-checking each pair's short leg's
        # underlying rather than adding a redundant denormalized column to
        # the schema for this one CLI convenience.
        filtered = []
        for row in candidate_rows:
            short = get_contract(conn, row["short_exchange"], row["short_instrument_id"])
            if short is not None and short.underlying == args.underlying:
                filtered.append(row)
        candidate_rows = filtered

    logger.info(
        "Loaded %d candidate pair(s) (min_confidence=%.2f%s).",
        len(candidate_rows), args.min_confidence,
        f", underlying={args.underlying}" if args.underlying else "",
    )

    if not candidate_rows:
        logger.error(
            "No candidate pairs found. Has matching/run_matcher.py run yet? "
            "See matching/run_matcher.py --help."
        )
        conn.close()
        return 1

    priced = 0
    skipped_no_candidate = 0
    skipped_no_snapshot = 0
    skipped_insufficient_data = 0
    entry_eligible_count = 0
    positive_ev_count = 0
    results: list[tuple[str, EVResult, bool]] = []

    for row in candidate_rows:
        candidate = _row_to_candidate(conn, row)
        if candidate is None:
            skipped_no_candidate += 1
            continue

        short_snapshot = _load_latest_snapshot(
            conn, candidate.short_contract.exchange, candidate.short_contract.instrument_id
        )
        long_snapshot = _load_latest_snapshot(
            conn, candidate.long_contract.exchange, candidate.long_contract.instrument_id
        )
        if short_snapshot is None or long_snapshot is None:
            skipped_no_snapshot += 1
            logger.debug(
                "%s: no market_data for %s and/or %s -- has the collector polled these instruments?",
                candidate.pair_id, candidate.short_contract.instrument_id, candidate.long_contract.instrument_id,
            )
            continue

        engine = LeanEVEngine(
            short_taker_fee_pct=_fee_pct_for(candidate.short_contract.exchange),
            long_taker_fee_pct=_fee_pct_for(candidate.long_contract.exchange),
            min_contract_size=min_liquidity,
        )

        try:
            result = engine.evaluate(candidate, short_snapshot, long_snapshot)
        except InsufficientDataError as exc:
            skipped_insufficient_data += 1
            logger.debug("%s: %s", candidate.pair_id, exc)
            continue

        # Section M.2: net_entry_cost must clear min_net_credit to be
        # entry_eligible. ALL priced candidates are still persisted below
        # (net-debit entries included) -- this flag filters the *ranked/
        # tradeable view*, it does not exclude rows from the table itself,
        # so "why wasn't this shown as tradeable" is always answerable from
        # signals alone.
        entry_eligible = result.net_entry_cost > min_net_credit
        if entry_eligible:
            entry_eligible_count += 1
        if result.expected_value > 0:
            positive_ev_count += 1

        priced += 1
        results.append((candidate.pair_id, result, entry_eligible))

    logger.info(
        "Priced %d/%d candidate(s). Skipped: %d (no contract data), %d (no market_data snapshot), "
        "%d (insufficient data / not executable per ev_engine.py).",
        priced, len(candidate_rows), skipped_no_candidate, skipped_no_snapshot, skipped_insufficient_data,
    )
    logger.info(
        "Of %d priced: %d entry_eligible (net_entry_cost > %s), %d positive expected_value.",
        priced, entry_eligible_count, min_net_credit, positive_ev_count,
    )

    if args.dry_run:
        ranked = sorted(results, key=lambda r: r[1].expected_value * r[1].probability_of_profit, reverse=True)
        for pair_id, result, entry_eligible in ranked[:20]:
            logger.info(
                "  [%s] EV=%s P(profit)=%.2f net_entry_cost=%s stress(-10%%)=%s stress(+10%%)=%s",
                "CREDIT" if entry_eligible else "debit ",
                result.expected_value, float(result.probability_of_profit),
                result.net_entry_cost, result.stress_pnl_down_10pct, result.stress_pnl_up_10pct,
            )
        if len(ranked) > 20:
            logger.info("  ... and %d more (use without --dry-run to persist all).", len(ranked) - 20)
    else:
        for pair_id, result, entry_eligible in results:
            _persist_signal(conn, pair_id, result, entry_eligible)
        conn.commit()
        logger.info("Wrote %d signal(s) to `signals`.", priced)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
