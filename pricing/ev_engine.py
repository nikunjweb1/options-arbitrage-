"""
Lean EV/scenario engine -- Phase 5, per docs/architecture.md Section L.2.

Implements Section D.2 (entry economics), D.3 (long-leg repricing at T1),
and D.4 (P&L per scenario) using a small deterministic scenario grid instead
of a full Monte Carlo simulation or a fitted historical IV distribution.

WHY A GRID, NOT JUST AN IV SHOCK BAND: the short leg's settlement payoff
(Section D.4, Short_payoff) depends on the underlying price at T1, not on
IV at all -- IV only affects the long leg's repriced value. A model that
only shocks IV at a fixed spot price would silently ignore the dominant
source of P&L variance for this strategy (the short leg's own payoff). So
this engine crosses an underlying-price grid (21 points -- see "GRID
RESOLUTION FIX" below) with a small IV-shock grid (3 points).

WHAT THIS DOES NOT DO (be honest about the gap vs. v1's full plan):
  - Does not fit sigma_effective_at_T1 to historical IV term-structure
    behavior -- uses today's observed IV as the base and a fixed +/-30%
    shock band around it.
  - Does not model IV smile/skew across strikes.
  - Scenario weights are a discretized-normal approximation, not a properly
    calibrated risk-neutral distribution.
  - Does not account for legging risk, slippage beyond a flat assumption,
    or partial fills -- those are Phase 8/9 concerns.
Every EVResult carries a `model_notes` field stating this explicitly.

BUG #1 FOUND + FIXED (2026-08-21): contract_multiplier was loaded but never
applied to short_payoff/v_long. Fixed by scaling each leg's payoff/value by
its own contract_multiplier. See TestLeanEVEngineUnitConsistency.

BUG #2 FOUND + FIXED (2026-08-22, widened grid, symptom persisted): after
Bug #1's fix, P(profit) was still landing at exactly 0.0 or 1.0 for all 330
live-priced candidates, even after widening the price grid from 5 to 21
points. Root cause, found via pricing/diagnose_pair.py against real Delta
testnet data: exchange-quoted best_bid/best_ask are in RAW per-1-BTC terms
-- the SAME scale as spot/strike -- not already scaled to one contract's
notional. Confirmed directly: a deep-ITM call showed best_bid=12750 against
spot=77223.2, strike=64400 -- intrinsic value (spot-strike) = 12823.2, which
best_bid (12750) and mark_price (12823.5) both sit right next to. That only
makes sense if the quoted price is per-1-BTC (matching Delta's own displayed
scale), not per-contract -- a real 0.001-BTC-notional contract's actual cost
is best_bid * contract_multiplier = 12750 * 0.001 = $12.75, not $12,750.

Bug #1's fix correctly scaled short_payoff/v_long by contract_multiplier,
but never scaled short_bid/long_ask (and therefore net_entry_cost) the same
way -- based on the wrong assumption that quoted premiums were already
contract-scaled. That left net_entry_cost roughly 1/contract_multiplier too
LARGE relative to the (correctly scaled) payoff/repricing terms -- the
opposite-direction version of Bug #1, on the other side of the same P&L
formula. That's why net_entry_cost (e.g. ~252) totally dwarfed any realistic
payoff swing (a few dollars) regardless of grid resolution -- Bug #2 was
never actually a resolution problem, widening the grid in the "GRID
RESOLUTION FIX" commit was necessary-but-insufficient, treating a symptom
without yet having found this root cause.

FIX: short_bid and long_ask are now scaled by their own leg's
contract_multiplier BEFORE computing gross_entry_credit/fees/net_entry_cost,
exactly the same treatment already applied to short_payoff/v_long -- so
every dollar figure in the P&L formula is consistently in "per one real
exchange contract" terms. See
tests/test_ev_engine.py::TestPremiumScalingUnitConsistency for the
regression test built directly from the real diagnose_pair.py output above.

BUG #3 FOUND + FIXED (2026-08-23): `base_iv` was read ONLY from
short_snapshot.iv, and that single value was then reused to reprice the
LONG leg's Black-Scholes value at T1 -- long_snapshot.iv was never read
anywhere in this file. This directly undermines the strategy's actual
economic basis (per the project owner's own description of the trade):
"compare their premium and implied volatility (IV)... if one option is
relatively overpriced, sell that option and buy the relatively cheaper
later-expiry option." If the long leg's own quoted IV is silently replaced
with the short leg's, the model can never detect -- let alone correctly
price -- exactly the cross-leg IV divergence this strategy is supposed to
be trading on. Quantified via a realistic example (short IV=80%, long
IV=40%, 7d option, spot=strike=65000): repricing the long leg with the
short leg's IV instead of its own overstates its fair value by ~$1435 on a
~$1436 true value -- roughly 2x, not a rounding error.

FIX: long-leg repricing now uses long_snapshot.iv as its own base IV
(falling back to the short leg's IV only if the long snapshot has none,
which is strictly better than the previous unconditional reuse). The
underlying price-move grid (sigma_move) still uses the short leg's IV,
since that governs the near-term move up to the short leg's own expiry --
a different, still-correct use of short IV, not affected by this fix.
See tests/test_ev_engine.py::TestCrossLegIVDivergence.

GAP #1 -- NO LIQUIDITY CHECK (2026-08-23, this session): is_executable()
only confirmed a bid/ask *exists*, never that there's real size behind it.
A candidate could show a "certain" profit purely because the model assumed
unlimited fill size at the quoted top-of-book price. evaluate() now takes a
min_contract_size and fails closed (InsufficientDataError) if either leg's
resting size (bid_size for the short leg, ask_size for the long leg) is
below it or unknown -- consistent with the fail-closed principle in
architecture.md Section A.1. This closes a real gap in config/settings.py
too: `RiskLimits.min_liquidity` has existed since Phase 2/3 but was never
actually wired to anything (its own docstring: "presence here does not by
itself constitute enforcement") -- pricing/run_pricing.py now passes it
through as this parameter, the same way MIN_NET_CREDIT went from
documented-but-inert to actually enforced on 2026-08-23.

GAP #2 -- NO CHECK OUTSIDE THE NORMAL GRID (2026-08-23, this session): the
21-point grid only explores lognormal moves within +/-3 sigma of
sigma_move, which for a low-IV, short-dated leg can be a very narrow band.
This does NOT change expected_value or probability_of_profit -- doing so
would silently redefine what those numbers mean vs. the documented Section
D.3/D.4 methodology. Instead, evaluate() now separately computes two flat
+/-10% spot-shock scenarios (using each leg's own unshocked IV, consistent
with Bug #3's fix) and reports them via stress_pnl_down_10pct /
stress_pnl_up_10pct -- visibility into whether a "certain" grid result
survives a real tail move, without pretending that's part of the
calibrated probability estimate. This is a visibility fix, same category as
the 2026-08-22 diagnostic fields (time_to_short_expiry_hours/sigma_move) --
it does not gate anything on its own; pricing/run_pricing.py's ranked
display surfaces it so a human can apply judgment, the same way it already
does for the P(profit) exact-0/1 split.

Note on scope: this session's two additions (GAP #1, GAP #2) are
deliberately independent of the Section M.2 MIN_NET_CREDIT/entry_eligible
gate implemented the same day (see pricing/run_pricing.py's
`is_entry_eligible`/`entry_eligible` column) -- that gate lives entirely in
run_pricing.py's application layer, not in this engine, and this file does
not duplicate or alter that logic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from scipy.stats import norm

from matching.schemas import MatchCandidate
from normalization.schemas import MarketSnapshot, OptionType
from pricing.black_scholes import OptionKind, black_scholes_price, settlement_payoff

# Underlying-price scenario grid, in standard-deviation units of the
# lognormal move over time-to-T1. 21 points spanning +/-3 sigma.
_PRICE_GRID_POINTS = 21
_PRICE_GRID_RANGE_SIGMA = 3.0
_PRICE_GRID_Z: tuple[float, ...] = tuple(
    -_PRICE_GRID_RANGE_SIGMA + (2 * _PRICE_GRID_RANGE_SIGMA) * i / (_PRICE_GRID_POINTS - 1)
    for i in range(_PRICE_GRID_POINTS)
)

# IV shock grid applied to the long leg's repricing at each price scenario.
_IV_SHOCK_GRID = (-0.30, 0.0, 0.30)

# Flat spot shocks explored OUTSIDE the normal grid/weighting, per GAP #2
# above -- deliberately not folded into expected_value/probability_of_profit.
_STRESS_SHOCKS: tuple[Decimal, Decimal] = (Decimal("-0.10"), Decimal("0.10"))

# Default minimum resting size (in contracts) required on the short leg's
# bid and the long leg's ask for a quote to be treated as genuinely
# executable, per GAP #1 above. 1 contract is a conservative floor used only
# when a caller doesn't override it -- pricing/run_pricing.py wires the real
# default from config.settings.RISK.min_liquidity.
_DEFAULT_MIN_CONTRACT_SIZE = Decimal("1")

_RISK_FREE_RATE = 0.0  # crypto options: no natural risk-free rate; treated as 0 per Section D.3's r term, documented rather than left implicit.


def _grid_weights(z_points: tuple[float, ...]) -> list[float]:
    """Discretized-normal weights for the given z-score grid points,
    normalized to sum to 1.0. A simple pdf-at-point-normalized-by-sum
    approximation, not a proper Gaussian quadrature."""
    raw = [norm.pdf(z) for z in z_points]
    total = sum(raw)
    return [w / total for w in raw]


@dataclass(frozen=True)
class EVResult:
    pair_id: str
    net_entry_cost: Decimal
    expected_value: Decimal
    probability_of_profit: Decimal
    worst_case_pnl: Decimal
    best_case_pnl: Decimal
    scenario_count: int
    short_bid_used: Decimal  # raw quoted premium (per-1-BTC terms), NOT scaled -- see short_bid_scaled
    long_ask_used: Decimal   # raw quoted premium (per-1-BTC terms), NOT scaled -- see long_ask_scaled
    short_bid_scaled: Decimal = Decimal("0")  # short_bid_used * short leg's contract_multiplier -- actual per-contract cost
    long_ask_scaled: Decimal = Decimal("0")   # long_ask_used * long leg's contract_multiplier -- actual per-contract cost
    fees_total: Decimal = Decimal("0")
    time_to_short_expiry_hours: float = 0.0
    sigma_move: float = 0.0
    base_iv_used: float = 0.0  # short leg's own quoted IV -- drives the underlying price-move grid (sigma_move)
    long_iv_used: float = 0.0  # long leg's own quoted IV -- drives long-leg repricing (Bug #3 fix, see module docstring)
    # GAP #1 -- resting size actually seen on each leg's relevant side of
    # book at evaluation time (short leg's bid_size, long leg's ask_size),
    # so a downstream consumer can see what depth backed the "executable"
    # check, not just that it passed.
    short_bid_size: Decimal | None = None
    long_ask_size: Decimal | None = None
    # GAP #2 -- flat +/-10% spot-shock P&L, computed with each leg's own
    # unshocked IV (consistent with Bug #3's fix), OUTSIDE the normal
    # grid/weighting -- see module docstring. Deliberately NOT part of
    # expected_value/probability_of_profit.
    stress_pnl_down_10pct: Decimal = Decimal("0")
    stress_pnl_up_10pct: Decimal = Decimal("0")
    model_notes: tuple[str, ...] = field(default_factory=lambda: (
        "Lean scenario grid (Section L.2, 21-point price grid x 3-point IV "
        "shock). ALL dollar figures (premiums, payoffs, repricing) are "
        "scaled by each leg's own contract_multiplier to real per-contract "
        "terms -- see pricing/ev_engine.py module docstring, Bug #2. "
        "Long-leg repricing uses the LONG leg's own quoted IV, not the "
        "short leg's -- see Bug #3 -- so genuine cross-leg IV divergence "
        "(the actual economic basis of this strategy) is priced in rather "
        "than silently assumed away.",
        "expected_value/probability_of_profit come ONLY from the 21x3 grid "
        "(+/-3 sigma lognormal, IV shocked +/-30%). stress_pnl_down_10pct "
        "and stress_pnl_up_10pct are a separate, flat +/-10% spot check, "
        "NOT folded into the probability estimate -- a candidate can show "
        "probability_of_profit=1.0 from the grid and still show a negative "
        "stress_pnl if the move needed to lose money sits outside the "
        "grid's +/-3 sigma range. Treat a negative stress figure as a "
        "reason for extra scrutiny, not a contradiction to be resolved.",
    ))
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InsufficientDataError(RuntimeError):
    """Raised when a candidate can't be priced -- e.g. no executable bid/ask,
    or executable but insufficient resting size (see GAP #1 in the module
    docstring). Per architecture.md's fail-closed principle: a pair with
    missing or insufficient data is excluded from results, never silently
    scored as zero or skipped without a trace."""


class LeanEVEngine:
    def __init__(
        self,
        short_taker_fee_pct: Decimal,
        long_taker_fee_pct: Decimal,
        risk_free_rate: float = _RISK_FREE_RATE,
        min_contract_size: Decimal = _DEFAULT_MIN_CONTRACT_SIZE,
    ) -> None:
        self._short_fee_pct = short_taker_fee_pct
        self._long_fee_pct = long_taker_fee_pct
        self._risk_free_rate = risk_free_rate
        self._min_contract_size = min_contract_size

    def evaluate(
        self,
        candidate: MatchCandidate,
        short_snapshot: MarketSnapshot,
        long_snapshot: MarketSnapshot,
    ) -> EVResult:
        """
        Per Section D.2: this REQUIRES executable bid/ask on both legs. A
        snapshot without both is not scored -- raises InsufficientDataError
        rather than falling back to mark price, per the executable-price-only
        principle in Section A.1.

        Per GAP #1 (module docstring): also REQUIRES at least
        `min_contract_size` of resting size on the short leg's bid and the
        long leg's ask. A quote that exists but is too thin to actually fill
        at is treated the same as a missing quote -- fail closed, not scored.
        """
        if not short_snapshot.is_executable() or not long_snapshot.is_executable():
            raise InsufficientDataError(
                f"{candidate.pair_id}: missing executable bid/ask on one or both legs "
                f"(short executable={short_snapshot.is_executable()}, "
                f"long executable={long_snapshot.is_executable()}). Per Section A.1, "
                f"mark price is never substituted for a missing bid/ask."
            )

        short_bid_size = short_snapshot.bid_size
        long_ask_size = long_snapshot.ask_size
        if (
            short_bid_size is None
            or long_ask_size is None
            or short_bid_size < self._min_contract_size
            or long_ask_size < self._min_contract_size
        ):
            raise InsufficientDataError(
                f"{candidate.pair_id}: insufficient resting size for min_contract_size="
                f"{self._min_contract_size} (short leg bid_size={short_bid_size}, "
                f"long leg ask_size={long_ask_size}). A top-of-book quote that exists "
                f"but can't be filled at the intended size is not treated as executable -- "
                f"see GAP #1 in pricing/ev_engine.py's module docstring."
            )

        short = candidate.short_contract
        long_ = candidate.long_contract

        short_bid_raw = short_snapshot.best_bid
        long_ask_raw = long_snapshot.best_ask

        # -- Section D.2: entry economics --------------------------------------
        # BUG #2 FIX: short_bid_raw/long_ask_raw are quoted in raw per-1-BTC
        # terms (same scale as spot/strike) per the diagnose_pair.py finding
        # in the module docstring -- NOT already scaled to one contract's
        # notional. Scale each leg's premium by its OWN contract_multiplier
        # before computing entry economics, matching the treatment already
        # applied to short_payoff/v_long below.
        short_bid = short_bid_raw * short.contract_multiplier
        long_ask = long_ask_raw * long_.contract_multiplier

        gross_entry_credit = short_bid - long_ask
        short_fee = short_bid * self._short_fee_pct
        long_fee = long_ask * self._long_fee_pct
        fees_total = short_fee + long_fee
        net_entry_cost = gross_entry_credit - fees_total

        # -- Section D.3/D.4: scenario grid over price x IV shock --------------

        now = datetime.now(timezone.utc)
        time_to_T1_years = max(
            (short.expiry_timestamp - now).total_seconds() / (365 * 24 * 3600), 0.0
        )
        time_to_T2_at_T1_years = max(
            (long_.expiry_timestamp - short.expiry_timestamp).total_seconds() / (365 * 24 * 3600), 0.0
        )

        # BUG #3 FIX: short and long legs now each use THEIR OWN quoted IV,
        # not one value silently reused for both. short_base_iv drives the
        # underlying price-move grid (sigma_move, below); long_base_iv drives
        # the long leg's own Black-Scholes repricing. Falling back to the
        # short leg's IV only when the long snapshot genuinely has none is
        # strictly better than the previous unconditional reuse -- it's a
        # last-resort fallback, not the primary source.
        short_base_iv = float(short_snapshot.iv) if short_snapshot.iv is not None and short_snapshot.iv > 0 else 0.80
        long_base_iv = float(long_snapshot.iv) if long_snapshot.iv is not None and long_snapshot.iv > 0 else short_base_iv

        spot_now = short_snapshot.underlying_spot or short_snapshot.underlying_index
        if spot_now is None:
            raise InsufficientDataError(
                f"{candidate.pair_id}: no underlying spot/index price available on the "
                f"short leg's snapshot -- cannot build the price scenario grid."
            )

        sigma_move = short_base_iv * math.sqrt(time_to_T1_years) if time_to_T1_years > 0 else 0.0
        price_weights = _grid_weights(_PRICE_GRID_Z)

        short_kind = OptionKind.CALL if short.option_type == OptionType.CALL else OptionKind.PUT
        long_kind = OptionKind.CALL if long_.option_type == OptionType.CALL else OptionKind.PUT

        pnl_scenarios: list[tuple[Decimal, float]] = []

        for z, p_weight in zip(_PRICE_GRID_Z, price_weights):
            move_factor = math.exp(z * sigma_move - 0.5 * sigma_move**2) if sigma_move > 0 else 1.0
            s_t1 = spot_now * Decimal(str(move_factor))

            short_payoff_raw = settlement_payoff(s_t1, short.strike, short_kind)
            short_payoff = short_payoff_raw * short.contract_multiplier

            for iv_shock, iv_weight in zip(_IV_SHOCK_GRID, [1 / len(_IV_SHOCK_GRID)] * len(_IV_SHOCK_GRID)):
                # BUG #3 FIX: was base_iv (short leg's IV, wrong for this
                # purpose) -- now long_base_iv, the long leg's OWN quoted IV.
                sigma_at_t1 = max(long_base_iv * (1 + iv_shock), 0.01)
                v_long_raw = black_scholes_price(
                    spot=s_t1,
                    strike=long_.strike,
                    time_to_expiry_years=time_to_T2_at_T1_years,
                    volatility=sigma_at_t1,
                    risk_free_rate=self._risk_free_rate,
                    option_kind=long_kind,
                )
                v_long = v_long_raw * long_.contract_multiplier
                exit_fee = v_long * self._long_fee_pct

                pnl = net_entry_cost - short_payoff + v_long - exit_fee
                combined_weight = p_weight * iv_weight
                pnl_scenarios.append((pnl, combined_weight))

        total_weight = sum(w for _, w in pnl_scenarios)
        expected_value = sum(pnl * Decimal(str(w)) for pnl, w in pnl_scenarios) / Decimal(str(total_weight))
        profitable_weight = sum(w for pnl, w in pnl_scenarios if pnl > 0)
        probability_of_profit = Decimal(str(profitable_weight / total_weight))
        worst_case = min(pnl for pnl, _ in pnl_scenarios)
        best_case = max(pnl for pnl, _ in pnl_scenarios)

        # -- GAP #2: flat +/-10% stress check, OUTSIDE the grid/weighting ------
        # Uses each leg's own unshocked IV (consistent with Bug #3's fix) --
        # this isn't meant to explore IV uncertainty, just "what if the price
        # move itself is bigger than the grid's +/-3 sigma range assumed."
        stress_pnls: dict[Decimal, Decimal] = {}
        for shock in _STRESS_SHOCKS:
            s_t1_stress = spot_now * (Decimal("1") + shock)
            short_payoff_stress = settlement_payoff(s_t1_stress, short.strike, short_kind) * short.contract_multiplier
            v_long_stress_raw = black_scholes_price(
                spot=s_t1_stress,
                strike=long_.strike,
                time_to_expiry_years=time_to_T2_at_T1_years,
                volatility=long_base_iv,
                risk_free_rate=self._risk_free_rate,
                option_kind=long_kind,
            )
            v_long_stress = v_long_stress_raw * long_.contract_multiplier
            exit_fee_stress = v_long_stress * self._long_fee_pct
            stress_pnls[shock] = net_entry_cost - short_payoff_stress + v_long_stress - exit_fee_stress

        return EVResult(
            pair_id=candidate.pair_id,
            net_entry_cost=net_entry_cost,
            expected_value=expected_value,
            probability_of_profit=probability_of_profit,
            worst_case_pnl=worst_case,
            best_case_pnl=best_case,
            scenario_count=len(pnl_scenarios),
            short_bid_used=short_bid_raw,
            long_ask_used=long_ask_raw,
            short_bid_scaled=short_bid,
            long_ask_scaled=long_ask,
            fees_total=fees_total,
            time_to_short_expiry_hours=time_to_T1_years * 365 * 24,
            sigma_move=sigma_move,
            base_iv_used=short_base_iv,
            long_iv_used=long_base_iv,
            short_bid_size=short_bid_size,
            long_ask_size=long_ask_size,
            stress_pnl_down_10pct=stress_pnls[Decimal("-0.10")],
            stress_pnl_up_10pct=stress_pnls[Decimal("0.10")],
        )
