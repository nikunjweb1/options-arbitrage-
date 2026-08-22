"""
CLI: Phase 6 (lean backtester) -- simulates every already-settled candidate
pair against real historical `market_data` ticks and produces one honest
report.

Per docs/architecture.md Section I / L.3 exit criterion:
    "one honest report, whatever window the data supports, explicitly
    labeled 'lean pass' -- a clear go/no-go signal on whether to invest in
    the fuller v1 backtest matrix."

WHAT THIS DOES NOT DO (Section G.3, explicitly deferred not cancelled):
  - No in-sample/out-of-sample split, no walk-forward re-estimation.
  - No segmentation by moneyness/vol regime, no threshold sweep.
  - No stress-scenario suite (+/-5%/10% BTC moves, IV shocks, liquidity
    collapse) beyond what real historical data happened to contain.
These come back into scope only if this lean pass shows something worth the
additional engineering time (Section L.5).

WHY THIS MIGHT REPORT VERY FEW OR ZERO COMPLETED TRADES: Phase 2's
collectors have only been running for a short window relative to how far
out most candidate pairs' short-leg expiries are. A trade can only be
backtested once its short leg has actually settled AND the collector was
running continuously through both the entry moment and the settlement TWAP
window. If most candidates' short legs haven't settled yet, or settled
before collection started, this is reported honestly as
not_yet_settled / gap counts -- never quietly reinterpreted as "no edge
found" or papered over with synthetic data (Section G.1 item 2).

Usage:
    python -m backtest.run_backtest --underlying BTC
    python -m backtest.run_backtest --underlying BTC --classification same_exchange_calendar_spread
    python -m backtest.run_backtest --underlying BTC --report-path reports/

Does not require network access except one call to DeltaAdapter.get_fees()
(a static, documented fee schedule -- see exchange_adapters/delta.py) --
everything else is read from the local `market_data`/`candidate_pairs`
tables already populated by Phase 2/3/5.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backtest.engine import LeanBacktester
from backtest.schemas import BacktestReport, BacktestTradeResult, HistoricalTick
from config.settings import DB
from exchange_adapters.delta import DeltaAdapter, DeltaAdapterError
from matching.schemas import Classification, MatchCandidate
from normalization.schemas import OptionContract, OptionType, OptionVariant, SettlementMethod

logger = logging.getLogger("backtest.run_backtest")

# Only Delta is wired end-to-end as of this phase -- see pricing/run_pricing.py's
# identical rationale for a single-adapter map rather than a hardcoded assumption.
_ADAPTERS = {"delta_india": DeltaAdapter()}


def _row_to_contract(row: sqlite3.Row) -> OptionContract | None:
    """Same duplicated-not-imported pattern as pricing/run_pricing.py's loader."""
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
    conn: sqlite3.Connection, underlying: str, classification: str | None
) -> list[MatchCandidate]:
    conn.row_factory = sqlite3.Row
    query = """
        SELECT cp.* FROM candidate_pairs cp
        JOIN instruments si ON si.exchange = cp.short_exchange AND si.instrument_id = cp.short_instrument_id
        WHERE si.underlying = ?
    """
    params: list = [underlying]
    if classification:
        query += " AND cp.classification = ?"
        params.append(classification)
    query += " ORDER BY cp.created_at"

    rows = conn.execute(query, params).fetchall()
    candidates: list[MatchCandidate] = []
    for row in rows:
        short_contract = _load_contract(conn, row["short_exchange"], row["short_instrument_id"])
        long_contract = _load_contract(conn, row["long_exchange"], row["long_instrument_id"])
        if short_contract is None or long_contract is None:
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
    return candidates


def _load_ticks(conn: sqlite3.Connection, exchange: str, instrument_id: str) -> list[HistoricalTick]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, best_bid, best_ask, index_price FROM market_data "
        "WHERE exchange = ? AND instrument_id = ? ORDER BY ts",
        (exchange, instrument_id),
    ).fetchall()
    ticks = []
    for row in rows:
        ticks.append(
            HistoricalTick(
                ts=datetime.fromisoformat(row["ts"]),
                best_bid=Decimal(row["best_bid"]) if row["best_bid"] is not None else None,
                best_ask=Decimal(row["best_ask"]) if row["best_ask"] is not None else None,
                index_price=Decimal(row["index_price"]) if row["index_price"] is not None else None,
            )
        )
    return ticks


