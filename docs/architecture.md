# Cross-Exchange Options Expiry/IV Arbitrage System
## Architecture, Research Findings & Roadmap — v2 (Fast-Track)

**Status:** Phase 2 (data collection) and Phase 3 (contract matching) complete, validated against real Delta testnet data. Live trading remains disabled by design throughout this document.

**v2 revision note:** After Phase 3 produced real results (374 live BTC contracts, 1,504 structurally valid candidate pairs found, 68,247 correctly rejected), the project owner asked to move as fast as possible to a real answer on whether this strategy has an edge, and to cut anything that isn't load-bearing. This revision compresses Phases 5-10 into leaner, faster versions. **What did not change:** backtesting, paper trading, and the risk/kill-switch layer are still in the plan — they're the only things standing between "we think this works" and finding out with real money that it doesn't, on a strategy whose "low risk" premise is exactly the thing this project exists to verify rather than assume. What changed is scope and depth, not whether these steps happen. See Section L for the compressed plan and the honest trade-offs it makes.

---

## 0. Executive summary (read this first)

Before any design work, I researched the exchanges you named against their own documentation, because the whole strategy lives or dies on settlement mechanics that are easy to get wrong from a video.

**Headline finding: the numeric example in the brief (Exchange A expires 1:30 PM, Exchange B expires 5:30 PM, same day) does not match Delta Exchange India's documented behavior.** Delta's own guide states plainly that *all* options contracts — daily, weekly, monthly, quarterly — settle at **5:30 PM IST**, computed from a 30-minute TWAP of the index price at that fixed clock time. There is no 1:30 PM settlement bucket on Delta. "D1," "D2" and weekly maturities differ by *which day* they settle, not by *what time of day*. So if the video's edge is real, it isn't "Delta's early option vs. Delta's late option" — it has to come from somewhere else entirely: a genuine intraday settlement-time difference on **another** exchange, a calendar-day (not intraday) version of the trade, or it's an artifact that disappears once you price off real bid/ask instead of a chart.

This doesn't kill the project. It means **Phase 1's real deliverable is figuring out which exchanges, if any, actually have differing intraday settlement clocks**, before we build anything around the specific 1:30/5:30 example. The architecture is built to be agnostic to this — the matching engine's whole job is to *discover* real expiry-time deltas rather than assume one, and Phase 3's real run confirmed same-exchange calendar-spread structure exists on Delta (433 exact-strike pairs found) — which is evidence there's *something* to analyze, not evidence of profit.

**Exchange readiness (researched, not assumed):**

| Exchange | Public options API | Settlement mechanics documented? | Verdict |
|---|---|---|---|
| **Delta Exchange India** | Yes — full public REST v2 (`api.india.delta.exchange`) + WebSocket (`socket.india.delta.exchange`), testnet, official Python/Node SDKs, CCXT support | Yes, precisely: European, cash-settled, 30-min TWAP of index at strike, fixed 5:30 PM IST settlement clock for every contract | **Primary exchange, in active use.** Phase 2/3 built and validated against this. |
| **CoinSwitch PRO** | Options API listed as "**available on request**," separate from the fully self-serve Spot/Futures/HFT surfaces | Marketing language only ("real-time settlement," "daily expiries," "24x7," USDT-settled, micro lot sizes) — no settlement-time or settlement-formula documentation found | **Still blocked.** Deferred under the fast-track plan (Section L) — not worth chasing docs for until Delta-only proves or disproves an edge. |
| **Shark Exchange** | No public options API documentation found. | Unknown | **Dropped from v1/v2 scope.** Revisit only if they publish real docs. |

---

## A. Complete system architecture

### A.1 Design philosophy

- **Data → Matching → Math → Scan → Backtest → Paper → Execute → Dashboard**, strictly in that order. No component downstream is trusted until the component upstream is validated against real data. (Section L compresses *how much* validation happens at each stage, not the order.)
- **Adapter-isolated.** Every exchange-specific quirk (symbol format, settlement time, fee schedule, contract multiplier) lives in one adapter file. Nothing else in the system is allowed to hardcode an exchange assumption.
- **Executable-price-only.** Anything that touches a P&L number uses top-of-book bid/ask (or deeper book if size requires it), never mark price, last price, or index price, for entry/exit decisioning. Mark/index price is used only for margin and risk calculations where the exchange itself uses it that way.
- **Fail-closed.** Any missing data, stale quote, or unverified assumption blocks the opportunity from being scored positively — it does not default to "assume it's fine."
- **SCAN_ONLY by default**, everywhere, always, until you explicitly flip a config flag per environment.

