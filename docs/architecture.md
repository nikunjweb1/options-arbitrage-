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

**v2.1 revision note (2026-08-23):** The project owner shared a detailed third-party analysis
(a NotebookLM breakdown of the specific YouTube video this whole project started from) that
sharpens the strategy definition considerably. See Section M for the full writeup — short
version: it mostly *confirms* this architecture's existing math and fee model rather than
requiring a rewrite, and it adds one important hard rule (net-credit-only entry) that wasn't
previously enforced as a gate. **Update, same day:** the project owner then personally
confirmed CoinSwitch/Shark's 1:30 PM IST settlement time, and independent research located
Shark Exchange's own live contract-details page and official support documentation
confirming this and surfacing genuinely new information (real fee/P&L formulas, and a
previously-unflagged currency-basis risk). See Section M.6 for what's now confirmed from
primary sources vs. what's still open.

---

## 0. Executive summary (read this first)

**Headline finding: the numeric example in the brief (Exchange A expires 1:30 PM, Exchange B
expires 5:30 PM, same day) does not match Delta Exchange India's documented behavior.** Delta
settles *all* options contracts — daily, weekly, monthly, quarterly — at **5:30 PM IST**,
computed from a 30-minute TWAP of the index price. "D1"/"D2"/weekly maturities differ by
*which day* they settle, not *what time of day*.

**Clarification (added 2026-08-23, see Section M):** this finding does **not** contradict the
source video, and shouldn't be read as having debunked the strategy. The video's actual claim
(per Section M's detailed breakdown) is an asymmetric two-exchange comparison — CoinSwitch/Shark
settling at 1:30 PM IST, Delta settling at 5:30 PM IST — not a claim that Delta itself has two
different intraday settlement times. Delta's fixed 5:30 PM is fully *consistent* with that
claim. **Update (2026-08-23, Section M.6): the CoinSwitch/Shark 1:30 PM IST claim is now
confirmed from two independent sources** — the project owner's own direct account check, and
Shark Exchange's own live options contract-details page (`Delivery Time: 01:30 PM`, fetched
directly, not inferred from marketing copy). CoinSwitch's own settlement time was not
independently re-verified in this pass — see Section M.6.

**Exchange readiness (researched, not assumed):**

| Exchange | Public options API | Settlement mechanics documented? | Verdict |
|---|---|---|---|
| **Delta Exchange India** | Yes — full public REST v2 + WebSocket, testnet, official SDKs | Yes: European, cash-settled, 30-min TWAP, fixed 5:30 PM IST | **Primary exchange, in active use. Phase 2/3/5 all built and validated.** |
| **CoinSwitch PRO** | Options API confirmed "available on request" (not self-serve) — see Section M.6. Spot/Futures/HFT have full public docs and SDKs; Options does not. | Marketing/product pages found (USDT-settled, 11 expiries, fees from 0.015%); exact settlement price formula (TWAP vs. spot vs. custom index) still not found in any public document — see Section M.6. | Still blocked on the settlement-formula gap specifically; API access itself is requestable, not yet requested. |
| **Shark Exchange** | No self-serve public API docs found for options specifically (Spot/Futures marketing exists; no options API reference located) | **Partially confirmed from primary sources (2026-08-23):** live contract-details page confirms `Delivery Time: 01:30 PM`, cash-settled. Official support docs confirm real fee/P&L formulas (Section M.6). Exact "Delivery Price" reference construction (TWAP? which index? what window?) still undocumented anywhere found. | Meaningfully de-risked vs. the original "no docs found" verdict, but the core basis-risk question (Section M.4.2) remains open. |

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
│  MIN_NET_CREDIT gate added per Section M.2 (2026-08-23)                │
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
│   Exit rule for THIS strategy = Exit A (close long leg AT T1),        │
│   confirmed as the video's own mechanism, not just our default -      │
│   see Section M.3                                                      │
└──────────────────┬───────────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│   RISK ENGINE + KILL SWITCH (built alongside execution engine)        │
│   MIN_NET_CREDIT hard gate added per Section M.2                       │
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

