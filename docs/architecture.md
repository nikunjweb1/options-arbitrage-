# Cross-Exchange Options Expiry/IV Arbitrage System
## Architecture, Research Findings & Roadmap — v2 (Fast-Track)

**Status:** Phase 2 (data collection), Phase 3 (contract matching), and Phase 5
(pricing/EV engine) are all complete and validated against real, live Delta testnet data.
Two real bugs were found and fixed during Phase 5's live-run validation (see Status section
for the full writeup). Live trading remains disabled by design throughout this document.

**v2 revision note:** After Phase 3 produced real results (374 live BTC contracts, 1,504
structurally valid candidate pairs found, 68,247 correctly rejected), the project owner
asked to move as fast as possible to a real answer on whether this strategy has an edge, and
to cut anything that isn't load-bearing. This revision compresses Phases 5-10 into leaner,
faster versions. **What did not change:** backtesting, paper trading, and the risk/kill-switch
layer are still in the plan. See Section L for the compressed plan.

---

## 0. Executive summary (read this first)

**Headline finding: the numeric example in the brief (Exchange A expires 1:30 PM, Exchange B
expires 5:30 PM, same day) does not match Delta Exchange India's documented behavior.** Delta
settles *all* options contracts — daily, weekly, monthly, quarterly — at **5:30 PM IST**,
computed from a 30-minute TWAP of the index price. "D1"/"D2"/weekly maturities differ by
*which day* they settle, not *what time of day*.

**Exchange readiness (researched, not assumed):**

| Exchange | Public options API | Settlement mechanics documented? | Verdict |
|---|---|---|---|
| **Delta Exchange India** | Yes — full public REST v2 + WebSocket, testnet, official SDKs | Yes: European, cash-settled, 30-min TWAP, fixed 5:30 PM IST | **Primary exchange, in active use. Phase 2/3/5 all built and validated.** |
| **CoinSwitch PRO** | Options API "available on request" | Marketing language only, no settlement docs found | Still blocked, deferred (Section L). |
| **Shark Exchange** | No public options API documentation found | Unknown | Dropped from scope. |

---

## A. Complete system architecture

### A.1 Design philosophy

- **Data → Matching → Math → Scan → Backtest → Paper → Execute → Dashboard**, strictly in order.
- **Adapter-isolated.**
- **Executable-price-only.** Top-of-book bid/ask only for anything touching P&L, never mark/last/index price.
- **Fail-closed.**
- **SCAN_ONLY by default**, everywhere, always.

### A.2 Component diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                         EXCHANGE ADAPTERS                            │
│   DeltaAdapter (REST + WS, DONE, retries transient errors)           │
│   CoinSwitchAdapter (deferred)                                        │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                 NORMALIZATION LAYER (DONE)                           │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      SQLITE STORE (DONE)                             │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                 CONTRACT MATCHING ENGINE (DONE)                      │
│  1,500+ candidate pairs found and persisted from real Delta data      │
│  (count grows as new daily D1 contracts get listed)                   │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  PRICING / EV / MONTE CARLO ENGINE -- DONE, exit criterion met        │
│  lean version per Section L — real bid/ask, real fees, real EV        │
│  292/404 priced candidates show positive EV in the latest live run    │
└───────────────┬────────────────────────────────────────────────────-┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       OPPORTUNITY SCANNER                             │
│  folded into pricing/run_pricing.py's ranking pass for the lean plan  │
└───────────────┬────────────────────────────────────────────────────-┘
        ┌───────┴────────┐
        ▼                ▼