### A.2 Component diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                         EXCHANGE ADAPTERS                            │
│   DeltaAdapter (REST + WS, DONE) │ CoinSwitchAdapter (deferred)      │
│   implements: get_instruments, get_option_chain, get_orderbook,      │
│   get_ticker, get_positions, get_balance, place_order, cancel_order, │
│   modify_order, get_order_status, get_fees, get_contract_spec        │
└───────────────┬────────────────────────────────────────────────────-┘
                │  raw exchange payloads
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                 NORMALIZATION LAYER (DONE)                           │
│  raw JSON → OptionContract, MarketSnapshot                            │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      SQLITE STORE (DONE)                             │
│  instruments, market_data (tick-level), candidate_pairs, signals,     │
│  trades                                                                │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                 CONTRACT MATCHING ENGINE (DONE)                      │
│  1,504 candidate pairs found and persisted from real Delta data       │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│           PRICING / EV / MONTE CARLO ENGINE  ← BUILDING NEXT         │
│  lean version per Section L — real bid/ask, real fees, real EV        │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       OPPORTUNITY SCANNER                             │
│  continuous scan → executable net entry → classify → score            │
└───────────────┬────────────────────────────────────────────────────-┘
        ┌───────┴────────┐
        ▼                ▼
┌───────────────┐  ┌──────────────────────────────────────────────────┐
│  LEAN          │  │              DASHBOARD (deferred)                 │
│  BACKTESTER    │  │  built only after an edge is confirmed             │
└───────────────┘  └──────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│        SHORT PAPER TRADING WINDOW (mirrors live, simulated fills)     │
└──────────────────┬───────────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│   LEG EXECUTION ENGINE (built, disabled: LIVE=False)                  │
│   validate → lock → leg 1 → confirm → leg 2 → confirm → monitor       │
└──────────────────┬───────────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│   RISK ENGINE + KILL SWITCH (built alongside execution engine,        │
│   not deferred — see Section L)                                       │
└──────────────────────────────────────────────────────────────────────┘
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

See `exchange_adapters/base.py` (Protocol), `exchange_adapters/delta.py` (REST, done), `exchange_adapters/delta_ws.py` (WebSocket, done).

---

## B. Exchange API comparison (as documented, not assumed)

| Item | Delta Exchange India | CoinSwitch PRO |
|---|---|---|
| REST base URL | `https://api.india.delta.exchange` (v2) | Not publicly documented for options |
| WebSocket | `wss://socket.india.delta.exchange` | Unknown for options |
| Testnet | Yes — `cdn-ind.testnet.deltaex.org` | Unknown |
| Settlement time | **Fixed at 5:30 PM IST for every contract** | Unclear |
| Settlement price formula | `max(30-min TWAP index − strike, 0)` for calls, mirrored for puts | Not documented publicly |
| Fees | Maker/taker on notional; capped at 7.5–12.5% of premium; **zero settlement fee on OTM**; 18% GST (India accounts) | Not documented publicly |

CoinSwitch and Shark remain deferred per Section L — not pursued further until Delta-only data answers the core question.

---

## C. Contract specification comparison

The single biggest source of false-positive "arbitrage" if done sloppily, and the checklist `matching/engine.py` actually implements:

1. **Settlement clock** — pulled from live instrument metadata, never hardcoded.
2. **Settlement price basis** — TWAP vs. last-price vs. different index construction can manufacture a fake edge even at identical strike/expiry.
3. **Settlement currency / margin currency** — an unhedged FX/stablecoin-basis difference can look like edge and isn't.
4. **Contract multiplier & lot size** — size to notional-equivalent, never contract-count-equivalent.
5. **European vs. knockout/Turbo variant** — never paired even if strike/underlying/type match.
6. **Index construction** — each exchange computes its own index; a spread can be explained entirely by index-construction differences.
7. **Fee structure asymmetry** — GST + premium-based fee caps mean identical quoted spreads can have very different net economics.