| Item | Delta Exchange India | CoinSwitch PRO | Shark Exchange |
|---|---|---|---|
| REST base URL | `https://api.india.delta.exchange` (prod) / `cdn-ind.testnet.deltaex.org` (testnet) | Not publicly documented for options specifically (Spot/Futures/HFT have `api-trading.coinswitch.co` docs) | Not publicly documented (no options API reference located) |
| WebSocket | `wss://socket.india.delta.exchange` | Unknown for options | Unknown |
| Settlement time | Fixed at 5:30 PM IST for every contract | Claimed 1:30 PM IST (video) — not independently re-verified this pass | **Confirmed 1:30 PM IST** — project owner's direct check + Shark's own live contract-details page (`Delivery Time: 01:30 PM`), 2026-08-23 |
| Settlement price formula | `max(30-min TWAP index − strike, 0)` for calls, mirrored for puts | Not documented publicly | Formula shape confirmed (`max(Delivery Price − Strike, 0) × Qty + Premium − Delivery Fee − Trading Fee`, per Shark's own support docs), but the exact construction of "Delivery Price" itself (TWAP? window? which index?) is not defined in any document found — see Section M.6 |
| Fees | Maker/taker on notional; capped at 7.5–12.5% of premium; zero settlement fee on OTM; 18% GST (India accounts) | Trading fees from 0.015% (marketing figure, not a full schedule) | **Confirmed from official docs:** Trading fee = min(0.012% × index price, 12.5% × option price), capped at 5% of option price; Delivery fee = min(0.015% × delivery price, 12.5% × (delivery − strike)); 18% GST separate |
| Settlement currency | Same as quote currency | Unknown | **USDT-quoted, INR-settled** — a currency-basis risk not previously flagged, see Section M.6 |

---

## C. Contract specification comparison

All seven checks (settlement clock, settlement price basis, settlement currency, contract
multiplier/lot size, European vs. Turbo variant, index construction, fee structure asymmetry)
are enforced as required, checked fields in `matching/engine.py`. Confirmed working against
374 real Delta contracts (Phase 3 run).

**Note on contract multiplier ratios (2026-08-23):** the source video claims a specific
CoinSwitch:Delta contract-size ratio ("10 slots on CoinSwitch = 100 slots on Delta"). This
number is **never hardcoded anywhere in this system** — `matching/engine.py`'s Section C.4
check always pulls `contract_multiplier` from each exchange's own live
`get_contract_specification()` response. The video's ratio is a useful sanity-check target
once CoinSwitch is integrated, not a value to trust directly, since a wrong hardcoded ratio
is exactly how you end up "90% unhedged" (the video's own phrase for this failure mode).
**Still unresolved (2026-08-23):** Shark Exchange's own live options page has a "Min Order
Size" field in its contract-details panel, but it renders client-side (JavaScript) and was
not readable via a static fetch — an actual account login or browser session is needed to
read the real value. Same open status as before for CoinSwitch's multiplier.

**New item (2026-08-23, Section M.6): currency-basis risk.** Shark Exchange's own support
docs confirm options are quoted in USDT but settled in INR. This is a *different* risk from
the index/settlement-price basis risk already tracked in Section C.2/C.6 — it's a currency
conversion exposure on top of it. Delta's options are not cross-currency this way. Section
H's risk table should track this as its own line item once a Shark adapter is built, not
merged into the existing basis-risk row.

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

**Hard rule from Section M.2, IMPLEMENTED 2026-08-23: `Net entry cost` (equivalently
`Gross entry credit`) MUST be > 0 (a real net credit, with a safety margin) for a candidate
to be tradeable at all.** This isn't new math — it's a **gating threshold** on math that
already existed. `pricing/run_pricing.py` now enforces this via `RISK.min_net_credit`
(`config/settings.py`, default `0.0`) and `signals.entry_eligible` (`db/schema.sql`) — see
Section H for how this maps onto the broader risk-limit set.

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

**Confirms Section M's "ATM jackpot" scenario directly:** this grid structurally produces its
highest `V_long(T1)` values exactly when `S_T1 ≈ K` (at-the-money at T1) — matching the
video's own claim that maximum remaining time value, and therefore maximum profit, occurs
right at the strike. Nothing needed to change here; the existing grid already captures this.

### D.4 P&L per simulated path

```
Short_payoff(T1) = settlement_formula(S_T1, K1)
P&L_path = Net_entry_cost − Short_payoff(T1) + V_long(T1) − exit_fees(long) − exit_slippage(long)
```

Every dollar term (`Net_entry_cost`, `Short_payoff`, `V_long`) is scaled by the relevant leg's
`contract_multiplier` — this consistency was the subject of both Bug #1 and Bug #2 (Status
section). `exit_slippage` is not yet modeled, same gap as D.2.

**Cross-check against Shark's own Delivery P&L formula (Section M.6):**
`max(Delivery Price − Strike, 0) × Qty + Premium − Delivery Fee − Trading Fee` (calls,
mirrored for puts) is structurally the same shape as `D.4`'s `P&L_path`, just phrased in
per-leg terms rather than as a two-leg spread. No change needed here — this is corroborating
evidence the existing formula shape generalizes correctly to a second exchange's documented
convention, not a reason to modify it.

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
(populated with real Phase 5 results, now including `entry_eligible` per Section M.2),
`trades` all exist.

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
- **Named stress scenarios added from Section M.5's four-scenario breakdown** (flat/OTM,
  ATM "jackpot", high-momentum/deep-ITM-or-OTM, execution-failure/unhedged) — these map onto
  specific slices of the existing price grid (D.3) plus one new explicit test: a forced
  "long leg never closed at T1" path, to quantify Section M's Scenario 4 (execution failure)
  rather than leaving it as a purely qualitative risk.

