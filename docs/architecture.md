# Cross-Exchange Options Expiry/IV Arbitrage System
## Architecture, Research Findings & MVP Plan — v1 (Pre-Code)

**Status:** Research + architecture only. No code has been written. Live trading remains disabled by design throughout this document.

---

## 0. Executive summary (read this first)

Before any design work, I researched the exchanges you named against their own documentation, because the whole strategy lives or dies on settlement mechanics that are easy to get wrong from a video.

**Headline finding: the numeric example in the brief (Exchange A expires 1:30 PM, Exchange B expires 5:30 PM, same day) does not match Delta Exchange India's documented behavior.** Delta's own guide states plainly that *all* options contracts — daily, weekly, monthly, quarterly — settle at **5:30 PM IST**, computed from a 30-minute TWAP of the index price at that fixed clock time. There is no 1:30 PM settlement bucket on Delta. "D1," "D2" and weekly maturities differ by *which day* they settle, not by *what time of day*. So if the video's edge is real, it isn't "Delta's early option vs. Delta's late option" — it has to come from somewhere else entirely: a genuine intraday settlement-time difference on **another** exchange, a calendar-day (not intraday) version of the trade, or it's an artifact that disappears once you price off real bid/ask instead of a chart.

This doesn't kill the project. It means **Phase 1's real deliverable is figuring out which exchanges, if any, actually have differing intraday settlement clocks**, before we build anything around the specific 1:30/5:30 example. I've built the architecture to be agnostic to this — the matching engine's whole job is to *discover* real expiry-time deltas rather than assume one.

**Exchange readiness (researched, not assumed):**

| Exchange | Public options API | Settlement mechanics documented? | Verdict for Phase 1 |
|---|---|---|---|
| **Delta Exchange India** | Yes — full public REST v2 (`api.india.delta.exchange`) + WebSocket (`socket.india.delta.exchange`), testnet, official Python/Node SDKs, CCXT support | Yes, precisely: European, cash-settled, 30-min TWAP of index at strike, fixed 5:30 PM IST settlement clock for every contract | **Build against this first.** Everything else is secondary until this is proven or disproven. |
| **CoinSwitch PRO** | Options API listed as "**available on request**," separate from the fully self-serve Spot/Futures/HFT surfaces | Marketing language only ("real-time settlement," "daily expiries," "24x7," USDT-settled, micro lot sizes) — no settlement-time or settlement-formula documentation found | **Blocked.** Must request options API docs directly from CoinSwitch before writing an adapter. Do not assume "real-time settlement" means anything specific — it could mean continuous settlement or just a fast daily cycle. |
| **Shark Exchange** | No public options API documentation found. Primarily an INR-native perpetual futures platform; third-party comparison articles say options were "added," but no official endpoint spec surfaced | Unknown | **Not viable for Phase 1.** Revisit only if they publish real docs, or drop from initial scope. |

**Implication for the roadmap:** Phase 1 is not "confirm three exchanges work." It's "confirm Delta's mechanics precisely, and determine — by directly asking CoinSwitch for their options API spec — whether a real cross-exchange settlement-time gap exists at all." Everything downstream (matching, EV model, backtest) depends on this answer. I'd rather tell you that now than build a scanner around a gap that turns out not to exist.