All seven are enforced as required, checked fields in `matching/engine.py` — not display-name heuristics. Confirmed working against 374 real Delta contracts (Phase 3 run).

---

## D. Mathematical strategy definition

*(Unchanged from v1 — this is the math the lean EV engine in Section L implements, just without the full historical-IV-distribution modeling deferred to a later pass.)*

### D.1 Definitions

- `t0` = now; `T1` = earlier expiry (short leg); `T2` = later expiry (long leg), `T2 > T1`
- `K` = strike; `S_t` = underlying price at time `t`; `σ` = implied vol
- `B_short`, `A_long` = executable bid (short leg) and ask (long leg) *right now*

### D.2 Entry economics (must use executable quotes, never mark)

```
Gross entry credit = B_short − A_long
Net entry cost = Gross entry credit
                 − trading_fees(short) − trading_fees(long)
                 − expected_slippage(short, size) − expected_slippage(long, size)
```

### D.3 Expected value of the long leg at T1

```
V_long(T1) = OptionPricingModel(
    S = S_T1, K = K2, time_to_expiry = T2 − T1,
    sigma = sigma_effective_at_T1,   # lean version: today's IV +/- a simple shock band, not a full historical term-structure model
    r = risk_free_rate_or_funding_rate,
    model = BlackScholes
)
```

**Lean-plan simplification (see Section L.2):** the full v1 plan called for drawing `sigma_effective_at_T1` from a distribution fit to historical IV term-structure behavior. The lean version uses today's observed IV plus a small number of shock scenarios (e.g. -30%/0/+30%) instead of a fitted distribution — faster to build, less statistically rigorous, and explicitly labeled as such in every output so it's never mistaken for the fuller model.

### D.4 P&L per simulated path

```
Short_payoff(T1) = settlement_formula(S_T1, K1)
P&L_path = Net_entry_cost − Short_payoff(T1) + V_long(T1) − exit_fees(long) − exit_slippage(long)
```

### D.5 Classification logic — implemented in `matching/engine.py`, confirmed working