### G.3 What's explicitly deferred

In-sample/out-of-sample split, walk-forward re-estimation, full stress-scenario suite — still
in the plan, after a lean pass justifies the time.

---

## H. Risk model — NOT COMPRESSED

(Unchanged from v1 in structure — see prior revisions for the full table.) **Section M cross-
check (2026-08-23):** the video's own "five hidden risks" map cleanly onto risk categories
already in this table, confirming the existing taxonomy rather than requiring new categories:

| Video's risk (Section M) | Existing risk-model row |
|---|---|
| Momentum/net-debit math flaw | Market risk + `MIN_NET_CREDIT` gate (Section M.2/D.2) — **implemented 2026-08-23** |
| Fee stacking (4 fee points) | Already modeled: `short_entry_fee`, `long_entry_fee`, `settlement_fee`, `long_exit_fee` all present in `pricing/ev_engine.py` and `backtest/engine.py`. Shark's real fee formulas (Section M.6) confirm this modeling approach generalizes correctly. |
| Execution latency / legging-in | Legging risk (already modeled, incl. `backtest/engine.py`'s legging-failure simulation) |
| Basis risk (index discrepancy) | Settlement risk + Contract-spec risk (Section C.2/C.6) — **narrowed but not resolved for Shark, see Section M.6**: formula shape confirmed, exact reference-price construction still undocumented |
| Contract multiplier mismatch ("90% unhedged") | Contract-spec risk (Section C.4) — reinforces why multiplier is never hardcoded (see Section C note above). Still unresolved for both CoinSwitch and Shark. |
| **New (2026-08-23): currency-basis risk** (Shark quotes USDT, settles INR) | **Not previously a tracked risk category** — add as its own line item once a Shark adapter exists, per Section C's new note. |

`MIN_NET_CREDIT` is now live in the hard-limits list (`config/settings.py` `RiskLimits.min_net_credit`,
alongside `MAX_TOTAL_CAPITAL`, `MAX_MARGIN_PER_TRADE`, etc.) — a candidate with
`Net_entry_cost <= 0` is enforced as a `DO_NOT_ENTER` case in `pricing/run_pricing.py`
(Section M.2), not merely a lower-ranked one. Full risk-engine enforcement (Phase 9) is still
pending, per this section's original scope.

---

## I. Implementation roadmap — STATUS AS OF v2.1

**Phase 1 — Research.** ✅ Done. **CoinSwitch/Shark verification targets from Section M.4
partially resolved 2026-08-23 — see Section M.6. Settlement-time claim confirmed for Shark
(two independent sources) and personally confirmed by the project owner for both. Settlement
price formula and contract multiplier remain open for both.**

**Phase 2 — Market-data collectors.** ✅ Done.

**Phase 3 — Contract matcher.** ✅ Done. Candidate count grows over time as new D1 contracts
list (1,504 at the original Phase 3 run; re-run `matching.run_matcher` periodically).

**Phase 5 — Pricing/EV engine.** ✅ **Done, exit criterion met, MIN_NET_CREDIT gate added.**
- `pricing/ev_engine.py`'s `LeanEVEngine` (21×3=63-scenario grid), `pricing/black_scholes.py`,
  and `pricing/run_pricing.py` (candidate_pairs → live ticker/fees → EV → `signals`) are all
  built, tested (19 tests in `tests/test_ev_engine.py`), and have been run live against real
  Delta testnet data multiple times during debugging.
- **Two real bugs found and fixed via live diagnostic evidence** (not guessed): (1)
  `contract_multiplier` omitted from `short_payoff`/`v_long`; (2) `contract_multiplier` also
  omitted from the premium side (`short_bid`/`long_ask`) — confirmed via `pricing/diagnose_pair.py`
  against a real quote.
- **Final confirmed-good live run (2026-08-23):** 674 non-expired candidates loaded, 404
  priced (270 skipped for no executable live data), 292/404 (72%) positive EV, `P(profit)`
  distributed across a real range for all 404 results.
- **DONE 2026-08-23: `MIN_NET_CREDIT` hard gate.** `run_pricing.py` now tags every priced
  candidate `entry_eligible` based on `net_entry_cost > RISK.min_net_credit`, excludes
  ineligible (net-debit) candidates from the ranked opportunity display, and persists the
  flag as a real, queryable `signals` column — never silently dropping blocked candidates.
- **Known gap to carry into Phase 6/7:** `expected_slippage` (Section D.2) is not yet modeled.

**Phase 6 — Lean backtester.** Built (`backtest/engine.py`, `schemas.py`, `run_backtest.py`),
per Section G.2, including Section M's four named stress scenarios.

**Phase 7 — Short paper trading window.** Sized to observed signal frequency from Phase 5/6.

**Phase 8 — Execution engine + Phase 9 — Risk/kill switch.** Built together.
`LIVE_TRADING = FALSE` hardcoded regardless. Execution engine's default exit rule for this
strategy confirmed as Exit A (close long leg at T1) per Section M.3 — matches the video's own
mechanism exactly, so this isn't an open design choice anymore for this specific strategy.

**Phase 10 — Live trading.** Enabled only after explicit, separate approval, and only if the
lean backtest + paper trading both show a real, cost-inclusive, positive edge, **and** the
cross-exchange leg's remaining open items in Section M.6 (settlement price formula, contract
multiplier, API access/reliability) are resolved for whichever of CoinSwitch/Shark is
actually used — the Delta-only same-exchange version of this strategy does not depend on
these, but the cross-exchange version the source video actually describes does.