┌───────────────┐  ┌──────────────────────────────────────────────────┐
│  LEAN          │  │              DASHBOARD (deferred)                 │
│  BACKTESTER    │  │  built only after an edge is confirmed             │
│  (NEXT PHASE)  │  │                                                     │
└───────────────┘  └──────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│        SHORT PAPER TRADING WINDOW (mirrors live, simulated fills)     │
└──────────────────┬───────────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│   LEG EXECUTION ENGINE (built, disabled: LIVE=False)                  │
└──────────────────┬───────────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│   RISK ENGINE + KILL SWITCH (built alongside execution engine)        │
└──────────────────────────────────────────────────────────────────────┘
```

### A.3 Exchange adapter interface

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

See `exchange_adapters/base.py` (Protocol), `exchange_adapters/delta.py` (REST, done,
retries transient network errors with backoff), `exchange_adapters/delta_ws.py` (WebSocket, done).

---

## B. Exchange API comparison

| Item | Delta Exchange India | CoinSwitch PRO |
|---|---|---|
| REST base URL | `https://api.india.delta.exchange` (prod) / `cdn-ind.testnet.deltaex.org` (testnet) | Not publicly documented for options |
| WebSocket | `wss://socket.india.delta.exchange` | Unknown for options |
| Settlement time | Fixed at 5:30 PM IST for every contract | Unclear |
| Settlement price formula | `max(30-min TWAP index − strike, 0)` for calls, mirrored for puts | Not documented publicly |
| Fees | Maker/taker on notional; capped at 7.5–12.5% of premium; zero settlement fee on OTM; 18% GST (India accounts) | Not documented publicly |

---

## C. Contract specification comparison

All seven checks (settlement clock, settlement price basis, settlement currency, contract
multiplier/lot size, European vs. Turbo variant, index construction, fee structure asymmetry)
are enforced as required, checked fields in `matching/engine.py`. Confirmed working against
374 real Delta contracts (Phase 3 run).

---

## D. Mathematical strategy definition

### D.1 Definitions

- `t0` = now; `T1` = earlier expiry (short leg); `T2` = later expiry (long leg), `T2 > T1`
- `K` = strike; `S_t` = underlying price at time `t`; `σ` = implied vol
- `B_short`, `A_long` = executable bid (short leg) and ask (long leg) *right now*

### D.2 Entry economics

```
Gross entry credit = B_short − A_long
Net entry cost = Gross entry credit
                 − trading_fees(short) − trading_fees(long)
                 − expected_slippage(short, size) − expected_slippage(long, size)
```

**Implementation status: partial.** `pricing/ev_engine.py` computes `Gross entry credit`
and subtracts `trading_fees` correctly (and, critically, scales `B_short`/`A_long` by each
leg's `contract_multiplier` — see Bug #2 in the Status section). It does **not** yet subtract
`expected_slippage` on either leg — this is disclosed in every `EVResult.model_notes`, not
silently assumed to be zero, but it means every EV number in the current live-run results is
optimistic relative to what real execution would achieve, especially on the sub-$2 premiums
typical of this candidate pool. Worth modeling explicitly before Phase 7 (paper trading).

### D.3 Expected value of the long leg at T1

```
V_long(T1) = OptionPricingModel(
    S = S_T1, K = K2, time_to_expiry = T2 − T1,
    sigma = sigma_effective_at_T1,   # lean version: today's IV +/- a shock band
    r = risk_free_rate_or_funding_rate,
    model = BlackScholes
)
```

**Lean-plan simplification:** today's observed IV plus a ±30% shock band (3 points), crossed
with a 21-point underlying-price grid (±3σ) — 63 scenarios per candidate. The price grid was
widened from an initial 5 points to 21 during live-run debugging (see Status section, Bug #2)
after a too-narrow grid was initially suspected as the cause of an exact 0%/100% P(profit)
split; widening the grid alone didn't fix it (the real cause was Bug #2's unit mismatch), but
21 points is a more defensible resolution regardless and was kept.

### D.4 P&L per simulated path

```
Short_payoff(T1) = settlement_formula(S_T1, K1)
P&L_path = Net_entry_cost − Short_payoff(T1) + V_long(T1) − exit_fees(long) − exit_slippage(long)
```

Every dollar term (`Net_entry_cost`, `Short_payoff`, `V_long`) is scaled by the relevant leg's
`contract_multiplier` — this consistency was the subject of both Bug #1 and Bug #2 (Status
section). `exit_slippage` is not yet modeled, same gap as D.2.

### D.5 Classification logic

- Same strike, same underlying, same exchange → **Same-exchange calendar spread**
- Different (in-tolerance) strikes → **Options relative-value arbitrage**
- Same strike, cross-exchange, same calendar date → **Cross-exchange expiry arbitrage** (blocked on a second exchange)
- Same strike, cross-exchange, different calendar date → **Cross-exchange calendar spread**

---

## E. Data requirements

Implemented in `normalization/schemas.py`. Done and in use.

---

## F. Database schema

SQLite, `db/schema.sql`. `instruments`, `market_data`, `candidate_pairs`, `signals`
(populated with real Phase 5 results), `trades` all exist.