- Same strike, same underlying, same exchange → **Same-exchange calendar spread** (433 found in Phase 3 run)
- Different (in-tolerance) strikes → **Options relative-value arbitrage** (1,071 found)
- Same strike, cross-exchange, same calendar date → **Cross-exchange expiry arbitrage** (the video's actual premise — not yet tested, blocked on a second exchange)
- Same strike, cross-exchange, different calendar date → **Cross-exchange calendar spread**

---

## E. Data requirements

Implemented in `normalization/schemas.py`. Collection is WebSocket-driven (`collectors/realtime_collector.py`), sub-1-second flush to SQLite, REST fallback for instrument specs. Both done and in use.

---

## F. Database schema

SQLite, implemented in `db/schema.sql`. `instruments`, `market_data`, `candidate_pairs` (populated, 1,504 rows), `signals`, `trades` tables all exist.

---

## G. Backtesting methodology — LEAN VERSION (see Section L for full rationale)

### G.1 Non-negotiables (unchanged — these are not being cut)

1. Historical bid/ask, not last price, if the data exists.
2. No synthetic/manufactured historical data — gaps are reported, not filled in.
3. Fees and settlement rules match documented formulas, including OTM-zero-settlement-fee.
4. Legging failure is simulated for a meaningful fraction of trades, not ignored.

### G.2 Lean test matrix (compressed from v1's full matrix)

- **One window**, sized to whatever historical bid/ask Delta's API actually exposes — not the v1 plan's independent 30/90/180/365-day reports.
- **One underlying** (BTC), not segmented by moneyness/vol-regime buckets in the first pass.
- **One entry threshold**, not a 0.5%-5% sweep.
- If this lean pass shows no edge, the answer is "no edge, stop here" — no reason to build the fuller matrix. If it shows a positive signal, *then* the fuller v1 matrix (segmentation, threshold sweep, walk-forward) becomes worth the time, not before.

### G.3 What's explicitly deferred, not removed

In-sample/out-of-sample split, walk-forward re-estimation, and full stress-scenario suite (±5%/±10% BTC moves, IV shocks, liquidity collapse) — all still in the plan, just after a lean pass justifies the additional time. See Section L.

---

## H. Risk model — NOT COMPRESSED

| Risk | Where it's measured | Where it's mitigated |
|---|---|---|
| Market risk | Monte Carlo/shock-scenario P&L distribution (D.4) | Position sizing, `MAX_UNDERLYING_EXPOSURE` |
| IV risk | Shock-band repricing at T1 (lean D.3) | Score penalizes high IV-uncertainty candidates |
| Liquidity risk | Book depth vs. intended size at scan time | `MIN_LIQUIDITY` hard limit |
| Slippage risk | Empirical slippage tracked per trade | `MAX_SLIPPAGE` hard limit |
| Legging risk | Backtest legging-failure simulation | `MAX_LEGGING_TIME`; hedge/cancel-first-leg fallback |
| Exchange/API risk | Health-check heartbeats per adapter | Kill switch on repeated API errors |
| Settlement risk | Matching engine settlement check (Section C) — **live and enforced today** | Reject pairs with incompatible settlement mechanics |
| Contract-spec risk | `get_contract_specification()` diffed on every match | Match confidence downgraded on mismatch |
| Margin risk | Margin requirement pulled live | `MAX_MARGIN_PER_TRADE` |
| Liquidation risk | Position monitor tracks margin ratio | Auto-reduce/alert before exchange liquidation threshold |
| Transfer/withdrawal risk | Capital assumed pre-funded on both exchanges | Capital allocation planned, never dynamically moved mid-trade |

This table is unchanged from v1. Per the fast-track discussion: risk limits and the kill switch are cheap to build (hours, not phase-length) and are what stops a bad trade from becoming a large loss — they are built alongside the execution engine, not deferred to "later."

---

## I. Implementation roadmap — STATUS AS OF v2

**Phase 1 — Research.** ✅ Done. Exchange mechanics confirmed against real docs (Section 0/B).

**Phase 2 — Market-data collectors.** ✅ Done, per project owner confirmation. REST + WebSocket adapters, both collectors, SQLite persistence all built and in use.

**Phase 3 — Contract matcher.** ✅ Done. Real run against 374 live Delta contracts: 1,504 candidates found (433 exact same-exchange calendar spreads, 1,071 relative-value), 68,247 correctly rejected. Persisted to `candidate_pairs`.

**Phase 5 — Pricing/EV engine.** 🔨 Building next, lean version (Section D.3 simplification, Section L.2).
- Exit criteria (lean): EV, net-of-fees profit, and a probability-of-profit estimate computed for all 1,504 real candidates, using real bid/ask pulled live, not backfilled.

**Phase 6 — Lean backtester.** Next after Phase 5. Per Section G.2.
- Exit criteria (lean): one honest report, whatever window the data supports, explicitly labeled "lean pass" — a clear go/no-go signal on whether to invest in the fuller v1 backtest matrix.

**Phase 7 — Short paper trading window.** Sized to observed signal frequency from Phase 5/6 (Section L.3), not a fixed multi-week duration.

**Phase 8 — Execution engine + Phase 9 — Risk/kill switch.** Built together, not sequentially deferred (Section L.4). `LIVE_TRADING = FALSE` hardcoded regardless.

**Phase 10 — Live trading.** Enabled only after explicit, separate approval, and only if the lean backtest + paper trading both show a real, cost-inclusive, positive edge. If they don't, we stop here and say so — the fast-track plan changes how quickly we find that out, not whether we're honest about the answer.

**Explicitly deferred (not cancelled):** full v1 backtest matrix (multi-window, segmented, threshold-swept, walk-forward, full stress suite), dashboard, alerting, CoinSwitch/Shark adapters. These come back into scope only if the lean pass shows something worth the additional rigor.

---

## J. MVP — superseded by real results

The v1 MVP question ("does Delta's own calendar structure produce candidates at all?") is answered: yes, 433 exact-strike same-exchange calendar-spread candidates exist structurally. The next question — "do any of them have positive expected value after real costs?" — is what Phase 5 (lean) answers next.

---

## K. Open-source component reuse strategy

| Project | License | Verdict |
|---|---|---|
| **Hummingbot** | Apache 2.0 | Connector architecture pattern reused for `ExchangeAdapter` shape. |
| **put-call-arb** (`lubintan`) | None found (all rights reserved) | Studied for methodology only; not copied. |
| **Deribit MCP** | MIT | Not currently in use — Delta-only fast-track per Section L. Available if Deribit is reconsidered later. |
| **"CORP"** | Unknown | Still not identified — deprioritized under the fast-track plan; revisit only if it becomes relevant. |

---

## L. Fast-track plan (v2) — what changed and why

### L.1 The instruction and the constraint

The project owner asked to move as fast as possible to a real answer, and to cut anything that's "timepass" given the strategy is believed to be low-risk arbitrage. The honest response: **low-risk is the hypothesis this project exists to test, not a fact we've established yet** — Phase 3 only proved 1,504 pairs are structurally comparable, not that any are profitable. So the plan below compresses *time spent per step*, not the *number of steps*, because the steps that got questioned (backtesting, paper trading, risk/kill switch) are specifically the ones that catch a wrong hypothesis before it costs money.

### L.2 Lean EV/Monte Carlo engine (replaces full Phase 5)

- Real bid/ask pulled live for both legs of all 1,504 candidates, real fees per Section B, real net entry cost per D.2.
- IV-at-T1 modeled via a small shock band (e.g. -30%/0/+30% of today's IV) instead of a fitted historical distribution — faster, clearly labeled as a simplification, upgradeable later without changing the interface.
- Output: net EV, rough probability-of-profit, ranked — enough to answer "is there anything here" without the full statistical rigor of v1's plan.

### L.3 Lean backtest + short paper trading (replaces full Phase 6/7)

- Backtest: one pass, whatever historical window Delta's API exposes, one underlying, one threshold. Honestly reported, gaps disclosed, not silently padded.
- Paper trading: window length is *derived from Phase 5's findings*, not fixed in advance — if positive-EV signals are frequent, days are enough for a real sample; if rare, it takes longer, and that's reported rather than cut short to hit an arbitrary deadline.

### L.4 Risk engine + kill switch: not deferred

Built alongside the execution engine (Phase 8/9 merged in the roadmap above), because these are specifically the cheap-to-build, high-value-per-hour components — skipping them doesn't save meaningful time and removes the one thing that limits damage if the lean EV/backtest pass turns out to be wrong in a way a small sample didn't catch.

### L.5 What "later" actually means

Deferred items (full backtest matrix, dashboard, CoinSwitch/Shark, alerting) come back into scope specifically **if and when the lean pass shows a real edge worth the additional engineering time** — not on a calendar date, and not automatically. If the lean pass shows no edge, the honest outcome is stopping, reporting why, and not building any of the deferred items at all.

---

## Status: what's built vs. what's pending

**Built and validated against real data:**
- `exchange_adapters/base.py`, `delta.py`, `delta_ws.py` — REST + WebSocket, real testnet data flowing
- `normalization/schemas.py` — `OptionContract`, `MarketSnapshot`
- `collectors/market_data_collector.py`, `realtime_collector.py`, `run.py`, `run_realtime.py`, `gap_report.py`
- `db/schema.sql`, `init_db.py` — populated with real instruments, market data, and 1,504 candidate pairs
- `config/settings.py` — `LIVE_TRADING` hardcoded `False`
- `matching/schemas.py`, `engine.py`, `run_matcher.py` — real run complete: 1,504 candidates found, 68,247 rejected, persisted

**Not built yet:**
- Pricing/EV engine (Phase 5, lean version — building next)
- Lean backtester (Phase 6)
- Paper trading (Phase 7)
- Execution engine + risk/kill switch (Phase 8/9, built together)
- Dashboard/alerting (deferred per Section L.5)

**Outstanding items, deprioritized under the fast-track plan (not resolved, not urgent):**
1. CoinSwitch options API docs.
2. Shark Exchange (dropped from scope).
3. Clarification on "CORP".