None of this means abandon the project — a real cross-exchange calendar/expiry-structure difference is a legitimate and well-known class of trade (it's essentially a cross-venue calendar spread). It means we verify the mechanism before writing the strategy math around your specific example.

---

## A. Complete system architecture

### A.1 Design philosophy

- **Data → Matching → Math → Scan → Backtest → Paper → Execute → Dashboard**, strictly in that order. No component downstream is trusted until the component upstream is validated against real data.
- **Adapter-isolated.** Every exchange-specific quirk (symbol format, settlement time, fee schedule, contract multiplier) lives in one adapter file. Nothing else in the system is allowed to hardcode an exchange assumption.
- **Executable-price-only.** Anything that touches a P&L number uses top-of-book bid/ask (or deeper book if size requires it), never mark price, last price, or index price, for entry/exit decisioning. Mark/index price is used only for margin and risk calculations where the exchange itself uses it that way.
- **Fail-closed.** Any missing data, stale quote, or unverified assumption blocks the opportunity from being scored positively — it does not default to "assume it's fine."
- **SCAN_ONLY by default**, everywhere, always, until you explicitly flip a config flag per environment.

### A.2 Component diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                         EXCHANGE ADAPTERS                            │
│   DeltaAdapter   │   CoinSwitchAdapter (blocked)  │  [future adapters]│
│   implements: get_instruments, get_option_chain, get_orderbook,      │
│   get_ticker, get_positions, get_balance, place_order, cancel_order, │
│   modify_order, get_order_status, get_fees, get_contract_spec        │
└───────────────┬────────────────────────────────────────────────────-┘
                │  raw exchange payloads
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      NORMALIZATION LAYER                             │
│  raw JSON → OptionContract, OrderBookSnapshot, TickerSnapshot        │
│  (schemas in Section E). One normalizer per adapter; output schema   │
│  is exchange-agnostic.                                                │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      TIME-SERIES STORE (DB)                          │
│  instruments, market_data (tick-level), signals, trades              │
│  (schema in Section F)                                                │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      CONTRACT MATCHING ENGINE                        │
│  same underlying + option type + compatible strike/multiplier/        │
│  settlement currency/settlement method → candidate pair               │
│  + confidence score (exact strike vs. interpolated vs. rejected)      │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                 PRICING / EV / MONTE CARLO ENGINE                    │
│  Black-Scholes / Black-76, IV surface, remaining-value distribution,  │
│  probability of profit, VaR/ES                                        │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       OPPORTUNITY SCANNER                             │
│  continuous scan → executable net entry → classify → score 0-100      │
└───────────────┬────────────────────────────────────────────────────-┘
        ┌───────┴────────┐
        ▼                ▼
┌───────────────┐  ┌──────────────────────────────────────────────────┐
│  BACKTESTER    │  │              ALERTING / DASHBOARD                 │
│  (offline)     │  │  Telegram / Discord / Email / web UI              │
└───────────────┘  └──────────────────┬───────────────────────────────┘
                                       ▼
                    ┌──────────────────────────────────────────────────┐
                    │        PAPER TRADING ENGINE (mirrors live)        │
                    └──────────────────┬───────────────────────────────┘
                                       ▼
                    ┌──────────────────────────────────────────────────┐
                    │   LEG EXECUTION ENGINE (disabled: LIVE=False)     │
                    │   validate → lock → leg 1 → confirm → leg 2 →     │
                    │   confirm → monitor → exit                        │
                    └──────────────────┬───────────────────────────────┘
                                       ▼
                    ┌──────────────────────────────────────────────────┐
                    │   RISK ENGINE + KILL SWITCH (cross-cutting)       │
                    │   hard limits checked before every state          │
                    │   transition in scanner, paper, and live paths    │
                    └──────────────────────────────────────────────────┘
```

### A.3 Exchange adapter interface (contract, not implementation)

```python
class ExchangeAdapter(Protocol):
    def get_instruments(self) -> list[OptionContract]: ...
    def get_option_chain(self, underlying: str, expiry: datetime | None = None) -> list[OptionContract]: ...
    def get_orderbook(self, instrument_id: str, depth: int = 5) -> OrderBookSnapshot: ...
    def get_ticker(self, instrument_id: str) -> TickerSnapshot: ...
    def get_positions(self) -> list[Position]: ...
    def get_balance(self) -> Balance: ...
    def place_order(self, order: OrderRequest) -> OrderResult: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def modify_order(self, order_id: str, changes: dict) -> OrderResult: ...
    def get_order_status(self, order_id: str) -> OrderStatus: ...
    def get_fees(self) -> FeeSchedule: ...
    def get_contract_specification(self, instrument_id: str) -> ContractSpec: ...
```

Every adapter must be validated against **testnet first** (Delta provides one: `cdn-ind.testnet.deltaex.org`) before touching production keys. See `exchange_adapters/base.py` for the real Python Protocol and `exchange_adapters/delta.py` for the Delta implementation.

---

## B. Exchange API comparison (as documented, not assumed)

| Item | Delta Exchange India | CoinSwitch PRO | Shark Exchange |
|---|---|---|---|
| REST base URL | `https://api.india.delta.exchange` (v2; v1 deprecated) | Not publicly documented for options | Not publicly documented |
| WebSocket | `wss://socket.india.delta.exchange` | Unknown for options | Unknown |
| Testnet | Yes — `cdn-ind.testnet.deltaex.org` | Unknown | Mentioned generically, unverified for options |
| Official SDKs | Python, Node.js, CCXT integration | Multi-language client for Spot/Futures/HFT; options "available on request" | Unknown |
| Auth | API key + HMAC signature + timestamp headers | Single signature scheme across their 4 API surfaces (per their docs) | Unknown |
| Options instrument type | European, cash-settled, BTC & ETH, D1/D2/weekly/monthly maturities | USDT-settled, described as "daily expiries," 24x7 | Recently added per third-party reviews; no spec found |
| Settlement time | **Fixed at 5:30 PM IST for every contract** | Unclear — "real-time settlement" is undefined | Unknown |
| Settlement price formula | `max(30-min TWAP index − strike, 0)` for calls, mirrored for puts | Not documented publicly | Unknown |
| Rate limits | 500 operations/sec/product (documented); higher limits for registered market makers | Unknown | Unknown |
| Fees | Maker/taker on notional; options fee capped at 7.5–12.5% of premium depending on region (India vs. Global pages differ slightly — needs reconciling against your actual account fee schedule); **zero settlement fee on contracts expiring OTM**; 18% GST added on India accounts | Marketed as "lowest fees," specifics not found | Fee cap of 5% of premium claimed by a third-party comparison, not an official source |
| Margin | Documented isolated/cross/portfolio margin formulas, TWAP/mark-based IM calculations | Not documented publicly | Not documented publicly |

**Action required from you before Phase 1 can close:** request CoinSwitch's options API documentation directly (their site explicitly routes this as "available on request" rather than self-serve) and get Shark Exchange's official API docs if you want to keep them in scope. Do not fabricate specs for either.

---

## C. Contract specification comparison

This is flagged as **the single biggest source of false-positive "arbitrage"** if done sloppily.

1. **Settlement clock.** Delta = fixed 5:30 PM IST, always. Any exchange pair must have its settlement times pulled from live instrument metadata, never hardcoded from an example.
2. **Settlement price basis.** Delta uses a 30-minute TWAP of its own index price. If CoinSwitch uses last traded price, its own index, or a different TWAP window, two "same strike" options can have materially different payoff distributions even at an identical strike and identical nominal expiry date — this alone can manufacture a fake edge.
3. **Settlement currency / margin currency.** Confirm whether both legs settle in USDT, or one in USDT and the other with an INR overlay — an unhedged FX/stablecoin-basis difference can look like an arbitrage margin and isn't.
4. **Contract multiplier & lot size.** Never assume "1 contract = 1 contract" across exchanges. Pull from `get_contract_specification()` every time, and if multipliers differ, size the pair to notional-equivalent, not contract-count-equivalent.
5. **European vs. any early-exercise or knockout variant.** Delta explicitly offers exotic "Turbo" options with knockout barriers alongside vanilla European options — a matching engine that isn't checking `option_variant` could pair a vanilla contract against a barrier contract that merely has a similar-looking symbol.
6. **Index construction.** "BTC index price" is not one universal number — each exchange computes its own index from its own basket of spot venues. A spread between two "identical" options can be entirely explained by a spread between the two exchanges' index calculations.
7. **Fee structure asymmetry.** GST (18%, India-specific) plus a premium-based fee cap on Delta vs. an unknown CoinSwitch fee schedule means identical quoted spreads can have very different net economics per leg.

The contract matching engine must treat every one of these seven items as a required, checked field — not a display-name heuristic.

---

## D. Mathematical strategy definition

### D.1 What we're actually trying to measure

Define:
- `t0` = now
- `T1` = earlier expiry (short leg), `T2` = later expiry (long leg), `T2 > T1`
- `K` = strike (or `K1`, `K2` if strikes differ and interpolation is used)
- `S_t` = underlying price at time `t`
- `σ1, σ2` = implied vol relevant to each leg's pricing model
- `B_short`, `A_long` = executable bid (short leg) and ask (long leg) *right now*

### D.2 Entry economics (must use executable quotes, never mark)

```
Gross entry credit = B_short − A_long
Net entry cost = Gross entry credit
                 − trading_fees(short) − trading_fees(long)
                 − expected_slippage(short, size) − expected_slippage(long, size)
                 − funding/carry costs where applicable
                 − transfer costs if capital must move between venues
```

### D.3 Expected value of the long leg at T1 (the crux of the whole strategy)

At `t = T1`, the short leg settles. The long leg does **not** expire; it has `(T2 − T1)` of remaining life. Its value is a *distribution*, not a point estimate:

```
V_long(T1) = OptionPricingModel(
    S = S_T1,                     # simulated/observed underlying at T1
    K = K2,
    time_to_expiry = T2 − T1,
    sigma = sigma_effective_at_T1,   # NOT today's sigma — vol can and does change
    r = risk_free_rate_or_funding_rate,
    model = BlackScholes | Black76   # per contract_spec.settlement_method
)
```

Two failure modes to explicitly guard against:
1. **Using today's IV to price the option at T1.** IV is not static. The model must draw `sigma_effective_at_T1` from a distribution informed by historical IV term-structure behavior, not hold it constant.
2. **Assuming linear time decay.** The EV must come from actually repricing the option under the Monte Carlo path, not from a rule of thumb.

### D.4 P&L per simulated path

```
Short_payoff(T1) = settlement_formula(S_T1, K1)      # per exchange's own documented formula
P&L_path = Net_entry_cost
           − Short_payoff(T1)                         # cost to close/settle the short
           + V_long(T1)                                # mark-to-model value of long leg at T1
           − exit_fees(long) − exit_slippage(long)
```

### D.5 Classification logic

- **Same strike, same underlying, differing settlement times, no synthetic/parity relationship involved** → *Cross-exchange expiry arbitrage* or, if the time gap is actually a calendar-day difference rather than intraday, **Cross-exchange calendar spread**.
- **Strikes differ and a delta/gamma-neutral relationship is being modeled between them** → *Options relative-value arbitrage*.
- **Position constructed from call+put+underlying replicating a synthetic forward, compared cross-exchange** → *Synthetic arbitrage* or *Put-call parity arbitrage*.
- **Four-leg box (two calls, two puts, two strikes) compared cross-exchange** → *Box arbitrage*, flagged for stricter tolerance since a "true" box should have near-zero risk *if and only if* settlement mechanics genuinely match.
- **Spread persists beyond what fees/slippage explain and no structural difference accounts for it** → *IV mispricing* / *Cross-exchange option mispricing*.

The scanner never defaults to "IV arbitrage" as a label; it must be earned by ruling out the structural explanations first.

---

## E. Data requirements

See `normalization/schemas.py` for the real dataclasses (`OptionContract`, `MarketSnapshot`).

### E.3 Collection cadence

- Order book top-of-book + Greeks: WebSocket push where available (Delta supports this); fallback to REST polling at a rate under the documented 500 ops/sec/product ceiling.
- Full instrument list refresh: on a timer (new strikes/maturities get added intraday per Delta's own "strike discovery every 5 minutes" rule) — poll `get_instruments()` at least every 5 minutes.
- Historical tick storage: append-only, never overwritten, timestamped at ingestion time in addition to exchange-reported time (to detect clock skew).

---

## F. Database schema

Development phase: **SQLite** for OLTP-style state (instruments, trades, positions) + **DuckDB** for analytical/backtest workloads. Migrate to TimescaleDB/ClickHouse only when tick volume or concurrent-writer load actually requires it. See `db/schema.sql` for the real schema.

---

## G. Backtesting methodology

### G.1 Non-negotiables

1. **Historical bid/ask, not last price, if the data exists.** Short leg fills against historical bid, long leg against historical ask.
2. **No synthetic/manufactured historical data.** If order-book history isn't available for a period, that period is excluded from the backtest and the exclusion is reported.
3. **Fees and settlement rules must match the documented formulas per exchange per period** — including the "OTM = zero settlement fee" rule on Delta.
4. **Simulate legging failure.** A meaningful fraction of backtested trades must assume the second leg fails to fill at the assumed price and measure the resulting P&L.

### G.2 Test matrix

- Windows: 30 / 90 / 180 / 365 days (independently reported).
- Segmentation: by underlying (BTC, ETH), by expiry-gap bucket, by realized-vol regime, by strike moneyness bucket.
- Position sizing variants: fixed-size, and size scaled to available liquidity.
- Entry threshold sweep: 0.5% to 5% of notional, to check overfitting sensitivity.

### G.3 Validation discipline

- **In-sample** on the earliest window only.
- **Out-of-sample** on later, untouched windows.
- **Walk-forward**: re-estimate parameters on a rolling basis.
- **Stress scenarios**: ±5%/±10% BTC moves at T1, IV −50%/+100% shocks, liquidity collapse, one-leg-fails-completely, API downtime during legging.

---

## H. Risk model

| Risk | Where it's measured | Where it's mitigated |
|---|---|---|
| Market risk | Monte Carlo P&L distribution (D.4) | Position sizing, `MAX_UNDERLYING_EXPOSURE` |
| IV risk | `sigma_effective_at_T1` drawn from a distribution | Score penalizes high IV-uncertainty candidates |
| Liquidity risk | Book depth vs. intended size at scan time | `MIN_LIQUIDITY` hard limit |
| Slippage risk | Empirical slippage tracked per trade | `MAX_SLIPPAGE` hard limit |
| Legging risk | Backtest legging-failure simulation | `MAX_LEGGING_TIME`; hedge/cancel-first-leg fallback |
| Exchange/API risk | Health-check heartbeats per adapter | Kill switch on repeated API errors |
| Settlement risk | Matching engine settlement check (Section C) | Reject pairs with incompatible settlement mechanics |
| Contract-spec risk | `get_contract_specification()` diffed on every match | Match confidence downgraded on mismatch |
| Margin risk | Margin requirement pulled live | `MAX_MARGIN_PER_TRADE` |
| Liquidation risk | Position monitor tracks margin ratio | Auto-reduce/alert before exchange liquidation threshold |
| Transfer/withdrawal risk | Capital assumed pre-funded on both exchanges | Capital allocation planned, never dynamically moved mid-trade |

---

## I. Implementation roadmap

**Phase 1 — Research (current phase).**
- [ ] Obtain CoinSwitch options API documentation directly from CoinSwitch.
- [ ] Decide whether to pursue Shark Exchange or drop it for v1.
- [ ] Confirm empirically whether any documented mechanism creates an intraday settlement gap anywhere in Delta's product suite.
- Exit criteria: written, sourced answers to the 18 research questions, for at least two exchanges.

**Phase 2 — Market-data collectors (current build target).** Delta adapter first. No trading logic. Output: contracts + market data landing in the DB.
- Exit criteria: 24h of continuous, gap-free Delta options + underlying data captured and queryable.

**Phase 3 — Contract matcher.** Self-matching Delta's own D1 vs D2 vs weekly chains first.
- Exit criteria: matcher correctly rejects deliberately-mismatched fixtures and accepts genuine matches, in a test suite.

**Phase 4 — Scanner (no orders).** Real-time candidate detection + executable entry-cost calculation.
- Exit criteria: a week of live-logged signals with no false "opportunity" traceable to a structural artifact.

**Phase 5 — Pricing/EV/Monte Carlo engine.**
- Exit criteria: EV estimates directionally consistent with what actually happened to matched pairs historically.

**Phase 6 — Backtester.**
- Exit criteria: a full backtest report for whatever real candidate pairs Phases 3-5 surfaced.

**Phase 7 — Paper trading.** Full system, simulated fills only.

**Phase 8 — Execution engine.** `LIVE_TRADING = FALSE` hardcoded default.

**Phase 9 — Risk management + kill switch.** Enforced pre-trade checks, not advisory logging.

**Phase 10 — Live trading**, enabled only after explicit, separate approval, and only after a positive, out-of-sample, stress-tested, cost-inclusive edge is demonstrated.

---

## J. MVP: the smallest thing that tells us if there's an edge

1. Delta adapter, collecting full options chain + underlying index continuously.
2. Self-matching engine applied *within Delta first* — D1 vs D2 vs weekly maturities against each other.
3. Executable-spread calculator (D.2) on that self-matched data.
4. Monte Carlo EV model (D.3-D.4) using Black-Scholes off Delta's own reported IV.
5. Backtest (Section G) over whatever historical window Delta's API exposes.

This answers one question cheaply: does Delta's own documented calendar structure produce a real, cost-inclusive, executable edge on its own — independent of whether CoinSwitch ever gives us API access?

---

## K. Open-source component reuse strategy

| Project | License | Verdict |
|---|---|---|
| **Hummingbot** (`hummingbot/hummingbot`) | Apache 2.0 | Reuse the connector architecture pattern (`ExchangeAdapter` alignment). No native options connector category found — options logic is still ours to build. |
| **put-call-arb** (`lubintan/put-call-arb`) | None found (all rights reserved by default) | Study the published methodology only; do not copy `pca.py` without asking the author. |
| **Deribit MCP** (`deribit_mcp` + `deribit-base`/`deribit-http`/`deribit-websocket`/`deribit-api`) | MIT | Reuse directly if Deribit is added as an exchange. Its explicit-flag-plus-credentials trading gate is the model for our `LIVE_TRADING` switch. |
| **"CORP"** | Unknown | Not identified — blocked pending a link/name from the project owner. |

**Open decision (K.2):** whether to validate the pipeline against Deribit first (fully documented, has reference prior art, unblocked today) before or instead of Delta-first as originally scoped (better fit for INR accounts, partially blocked on CoinSwitch's docs). Currently building Delta-first per the original scope; revisit if CoinSwitch access stalls.

---

## Status: what's built vs. what's pending

**Built (this commit):**
- `exchange_adapters/base.py` — `ExchangeAdapter` Protocol
- `exchange_adapters/delta.py` — real Delta India adapter (testnet-first, no trading logic)
- `normalization/schemas.py` — `OptionContract`, `MarketSnapshot` dataclasses
- `db/schema.sql`, `db/init_db.py` — SQLite schema for Phase 2
- `config/settings.py` — env-driven config, `LIVE_TRADING` hardcoded to `False`

**Not built yet (do not assume otherwise):**
- Matching engine (Phase 3)
- Pricing/EV/Monte Carlo engine (Phase 5)
- Backtester (Phase 6)
- Paper trading (Phase 7)
- Execution engine (Phase 8) — and it will ship with `LIVE_TRADING = FALSE` regardless
- Dashboard/alerting

**Outstanding items blocking Phase 1 closure:**
1. CoinSwitch options API docs (request directly from CoinSwitch).
2. Decision on Shark Exchange (drop vs. obtain their docs).
3. Confirmation of underlying(s) and Delta account entity (India vs. Global).
4. Decision on K.2 (Deribit-first vs. Delta-first).
5. Clarification on "CORP".
