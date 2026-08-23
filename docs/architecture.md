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
previously enforced as a gate. It does **not** resolve the still-open CoinSwitch/Shark
verification blocker — a video's claims about a competitor's settlement mechanics are not a
substitute for that exchange's own documentation, however carefully the video is analyzed.

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
claim. The open question was, and remains, whether CoinSwitch and/or Shark actually settle at
1:30 PM IST as claimed — which still requires their own official documentation to confirm, not
assumption from either the original brief or this new video analysis.

**Exchange readiness (researched, not assumed):**

| Exchange | Public options API | Settlement mechanics documented? | Verdict |
|---|---|---|---|
| **Delta Exchange India** | Yes — full public REST v2 + WebSocket, testnet, official SDKs | Yes: European, cash-settled, 30-min TWAP, fixed 5:30 PM IST | **Primary exchange, in active use. Phase 2/3/5 all built and validated.** |
| **CoinSwitch PRO** | Options API "available on request" | Marketing language only, no settlement docs found. Video claims 1:30 PM IST settlement — unverified against official docs (see Section M.4). | Still blocked, deferred (Section L). |
| **Shark Exchange** | No public options API documentation found | Video claims 1:30 PM IST settlement, same as CoinSwitch — unverified against official docs (see Section M.4). | Dropped from scope pending docs. |

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
│  NEXT: add MIN_NET_CREDIT entry gate per Section M.2                  │
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

| Item | Delta Exchange India | CoinSwitch PRO |
|---|---|---|
| REST base URL | `https://api.india.delta.exchange` (prod) / `cdn-ind.testnet.deltaex.org` (testnet) | Not publicly documented for options |
| WebSocket | `wss://socket.india.delta.exchange` | Unknown for options |
| Settlement time | Fixed at 5:30 PM IST for every contract | Claimed 1:30 PM IST by source video (Section M.4) — unverified |
| Settlement price formula | `max(30-min TWAP index − strike, 0)` for calls, mirrored for puts | Not documented publicly |
| Fees | Maker/taker on notional; capped at 7.5–12.5% of premium; zero settlement fee on OTM; 18% GST (India accounts) | Not documented publicly |

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