def _data_window(conn: sqlite3.Connection) -> tuple[datetime | None, datetime | None]:
    row = conn.execute("SELECT MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM market_data").fetchone()
    if row is None or row["min_ts"] is None:
        return None, None
    return datetime.fromisoformat(row["min_ts"]), datetime.fromisoformat(row["max_ts"])


def _build_report(
    underlying: str,
    window_start: datetime | None,
    window_end: datetime | None,
    results: list[BacktestTradeResult],
) -> BacktestReport:
    by_status: dict[str, list[BacktestTradeResult]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r)

    completed = tuple(by_status.get("completed", []))
    wins = [r for r in completed if r.realized_pnl is not None and r.realized_pnl > 0]
    losses = [r for r in completed if r.realized_pnl is not None and r.realized_pnl <= 0]

    win_rate = Decimal(len(wins)) / Decimal(len(completed)) if completed else None
    total_pnl = sum((r.realized_pnl for r in completed), Decimal("0")) if completed else None
    avg_pnl = (total_pnl / Decimal(len(completed))) if completed else None

    return BacktestReport(
        underlying=underlying,
        generated_at=datetime.now(timezone.utc),
        window_start=window_start,
        window_end=window_end,
        total_candidates_considered=len(results),
        not_yet_settled_count=len(by_status.get("not_yet_settled", [])),
        gap_no_entry_data_count=len(by_status.get("gap_no_entry_data", [])),
        gap_no_settlement_data_count=len(by_status.get("gap_no_settlement_data", [])),
        gap_no_exit_data_count=len(by_status.get("gap_no_exit_data", [])),
        legging_failed_count=len(by_status.get("legging_failed", [])),
        completed_trades=completed,
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=win_rate,
        total_realized_pnl=total_pnl,
        avg_pnl_per_trade=avg_pnl,
    )


def _report_to_dict(report: BacktestReport) -> dict:
    def _s(v):
        return str(v) if v is not None else None

    return {
        "underlying": report.underlying,
        "generated_at": report.generated_at.isoformat(),
        "window_start": report.window_start.isoformat() if report.window_start else None,
        "window_end": report.window_end.isoformat() if report.window_end else None,
        "total_candidates_considered": report.total_candidates_considered,
        "not_yet_settled_count": report.not_yet_settled_count,
        "gap_no_entry_data_count": report.gap_no_entry_data_count,
        "gap_no_settlement_data_count": report.gap_no_settlement_data_count,
        "gap_no_exit_data_count": report.gap_no_exit_data_count,
        "legging_failed_count": report.legging_failed_count,
        "completed_trade_count": len(report.completed_trades),
        "win_count": report.win_count,
        "loss_count": report.loss_count,
        "win_rate": _s(report.win_rate),
        "total_realized_pnl": _s(report.total_realized_pnl),
        "avg_pnl_per_trade": _s(report.avg_pnl_per_trade),
        "model_notes": list(report.model_notes),
        "completed_trades": [
            {
                "pair_id": t.pair_id,
                "entry_ts": t.entry_ts.isoformat() if t.entry_ts else None,
                "net_entry_cost": _s(t.net_entry_cost),
                "settlement_index_price": _s(t.settlement_index_price),
                "short_payoff": _s(t.short_payoff),
                "settlement_fee": _s(t.settlement_fee),
                "long_exit_price": _s(t.long_exit_price),
                "long_exit_fee": _s(t.long_exit_fee),
                "realized_pnl": _s(t.realized_pnl),
            }
            for t in report.completed_trades
        ],
    }