**Explicitly deferred (not cancelled):** full v1 backtest matrix, dashboard, alerting,
CoinSwitch/Shark adapters (still blocked on the items in Section M.6).

---

## J. MVP — superseded by real results

Answered: yes, same-exchange calendar-spread candidates exist structurally (hundreds found),
and a meaningful fraction (72% of what could be priced in the latest run) show positive lean-EV
after real fees, before slippage. Whether that survives slippage modeling, the `MIN_NET_CREDIT`
gate (now implemented), and a real backtest is Phase 6's question now.

---

## K. Open-source component reuse strategy

| Project | License | Verdict |
|---|---|---|
| **Hummingbot** | Apache 2.0 | Connector architecture pattern reused for `ExchangeAdapter` shape. |
| **put-call-arb** (`lubintan`) | None found (all rights reserved) | Studied for methodology only; not copied. |
| **Deribit MCP** | MIT | Not currently in use. |
| **"CORP"** | Unknown | Still not identified — deprioritized. |

**Item from 2026-08-23: "Mirror Web platform"** (as recorded from the source video) vs.
**"Mirror Pip platform"** (as referenced by the project owner in a follow-up message,
2026-08-23) — **these may be the same third-party tool with a name transcribed differently,
or two different tools. Not yet resolved; flagged for the project owner to clarify.** Per the
existing decision below, this doesn't change anything about how this system is built either
way, but the discrepancy itself is worth pinning down before this tool is discussed further,
since Section M.1 discusses it as part of the video's described workflow.

