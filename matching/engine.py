"""
Contract matching engine (Phase 3).

Scope per docs/architecture.md Section I, Phase 3: self-matching Delta's own
D1 vs D2 vs weekly option chains first -- i.e. pairing contracts on the SAME
exchange, same underlying, same option type, and (for now) the same strike,
across different expiries. Cross-exchange matching is explicitly out of
scope until this self-matching case is proven correct, per the project's
"no phase begins until the prior phase's exit criteria are met" rule.

Every pairing decision runs through the seven structural checks in
docs/architecture.md Section C, in order, and EVERY outcome -- accepted or
rejected -- is recorded. A matcher that only reports acceptances gives no
way to audit *why* something didn't match without re-running it by hand
(see matching/schemas.py's module docstring for the same point).

The short/long convention throughout: "short" = the earlier-expiring leg,
"long" = the later-expiring leg, per docs/architecture.md Section D.1.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from matching.schemas import (
    Classification,
    MatchCandidate,
    RejectedPair,
    RejectionReason,
)
from normalization.schemas import OptionContract


@dataclass(frozen=True)
class MatchResult:
    """Everything the engine produced from one run over one contract set."""

    candidates: list[MatchCandidate]
    rejections: list[RejectedPair]


class MatchingEngine:
    """
    Self-matching engine for a single exchange's own option chain.

    strike_tolerance_pct controls how far apart two strikes may be and still
    be considered a match: 0 (the default) means "same exchange, same strike
    grid -- require an exact match," which is the correct default for
    self-matching within one exchange (Phase 3's actual scope). A nonzero
    tolerance exists for later phases (cross-exchange matching, where
    strikes may never align exactly) and produces a downgraded
    match_confidence rather than a full-confidence match -- see
    docs/architecture.md Section C.6, which explicitly says never assume
    contract-count equivalence and flag anything that isn't an exact
    structural match.
    """

    def __init__(self, strike_tolerance_pct: Decimal = Decimal("0")) -> None:
        if strike_tolerance_pct < 0:
            raise ValueError("strike_tolerance_pct must be >= 0")
        self._strike_tolerance_pct = strike_tolerance_pct

    # -- public API -----------------------------------------------------

    def find_matches(self, contracts: list[OptionContract]) -> MatchResult:
        """
        Evaluates every unordered pair of contracts exactly once and returns
        both accepted candidates and recorded rejections. O(n^2) in the
        number of contracts, which is fine at Phase 3's scope (a single
        exchange's chain for one or two underlyings); revisit if this ever
        needs to run over a much larger combined instrument set.
        """
        candidates: list[MatchCandidate] = []
        rejections: list[RejectedPair] = []

        for contract_a, contract_b in itertools.combinations(contracts, 2):
            outcome = self._evaluate_pair(contract_a, contract_b)
            if isinstance(outcome, MatchCandidate):
                candidates.append(outcome)
            else:
                rejections.append(outcome)

        return MatchResult(candidates=candidates, rejections=rejections)

    # -- internal: the actual structural checks --------------------------

    def _evaluate_pair(
        self, contract_a: OptionContract, contract_b: OptionContract
    ) -> MatchCandidate | RejectedPair:
        # Guard against matching a contract with itself. Two distinct
        # OptionContract records that happen to carry the same
        # instrument_id would indicate a data problem upstream, not a real
        # pair -- reject either way rather than silently accepting or
        # crashing on a degenerate expiry_gap of zero.
        if contract_a.instrument_id == contract_b.instrument_id:
            return self._reject(
                contract_a,
                contract_b,
                RejectionReason.IDENTICAL_INSTRUMENT,
                "Both contracts share the same instrument_id -- not a real pair.",
            )

        # Section C.1 (structural prerequisite): same underlying only.
        if contract_a.underlying != contract_b.underlying:
            return self._reject(
                contract_a,
                contract_b,
                RejectionReason.DIFFERENT_UNDERLYING,
                f"{contract_a.underlying!r} vs {contract_b.underlying!r}.",
            )

        # Structural prerequisite: this engine pairs calls-with-calls and
        # puts-with-puts only (a calendar spread on one option type). A
        # call-vs-put relationship is a different structure entirely
        # (synthetic/parity -- Section D.5) and is explicitly out of scope
        # for Phase 3's self-matching case.
        if contract_a.option_type != contract_b.option_type:
            return self._reject(
                contract_a,
                contract_b,
                RejectionReason.DIFFERENT_OPTION_TYPE,
                f"{contract_a.option_type.value} vs {contract_b.option_type.value}.",
            )

        # Section C.5: never assume vanilla by default. A vanilla European
        # contract and a Turbo (knockout) contract must never be paired even
        # if strike/expiry/underlying look identical.
        if contract_a.option_variant != contract_b.option_variant:
            return self._reject(
                contract_a,
                contract_b,
                RejectionReason.DIFFERENT_OPTION_VARIANT,
                f"{contract_a.option_variant.value} vs {contract_b.option_variant.value}.",
            )

        # Section C: settlement method (cash vs physical) must match.
        if contract_a.settlement_method != contract_b.settlement_method:
            return self._reject(
                contract_a,
                contract_b,
                RejectionReason.DIFFERENT_SETTLEMENT_METHOD,
                f"{contract_a.settlement_method.value} vs {contract_b.settlement_method.value}.",
            )

        # Section C.2: settlement price basis. Two "same strike" options
        # priced off different settlement formulas (e.g. 30-min TWAP vs
        # last-traded-price) can have materially different payoff
        # distributions even at an identical strike -- this alone can
        # manufacture a fake edge, so it's a hard reject, not a confidence
        # penalty.
        if contract_a.settlement_price_formula != contract_b.settlement_price_formula:
            return self._reject(
                contract_a,
                contract_b,
                RejectionReason.DIFFERENT_SETTLEMENT_FORMULA,
                f"{contract_a.settlement_price_formula!r} vs {contract_b.settlement_price_formula!r}.",
            )

        # Section C.6: strike compatibility. Exact match (the Phase 3
        # default) gets full confidence; a nonzero tolerance allows a
        # near-match at reduced confidence, but strikes further apart than
        # the configured tolerance are rejected outright rather than
        # silently treated as equivalent.
        strike_diff = abs(contract_a.strike - contract_b.strike)
        reference_strike = max(contract_a.strike, contract_b.strike)
        strike_diff_pct = (
            (strike_diff / reference_strike) if reference_strike != 0 else Decimal("0")
        )
        if strike_diff_pct > self._strike_tolerance_pct:
            return self._reject(
                contract_a,
                contract_b,
                RejectionReason.STRIKE_OUT_OF_TOLERANCE,
                f"strike diff {strike_diff} ({strike_diff_pct:.4%}) exceeds "
                f"tolerance {self._strike_tolerance_pct:.4%}.",
            )

        # Determine short/long by expiry. Per D.1, the short leg must
        # genuinely expire before the long leg -- an identical expiry
        # timestamp gives no time advantage to exploit and isn't a calendar
        # structure at all.
        if contract_a.expiry_timestamp == contract_b.expiry_timestamp:
            return self._reject(
                contract_a,
                contract_b,
                RejectionReason.SAME_EXPIRY_NO_TIME_ADVANTAGE,
                f"both expire at {contract_a.expiry_timestamp.isoformat()}.",
            )
        elif contract_a.expiry_timestamp < contract_b.expiry_timestamp:
            short_contract, long_contract = contract_a, contract_b
        else:
            short_contract, long_contract = contract_b, contract_a

        # All structural checks passed -- build the accepted candidate.
        match_confidence = (
            Decimal("1.0")
            if strike_diff == 0
            else (Decimal("1.0") - (strike_diff_pct / self._strike_tolerance_pct) * Decimal("0.5"))
        )

        classification = self._classify(short_contract, long_contract)

        expiry_gap: timedelta = long_contract.expiry_timestamp - short_contract.expiry_timestamp

        return MatchCandidate(
            pair_id=f"{short_contract.instrument_id}::{long_contract.instrument_id}",
            short_contract=short_contract,
            long_contract=long_contract,
            match_confidence=match_confidence,
            classification=classification,
            strike_diff=strike_diff,
            expiry_gap=expiry_gap,
            same_exchange=(short_contract.exchange == long_contract.exchange),
            notes=(
                f"strike_diff_pct={strike_diff_pct:.6%}",
                f"expiry_gap={expiry_gap}",
            ),
        )

    @staticmethod
    def _classify(
        short_contract: OptionContract, long_contract: OptionContract
    ) -> Classification:
        """
        Per docs/architecture.md Section D.5: never default to a specific
        label -- earn it by ruling out structural alternatives. Phase 3's
        scope is self-matching within one exchange, so every accepted pair
        here is, by construction (all seven structural checks already
        passed and same_exchange is true), a same-exchange calendar
        structure. Cross-exchange classification logic (expiry arbitrage
        vs. calendar spread vs. relative value) is Phase 4+ scope and
        deliberately not implemented here -- returning UNCLASSIFIED for a
        cross-exchange pair rather than guessing would be the correct
        behavior once that path exists, but Phase 3 never produces one.
        """
        if short_contract.exchange == long_contract.exchange:
            return Classification.SAME_EXCHANGE_CALENDAR_SPREAD
        return Classification.UNCLASSIFIED

    @staticmethod
    def _reject(
        contract_a: OptionContract,
        contract_b: OptionContract,
        reason: RejectionReason,
        detail: str,
    ) -> RejectedPair:
        return RejectedPair(
            contract_a_id=contract_a.instrument_id,
            contract_b_id=contract_b.instrument_id,
            reason=reason,
            detail=detail,
        )
