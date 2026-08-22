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
RESOLUTION FIX" below for why 21, not the original 5) with a small IV-shock
grid (3 points).

WHAT THIS DOES NOT DO (be honest about the gap vs. v1's full plan):
  - Does not fit sigma_effective_at_T1 to historical IV term-structure
    behavior -- uses today's observed IV as the base and a fixed +/-30%
    shock band around it.
  - Does not model IV smile/skew across strikes.
  - Scenario weights are a discretized-normal approximation, not a properly
    calibrated risk-neutral distribution -- even at 21 points, this is still
    a deterministic grid, not a real Monte Carlo or closed-form probability.
  - Does not account for legging risk, slippage beyond a flat assumption,
    or partial fills -- those are Phase 8/9 concerns.
Every EVResult carries a `model_notes` field stating this explicitly.

BUG FOUND + FIXED post-Phase-5-smoke-run (2026-08-21): contract_multiplier
was loaded but never applied to short_payoff/v_long, mixing "dollars per
contract" (real quoted premiums) with "dollars per 1 BTC" (raw intrinsic/BS
values) -- roughly a 1/contract_multiplier scale error. Fixed by scaling
each leg's payoff/value by its OWN contract_multiplier before combining with
net_entry_cost. See tests/test_ev_engine.py::TestLeanEVEngineUnitConsistency.

