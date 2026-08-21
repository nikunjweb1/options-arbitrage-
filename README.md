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
*time of day*). See `/docs/architecture.md` for the full research writeup and the MVP
designed to test this cheaply (Delta's own calendar structure) before spending effort on
a second, currently under-documented exchange.

## Hard safety defaults (do not change without explicit sign-off)

```
LIVE_TRADING = FALSE   # always. Flipping this requires explicit, separate approval.
MODE = SCAN_ONLY        # SCAN_ONLY | PAPER | LIVE, default SCAN_ONLY
```

No order-placement code path is reachable while `LIVE_TRADING = FALSE`. Paper trading
mirrors live behavior with simulated fills only.

## Speed requirement

Data collection uses Delta's **WebSocket** feed, not REST polling, for real-time ticker
data — REST polling alone can't reliably hit sub-second latency. The realtime collector
(`collectors/realtime_collector.py`) buffers incoming ticks in memory and flushes to SQLite
on a timer **hard-capped at 1 second** (`_MAX_FLUSH_INTERVAL_SEC` in that file, enforced in
code — not a config value that can be quietly set slower). Default flush interval is 0.5s.

**Honesty note:** Delta's official WebSocket message-format documentation lives in a page
too large to fully retrieve during this research pass. The subscribe/unsubscribe message
shape used in `exchange_adapters/delta_ws.py` is reconstructed from two independent
community sources that agree with each other, not copied directly from
`docs.delta.exchange`. This needs to be validated against a live testnet connection before
being trusted for anything beyond Phase 2 — see the module docstring in `delta_ws.py`, and
run `tests/test_delta_ws_integration.py` for the actual validation, for specifics.

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
Section D.5 classification logic, covered by `tests/test_matching_engine.py`.
`matching/run_matcher.py` has been run against real Delta data: 1,504 candidate pairs
found (433 same-exchange calendar spreads, 1,071 relative-value), 68,247 correctly
rejected, persisted to `candidate_pairs`.

**Phase 5 (pricing/EV engine): code complete and unit-tested against fixtures; the live
run against real candidate_pairs is the one remaining step.**
`pricing/black_scholes.py` (Black-Scholes pricer) and `pricing/ev_engine.py` (lean
scenario-grid EV engine, Section L.2 — a 5-point underlying-price grid crossed with a
3-point IV-shock grid, 15 scenarios per candidate) implement Section D.2–D.4 of the math
spec. `pricing/run_pricing.py` is the CLI that wires real `candidate_pairs` to LIVE
bid/ask + fee-schedule calls via the exchange adapters (never backfilled `market_data`)
and persists EV / probability-of-profit / a ranking score to `signals`. All of this is
covered by `tests/test_ev_engine.py` (fixture-based, no network) — every case has been
re-verified. **What hasn't happened yet: an actual run against live Delta testnet data.**
That's the real Phase 5 exit criterion ("EV, net-of-fees profit, and a probability-of-profit
estimate computed for all 1,504 real candidates, using real bid/ask pulled live, not
backfilled") — see "Running the pricing engine" below.

No CoinSwitch or Shark adapters exist yet — CoinSwitch's options API is "available on
request" (not self-serve docs) and Shark Exchange has no public options API documentation
found; both are blocked pending real documentation, not implemented against guesses.

## Repository layout

```
docs/
  architecture.md              # full architecture, research findings, roadmap (source of truth)
exchange_adapters/
  base.py                      # ExchangeAdapter protocol — every adapter implements this
  delta.py                     # Delta Exchange India REST adapter (testnet-first, read-only in Phase 2)
  delta_ws.py                  # Delta WebSocket client — real-time ticker feed
normalization/
  schemas.py                   # OptionContract, MarketSnapshot, FeeSchedule, ContractSpec — exchange-agnostic
matching/
  schemas.py                    # MatchCandidate, RejectedPair, Classification, RejectionReason
  engine.py                      # MatchingEngine -- Section C checks + Section D.5 classification
  run_matcher.py                  # CLI: runs the engine against real DB data, persists candidate_pairs
pricing/
  black_scholes.py                 # European option pricer (Black-Scholes), used to reprice the long leg at T1
  ev_engine.py                      # LeanEVEngine -- Section D.2-D.4 lean scenario-grid EV/probability-of-profit
  run_pricing.py                     # CLI: prices real candidate_pairs against LIVE bid/ask, persists to signals
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
  test_ev_engine.py                          # Phase 5 exit-criterion suite: black_scholes.py + ev_engine.py, fixture-based, no network
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
not as a bare script (`python db/init_db.py`) — running it directly breaks the
`from config.settings import DB` import because Python puts `db/` itself on `sys.path`
instead of the repo root.

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

## Running the pricing engine (Phase 5)

Requires `candidate_pairs` to already be populated (run the matcher above first) and
network access to Delta's testnet REST API for live ticker/fee-schedule calls.

```bash
# Smoke test first -- small, non-destructive (nothing written to `signals`)
python -m pricing.run_pricing --underlying BTC --limit 20 --dry-run

# Full run against all real candidates, writes EV/probability-of-profit/score to `signals`
python -m pricing.run_pricing --underlying BTC

# Useful filters
python -m pricing.run_pricing --underlying BTC --min-confidence 0.8 --classification same_exchange_calendar_spread --top 50
```

Watch the summary line (`N priced, M skipped (no data), K skipped (no fees), P of N show
positive EV`) — a high skip count usually means a testnet endpoint or fee-schedule call
isn't behaving as expected, not that the candidates themselves are bad. `pricing/ev_engine.py`
fails closed on any candidate missing an executable bid/ask (per Section A.1) rather than
substituting mark price, so legitimate data gaps show up as skips, not silently-wrong EVs.

## License note

This project may study (never blindly copy) public methodology from other open-source
projects. See `/docs/architecture.md` Section K for a license-by-license breakdown of what
was reused, from where, and under what terms (Apache-2.0, MIT, or "study only, no
license granted" as applicable).
