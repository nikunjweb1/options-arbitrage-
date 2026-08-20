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

**Phase 2: market-data collectors — adapters and integration tests built, real testnet
validation run still pending.** Delta Exchange India REST adapter, WebSocket client, both a
polling collector and a real-time collector, and integration test suites for both (REST and
WS) exist. What's still outstanding before Phase 2 can be marked done:

1. Actually running `RUN_INTEGRATION_TESTS=true pytest tests/test_delta_ws_integration.py -v -s`
   against real testnet, to confirm the WebSocket subscribe/message-format assumptions hold.
2. A **24h continuous run** using the realtime collector, verified gap-free via
   `collectors/gap_report.py`.

Nothing in this repo claims either of those has happened yet just because the code to do
them exists.

No CoinSwitch or Shark adapters exist yet — CoinSwitch's options API is "available on
request" (not self-serve docs) and Shark Exchange has no public options API documentation
found; both are blocked pending real documentation, not implemented against guesses.

## Repository layout

```
docs/
  architecture.md              # full architecture, research findings, MVP plan (source of truth)
exchange_adapters/
  base.py                      # ExchangeAdapter protocol — every adapter implements this
  delta.py                     # Delta Exchange India REST adapter (testnet-first, read-only in Phase 2)
  delta_ws.py                  # Delta WebSocket client — real-time ticker feed
normalization/
  schemas.py                   # OptionContract, MarketSnapshot, etc. — exchange-agnostic
collectors/
  market_data_collector.py      # REST-polling collector (instrument specs, fallback path)
  realtime_collector.py          # WebSocket-driven collector, sub-1s flush loop (primary path)
  run.py                          # CLI for the REST collector (python -m collectors.run)
  run_realtime.py                  # CLI for the realtime collector (python -m collectors.run_realtime)
  gap_report.py                     # verifies the Phase 2 "24h gap-free" exit criterion
db/
  schema.sql                        # SQLite schema for Phase 2 (instruments, market_data, ...)
  init_db.py                         # creates a local SQLite DB from schema.sql
config/
  settings.py                         # env-driven config, LIVE_TRADING hardcoded default
  .env.example                         # no real secrets, ever
tests/
  test_delta_adapter.py                 # REST normalization unit tests, fixture-based, no network
  test_delta_ws.py                       # WS parsing + flush-ceiling unit tests, no network
  test_delta_integration.py               # real testnet REST integration suite, skipped unless explicitly enabled
  test_delta_ws_integration.py             # real testnet WS integration suite (subscribe format, reconnect, e2e persistence), skipped unless explicitly enabled
  test_gap_report.py                       # gap-detection unit tests, no network
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
python db/init_db.py
```

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

## License note

This project may study (never blindly copy) public methodology from other open-source
projects. See `/docs/architecture.md` Section K for a license-by-license breakdown of what
was reused, from where, and under what terms (Apache-2.0, MIT, or "study only, no
license granted" as applicable).