---

## G. Backtesting methodology — LEAN VERSION

### G.1 Non-negotiables (unchanged)

1. Historical bid/ask, not last price.
2. No synthetic/manufactured historical data.
3. Fees and settlement rules match documented formulas, including OTM-zero-settlement-fee.
4. Legging failure simulated for a meaningful fraction of trades.

### G.2 Lean test matrix

- One window, one underlying (BTC), one entry threshold, first pass.
- If this shows no edge: "no edge, stop here." If positive: the fuller v1 matrix becomes worth building.

### G.3 What's explicitly deferred

In-sample/out-of-sample split, walk-forward re-estimation, full stress-scenario suite — still
in the plan, after a lean pass justifies the time.

---

## H. Risk model — NOT COMPRESSED

(Unchanged from v1 — see prior revisions. Built alongside the execution engine, not deferred.)

---

## I. Implementation roadmap — STATUS AS OF v2

**Phase 1 — Research.** ✅ Done.

**Phase 2 — Market-data collectors.** ✅ Done.

**Phase 3 — Contract matcher.** ✅ Done. Candidate count grows over time as new D1 contracts
list (1,504 at the original Phase 3 run; re-run `matching.run_matcher` periodically).

**Phase 5 — Pricing/EV engine.** ✅ **Done, exit criterion met.**
- `pricing/ev_engine.py`'s `LeanEVEngine` (21×3=63-scenario grid), `pricing/black_scholes.py`,
  and `pricing/run_pricing.py` (candidate_pairs → live ticker/fees → EV → `signals`) are all
  built, tested (19 tests in `tests/test_ev_engine.py`), and have been run live against real
  Delta testnet data multiple times during debugging.
- **Two real bugs found and fixed via live diagnostic evidence** (not guessed): (1)
  `contract_multiplier` omitted from `short_payoff`/`v_long`; (2) `contract_multiplier` also
  omitted from the premium side (`short_bid`/`long_ask`) — confirmed via `pricing/diagnose_pair.py`
  against a real quote, and the actual explanation for the first live run's wildly implausible
  EV numbers (~1000x too large) and the exact-0%/100% `P(profit)` split.
- **Final confirmed-good live run (2026-08-23):** 674 non-expired candidates loaded, 404
  priced (270 skipped for no executable live data), 292/404 (72%) positive EV, `P(profit)`
  distributed across a real range for all 404 results (zero landed at exactly 0.0 or 1.0).
  EV magnitudes now the same order as net entry cost.
- **Known gap to carry into Phase 6/7:** `expected_slippage` (Section D.2) is not yet modeled;
  on the sub-$2 premiums typical here, this could matter a lot. 40% of live-data fetch attempts
  found no executable bid/ask — real testnet illiquidity to keep in mind for position sizing.
- Exit criteria (lean): EV, net-of-fees profit, and a probability-of-profit estimate computed
  for real candidates using real bid/ask pulled live, not backfilled. **Met.**

**Phase 6 — Lean backtester.** Next up. Per Section G.2.
- Exit criteria (lean): one honest report, whatever window the data supports, explicitly
  labeled "lean pass" — a clear go/no-go signal on the fuller v1 backtest matrix. Should
  explicitly account for the slippage gap flagged above rather than inheriting Phase 5's
  optimistic-by-construction numbers unchanged.

**Phase 7 — Short paper trading window.** Sized to observed signal frequency from Phase 5/6.

**Phase 8 — Execution engine + Phase 9 — Risk/kill switch.** Built together.
`LIVE_TRADING = FALSE` hardcoded regardless.

**Phase 10 — Live trading.** Enabled only after explicit, separate approval, and only if the
lean backtest + paper trading both show a real, cost-inclusive, positive edge.

**Explicitly deferred (not cancelled):** full v1 backtest matrix, dashboard, alerting,
CoinSwitch/Shark adapters.

---

## J. MVP — superseded by real results

Answered: yes, same-exchange calendar-spread candidates exist structurally (hundreds found),
and a meaningful fraction (72% of what could be priced in the latest run) show positive lean-EV
after real fees, before slippage. Whether that survives slippage modeling and a real backtest is
Phase 6's question now.

---

## K. Open-source component reuse strategy