**New hard rule from Section M.2: `Net entry cost` (equivalently `Gross entry credit`) MUST
be > 0 (a real net credit, with a safety margin) for a candidate to be tradeable at all.**
This isn't new math — it's a **gating threshold** on math that already existed. See Section
M.2 for the reasoning and Section H for how this maps onto `MIN_EXPECTED_PROFIT`/risk limits.

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
| Momentum/net-debit math flaw | Market risk + new `MIN_NET_CREDIT` gate (Section M.2/D.2) |
| Fee stacking (4 fee points) | Already modeled: `short_entry_fee`, `long_entry_fee`, `settlement_fee`, `long_exit_fee` all present in `pricing/ev_engine.py` and `backtest/engine.py` |
| Execution latency / legging-in | Legging risk (already modeled, incl. `backtest/engine.py`'s legging-failure simulation) |
| Basis risk (index discrepancy) | Settlement risk + Contract-spec risk (Section C.2/C.6) |
| Contract multiplier mismatch ("90% unhedged") | Contract-spec risk (Section C.4) — reinforces why multiplier is never hardcoded (see Section C note above) |

One genuinely new item: **`MIN_NET_CREDIT` should be added to the hard-limits list in Section
24 of the original master prompt** (`MAX_TOTAL_CAPITAL`, `MAX_MARGIN_PER_TRADE`, etc.) — a
candidate with `Net_entry_cost <= 0` should be a `DO NOT ENTER` case in Phase 9's risk engine,
not merely a lower-ranked one.

---

## I. Implementation roadmap — STATUS AS OF v2

**Phase 1 — Research.** ✅ Done. **Sharpened targets to verify for CoinSwitch/Shark added,
Section M.4 — not yet resolved.**

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
  against a real quote.
- **Final confirmed-good live run (2026-08-23):** 674 non-expired candidates loaded, 404
  priced (270 skipped for no executable live data), 292/404 (72%) positive EV, `P(profit)`
  distributed across a real range for all 404 results.
- **NOT YET IMPLEMENTED, now a concrete near-term task per Section M.2:** the
  `MIN_NET_CREDIT` hard gate. `run_pricing.py` currently ranks and reports all 404 priced
  candidates, including net-debit ones, whose worst-case loss per Section M.1's math equals
  the entry debit itself. This should be filtered/flagged explicitly, not just left implicit
  in the EV ranking.
- **Known gap to carry into Phase 6/7:** `expected_slippage` (Section D.2) is not yet modeled.

**Phase 6 — Lean backtester.** Next up. Per Section G.2, now including Section M's four named
stress scenarios.

**Phase 7 — Short paper trading window.** Sized to observed signal frequency from Phase 5/6.

**Phase 8 — Execution engine + Phase 9 — Risk/kill switch.** Built together.
`LIVE_TRADING = FALSE` hardcoded regardless. Execution engine's default exit rule for this
strategy confirmed as Exit A (close long leg at T1) per Section M.3 — matches the video's own
mechanism exactly, so this isn't an open design choice anymore for this specific strategy.

**Phase 10 — Live trading.** Enabled only after explicit, separate approval, and only if the
lean backtest + paper trading both show a real, cost-inclusive, positive edge.

**Explicitly deferred (not cancelled):** full v1 backtest matrix, dashboard, alerting,
CoinSwitch/Shark adapters (still blocked on real docs — see Section M.4).

---

## J. MVP — superseded by real results

Answered: yes, same-exchange calendar-spread candidates exist structurally (hundreds found),
and a meaningful fraction (72% of what could be priced in the latest run) show positive lean-EV
after real fees, before slippage. Whether that survives slippage modeling, the `MIN_NET_CREDIT`
gate, and a real backtest is Phase 6's question now.

---

## K. Open-source component reuse strategy

| Project | License | Verdict |
|---|---|---|
| **Hummingbot** | Apache 2.0 | Connector architecture pattern reused for `ExchangeAdapter` shape. |
| **put-call-arb** (`lubintan`) | None found (all rights reserved) | Studied for methodology only; not copied. |
| **Deribit MCP** | MIT | Not currently in use. |
| **"CORP"** | Unknown | Still not identified — deprioritized. |

**New item (2026-08-23): "Mirror Web platform"**, mentioned in the source video as a
third-party execution/education tool. Not researched, not integrated, and not currently
planned to be — this system's execution engine (Phase 8) is being built as our own adapter-
based architecture (Section A.3), not as a wrapper around an unverified third-party trading
tool with unknown API access, reliability, or security posture. If there's a specific reason
to reconsider this, that's a separate decision to make deliberately, not something to fold in
because a video mentioned it.

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
**Next code change: add the `MIN_NET_CREDIT` gate from Section M.2.**

### L.3 Lean backtest + short paper trading (replaces full Phase 6/7) — NEXT

- Backtest: one pass, whatever historical window Delta's API exposes, one underlying, one
  threshold. Should incorporate a slippage estimate given Phase 5's disclosed gap, and the
  four named scenarios from Section M.5/G.2.
- Paper trading: window length derived from Phase 5/6's findings.

### L.4 Risk engine + kill switch: not deferred

Built alongside the execution engine (Phase 8/9). `MIN_NET_CREDIT` added to the hard-limit set.

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

### M.2 The net-credit rule — the one genuinely new hard requirement

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
was not previously enforced as a gate.** `pricing/run_pricing.py` currently ranks and surfaces
both net-credit and net-debit candidates by EV alone. Per this finding, **`Net_entry_cost <= 0`
should be treated as a hard `DO NOT ENTER` condition** (a new `MIN_NET_CREDIT` risk limit,
Section H), not just a candidate that scores lower. This is the one concrete, actionable
change this analysis produces — everything else confirms existing design rather than changing
it.

### M.3 Exit timing confirmed, not just assumed

The video is explicit and repeated on this point: the long leg must be closed **at the short
leg's expiry (T1)**, and forgetting to do so (its own "Scenario 4") leaves an unhedged position
that decays toward the long leg's own expiry with real loss potential. This directly validates
building `EXIT_TRIGGER = short_leg_expired` as the primary, default automated exit condition
for Phase 8's execution engine for this specific strategy — not a configurable choice to leave
open-ended, given how explicitly both the video and this project's own math agree on it.

### M.4 What remains genuinely unverified — sharpened, not resolved

A YouTube video, however carefully analyzed, is not primary-source exchange documentation.
The specific, falsifiable claims that still need checking against CoinSwitch's and Shark's own
official docs (or, failing that, direct empirical observation of a real contract's settlement)
before this cross-exchange leg of the strategy is trusted with real capital:

1. **Do CoinSwitch and/or Shark actually settle options at exactly 1:30 PM IST?** (Video claim,
   unverified — Section 0/B.)
2. **What is their settlement price formula** (TWAP, last-price, their own index)? Getting this
   wrong is exactly the "basis risk" the video itself flags (Section C.2/C.6) — a $63,010 vs.
   $62,995 settlement discrepancy across exchanges can eat the entire arbitrage margin on its
   own, independent of everything else being right.
3. **What is the real, current contract-multiplier ratio between CoinSwitch/Shark and Delta**
   for a given underlying? The video's "10:100" example is a snapshot, not a documented,
   stable constant — must be pulled live via `get_contract_specification()` every time, per
   Section C's existing (and unchanged) design.
4. **Does CoinSwitch's/Shark's API even support the timely manual-close-at-T1 execution this
   strategy structurally requires?** If placing an order at exactly 1:30 PM IST is unreliable
   on their API (rate limits, latency, downtime), that's Section M.3's exit trigger becoming
   Section M.1's "Scenario 4" (execution failure) by default, not by exception.

None of this is resolvable from a video, however good the analysis. **This remains the single
highest-priority blocker to trading the actual cross-exchange version of this strategy for
real money**, and this new information sharpens exactly what to check for, rather than
providing a shortcut around checking it.

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
    Found via `pricing/diagnose_pair.py` against real live data. Fixed; regression test
    (`TestPremiumScalingUnitConsistency`) built directly from the real diagnosed numbers.
  - Grid widened from 5 to 21 price points (63 total scenarios) during the same debugging pass.
  - **Final confirmed-good run (2026-08-23):** 404 priced, 292 positive EV, P(profit) properly
    distributed. Exit criterion met.

**Not built yet:**
- **`MIN_NET_CREDIT` hard gate (Section M.2) — new, concrete, near-term task.**
- Lean backtester (Phase 6), now incorporating Section M.5's four named scenarios
- Paper trading (Phase 7)
- Execution engine + risk/kill switch (Phase 8/9) — exit trigger design now confirmed (Section M.3)
- Dashboard/alerting (deferred)
- Slippage modeling in `pricing/ev_engine.py`

**Outstanding items, sharpened but still unresolved (Section M.4):**
1. CoinSwitch options API docs — now with specific claims to check: 1:30 PM IST settlement,
   settlement price formula, live contract-multiplier ratio.
2. Same for Shark Exchange, if it stays in scope.
3. Clarification on "CORP" (unrelated to this update, still open).
