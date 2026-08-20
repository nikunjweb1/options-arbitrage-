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

## Build order (do not skip ahead)

```
Data → Matching → Mathematics → Scanner → Backtest → Paper Trading → Execution → Dashboard
```

Each phase has an explicit exit criterion in `/docs/architecture.md` (Section I). We do not
start Phase N+1 until Phase N's exit criterion is met.

## Current status

**Phase 2: market-data collectors.** Delta Exchange India adapter only. No trading logic
exists yet. No CoinSwitch or Shark adapters exist yet — CoinSwitch's options API is
"available on request" (not self-serve docs) and Shark Exchange has no public options API
documentation found; both are blocked pending real documentation, not implemented against
guesses.

## Repository layout

```
docs/
  architecture.md          # full architecture, research findings, MVP plan (source of truth)
exchange_adapters/
  base.py                  # ExchangeAdapter protocol — every adapter implements this
  delta.py                 # Delta Exchange India adapter (testnet-first)
normalization/
  schemas.py               # OptionContract, MarketSnapshot, etc. — exchange-agnostic
db/
  schema.sql                # SQLite schema for Phase 2 (instruments, market_data, ...)
  init_db.py                 # creates a local SQLite DB from schema.sql
config/
  settings.py                # env-driven config, LIVE_TRADING default enforced here
  .env.example                # no real secrets, ever
tests/
  test_delta_adapter.py       # adapter tests, run against Delta testnet
requirements.txt
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

## License note

This project may study (never blindly copy) public methodology from other open-source
projects. See `/docs/architecture.md` Section K for a license-by-license breakdown of what
was reused, from where, and under what terms (Apache-2.0, MIT, or "study only, no
license granted" as applicable).