GRID RESOLUTION FIX (2026-08-22): after the multiplier fix, the first real
re-run against all priced candidates (330 of 1,504 had live executable data)
showed `P(profit)` landing at EXACTLY 1.0 or EXACTLY 0.0 for all 330 --
zero candidates with a probability strictly between 0 and 1. Root cause:
these are short-dated options (6-150 hours to expiry), so
sigma_move = IV * sqrt(time_to_T1) is naturally small (observed range:
~0.01-0.12, i.e. a 1-12% one-std price move). The original 5-point grid
(z in {-2,-1,0,1,2}) sampled too coarsely relative to that narrow range to
ever land a scenario near a given candidate's actual payoff breakeven --
every one of the 5x3=15 scenarios for a given candidate ended up on the same
side of profitable/unprofitable, so the weighted probability collapsed to
0 or 1 regardless of how close the true (continuous) probability actually
was. This is NOT the same class of bug as the multiplier issue -- it's a
disclosed lean-model limitation (see docstring above: "discretized-normal
approximation, not a properly calibrated ... distribution") that turned out
to bite harder than expected on real short-dated data. Fix: widened the
price grid from 5 to 21 points (z from -3.0 to 3.0, evenly spaced) so
scenarios land close enough to a candidate's breakeven to produce genuine
fractional probabilities. This does NOT make the model a real Monte Carlo
or closed-form probability -- it is still a discrete approximation, now
just finer. See tests/test_ev_engine.py::TestGridResolution for the
regression test that would have caught the original coarse-grid collapse.
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
# lognormal move over time-to-T1. 21 points spanning +/-3 sigma -- widened
# from an original 5-point grid per the "GRID RESOLUTION FIX" note above.
# Still a discrete approximation of a continuous distribution, not a real
# Monte Carlo run -- just fine enough not to collapse every short-dated
# candidate's probability to exactly 0 or 1.
_PRICE_GRID_POINTS = 21
_PRICE_GRID_RANGE_SIGMA = 3.0
_PRICE_GRID_Z: tuple[float, ...] = tuple(
    -_PRICE_GRID_RANGE_SIGMA + (2 * _PRICE_GRID_RANGE_SIGMA) * i / (_PRICE_GRID_POINTS - 1)
    for i in range(_PRICE_GRID_POINTS)
)

# IV shock grid applied to the long leg's repricing at each price scenario.
_IV_SHOCK_GRID = (-0.30, 0.0, 0.30)

_RISK_FREE_RATE = 0.0  # crypto options: no natural risk-free rate; treated as 0 per Section D.3's r term, documented rather than left implicit.


def _grid_weights(z_points: tuple[float, ...]) -> list[float]:
    """
    Discretized-normal weights for the given z-score grid points, normalized
    to sum to 1.0. This is a simple pdf-at-point-normalized-by-sum
    approximation, not a proper Gaussian quadrature -- adequate at 21 points
    for a lean model, not something to rely on for precise tail-risk (VaR/ES)
    figures without further validation.
    """
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
    short_bid_used: Decimal
    long_ask_used: Decimal
    fees_total: Decimal
    # --- Diagnostic fields -------------------------------
    # Not used in the P&L math itself -- these exist so a caller (e.g.
    # run_pricing.py's ranked printout) can tell WHY a result landed where
    # it did, instead of guessing.
    time_to_short_expiry_hours: float = 0.0
    sigma_move: float = 0.0
    base_iv_used: float = 0.0
    model_notes: tuple[str, ...] = field(default_factory=lambda: (
        "Lean scenario grid (Section L.2, 21-point price grid x 3-point IV "
        "shock), not a full Monte Carlo or historical-IV-fitted model -- see "
        "pricing/ev_engine.py module docstring for exactly what's "
        "simplified. Payoff/repricing terms are scaled by each leg's own "
        "contract_multiplier so they're in the same per-contract units as "
        "the quoted premiums.",
    ))
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InsufficientDataError(RuntimeError):
    """Raised when a candidate can't be priced -- e.g. no executable bid/ask.
    Per architecture.md's fail-closed principle: a pair with missing data is
    excluded from results, never silently scored as zero or skipped without
    a trace."""


class LeanEVEngine:
    def __init__(
        self,
        short_taker_fee_pct: Decimal,
        long_taker_fee_pct: Decimal,
        risk_free_rate: float = _RISK_FREE_RATE,
    ) -> None:
        self._short_fee_pct = short_taker_fee_pct
        self._long_fee_pct = long_taker_fee_pct
        self._risk_free_rate = risk_free_rate

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
        """
        if not short_snapshot.is_executable() or not long_snapshot.is_executable():
            raise InsufficientDataError(
                f"{candidate.pair_id}: missing executable bid/ask on one or both legs "
                f"(short executable={short_snapshot.is_executable()}, "
                f"long executable={long_snapshot.is_executable()}). Per Section A.1, "
                f"mark price is never substituted for a missing bid/ask."
            )

        short_bid = short_snapshot.best_bid
        long_ask = long_snapshot.best_ask

        # -- Section D.2: entry economics --------------------------------------
        # short_bid/long_ask are real exchange-quoted premiums -- already
        # scaled to one contract's notional by the exchange itself. Nothing
        # here needs contract_multiplier applied a second time.

        gross_entry_credit = short_bid - long_ask
        short_fee = short_bid * self._short_fee_pct
        long_fee = long_ask * self._long_fee_pct
        fees_total = short_fee + long_fee
        net_entry_cost = gross_entry_credit - fees_total

        # -- Section D.3/D.4: scenario grid over price x IV shock --------------

        short = candidate.short_contract
        long_ = candidate.long_contract

        # Time to T1 (short expiry), in years, from now -- this determines
        # both the price-move magnitude for the grid and how much life the
        # long leg has left at T1.
        now = datetime.now(timezone.utc)
        time_to_T1_years = max(
            (short.expiry_timestamp - now).total_seconds() / (365 * 24 * 3600), 0.0
        )
        time_to_T2_at_T1_years = max(
            (long_.expiry_timestamp - short.expiry_timestamp).total_seconds() / (365 * 24 * 3600), 0.0
        )

        # Use the short leg's own observed IV as the move-size estimate for
        # the price grid -- if IV is missing (Delta didn't return one),
        # fall back to a conservative flat 80% annualized vol assumption for
        # crypto options rather than dividing by zero or crashing the batch.
        base_iv = float(short_snapshot.iv) if short_snapshot.iv is not None and short_snapshot.iv > 0 else 0.80

        spot_now = short_snapshot.underlying_spot or short_snapshot.underlying_index
        if spot_now is None:
            raise InsufficientDataError(
                f"{candidate.pair_id}: no underlying spot/index price available on the "
                f"short leg's snapshot -- cannot build the price scenario grid."
            )

        sigma_move = base_iv * math.sqrt(time_to_T1_years) if time_to_T1_years > 0 else 0.0
        price_weights = _grid_weights(_PRICE_GRID_Z)

        short_kind = OptionKind.CALL if short.option_type == OptionType.CALL else OptionKind.PUT
        long_kind = OptionKind.CALL if long_.option_type == OptionType.CALL else OptionKind.PUT

        pnl_scenarios: list[tuple[Decimal, float]] = []  # (pnl, combined_weight)

        for z, p_weight in zip(_PRICE_GRID_Z, price_weights):
            # Lognormal price move: S_T1 = S_now * exp(z * sigma_move - 0.5*sigma_move^2)
            move_factor = math.exp(z * sigma_move - 0.5 * sigma_move**2) if sigma_move > 0 else 1.0
            s_t1 = spot_now * Decimal(str(move_factor))

            short_payoff_raw = settlement_payoff(s_t1, short.strike, short_kind)
            short_payoff = short_payoff_raw * short.contract_multiplier

            for iv_shock, iv_weight in zip(_IV_SHOCK_GRID, [1 / len(_IV_SHOCK_GRID)] * len(_IV_SHOCK_GRID)):
                sigma_at_t1 = max(base_iv * (1 + iv_shock), 0.01)
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

        return EVResult(
            pair_id=candidate.pair_id,
            net_entry_cost=net_entry_cost,
            expected_value=expected_value,
            probability_of_profit=probability_of_profit,
            worst_case_pnl=worst_case,
            best_case_pnl=best_case,
            scenario_count=len(pnl_scenarios),
            short_bid_used=short_bid,
            long_ask_used=long_ask,
            fees_total=fees_total,
            time_to_short_expiry_hours=time_to_T1_years * 365 * 24,
            sigma_move=sigma_move,
            base_iv_used=base_iv,
        )
