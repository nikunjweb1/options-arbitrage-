"""
Black-Scholes European option pricing -- used by the lean EV engine
(pricing/ev_engine.py) to reprice the long leg at T1 under repricing
scenarios, per docs/architecture.md Section D.3.

Per the same section: Delta's options are cash-settled and European, so
Black-Scholes is the correct model per contract_spec.settlement_method /
is_european. If a future exchange or contract type is futures-margined
(Black-76) or American-style, this module is NOT the right pricer for it --
callers must check is_european before using this, not assume it.

All inputs/outputs at the public function boundary use Decimal (consistent
with the rest of this codebase's "never use float for money" convention);
internally this converts to float for the actual math, since scipy/math
don't operate on Decimal and options pricing precision doesn't need
Decimal's exactness the way ledger arithmetic does.
"""

from __future__ import annotations

import math
from decimal import Decimal
from enum import Enum

from scipy.stats import norm


class OptionKind(str, Enum):
    CALL = "call"
    PUT = "put"


def black_scholes_price(
    spot: Decimal,
    strike: Decimal,
    time_to_expiry_years: float,
    volatility: float,
    risk_free_rate: float,
    option_kind: OptionKind,
) -> Decimal:
    """
    Standard Black-Scholes closed-form price for a European option.

    Edge cases handled explicitly rather than left to blow up on a live run:
      - time_to_expiry_years <= 0: returns intrinsic value (option has expired
        or is at expiry in this scenario).
      - volatility <= 0: returns intrinsic value (a zero/negative vol input
        is degenerate for the log-normal model; treating it as "no time
        value" is the conservative choice for an EV calculation rather than
        raising and aborting the whole batch).
    """
    S = float(spot)
    K = float(strike)
    T = time_to_expiry_years
    sigma = volatility
    r = risk_free_rate

    intrinsic = max(S - K, 0.0) if option_kind == OptionKind.CALL else max(K - S, 0.0)

    if T <= 0 or sigma <= 0:
        return Decimal(str(intrinsic))

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if option_kind == OptionKind.CALL:
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    # Price can't be negative (deep OTM numerical noise can produce a tiny
    # negative value) or below intrinsic (numerical edge case near T->0) --
    # clamp rather than let a downstream EV calculation silently go wrong.
    price = max(price, intrinsic, 0.0)
    return Decimal(str(round(price, 8)))


def settlement_payoff(spot_at_settlement: Decimal, strike: Decimal, option_kind: OptionKind) -> Decimal:
    """
    The actual cash settlement payoff at expiry -- per
    docs/architecture.md Section D.4 (Short_payoff), and matches Delta's
    documented formula (Section B): max(index - strike, 0) for calls,
    mirrored for puts. This is NOT the same function as black_scholes_price
    with T=0 by coincidence -- it's the same formula because a European
    option's terminal payoff *is* its intrinsic value, which is what
    black_scholes_price(T<=0) already returns. Kept as a separate,
    explicitly-named function so call sites are unambiguous about which
    concept (settlement payoff vs. pre-expiry fair value) they're computing.
    """
    if option_kind == OptionKind.CALL:
        return max(spot_at_settlement - strike, Decimal("0"))
    return max(strike - spot_at_settlement, Decimal("0"))
