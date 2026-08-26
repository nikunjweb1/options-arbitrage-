"""
Read-only research dashboard API.

Serves data that already exists in the project's SQLite DB (instruments,
candidate_pairs, signals, manual_recommendations) so results from
pricing/run_pricing.py and pricing/manual_spread_finder.py are visible
without re-running anything.

Deliberately self-contained: does NOT import config.settings, because that
module is currently a placeholder stub in the repo (see the "known repo
issue" note in README.md alongside this file). DB path is read from the
SQLITE_PATH env var directly, matching the convention already established
in config/.env.example, with a same default (data/options_arb.db).

Deliberately does nothing else:
- No write endpoints. No order placement. No LIVE_TRADING dependency at all.
- Opens SQLite in read-only mode (mode=ro) so a bug here structurally cannot
  corrupt data the pricing/backtest engines depend on.
- If this file and run_pricing.py's DB writes ever disagree about schema,
  that's a signal to fix schema drift, not a reason to add write paths here.

Run:
    pip install fastapi uvicorn
    SQLITE_PATH=data/options_arb.db uvicorn dashboard.backend.app:app --reload --port 8000
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = Path(os.environ.get("SQLITE_PATH", "data/options_arb.db"))

app = FastAPI(
    title="Options Arbitrage — Research Dashboard (read-only)",
    description=(
        "Visualizes existing signals/candidate_pairs/manual_recommendations data. "
        "No write endpoints. No trading. No LIVE_TRADING dependency."
    ),
)

# Wide-open CORS is fine here: this is a read-only, no-auth, localhost
# research tool reading data that's already on disk. Do not copy this
# CORS config into anything that gains a write endpoint later.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                f"SQLite DB not found at {DB_PATH.resolve()}. "
                f"Set SQLITE_PATH env var, or run `python -m db.init_db` "
                f"and a data-collection/pricing pass first."
            ),
        )
    # mode=ro: this process can never write to the DB, no matter what a bug
    # in this file does. uri=True is required for the ?mode=ro query param.
    uri = f"file:{DB_PATH.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _f(value: Optional[str]) -> Optional[float]:
    """Schema stores numeric fields as TEXT (Decimal in the app layer, per
    schema.sql's own comment: 'never float'). For dashboard display only —
    never feed this back into any P&L calculation — float is fine here."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "db_path": str(DB_PATH.resolve()), "db_exists": DB_PATH.exists()}


@app.get("/api/summary")
def summary() -> dict:
    """Headline numbers: same shape as the run_pricing.py console summary
    (total priced, entry_eligible count, % positive EV, P(profit) spread),
    so the dashboard corroborates the CLI output rather than inventing a
    different framing of the same data."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                    AS total_signals,
                SUM(entry_eligible)                          AS eligible_count,
                SUM(CASE WHEN CAST(expected_value AS REAL) > 0 THEN 1 ELSE 0 END) AS positive_ev_count,
                MIN(ts)                                        AS earliest_ts,
                MAX(ts)                                          AS latest_ts
            FROM signals
            """
        ).fetchone()

        # P(profit) histogram in 10 buckets [0.0-0.1) ... [0.9-1.0]
        buckets = [0] * 10
        for r in conn.execute("SELECT prob_of_profit FROM signals"):
            p = _f(r["prob_of_profit"])
            if p is None:
                continue
            idx = min(int(p * 10), 9)
            buckets[idx] += 1

        classification_counts = {
            r["classification"]: r["n"]
            for r in conn.execute(
                """
                SELECT cp.classification AS classification, COUNT(*) AS n
                FROM signals s
                JOIN candidate_pairs cp ON cp.pair_id = s.pair_id
                GROUP BY cp.classification
                """
            )
        }

    total = row["total_signals"] or 0
    eligible = row["eligible_count"] or 0
    positive_ev = row["positive_ev_count"] or 0

    return {
        "total_signals": total,
        "entry_eligible_count": eligible,
        "entry_eligible_pct": round(100 * eligible / total, 1) if total else None,
        "positive_ev_count": positive_ev,
        "positive_ev_pct": round(100 * positive_ev / total, 1) if total else None,
        "earliest_ts": row["earliest_ts"],
        "latest_ts": row["latest_ts"],
        "prob_of_profit_histogram": buckets,
        "classification_counts": classification_counts,
    }


