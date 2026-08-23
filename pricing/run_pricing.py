"""
CLI: Phase 5 (lean) — prices every candidate pair in `candidate_pairs`
against LIVE bid/ask pulled from the exchange right now, and persists the
result to `signals`.

Per docs/architecture.md Section L.2 / Phase 5 exit criterion:
    "EV, net-of-fees profit, and a probability-of-profit estimate computed
    for all 1,504 real candidates, using real bid/ask pulled live, not
    backfilled."

This is what actually connects Phase 3's candidate_pairs to
pricing/ev_engine.py -- ev_engine.py and black_scholes.py are pure functions
(tested against fixtures in tests/test_ev_engine.py); this script is what
proves the whole thing works against live Delta testnet quotes, the same
way matching/run_matcher.py proved the matcher against real Phase 2 data.

IMPORTANT -- what "ranked"/"scored" means here: the architecture diagram
(Section A.2) has an OPPORTUNITY SCANNER stage between the EV engine and the
backtester ("continuous scan -> executable net entry -> classify -> score").
The compressed v2 roadmap (Section I) doesn't carve that out as its own
numbered phase, so this script's ranking output *is* that scan/score step
for the lean plan: one-shot, not continuous, and the `score` persisted to
`signals` is a simple, explicitly-documented heuristic
(expected_value * probability_of_profit), not a calibrated scoring model.
Treat it as a ranking convenience for the lean pass, not a trading signal.

WHAT'S NOT COMPUTED (be honest about the gap, per ev_engine.py's own
docstring): var_95, expected_shortfall, and required_margin are persisted as
NULL. The lean scenario-grid engine doesn't produce a proper risk-neutral
distribution or pull live margin requirements -- both are explicitly
deferred items (Section H: "Margin risk... pulled live" is listed as a
mitigation for later, not built in Phase 5).

KNOWN GAP FOUND DURING FIRST LIVE RUN (2026-08-21): candidate_pairs can go
stale within hours, not just days. A short leg with a same-day (D1) expiry
that was live when matching/run_matcher.py ran can expire and be delisted
from Delta's ticker endpoint by the time this script runs against it --
observed directly: instrument 195593 (C-BTC-72200-210826, expiry 12:00 UTC)
returned `success=true, result=null` from `/v2/tickers/{id}` about 4.5 hours
after its own expiry. This script now filters those out at load time (see
`_load_candidates`'s `now` check) instead of letting them surface as a
confusing per-ticker "fetch failed" warning. The practical implication:
run matching/run_matcher.py and this script close together in time,
especially for same_exchange_calendar_spread candidates with short-dated
D1 short legs -- the gap between match time and price time is itself a
source of data loss, not just a cosmetic delay.

DIAGNOSTIC FIELDS ADDED (2026-08-22): every ranked print line includes
time_to_short_expiry_hours, sigma_move, and base_iv_used so a P(profit) of
exactly 1.0 or 0.0 can be explained rather than just reported -- whether
"guaranteed profit" is backed by a real price range or an accidentally too-
narrow one. This is a visibility fix, not a pricing fix.

MIN_NET_CREDIT GATE ADDED (2026-08-23, per docs/architecture.md Section
M.2): a candidate whose net_entry_cost is not a real net credit
(net_entry_cost > RISK.min_net_credit, default 0.0) is now a hard
DO_NOT_ENTER, not just a lower-ranked candidate. Every priced candidate is
still evaluated and persisted (never silently dropped), tagged with a real
`entry_eligible` column in `signals` -- but the ranked "Top N" / "Bottom N"
opportunity display now only shows entry_eligible candidates, since a
net-debit calendar spread has no scenario in this payoff structure where it
outperforms a net-credit one in the tail (Section M.2's worked-example
argument), so surfacing it as a ranked "opportunity" is actively misleading
even when its EV happens to look positive under the lean model's
simplifications.

Usage:
    python -m pricing.run_pricing --underlying BTC
    python -m pricing.run_pricing --underlying BTC --min-confidence 0.8 --limit 50
    python -m pricing.run_pricing --underlying BTC --dry-run

Requires network access to Delta's REST API (testnet by default, per
config/settings.py DELTA.use_testnet) -- this fetches LIVE tickers, it does
not read backfilled market_data rows, per the Phase 5 exit criterion above.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from config.settings import COLLECTOR, DB, RISK
from exchange_adapters.delta import DeltaAdapter, DeltaAdapterError
from matching.schemas import Classification, MatchCandidate
from normalization.schemas import (
    FeeSchedule,
    MarketSnapshot,
    OptionContract,
    OptionType,
    OptionVariant,
    SettlementMethod,
)
from pricing.ev_engine import EVResult, InsufficientDataError, LeanEVEngine

logger = logging.getLogger("pricing.run_pricing")

# Only Delta is wired end-to-end as of Phase 5 (per architecture.md Section 0
# -- CoinSwitch/Shark remain deferred). Every candidate_pairs row currently
# in the DB is same-exchange delta_india per the Phase 3 run, so a
# single-adapter map is an honest reflection of current scope, not a
# hardcoded assumption baked in for the future -- add entries here as
# adapters land, per architecture.md Section A.1's adapter-isolation rule.
_ADAPTERS = {
    "delta_india": DeltaAdapter(),
}


def _ensure_entry_eligible_column(conn: sqlite3.Connection) -> None:
    """
    Idempotent migration for `signals.entry_eligible` (Section M.2), for
    databases created before this column existed in db/schema.sql.
    ALTER TABLE ... ADD COLUMN has no "IF NOT EXISTS" guard in the SQL
    dialect itself pre-3.35, so this catches sqlite3's specific
    "duplicate column name" error rather than trying to pre-detect it via
    PRAGMA table_info, which would be one more thing to keep in sync with
    the column name below.
    """
    try:
        conn.execute(
            "ALTER TABLE signals ADD COLUMN entry_eligible INTEGER NOT NULL DEFAULT 0 "
            "CHECK (entry_eligible IN (0, 1))"
        )
        conn.commit()
        logger.info("Migrated existing `signals` table: added entry_eligible column (Section M.2).")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def is_entry_eligible(net_entry_cost: Decimal, min_net_credit: Decimal) -> bool:
    """
    Per docs/architecture.md Section M.2: a real net CREDIT is a hard gate,
    not a ranking preference. Pulled out as its own function so the gate
    logic itself -- not just its wiring into the CLI -- has a direct unit
    test (tests/test_run_pricing.py).
    """
    return net_entry_cost > min_net_credit


def _row_to_contract(row: sqlite3.Row) -> OptionContract | None:
    """
    Same shape/logic as matching/run_matcher.py's private loader, duplicated
    rather than imported -- pricing/ shouldn't reach into matching/'s CLI
    module for a private helper; both just read the `instruments` row shape
    defined in normalization/schemas.py.
    """
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


def _load_contract(conn: sqlite3.Connection, exchange: str, instrument_id: str) -> OptionContract | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM instruments WHERE exchange = ? AND instrument_id = ?",
        (exchange, instrument_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_contract(row)


def _load_candidates(
    conn: sqlite3.Connection,
    underlying: str,
    min_confidence: Decimal,
    classification: str | None,
    limit: int | None,
    now: datetime | None = None,
) -> tuple[list[MatchCandidate], int]:
    """
    Returns (candidates, skipped_expired_count).

    skipped_expired_count is candidates whose short leg has already passed
    its expiry_timestamp as of `now` -- these are filtered out here, before
    any ticker fetch, rather than being discovered one wasted API call at a
    time. See the module docstring's "KNOWN GAP" note: a short-dated (D1)
    short leg that was live when matching/run_matcher.py ran can expire and
    be delisted from Delta's ticker endpoint within hours.
    """
    now = now or datetime.now(timezone.utc)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT cp.* FROM candidate_pairs cp
        JOIN instruments si ON si.exchange = cp.short_exchange AND si.instrument_id = cp.short_instrument_id
        WHERE si.underlying = ? AND CAST(cp.match_confidence AS REAL) >= ?
    """
    params: list = [underlying, float(min_confidence)]
    if classification:
        query += " AND cp.classification = ?"
        params.append(classification)
    query += " ORDER BY cp.created_at"
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    candidates: list[MatchCandidate] = []
    skipped_expired = 0
    for row in rows:
        short_contract = _load_contract(conn, row["short_exchange"], row["short_instrument_id"])
        long_contract = _load_contract(conn, row["long_exchange"], row["long_instrument_id"])
        if short_contract is None or long_contract is None:
            logger.warning(
                "Skipping candidate %s: could not reload one or both legs from instruments "
                "(short=%s/%s found=%s, long=%s/%s found=%s)",
                row["pair_id"],
                row["short_exchange"], row["short_instrument_id"], short_contract is not None,
                row["long_exchange"], row["long_instrument_id"], long_contract is not None,
            )
            continue
        if short_contract.expiry_timestamp <= now:
            logger.debug(
                "Skipping candidate %s: short leg %s expired at %s (now=%s)",
                row["pair_id"], short_contract.contract_symbol, short_contract.expiry_timestamp, now,
            )
            skipped_expired += 1
            continue
        candidates.append(
            MatchCandidate(
                pair_id=row["pair_id"],
                short_contract=short_contract,
                long_contract=long_contract,
                match_confidence=Decimal(row["match_confidence"]),
                classification=Classification(row["classification"]),
                strike_diff=abs(long_contract.strike - short_contract.strike),
                expiry_gap=long_contract.expiry_timestamp - short_contract.expiry_timestamp,
                same_exchange=row["short_exchange"] == row["long_exchange"],
            )
        )
    return candidates, skipped_expired