def _print_summary(report: BacktestReport) -> None:
    logger.info(
        "Lean backtest report for %s: window %s -> %s (whatever the collector actually captured).",
        report.underlying, report.window_start, report.window_end,
    )
    logger.info(
        "  %d candidates considered: %d not_yet_settled, %d gap_no_entry_data, "
        "%d gap_no_settlement_data, %d gap_no_exit_data, %d legging_failed, %d completed.",
        report.total_candidates_considered, report.not_yet_settled_count,
        report.gap_no_entry_data_count, report.gap_no_settlement_data_count,
        report.gap_no_exit_data_count, report.legging_failed_count, len(report.completed_trades),
    )
    if report.completed_trades:
        logger.info(
            "  Of %d completed trades: %d wins, %d losses, win_rate=%s, "
            "total_realized_pnl=%s, avg_pnl_per_trade=%s.",
            len(report.completed_trades), report.win_count, report.loss_count,
            report.win_rate, report.total_realized_pnl, report.avg_pnl_per_trade,
        )
    else:
        logger.warning(
            "  Zero completed trades -- no win-rate or P&L number can be reported honestly. "
            "See gap counts above for why (most likely: not enough historical data yet, "
            "see this script's module docstring)."
        )
    for note in report.model_notes:
        logger.info("  NOTE: %s", note)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Phase 6 (lean): backtest every already-settled candidate pair against real historical ticks."
    )
    parser.add_argument("--underlying", required=True, help="e.g. BTC")
    parser.add_argument("--classification", default=None,
                         help="Filter to one Classification value, e.g. same_exchange_calendar_spread")
    parser.add_argument("--report-path", default="reports",
                         help="Directory to write the JSON report into (created if missing).")
    args = parser.parse_args()

    if not DB.sqlite_path.exists():
        logger.error("No database found at %s. Run db/init_db.py and the collectors/matcher/pricing steps first.", DB.sqlite_path)
        return 1

    conn = sqlite3.connect(DB.sqlite_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    window_start, window_end = _data_window(conn)
    if window_start is None:
        logger.error("`market_data` is empty -- nothing to backtest yet. Run the collectors first.")
        conn.close()
        return 1

    candidates = _load_candidates(conn, args.underlying, args.classification)
    if not candidates:
        logger.error("No candidate_pairs found for underlying=%s classification=%s.", args.underlying, args.classification)
        conn.close()
        return 1

    logger.info("Loaded %d candidate(s). Historical market_data window: %s -> %s.",
                len(candidates), window_start, window_end)

    adapter = _ADAPTERS.get("delta_india")
    try:
        fees = adapter.get_fees()
    except DeltaAdapterError as exc:
        logger.error("Could not load fee schedule: %s -- cannot backtest without documented fee values.", exc)
        conn.close()
        return 1

    engine = LeanBacktester(
        short_taker_fee_pct=fees.taker_fee_pct,
        long_taker_fee_pct=fees.taker_fee_pct,
        settlement_fee_pct=fees.settlement_fee_pct or Decimal("0"),
        fee_cap_pct_of_premium=fees.fee_cap_pct_of_premium,
        zero_fee_on_otm_settlement=fees.zero_fee_on_otm_settlement,
    )

    now = datetime.now(timezone.utc)
    tick_cache: dict[tuple[str, str], list[HistoricalTick]] = {}

    def _ticks_for(exchange: str, instrument_id: str) -> list[HistoricalTick]:
        key = (exchange, instrument_id)
        if key not in tick_cache:
            tick_cache[key] = _load_ticks(conn, exchange, instrument_id)
        return tick_cache[key]

    results: list[BacktestTradeResult] = []
    for i, candidate in enumerate(candidates, start=1):
        short_ticks = _ticks_for(candidate.short_contract.exchange, candidate.short_contract.instrument_id)
        long_ticks = _ticks_for(candidate.long_contract.exchange, candidate.long_contract.instrument_id)
        result = engine.simulate_pair(candidate, short_ticks, long_ticks, now)
        results.append(result)
        if i % 100 == 0:
            logger.info("  ...%d/%d simulated", i, len(candidates))

    report = _build_report(args.underlying, window_start, window_end, results)
    _print_summary(report)

    report_dir = Path(args.report_path)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"backtest_{args.underlying}_{report.generated_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    report_file.write_text(json.dumps(_report_to_dict(report), indent=2))
    logger.info("Wrote full report to %s", report_file)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
