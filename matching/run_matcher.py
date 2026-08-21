"""
CLI: runs MatchingEngine against real captured instruments in the SQLite DB
and writes results into candidate_pairs.

This is what actually connects Phase 3 to Phase 2's output -- everything in
matching/engine.py and tests/test_matching_engine.py works against fixtures;
this script is what proves it works against whatever Delta's testnet
actually returned during a real collector run.

Usage:
    python -m matching.run_matcher --underlying BTC
    python -m matching.run_matcher --underlying BTC --option-type call
    python -m matching.run_matcher --underlying BTC --min-confidence 0.8

Per architecture.md Section J (Phase 2 MVP) and the Phase 3 exit criterion,
the first real run of this should be self-matching Delta's own D1/D2/weekly
chain -- i.e. run with no --exchange filter against a DB that (for now) only
has Delta data in it anyway, so every candidate found is inherently a
same-exchange calendar-spread candidate until a second exchange's data
exists in the same DB.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from config.settings import DB
from matching.engine import MatchingConfig, MatchingEngine
from matching.schemas import MatchCandidate
from normalization.schemas import (
    OptionContract,
    OptionType,
    OptionVariant,
    SettlementMethod,
)

logger = logging.getLogger("matching.run_matcher")


def _row_to_contract(row: sqlite3.Row) -> OptionContract | None:
    try:
        return OptionContract(
            exchange=row["exchange"],
            underlying=row["underlying"],
            base_asset=row["underlying"],
            quote_asset=row["quote_currency"],
            option_type=OptionType(row["option_type"]),
            option_variant=OptionVariant(row["option_variant"]),
            strike=Decimal(row["strike"]),
            expiry_timestamp=datetime.fromisoformat(row["expiry_ts"]),
            settlement_timestamp=datetime.fromisoformat(row["settlement_ts"]),
            settlement_method=SettlementMethod(row["settlement_method"]),
            settlement_price_formula=row["settlement_price_formula"],
            contract_multiplier=Decimal(row["contract_multiplier"]),
            lot_size=Decimal(row["lot_size"]),
            tick_size=Decimal(row["tick_size"]),
            quote_currency=row["quote_currency"],
            settlement_currency=row["settlement_currency"],
            contract_symbol=row["symbol"],
            instrument_id=row["instrument_id"],
            is_european=bool(row["is_european"]),
        )
    except (ValueError, InvalidOperation, TypeError) as exc:
        logger.warning("Skipping malformed instrument row %s: %s", row["instrument_id"], exc)
        return None


def _load_contracts(
    conn: sqlite3.Connection, underlying: str, option_type: str | None, exchange: str | None
) -> list[OptionContract]:
    query = "SELECT * FROM instruments WHERE underlying = ?"
    params: list = [underlying]
    if option_type:
        query += " AND option_type = ?"
        params.append(option_type)
    if exchange:
        query += " AND exchange = ?"
        params.append(exchange)

    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    contracts = [c for c in (_row_to_contract(r) for r in rows) if c is not None]
    return contracts


def _persist_candidates(conn: sqlite3.Connection, candidates: list[MatchCandidate]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            c.pair_id,
            c.short_contract.exchange, c.short_contract.instrument_id,
            c.long_contract.exchange, c.long_contract.instrument_id,
            str(c.match_confidence), c.classification.value, now,
        )
        for c in candidates
    ]
    conn.executemany(
        """
        INSERT INTO candidate_pairs (
            pair_id, short_exchange, short_instrument_id, long_exchange, long_instrument_id,
            match_confidence, classification, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (pair_id) DO UPDATE SET
            match_confidence=excluded.match_confidence,
            classification=excluded.classification,
            created_at=excluded.created_at
        """,
        rows,
    )
    conn.commit()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Run the contract matching engine against captured instrument data")
    parser.add_argument("--underlying", required=True, help="e.g. BTC")
    parser.add_argument("--option-type", choices=["call", "put"], default=None,
                         help="Filter to one option type. Omit to match calls and puts together "
                              "(the engine will still reject cross-type pairs, but pre-filtering "
                              "avoids O(N^2) work on pairs that can never match).")
    parser.add_argument("--exchange", default=None,
                         help="Filter to one exchange -- omit for cross-exchange matching once "
                              "more than one exchange's data exists in the DB.")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--strike-tolerance-pct", type=float, default=0.01)
    parser.add_argument("--dry-run", action="store_true",
                         help="Print results without writing to candidate_pairs.")
    args = parser.parse_args()

    if not DB.sqlite_path.exists():
        logger.error("No database found at %s. Run db/init_db.py and a collector first.", DB.sqlite_path)
        return 1

    conn = sqlite3.connect(DB.sqlite_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    contracts = _load_contracts(conn, args.underlying, args.option_type, args.exchange)
    if not contracts:
        logger.error(
            "No instruments found for underlying=%s option_type=%s exchange=%s. "
            "Has the collector run yet? See collectors/run.py or collectors/run_realtime.py.",
            args.underlying, args.option_type, args.exchange,
        )
        conn.close()
        return 1

    logger.info("Loaded %d contracts. Running matching engine (strike_tolerance=%.2f%%)...",
                len(contracts), args.strike_tolerance_pct * 100)

    engine = MatchingEngine(config=MatchingConfig(strike_tolerance_pct=Decimal(str(args.strike_tolerance_pct))))
    candidates, rejected = engine.find_candidates(contracts)

    accepted = [c for c in candidates if c.match_confidence >= Decimal(str(args.min_confidence))]

    logger.info(
        "Result: %d candidate(s) found, %d meet min_confidence=%.2f, %d pair(s) rejected.",
        len(candidates), len(accepted), args.min_confidence, len(rejected),
    )

    by_classification: dict[str, int] = {}
    for c in accepted:
        by_classification[c.classification.value] = by_classification.get(c.classification.value, 0) + 1
    for classification, count in sorted(by_classification.items()):
        logger.info("  %s: %d", classification, count)

    if args.dry_run:
        for c in accepted[:20]:
            logger.info(
                "  [%.2f] %s (%s) short=%s@%s -> long=%s@%s gap=%s",
                c.match_confidence, c.classification.value,
                "same-exchange" if c.same_exchange else "cross-exchange",
                c.short_contract.instrument_id, c.short_contract.exchange,
                c.long_contract.instrument_id, c.long_contract.exchange,
                c.expiry_gap,
            )
        if len(accepted) > 20:
            logger.info("  ... and %d more (use without --dry-run to persist all).", len(accepted) - 20)
    else:
        _persist_candidates(conn, accepted)
        logger.info("Wrote %d candidate(s) to candidate_pairs.", len(accepted))

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