def _fetch_snapshot(
    cache: dict[tuple[str, str], MarketSnapshot | None],
    exchange: str,
    instrument_id: str,
    throttle_sec: float,
) -> MarketSnapshot | None:
    """
    Cached per (exchange, instrument_id) within a single run -- many
    candidates share a leg (e.g. one D1 contract can be the short leg of
    several calendar spreads against different longer maturities), so this
    avoids re-fetching the same live ticker repeatedly and keeps the total
    call count closer to O(unique instruments) than O(candidates).
    """
    key = (exchange, instrument_id)
    if key in cache:
        return cache[key]

    adapter = _ADAPTERS.get(exchange)
    if adapter is None:
        logger.warning("No adapter wired for exchange=%s (instrument=%s) -- skipping.", exchange, instrument_id)
        cache[key] = None
        return None

    snapshot: MarketSnapshot | None
    try:
        ticker = adapter.get_ticker(instrument_id)
        snapshot = ticker.snapshot
    except DeltaAdapterError as exc:
        logger.warning("Ticker fetch failed for %s/%s: %s", exchange, instrument_id, exc)
        snapshot = None

    cache[key] = snapshot
    # Same politeness rationale as collectors/market_data_collector.py --
    # Delta's documented limit is generous, but a batch of ~1,500 candidates
    # worth of live ticker calls shouldn't burst just because it can.
    time.sleep(throttle_sec)
    return snapshot