Not researched, not integrated, and not currently planned to be — this system's execution
engine (Phase 8) is being built as our own adapter-based architecture (Section A.3), not as a
wrapper around an unverified third-party trading tool with unknown API access, reliability, or
security posture. If there's a specific reason to reconsider this, that's a separate decision
to make deliberately, not something to fold in because a video mentioned it.

---

## L. Fast-track plan (v2)

### L.1 The instruction and the constraint

Low-risk is the hypothesis this project exists to test, not a fact established yet. Phase 5
now shows positive-EV candidates exist before slippage costs — a reason to proceed carefully
to Phase 6, not a reason to declare victory.

### L.2 Lean EV/Monte Carlo engine — DONE, including MIN_NET_CREDIT gate

Real bid/ask pulled live for both legs, real fees, real net entry cost (minus the
not-yet-modeled slippage term). `pricing/run_pricing.py` + `pricing/ev_engine.py`. Output:
net EV, probability-of-profit, ranked, `entry_eligible`. Confirmed working with sane numbers
as of 2026-08-23.

### L.3 Lean backtest + short paper trading (replaces full Phase 6/7) — NEXT

- Backtest: one pass, whatever historical window Delta's API exposes, one underlying, one
  threshold. Should incorporate a slippage estimate given Phase 5's disclosed gap, and the
  four named scenarios from Section M.5/G.2.
- Paper trading: window length derived from Phase 5/6's findings.

### L.4 Risk engine + kill switch: not deferred

Built alongside the execution engine (Phase 8/9). `MIN_NET_CREDIT` added to the hard-limit set
— **done 2026-08-23.**

### L.5 What "later" actually means

Deferred items come back into scope if and when a lean pass shows a real edge worth the
additional engineering time.

---

## M. Strategy definition refinement (source: third-party video analysis, 2026-08-23)

The project owner shared a detailed breakdown (a NotebookLM analysis of the specific YouTube
video — presenter identified as Pushkar Raj Thakur — that originally inspired this project)
covering the exact mechanics, worked examples, and stated risks of the strategy. This section
records what was learned and, critically, distinguishes **video claims** (unverified against
primary exchange documentation) from **things this analysis independently confirmed are sound
options math** (verifiable from first principles, regardless of what the video's source
exchanges turn out to actually do).

### M.1 The core mechanism, as described in the video

- Sell (short) an option on the earlier-expiry exchange (CoinSwitch or Shark, claimed 1:30 PM
  IST expiry). Buy (long) the same underlying/strike option on the later-expiry exchange
  (Delta, confirmed 5:30 PM IST settlement — see Section 0).
- At 1:30 PM, the short leg **expires automatically**. If OTM, it settles at $0 and the seller
  keeps the full premium.
- The long leg does **not** expire at 1:30 PM — it has ~4 hours of remaining life. It must be
  **manually closed (sold) at 1:30 PM** to capture its remaining time value, rather than held
  to its own 5:30 PM expiry. This is exactly Exit A in this project's execution-engine design
  (Section 14 of the original master prompt) — the video's own mechanism confirms Exit A as
  the right *default*, not just one option among several, for this specific strategy.
- **This part is sound, first-principles options math, independent of whether the video's
  specific exchange claims are correct**: this is a textbook calendar-spread payoff shape.
  Maximum value of the long leg's remaining time value occurs when the underlying is
  at-the-money at T1; the payoff degrades (time value collapses toward zero) the further the
  underlying moves from the strike in either direction by T1.

### M.2 The net-credit rule — the one genuinely new hard requirement — IMPLEMENTED 2026-08-23

The video's own worked examples (reconstructed and checked in the shared analysis) show:

