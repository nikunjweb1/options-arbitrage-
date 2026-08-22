# Cross-Exchange Options Arbitrage Research & Execution Engine

Research-first, safety-default-on system for investigating (not assuming) a cross-exchange
crypto options expiry/pricing edge, starting from a hypothesis observed in a trading video.

## Read this before touching anything

This project explicitly does **not** assume the underlying strategy works. It exists to
find out, with real executable prices, real fees, and real settlement mechanics — and to
say so plainly if the edge doesn't survive contact with reality.

**Known open issue (do not skip):** the originally described strategy example (Exchange A
expiring 1:30 PM, Exchange B expiring 5:30 PM, same day) does not match Delta Exchange
India's documented mechanics — Delta settles *every* options contract at a fixed 5:30 PM
IST clock time, regardless of maturity (`D1`/`D2`/weekly/monthly differ by *date*, not by
*time of day*). See `/docs/architecture.md` for the full research writeup.

## Hard safety defaults (do not change without explicit sign-off)

```
LIVE_TRADING = FALSE   # always. Flipping this requires explicit, separate approval.
MODE = SCAN_ONLY        # SCAN_ONLY | PAPER | LIVE, default SCAN_ONLY
```

No order-placement code path is reachable while `LIVE_TRADING = FALSE`. Paper trading
mirrors live behavior with simulated fills only.

## Speed requirement

Data collection uses Delta's **WebSocket** feed, not REST polling, for real-time ticker
data. The realtime collector (`collectors/realtime_collector.py`) buffers incoming ticks in
memory and flushes to SQLite on a timer hard-capped at 1 second, default 0.5s.