@app.get("/api/signals")
def list_signals(
    entry_eligible: Optional[bool] = Query(
        None, description="Filter to entry_eligible=1 candidates only if true"
    ),
    classification: Optional[str] = Query(None),
    min_prob_of_profit: Optional[float] = Query(None, ge=0.0, le=1.0),
    limit: int = Query(100, ge=1, le=1000),
    order_by: str = Query(
        "score", pattern="^(score|expected_value|prob_of_profit|ts)$"
    ),
) -> dict:
    """Ranked signal list, joined with candidate_pairs/instruments for
    human-readable exchange/strike/expiry context. Read-only; mirrors
    exactly what run_pricing.py already persists, adds no new computation."""
    where = []
    params: list = []

    if entry_eligible is not None:
        where.append("s.entry_eligible = ?")
        params.append(1 if entry_eligible else 0)
    if classification:
        where.append("cp.classification = ?")
        params.append(classification)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sql = f"""
        SELECT
            s.signal_id, s.ts, s.pair_id, s.net_entry_cost, s.expected_value,
            s.expected_profit, s.prob_of_profit, s.score, s.entry_eligible,
            cp.classification, cp.match_confidence,
            cp.short_exchange, cp.short_instrument_id,
            cp.long_exchange, cp.long_instrument_id,
            si.underlying AS short_underlying, si.strike AS short_strike,
            si.expiry_ts AS short_expiry_ts, si.option_type AS short_option_type,
            li.strike AS long_strike, li.expiry_ts AS long_expiry_ts
        FROM signals s
        JOIN candidate_pairs cp ON cp.pair_id = s.pair_id
        LEFT JOIN instruments si
            ON si.exchange = cp.short_exchange AND si.instrument_id = cp.short_instrument_id
        LEFT JOIN instruments li
            ON li.exchange = cp.long_exchange AND li.instrument_id = cp.long_instrument_id
        {where_sql}
        ORDER BY CAST(s.{order_by} AS REAL) DESC
        LIMIT ?
    """
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        if min_prob_of_profit is not None:
            p = _f(r["prob_of_profit"])
            if p is None or p < min_prob_of_profit:
                continue
        results.append(
            {
                "signal_id": r["signal_id"],
                "ts": r["ts"],
                "pair_id": r["pair_id"],
                "net_entry_cost": _f(r["net_entry_cost"]),
                "expected_value": _f(r["expected_value"]),
                "expected_profit": _f(r["expected_profit"]),
                "prob_of_profit": _f(r["prob_of_profit"]),
                "score": _f(r["score"]),
                "entry_eligible": bool(r["entry_eligible"]),
                "classification": r["classification"],
                "match_confidence": _f(r["match_confidence"]),
                "short_exchange": r["short_exchange"],
                "short_instrument_id": r["short_instrument_id"],
                "short_underlying": r["short_underlying"],
                "short_strike": _f(r["short_strike"]),
                "short_expiry_ts": r["short_expiry_ts"],
                "short_option_type": r["short_option_type"],
                "long_exchange": r["long_exchange"],
                "long_instrument_id": r["long_instrument_id"],
                "long_strike": _f(r["long_strike"]),
                "long_expiry_ts": r["long_expiry_ts"],
            }
        )
    return {"count": len(results), "results": results}


@app.get("/api/candidates")
def list_candidates(limit: int = Query(100, ge=1, le=1000)) -> dict:
    """Raw Phase 3 matcher output (candidate_pairs), independent of whether
    it's been priced yet — useful to see matcher throughput vs. pricing
    throughput side by side (e.g. the 674-loaded / 404-priced gap noted in
    docs/architecture.md's Phase 5 writeup)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT pair_id, short_exchange, short_instrument_id,
                   long_exchange, long_instrument_id, match_confidence,
                   classification, created_at
            FROM candidate_pairs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {
        "count": len(rows),
        "results": [dict(r) for r in rows],
    }


@app.get("/api/manual-recommendations")
def list_manual_recommendations(
    entry_eligible: Optional[bool] = Query(
        None, description="Filter to entry_eligible=1 (net-credit) recommendations only if true"
    ),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """
    Output of pricing/manual_spread_finder.py -- the manual-trading-
    assistant workflow's actual "message shown on the website" surface.
    Every recommendation ever computed is stored, not just the good ones
    (see that script's module docstring) -- entry_eligible=true here is
    what should drive a banner/highlight on the frontend, since per
    docs/architecture.md Section M.2, a net-debit recommendation has
    unbounded downside and should never be presented as an opportunity to
    act on, only as "checked, wasn't good enough."

    Tax figures returned here are per pricing/tax.py's documented
    assumptions -- NOT tax advice, surfaced as such in this response's
    field names deliberately (the "_estimate" suffix is not decorative).
    """
    where = "WHERE entry_eligible = 1" if entry_eligible else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
                recommendation_id, ts,
                short_exchange, short_underlying, short_option_type, short_strike,
                short_expiry_date, short_bid_input,
                long_exchange, long_instrument_id, long_expiry_ts,
                long_ask_live, long_ask_size_live,
                net_entry_cost, entry_eligible,
                short_size_input, max_safe_contracts,
                gross_profit_estimate, tax_owed_estimate,
                net_profit_after_tax_estimate, tds_withheld_estimate
            FROM manual_recommendations
            {where}
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    results = []
    for r in rows:
        max_safe = _f(r["max_safe_contracts"])
        results.append(
            {
                "recommendation_id": r["recommendation_id"],
                "ts": r["ts"],
                "short_exchange": r["short_exchange"],
                "short_underlying": r["short_underlying"],
                "short_option_type": r["short_option_type"],
                "short_strike": _f(r["short_strike"]),
                "short_expiry_date": r["short_expiry_date"],
                "short_bid_input": _f(r["short_bid_input"]),
                "long_exchange": r["long_exchange"],
                "long_instrument_id": r["long_instrument_id"],
                "long_expiry_ts": r["long_expiry_ts"],
                "long_ask_live": _f(r["long_ask_live"]),
                "long_ask_size_live": _f(r["long_ask_size_live"]),
                "net_entry_cost": _f(r["net_entry_cost"]),
                "entry_eligible": bool(r["entry_eligible"]),
                "short_size_input": _f(r["short_size_input"]),
                "max_safe_contracts": max_safe,
                "low_liquidity_warning": (max_safe is not None and max_safe < 1.0),
                "gross_profit_estimate": _f(r["gross_profit_estimate"]),
                "tax_owed_estimate": _f(r["tax_owed_estimate"]),
                "net_profit_after_tax_estimate": _f(r["net_profit_after_tax_estimate"]),
                "tds_withheld_estimate": _f(r["tds_withheld_estimate"]),
                "tax_disclaimer": "Estimate only, not tax advice -- see pricing/tax.py for assumptions.",
            }
        )
    return {"count": len(results), "results": results}