- **Net-credit entry** (`B_short > A_long`, i.e. `Net_entry_cost > 0`): in the worst case
  (a violent move collapsing the long leg's time value to ~0), the trade still nets out to
  **at least the entry credit** — bounded, not a loss, assuming the hedge ratio and both legs'
  execution are correct.
- **Net-debit entry** (`B_short < A_long`, i.e. `Net_entry_cost < 0`): in that same worst case,
  the trade loses **the full entry debit**. There is no scenario in this payoff structure where
  a net-debit entry does better than net-credit in the tail — this checks out mathematically
  (calendar spreads have bounded, strike-centered maximum value; a debit paid for that bounded
  structure is money that can be fully lost if the structure's value collapses to its floor).

**This is not new to our math — Section D.2/D.4 already computes exactly this P&L — but it
was not previously enforced as a gate.** ~~`pricing/run_pricing.py` currently ranks and
surfaces~~ **As of 2026-08-23, `pricing/run_pricing.py` no longer ranks** both net-credit and
net-debit candidates by EV alone. `Net_entry_cost <= RISK.min_net_credit` (default `0.0`) is
now enforced as a hard `DO_NOT_ENTER` condition (`entry_eligible=0` in `signals`), excluded
from the ranked opportunity display but still priced and persisted for visibility — matching
this section's original recommendation exactly.

### M.3 Exit timing confirmed, not just assumed

The video is explicit and repeated on this point: the long leg must be closed **at the short
leg's expiry (T1)**, and forgetting to do so (its own "Scenario 4") leaves an unhedged position
that decays toward the long leg's own expiry with real loss potential. This directly validates
building `EXIT_TRIGGER = short_leg_expired` as the primary, default automated exit condition
for Phase 8's execution engine for this specific strategy — not a configurable choice to leave
open-ended, given how explicitly both the video and this project's own math agree on it.

### M.4 What remains genuinely unverified — sharpened, not resolved (as of the M.2 update)

A YouTube video, however carefully analyzed, is not primary-source exchange documentation.
The specific, falsifiable claims that still need checking against CoinSwitch's and Shark's own
official docs (or, failing that, direct empirical observation of a real contract's settlement)
before this cross-exchange leg of the strategy is trusted with real capital:

1. ~~Do CoinSwitch and/or Shark actually settle options at exactly 1:30 PM IST?~~
   **CONFIRMED for Shark, 2026-08-23 — see Section M.6.** CoinSwitch not independently
   re-verified this pass (project owner reports personally checking both; Shark's confirmation
   is now doubly sourced).
2. **What is their settlement price formula** (TWAP, last-price, their own index)? Getting this
   wrong is exactly the "basis risk" the video itself flags (Section C.2/C.6) — a $63,010 vs.
   $62,995 settlement discrepancy across exchanges can eat the entire arbitrage margin on its
   own, independent of everything else being right. **Partially narrowed for Shark, still open
   — see Section M.6.**
3. **What is the real, current contract-multiplier ratio between CoinSwitch/Shark and Delta**
   for a given underlying? The video's "10:100" example is a snapshot, not a documented,
   stable constant — must be pulled live via `get_contract_specification()` every time, per
   Section C's existing (and unchanged) design. **Still open for both — see Section M.6.**
4. **Does CoinSwitch's/Shark's API even support the timely manual-close-at-T1 execution this
   strategy structurally requires?** If placing an order at exactly 1:30 PM IST is unreliable
   on their API (rate limits, latency, downtime), that's Section M.3's exit trigger becoming
   Section M.1's "Scenario 4" (execution failure) by default, not by exception. **Still open
   for both — CoinSwitch's options API is confirmed request-only (not self-serve), so this
   can't even be assessed until access is requested. See Section M.6.**

None of this is resolvable from a video, however good the analysis. **This remains the single
highest-priority blocker to trading the actual cross-exchange version of this strategy for
real money**, though item 1 is now resolved and items 2-3 are meaningfully narrowed for Shark
specifically — see Section M.6 for exactly what's confirmed vs. still open.

### M.5 Four scenarios — adopted as named stress-test cases (Section G.2)

| # | Scenario | Outcome for net-credit entry | Outcome for net-debit entry |
|---|---|---|---|
| 1 | Flat/OTM at T1 | Profit = credit + remaining long-leg time value | Same shape, smaller/negative depending on magnitude |
| 2 | ATM "jackpot" at T1 | Maximum profit (long leg retains maximum time value) | Same direction, smaller magnitude |
| 3 | High momentum (deep ITM or deep OTM at T1) | Profit collapses toward the entry credit (bounded, still ≥ 0 if hedge/execution correct) | **Loss equal to the full entry debit** |
| 4 | Execution failure (long leg not closed at T1) | Unhedged decay risk regardless of entry credit/debit — becomes a directional bet, not an arbitrage | Same, compounded by starting already underwater |

Scenarios 1-3 are natural slices of the existing D.3 price grid (already implemented).
Scenario 4 requires one new, explicit backtest/paper-trading test path — "what if the exit
trigger fires late or not at all" — since nothing in the current `backtest/engine.py` or
`pricing/ev_engine.py` models a missed or delayed exit. Worth adding in Phase 6/7, not Phase 5,
since it's fundamentally about execution reliability, not pricing.

### M.6 Findings from direct investigation, 2026-08-23 — what's now confirmed vs. still open

Following up on Section M.4's open items: the project owner personally confirmed CoinSwitch's
and Shark's 1:30 PM IST settlement time via their own account access. Independently, a search
for public documentation on both exchanges found the following, distinguishing real primary
sources from marketing copy:

**CONFIRMED, from primary sources:**
- **Shark Exchange's settlement time is 1:30 PM IST**, confirmed via the live "Contract
  Details" panel on `sharkexchange.in/options/btcusdt` itself (`Delivery Time: 01:30 PM`,
  `Delivery Method: Cash settled on expiry`) — not marketing copy, the actual per-contract
  data panel, same UI pattern as Delta's own contract pages. This independently corroborates
  the project owner's own account check and the video's claim.
- **Shark's real P&L/fee formulas**, from Shark's own official support documentation
  (`sharkexchange.freshdesk.com`, "Options P&L Calculations on Shark Exchange"):
  - `Delivery P&L = max(Delivery Price − Strike, 0) × Qty + Premium − Delivery Fee − Trading Fee` (calls; puts mirrored)
  - `Trading Fee = min(0.012% × Index Price, 12.5% × Option Price)`, capped at 5% of option price
  - `Delivery Fee = min(0.015% × Delivery Price, 12.5% × (Delivery Price − Strike))`
  - 18% GST applied separately
  - This structurally matches this project's own fee/P&L modeling approach (Section D.4) —
    corroborating evidence the existing design generalizes, not a reason to change it.
- **New risk surfaced: options are USDT-quoted but INR-settled on Shark.** This is a currency
  conversion exposure distinct from the index/settlement-price basis risk already tracked —
  see Section C and Section H's updated risk table.
- **CoinSwitch's options API is confirmed "available on request"** (per CoinSwitch's own
  official API docs site, `api-trading.coinswitch.co`) — meaning it is explicitly *not*
  self-serve like their Spot/Futures/HFT surfaces are. This is itself useful: the concrete
  next action for CoinSwitch specifically is requesting that access, not searching for docs
  that don't yet exist publicly.

**STILL OPEN, not resolved by this pass:**
- **Shark's exact "Delivery Price" reference construction** (is it a TWAP? over what window?
  against which index feed?) is not defined in any document found, including Shark's own
  support docs — the formula's *shape* is confirmed, but not the *input* that makes it
  trustworthy for basis-risk purposes. This is the crux of Section M.4 item 2 and remains the
  single most important unresolved question for trusting a cross-exchange settlement-price
  comparison with real capital.
- **Contract multiplier / lot size for both exchanges** — Shark's own contract-details page
  has a "Min Order Size" field, but it renders client-side and wasn't readable via a static
  fetch; an actual logged-in session is needed. CoinSwitch's multiplier remains undocumented
  publicly. Per Section C, this must come from each exchange's own live API response when an
  adapter is eventually built — never hardcoded from any source, video or otherwise.
- **API reliability for the manual-close-at-T1 execution** — genuinely can't be assessed for
  CoinSwitch until options API access is requested and granted; no public data found for
  Shark's options API either (only browser-based trading confirmed to exist).
- **"Mirror Web" vs. "Mirror Pip"** — naming discrepancy between Section K's original note and
  the project owner's most recent message. Not yet resolved; see Section K.

**Practical implication:** the Delta-only, same-exchange version of this strategy (currently
what Phases 2-6 are built and tested against) is unaffected by any of this — it doesn't depend
on CoinSwitch or Shark at all. The cross-exchange version the source video actually describes
is meaningfully closer to buildable than it was (settlement timing confirmed, fee/P&L formula
shape confirmed for Shark), but the settlement-price reference construction and contract
multiplier remain the concrete blockers, and neither is resolvable without either an actual
funded account on the relevant exchange or a direct support request for documentation.