REST (`collectors/market_data_collector.py`) still exists for instrument-specification
lookups (which don't need to be real-time) and as a fallback/smoke-test path.

## Build order (do not skip ahead)

```
Data → Matching → Mathematics → Scanner → Backtest → Paper Trading → Execution → Dashboard
```

Each phase has an explicit exit criterion in `/docs/architecture.md` (Section I). We do not
start Phase N+1 until Phase N's exit criterion is met.

## Current status

**Phase 2: done.** Delta Exchange India REST adapter, WebSocket client, both collectors,
and REST+WS integration test suites are built and validated against real testnet data.

**Phase 3: done.** `matching/engine.py` implements the Section C structural checks and
Section D.5 classification logic, covered by `tests/test_matching_engine.py`. Real runs
against live Delta data have found 1,500+ candidate pairs at different points in time
(the exact count grows as new daily D1 contracts get listed — re-running the matcher
periodically is expected, not a bug).

**Phase 5 (pricing/EV engine): DONE — exit criterion met.** `pricing/black_scholes.py` +
`pricing/ev_engine.py` (`LeanEVEngine`, a 21-point underlying-price grid × 3-point IV-shock
grid = 63 scenarios per candidate) implement Section D.2–D.4. `pricing/run_pricing.py` is
the CLI that wires real `candidate_pairs` to LIVE bid/ask + fee-schedule calls and persists
EV / probability-of-profit / a ranking score to `signals`. 19 tests in
`tests/test_ev_engine.py`, all fixture-based/no-network, all passing.

**Two real bugs were found and fixed during live-run validation, both via actual diagnostic
evidence against live Delta data, not guesses** (full writeup in each commit message and in
`pricing/ev_engine.py`'s module docstring):
- **Bug #1:** `contract_multiplier` was loaded but never applied to `short_payoff`/`v_long`
  in the P&L formula.
- **Bug #2 (the real cause of the wildly-implausible first live-run numbers):** exchange-quoted
  `best_bid`/`best_ask` are in raw per-1-BTC terms (same scale as spot/strike), not already
  scaled to one contract's notional — confirmed via `pricing/diagnose_pair.py` against a real
  quote (best_bid=12750 against spot=77223.2, strike=64400 — matches intrinsic almost exactly
  only if read as per-1-BTC). Bug #1's fix scaled the payoff/repricing side correctly but left
  the premium side unscaled, leaving `net_entry_cost` ~1000x too large relative to the
  (correctly scaled) payoff terms — an inverse-direction version of the same class of bug.

**Confirmed-good final live run (2026-08-23):** 674 non-expired candidates loaded (2,115
filtered as already-expired — see "stale candidate_pairs" note below), 404 successfully
priced against live bid/ask (270 skipped for no executable live data — real testnet
illiquidity, not a bug), 292/404 (72%) show positive EV. Critically,
**`P(profit)` is now spread across a real range for all 404 results — zero landed at exactly
1.0 or exactly 0.0** (the earlier hard 0/1 split was a symptom of Bug #2, now gone). EV
magnitudes are now the same rough order as net entry cost (e.g. EV≈$3.75 against a $1.24
entry cost), not the ~1000x-inflated numbers from before either bug fix.

**Caveats worth keeping in mind before calling this "edge":**
- Per Section D.2, `Net_entry_cost` is supposed to subtract `expected_slippage` on both legs.
  `ev_engine.py` still only subtracts trading fees — slippage is not modeled. On sub-$2
  premiums, a single tick of slippage could erase the whole EV. This is disclosed in every
  `EVResult.model_notes`, not silently assumed away.
- 40% of priced attempts (270/674) hit no executable live data — testnet liquidity is thin.
- **Stale candidate_pairs**: `candidate_pairs` grows over time as Delta lists new daily (D1)
  contracts; old rows whose short leg has since expired are filtered out at load time
  (`_load_candidates`) before wasting a live call on them, and the count is reported
  explicitly in the run summary rather than surfacing as confusing per-ticker "fetch failed"
  warnings. Re-run `matching.run_matcher` periodically to keep the candidate pool fresh.

No CoinSwitch or Shark adapters exist yet — both remain blocked on real public documentation,
per Section 0/L.5.

## Repository layout

```
docs/
  architecture.md              # full architecture, research findings, roadmap (source of truth)
exchange_adapters/
  base.py                      # ExchangeAdapter protocol — every adapter implements this
  delta.py                     # Delta Exchange India REST adapter, retries transient network errors with backoff
  delta_ws.py                  # Delta WebSocket client — real-time ticker feed
normalization/
  schemas.py                   # OptionContract, MarketSnapshot, FeeSchedule, ContractSpec — exchange-agnostic
matching/
  schemas.py                    # MatchCandidate, RejectedPair, Classification, RejectionReason
  engine.py                      # MatchingEngine -- Section C checks + Section D.5 classification
  run_matcher.py                  # CLI: runs the engine against real DB data, persists candidate_pairs
pricing/
  black_scholes.py                 # European option pricer (Black-Scholes), used to reprice the long leg at T1
  ev_engine.py                      # LeanEVEngine -- Section D.2-D.4, 21x3=63-scenario grid, contract_multiplier-scaled
  run_pricing.py                     # CLI: prices real candidate_pairs against LIVE bid/ask, persists to signals
  diagnose_pair.py                    # ad-hoc diagnostic script used to find Bug #2 against real live data
collectors/
  market_data_collector.py      # REST-polling collector (instrument specs, fallback path)
  realtime_collector.py          # WebSocket-driven collector, sub-1s flush loop (primary path)
  run.py                          # CLI for the REST collector (python -m collectors.run)
  run_realtime.py                  # CLI for the realtime collector (python -m collectors.run_realtime)
  gap_report.py                     # verifies the Phase 2 "24h gap-free" exit criterion
db/
  schema.sql                        # SQLite schema (instruments, market_data, candidate_pairs, signals, trades)
  init_db.py                         # creates a local SQLite DB from schema.sql (run as `python -m db.init_db`)
  loaders.py                          # shared row -> dataclass loading helpers
config/
  settings.py                         # env-driven config, LIVE_TRADING hardcoded default
  .env.example                         # no real secrets, ever
tests/
  test_delta_adapter.py                 # REST normalization unit tests, fixture-based, no network
  test_delta_ws.py                       # WS parsing + flush-ceiling unit tests, no network
  test_delta_integration.py               # real testnet REST integration suite, skipped unless explicitly enabled
  test_delta_ws_integration.py             # real testnet WS integration suite, skipped unless explicitly enabled
  test_gap_report.py                       # gap-detection unit tests, no network
  test_matching_engine.py                   # Phase 3 exit-criterion suite: accepts/rejects fixtures
  test_ev_engine.py                          # Phase 5 exit-criterion suite: 19 tests, incl. unit-consistency regressions for both bugs
requirements.txt
pyproject.toml
.gitignore
```

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env   # fill in your own Delta testnet API keys, never commit
python -m db.init_db
```

Note: `db/init_db.py` must be run as a module (`python -m db.init_db`) from the repo root,
not as a bare script — running it directly breaks the `from config.settings import DB`
import because Python puts `db/` itself on `sys.path` instead of the repo root.

Never commit `.env`, API keys, or account identifiers. `.gitignore` is configured to block
common secret file patterns, but review diffs before pushing regardless.

## Running tests

```bash
pytest tests/                                                                 # unit tests only (default, no network)
RUN_INTEGRATION_TESTS=true pytest tests/test_delta_integration.py -v -s        # real testnet REST, explicit opt-in
RUN_INTEGRATION_TESTS=true pytest tests/test_delta_ws_integration.py -v -s      # real testnet WS, explicit opt-in
```

## Running the collector

```bash
# Real-time (primary path) -- WebSocket-driven, sub-1s flush
python -m collectors.run_realtime --duration-hours 24

# REST (fallback / smoke-test path)
python -m collectors.run --once
python -m collectors.run --duration-hours 24

# Check either run's output for gaps
python -m collectors.gap_report
```

## Running the matcher

```bash
# Self-match Delta's own D1/D2/weekly chain first (Phase 2 MVP, Section J)
python -m matching.run_matcher --underlying BTC --exchange delta_india --dry-run

# Once you're happy with the results, drop --dry-run to persist to candidate_pairs
python -m matching.run_matcher --underlying BTC --exchange delta_india
```

Run this periodically — Delta lists new daily (D1) contracts continuously, so
`candidate_pairs` goes stale (short legs expiring) within hours for same-day candidates.

## Running the pricing engine (Phase 5 — done, re-run anytime for fresh numbers)

Requires `candidate_pairs` to already be populated (run the matcher above first, recently)
and network access to Delta's testnet REST API for live ticker/fee-schedule calls.

```bash
# Smoke test first -- small, non-destructive (nothing written to `signals`)
python -m pricing.run_pricing --underlying BTC --limit 20 --dry-run

# Full run, writes EV/probability-of-profit/score to `signals`
python -m pricing.run_pricing --underlying BTC

# Useful filters
python -m pricing.run_pricing --underlying BTC --min-confidence 0.8 --classification same_exchange_calendar_spread --top 50
```

Watch the summary line and the `P(profit) split` line specifically — if results start
clustering back at exactly 0.0/1.0, that's a regression signal worth investigating before
trusting the numbers (see Bug #2's writeup in `pricing/ev_engine.py` for why that pattern
is a red flag, not a good sign).

## License note

This project may study (never blindly copy) public methodology from other open-source
projects. See `/docs/architecture.md` Section K for a license-by-license breakdown of what
was reused, from where, and under what terms.