def _persist_results(conn: sqlite3.Connection, results: list[EVResult], eligibility: dict[str, bool]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in results:
        # Lean ranking heuristic -- see module docstring. Not a calibrated
        # score; a simplification, not a hidden claim of statistical rigor.
        score = r.expected_value * r.probability_of_profit
        breakdown = {
            "expected_value": str(r.expected_value),
            "probability_of_profit": str(r.probability_of_profit),
            "worst_case_pnl": str(r.worst_case_pnl),
            "best_case_pnl": str(r.best_case_pnl),
            "fees_total": str(r.fees_total),
            "scenario_count": r.scenario_count,
            "net_entry_cost": str(r.net_entry_cost),
            "short_bid_used": str(r.short_bid_used),
            "long_ask_used": str(r.long_ask_used),
            "time_to_short_expiry_hours": r.time_to_short_expiry_hours,
            "sigma_move": r.sigma_move,
            "base_iv_used": r.base_iv_used,
            "entry_eligible": eligibility.get(r.pair_id, False),
            "model_notes": list(r.model_notes),
        }
        rows.append((
            str(uuid.uuid4()), now, r.pair_id,
            str(r.net_entry_cost), str(r.expected_value), str(r.expected_value),
            str(r.probability_of_profit), None, None, None,
            str(score), json.dumps(breakdown),
            1 if eligibility.get(r.pair_id, False) else 0,
        ))
    conn.executemany(
        """
        INSERT INTO signals (
            signal_id, ts, pair_id, net_entry_cost, expected_value, expected_profit,
            prob_of_profit, var_95, expected_shortfall, required_margin, score, score_breakdown,
            entry_eligible
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def _log_result_line(r: EVResult, eligible: bool) -> None:
    """
    One ranked-result log line, including the diagnostic fields
    (time_to_short_expiry_hours, sigma_move, base_iv_used) so a P(profit) of
    exactly 1.0 or 0.0 can be explained rather than just reported, and the
    Section M.2 eligibility tag so it's visible even when scanning raw logs
    rather than querying `signals` directly.
    """
    logger.info(
        "  EV=%s  P(profit)=%s  net_entry=%s  fees=%s  "
        "hrs_to_short_expiry=%.2f  sigma_move=%.4f  iv_used=%.4f  entry_eligible=%s  %s",
        r.expected_value, r.probability_of_profit, r.net_entry_cost, r.fees_total,
        r.time_to_short_expiry_hours, r.sigma_move, r.base_iv_used, eligible, r.pair_id,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Phase 5 (lean): price every real candidate pair against live bid/ask and persist to `signals`."
    )
    parser.add_argument("--underlying", required=True, help="e.g. BTC")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--classification", default=None,
                         help="Filter to one Classification value, e.g. same_exchange_calendar_spread")
    parser.add_argument("--limit", type=int, default=None,
                         help="Price at most N candidates -- useful for a smoke test before a full 1,504-pair run.")
    parser.add_argument("--top", type=int, default=20, help="How many top-EV entry_eligible results to print.")
    parser.add_argument("--bottom", type=int, default=None,
                         help="How many lowest-EV entry_eligible results to also print, with the same diagnostic "
                              "fields as --top. Defaults to the same value as --top. Printed separately from --top "
                              "so both tails of the eligible distribution are always visible.")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to `signals`.")
    args = parser.parse_args()
    bottom_n = args.bottom if args.bottom is not None else args.top

    if not DB.sqlite_path.exists():
        logger.error("No database found at %s. Run db/init_db.py and the collectors/matcher first.", DB.sqlite_path)
        return 1

    conn = sqlite3.connect(DB.sqlite_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    _ensure_entry_eligible_column(conn)

    min_net_credit = Decimal(str(RISK.min_net_credit))
    logger.info("MIN_NET_CREDIT gate active: net_entry_cost must be > %s to be entry_eligible (Section M.2).",
                min_net_credit)

    candidates, skipped_expired = _load_candidates(
        conn, args.underlying, Decimal(str(args.min_confidence)), args.classification, args.limit
    )
    if skipped_expired:
        logger.warning(
            "%d candidate(s) skipped: short leg already expired since matching/run_matcher.py ran. "
            "If this number is large, re-run the matcher on fresh instrument data before pricing.",
            skipped_expired,
        )
    if not candidates:
        logger.error(
            "No usable candidate_pairs found for underlying=%s min_confidence=%.2f classification=%s "
            "(%d skipped as already-expired). Has matching/run_matcher.py run recently?",
            args.underlying, args.min_confidence, args.classification, skipped_expired,
        )
        conn.close()
        return 1

    logger.info("Loaded %d candidate(s). Pulling live fee schedules...", len(candidates))

    fee_cache: dict[str, FeeSchedule] = {}
    exchanges = {c.short_contract.exchange for c in candidates} | {c.long_contract.exchange for c in candidates}
    for exchange in exchanges:
        adapter = _ADAPTERS.get(exchange)
        if adapter is None:
            continue
        try:
            fee_cache[exchange] = adapter.get_fees()
        except DeltaAdapterError as exc:
            logger.error(
                "Could not load fee schedule for %s: %s -- candidates on this exchange will be skipped.",
                exchange, exc,
            )

    snapshot_cache: dict[tuple[str, str], MarketSnapshot | None] = {}
    results: list[EVResult] = []
    skipped_no_data = 0
    skipped_no_fees = 0

    logger.info(
        "Pricing %d candidate(s) against LIVE bid/ask (throttle=%.2fs/call)...",
        len(candidates), COLLECTOR.request_throttle_sec,
    )

    for i, candidate in enumerate(candidates, start=1):
        short_fees = fee_cache.get(candidate.short_contract.exchange)
        long_fees = fee_cache.get(candidate.long_contract.exchange)
        if short_fees is None or long_fees is None:
            skipped_no_fees += 1
            continue

        short_snapshot = _fetch_snapshot(
            snapshot_cache, candidate.short_contract.exchange, candidate.short_contract.instrument_id,
            COLLECTOR.request_throttle_sec,
        )
        long_snapshot = _fetch_snapshot(
            snapshot_cache, candidate.long_contract.exchange, candidate.long_contract.instrument_id,
            COLLECTOR.request_throttle_sec,
        )
        if short_snapshot is None or long_snapshot is None:
            skipped_no_data += 1
            continue

        engine = LeanEVEngine(
            short_taker_fee_pct=short_fees.taker_fee_pct,
            long_taker_fee_pct=long_fees.taker_fee_pct,
        )
        try:
            result = engine.evaluate(candidate, short_snapshot, long_snapshot)
            results.append(result)
        except InsufficientDataError as exc:
            logger.debug("Skipping %s: %s", candidate.pair_id, exc)
            skipped_no_data += 1

        if i % 100 == 0:
            logger.info("  ...%d/%d processed", i, len(candidates))

    # Section M.2 gate: computed for every priced result, never skipped.
    eligibility: dict[str, bool] = {
        r.pair_id: is_entry_eligible(r.net_entry_cost, min_net_credit) for r in results
    }
    eligible_results = [r for r in results if eligibility[r.pair_id]]
    ineligible_count = len(results) - len(eligible_results)

    positive_ev = [r for r in results if r.expected_value > 0]
    logger.info(
        "Result: %d priced, %d skipped (already expired), %d skipped (no executable live data), "
        "%d skipped (no fee schedule), %d of %d priced show positive EV, "
        "%d of %d priced are entry_eligible (net credit, Section M.2; %d are net-debit DO_NOT_ENTER).",
        len(results), skipped_expired, skipped_no_data, skipped_no_fees, len(positive_ev), len(results),
        len(eligible_results), len(results), ineligible_count,
    )

    # Ranked display is entry_eligible-only, per Section M.2: a net-debit
    # candidate is never shown as a ranked "opportunity", even if its EV
    # happens to look positive under the lean model's simplifications --
    # see module docstring for why. Every priced candidate (eligible or not)
    # is still persisted below, so nothing is silently dropped from `signals`.
    ranked = sorted(eligible_results, key=lambda r: r.expected_value, reverse=True)

    logger.info(
        "Top %d entry_eligible by EV (each line: EV, P(profit), net entry, fees, hours-to-short-expiry, "
        "sigma_move, IV used, eligibility, pair id) --", min(args.top, len(ranked)),
    )
    for r in ranked[: args.top]:
        _log_result_line(r, True)

    if bottom_n:
        bottom_slice = ranked[-bottom_n:] if bottom_n < len(ranked) else ranked
        logger.info("Bottom %d entry_eligible by EV, for comparison --", min(bottom_n, len(ranked)))
        for r in sorted(bottom_slice, key=lambda r: r.expected_value):
            _log_result_line(r, True)

    if ineligible_count:
        logger.info(
            "%d priced candidate(s) were net-debit (entry_eligible=0, Section M.2 DO_NOT_ENTER) and are "
            "excluded from the ranked display above -- still priced and persisted to `signals` for visibility, "
            "just never surfaced as a ranked opportunity.",
            ineligible_count,
        )

    # Quick aggregate check on the P(profit) split itself: how many results
    # land at exactly 1.0 or exactly 0.0 vs. somewhere in between.
    exact_one = sum(1 for r in results if r.probability_of_profit == Decimal("1"))
    exact_zero = sum(1 for r in results if r.probability_of_profit == Decimal("0"))
    if results:
        logger.info(
            "P(profit) split (all priced, not just entry_eligible): %d/%d exactly 1.0, %d/%d exactly 0.0, "
            "%d/%d strictly between 0 and 1.",
            exact_one, len(results), exact_zero, len(results),
            len(results) - exact_one - exact_zero, len(results),
        )

    if args.dry_run:
        logger.info("--dry-run: not writing to `signals`.")
    else:
        _persist_results(conn, results, eligibility)
        logger.info("Wrote %d signal(s) to `signals` (%d entry_eligible, %d net-debit).",
                    len(results), len(eligible_results), ineligible_count)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
