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
this engine crosses a small underlying-price grid (5 points, discretized
from a lognormal move over the time-to-T1 using the short leg's own IV as
the move-size estimate) with a small IV-shock grid (3 points) -- 15
scenarios total. This is still far cheaper than a real Monte Carlo (Section
D.4/Phase 5 v1 called for thousands of paths) but keeps the one relationship
that actually matters: short-leg payoff depends on where the underlying
actually goes.

WHAT THIS DOES NOT DO (be honest about the gap vs. v1's full plan):
  - Does not fit sigma_effective_at_T1 to historical IV term-structure
    behavior -- uses today's observed IV as the base and a fixed +/-30%
    shock band around it.
  - Does not model IV smile/skew across strikes.
  - Scenario weights are a discretized-normal approximation, not a properly
    calibrated risk-neutral distribution.
  - Does not account for legging risk, slippage beyond a flat assumption,
    or partial fills -- those are Phase 8/9 concerns (execution engine +
    risk engine), not this pricing step.
Every EVResult carries a `model_notes` field stating this explicitly so a
result is never mistaken for more rigorous than it is.

BUG FOUND + FIXED post-Phase-5-smoke-run (2026-08-21): settlement_payoff()
and black_scholes_price() both operate in raw underlying-price terms (e.g.
dollars per 1 BTC) -- they have no notion of contract size. short_bid and
long_ask, by contrast, are real exchange-quoted premiums, which are already
scaled to one contract's notional via OptionContract.contract_multiplier
(e.g. 0.001 BTC/contract on Delta). Before this fix, short_payoff and
v_long were combined directly with net_entry_cost with no multiplier
applied, mixing "dollars per contract" with "dollars per 1 BTC" -- a scale
error of roughly 1/contract_multiplier. Two symptoms in the first live run
against all 1,504 candidates traced back to exactly this:
  1. EV magnitudes far too large relative to net entry cost (e.g. EV=7613
     against a net entry cost of -1505) -- the unscaled payoff/repricing
     terms dominated the correctly-scaled entry economics.
  2. P(profit)=1.0 exactly, repeatedly, for the top-ranked (mostly
     exact-strike, same-exchange calendar spread) candidates. For an
     exact-strike calendar spread, short_payoff and the long leg's intrinsic
     value at T1 are identical, so (v_long - short_payoff) is just the long
     leg's remaining time value -- structurally >= 0 for any European option
     with positive time-to-expiry. Left unscaled, that always-non-negative
     term swamped net_entry_cost in every one of the 15 grid scenarios,
     making every exact-strike candidate look risk-free. That is a modeling
     artifact, not a real edge.
Fix: multiply short_payoff by short_contract.contract_multiplier and
v_long by long_contract.contract_multiplier before combining them with
net_entry_cost, so every term in the P&L formula is in the same units
(dollars per contract). See tests/test_ev_engine.py::TestLeanEVEngineUnitConsistency
for the regression test that would have caught this.
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
# lognormal move over time-to-T1. 5 points is a coarse discretization of a
# continuous distribution -- documented as a lean-plan simplification, not
# presented as equivalent to a real Monte Carlo run.
_PRICE_GRID_Z = (-2.0, -1.0, 0.0, 1.0, 2.0)

# IV shock grid applied to the long leg's repricing at each price scenario.
_IV_SHOCK_GRID = (-0.30, 0.0, 0.30)

_RISK_FREE_RATE = 0.0  # crypto options: no natural risk-free rate; treated as 0 per Section D.3's r term, documented rather than left implicit.


def _grid_weights(z_points: tuple[float, ...]) -> list[float]:
    """
    Discretized-normal weights for the given z-score grid points, normalized
    to sum to 1.0. This is a simple pdf-at-point-normalized-by-sum
    approximation, not a proper Gaussian quadrature -- adequate for a lean
    5-point grid, not something to rely on for tail-risk precision.
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
    # --- Diagnostic fields (added 2026-08-22) -------------------------------
    # Not used in the P&L math itself -- these exist so a caller (e.g.
    # run_pricing.py's ranked printout) can tell WHY a result landed at
    # P(profit)=1.0 or 0.0 exactly, instead of guessing. A hard 100%/0% split
    # with nothing in between across many candidates is a signal worth
    # checking, not necessarily a bug -- these fields make that checkable.
    time_to_short_expiry_hours: float = 0.0
    # sigma_move is the fractional 1-standard-deviation price move the price
    # grid explored over time_to_short_expiry_hours (base_iv * sqrt(T)). If
    # this is tiny (near-zero time-to-expiry), the +/-2 sigma grid barely
    # moves the price at all, so the scenario grid degenerates toward a
    # single point and can't discover a losing (or winning) scenario even if
    # one exists -- that's a modeling-resolution gap, not evidence the trade
    # itself is risk-free or hopeless.
    sigma_move: float = 0.0
    base_iv_used: float = 0.0
    model_notes: tuple[str, ...] = field(default_factory=lambda: (
        "Lean scenario grid (Section L.2), not a full Monte Carlo or "
        "historical-IV-fitted model -- see pricing/ev_engine.py module "
        "docstring for exactly what's simplified. Payoff/repricing terms "
        "are scaled by each leg's own contract_multiplier so they're in "
        "the same per-contract units as the quoted premiums.",
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
            # (the drift-adjustment term keeps E[S_T1] ~= S_now under this
            # discretization, consistent with a risk-neutral-ish assumption --
            # again, a simplification, not a calibrated forward price).
            move_factor = math.exp(z * sigma_move - 0.5 * sigma_move**2) if sigma_move > 0 else 1.0
            s_t1 = spot_now * Decimal(str(move_factor))

            # settlement_payoff() returns the payoff per 1 unit of underlying
            # (e.g. per 1 BTC) -- scale by the short leg's own
            # contract_multiplier so it's in the same "per contract" units as
            # short_bid/net_entry_cost. See module docstring's BUG FOUND note.
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
                # Same unit fix as short_payoff above, using the long leg's
                # own contract_multiplier (in practice equal to the short
                # leg's for a same-underlying calendar spread, but each leg
                # is scaled by its own contract spec rather than assuming
                # they match, per architecture.md's adapter-isolation rule).
                v_long = v_long_raw * long_.contract_multiplier

                # Exit fee on the long leg's repriced value at T1 -- short
                # leg's fee already accounted for in net_entry_cost (Section
                # D.2); this exit_fee is Section D.4's "exit_fees(long)" term.
                # Computed from the already-scaled v_long so the fee itself
                # is in per-contract dollars too.
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