| Project | License | Verdict |
|---|---|---|
| **Hummingbot** | Apache 2.0 | Connector architecture pattern reused for `ExchangeAdapter` shape. |
| **put-call-arb** (`lubintan`) | None found (all rights reserved) | Studied for methodology only; not copied. |
| **Deribit MCP** | MIT | Not currently in use. |
| **"CORP"** | Unknown | Still not identified — deprioritized. |

---

## L. Fast-track plan (v2)

### L.1 The instruction and the constraint

Low-risk is the hypothesis this project exists to test, not a fact established yet. Phase 5
now shows positive-EV candidates exist before slippage costs — a reason to proceed carefully
to Phase 6, not a reason to declare victory.

### L.2 Lean EV/Monte Carlo engine — DONE

Real bid/ask pulled live for both legs, real fees, real net entry cost (minus the
not-yet-modeled slippage term). `pricing/run_pricing.py` + `pricing/ev_engine.py`. Output:
net EV, probability-of-profit, ranked. Confirmed working with sane numbers as of 2026-08-23.

### L.3 Lean backtest + short paper trading (replaces full Phase 6/7) — NEXT

- Backtest: one pass, whatever historical window Delta's API exposes, one underlying, one
  threshold. Should incorporate a slippage estimate given Phase 5's disclosed gap.
- Paper trading: window length derived from Phase 5/6's findings.

### L.4 Risk engine + kill switch: not deferred

Built alongside the execution engine (Phase 8/9).

### L.5 What "later" actually means

Deferred items come back into scope if and when a lean pass shows a real edge worth the
additional engineering time.

---

## Status: what's built vs. what's pending

**Built and validated against real live data:**
- `exchange_adapters/base.py`, `delta.py` (retries transient network errors with backoff),
  `delta_ws.py`
- `normalization/schemas.py`
- `collectors/market_data_collector.py`, `realtime_collector.py`, `run.py`, `run_realtime.py`, `gap_report.py`
- `db/schema.sql`, `init_db.py` — populated with real instruments, market data, candidate pairs, and signals
- `config/settings.py` — `LIVE_TRADING` hardcoded `False`
- `matching/schemas.py`, `engine.py`, `run_matcher.py`
- `pricing/black_scholes.py`, `ev_engine.py`, `run_pricing.py`, `diagnose_pair.py` — **live-run
  validated, two real bugs found and fixed:**
  - **Bug #1** (2026-08-21): `contract_multiplier` loaded but never applied to
    `short_payoff`/`v_long`. Fixed; regression test added.
  - **Bug #2** (2026-08-22, the real explanation for the first live run's implausible numbers):
    `contract_multiplier` also never applied to the premium side (`short_bid`/`long_ask`),
    leaving `net_entry_cost` ~1000x too large relative to the correctly-scaled payoff terms.
    Found via `pricing/diagnose_pair.py` against real live data (best_bid=12750, spot=77223.2,
    strike=64400 only reconciles as a per-1-BTC quote). Fixed; regression test
    (`TestPremiumScalingUnitConsistency`) built directly from the real diagnosed numbers.
  - Grid widened from 5 to 21 price points (63 total scenarios) during the same debugging pass.
  - Expired-short-leg filtering added at load time after discovering `candidate_pairs` can go
    stale within hours (same-day D1 contracts), not just days.
  - Diagnostic fields (`time_to_short_expiry_hours`, `sigma_move`, `base_iv_used`) added to
    `EVResult` for visibility into *why* a given P(profit) came out the way it did.
  - **Final confirmed-good run (2026-08-23):** 404 priced, 292 positive EV, P(profit) properly
    distributed (no exact-0/1 collapse). Exit criterion met.

**Not built yet:**
- Lean backtester (Phase 6) — **next**
- Paper trading (Phase 7)
- Execution engine + risk/kill switch (Phase 8/9)
- Dashboard/alerting (deferred)
- Standalone continuous "opportunity scanner" (folded into `pricing/run_pricing.py` for the lean plan)
- Slippage modeling in `pricing/ev_engine.py` (Section D.2/D.4 gap, disclosed via `model_notes`,
  should be addressed before or during Phase 6 given how small the raw EV numbers are)

**Outstanding items, deprioritized under the fast-track plan:**
1. CoinSwitch options API docs.
2. Shark Exchange (dropped from scope).
3. Clarification on "CORP".
