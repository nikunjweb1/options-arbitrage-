# Research Dashboard (read-only)

Visualizes what's already in the SQLite DB — `signals` and `candidate_pairs`,
written by `pricing/run_pricing.py` and `matching/run_matcher.py`. Adds no
new computation, no write endpoints, no trading, no `LIVE_TRADING` dependency
of any kind. Safe to run at any point, including right now.

## ⚠️ Known repo issue this depends on being fixed

As of this dashboard being added, two files in the repo are **11-byte
placeholder stubs** (literal contents: `PLACEHOLDER`) despite
`docs/architecture.md` and `README.md` describing both as done, tested, and
run live:

- `config/settings.py`
- `pricing/run_pricing.py`

Both files have the **same git blob SHA**, meaning they contain byte-for-byte
identical placeholder content — this is almost certainly a sync issue between
parallel work sessions (per the project's own memory notes about a Claude
Code session and this session working the same repo), not two independent
accidents.

**This dashboard does not import `config.settings`** (see `backend/app.py`'s
module docstring for why), so it will run today regardless. But it can only
show you real data once `pricing/run_pricing.py` is restored and re-run to
populate `signals` — until then `/api/summary` will report `total_signals: 0`
against an empty or stale DB. Reconciling those two files with whatever the
other session has locally should happen before trusting any numbers this
dashboard shows.

## Setup

```bash
cd dashboard/backend
pip install -r requirements.txt

# Point at your real DB (same convention as config/.env.example's
# SQLITE_PATH). Defaults to data/options_arb.db if unset.
export SQLITE_PATH=../../data/options_arb.db

uvicorn app:app --reload --port 8000
```

Then open `dashboard/frontend/index.html` directly in a browser (no build
step, no npm, no server needed for the frontend — it's a static file that
calls `http://localhost:8000`).

## What it shows

- **Summary cards**: total signals, entry-eligible count (net-credit
  candidates per the `MIN_NET_CREDIT` gate, Section M.2), positive-EV count,
  latest pricing run timestamp.
- **P(profit) histogram**: 10 buckets across the full priced set. Per
  `pricing/ev_engine.py`'s own module docstring, if this histogram ever
  collapses back to two spikes at the 0.0 and 1.0 ends, that's the exact
  regression signature of Bug #2 (contract-multiplier scaling) — worth
  checking `pricing/ev_engine.py` immediately if it recurs.
- **Signals table**: filterable by entry-eligibility, minimum P(profit), and
  sortable by score/EV/P(profit)/recency. Joined against `instruments` for
  human-readable strike/expiry/exchange context.

## What it deliberately does not do

- No order placement, no `trades` table writes, no auth (this is a
  localhost research tool, not a public-facing app).
- No slippage-adjusted numbers — this shows exactly what's in `signals`,
  which per `docs/architecture.md` Section D.2 does **not** yet include
  `expected_slippage`. The dashboard doesn't hide that gap or fill it in;
  treat every EV/P(profit) number here with the same caveat the pricing
  engine itself discloses.
- No backtest (Phase 6) or paper-trading (Phase 7) results yet — those
  tables/views don't exist in `schema.sql` yet. Natural next extension once
  `backtest/run_backtest.py` has a persisted output table to read from.
