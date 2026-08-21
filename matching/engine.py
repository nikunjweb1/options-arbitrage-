"""
Phase 3: Contract matching engine.

Per docs/architecture.md Section 6 and Section C: takes a flat list of
normalized OptionContract records (which may span multiple exchanges, or --
per the Phase 2 MVP, Section J -- be entirely from one exchange for
same-exchange calendar-spread self-matching) and produces MatchCandidate
pairs, or explicit RejectedPair records with a reason.

Nothing here computes P&L, EV, or Greeks -- that's Phase 5. This module's
only job is: "are these two contracts even a legitimate pair to consider,
and if so how confident are we, and what kind of relationship is this."

Every check below maps directly to a numbered item in
docs/architecture.md Section C ("Contract specification comparison") or
Section 6 ("Contract matching engine") -- comments reference which.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal

from matching.schemas import Classification, MatchCandidate, RejectedPair, RejectionReason
from normalization.schemas import OptionContract, SettlementMethod


@dataclass(frozen=True)
class MatchingConfig:
    """
    Configurable tolerances -- per docs/architecture.md Section 6:
    "Make this configurable" for strike-mismatch handling.
    """

    # Exact strike match required for confidence 1.0. Anything within
    # strike_tolerance_pct of the reference strike is accepted at reduced
    # confidence (interpolated); anything beyond it is rejected outright,
    # per Section 6's explicit instruction not to blindly treat 63000 vs
    # 63250 as equivalent.
    strike_tolerance_pct: Decimal = Decimal("0.01")  # 1% of strike, conservative default

    # Confidence penalty applied per 0.01 (1%) of relative strike
    # difference -- linear falloff, capped so confidence never goes
    # negative. E.g. a 0.5%-off strike with penalty_per_pct=0.3 knocks
    # confidence from 1.0 to 0.85.
    confidence_penalty_per_pct: Decimal = Decimal("0.3")

    # Minimum confidence a pair must clear to be returned as a candidate
    # at all (rather than silently omitted -- pairs below this still show
    # up in get_all_candidates() but callers filtering for "real"
    # candidates should use this as the bar).
    min_confidence: Decimal = Decimal("0.5")


class MatchingEngine:
    def __init__(self, config: MatchingConfig | None = None) -> None:
        self._config = config or MatchingConfig()

    # -- public API -----------------------------------------------------------

    def find_candidates(
        self, contracts: list[OptionContract]
    ) -> tuple[list[MatchCandidate], list[RejectedPair]]:
        """
        All-pairs comparison across the given contract list. For N
        contracts this is O(N^2) -- fine for a single underlying's chain
        (dozens to low hundreds of contracts per exchange), not intended to
        be run against the entire unfiltered instrument universe across
        exchanges. Callers (the Phase 4 scanner) are expected to pre-filter
        by underlying and option_type before calling this.
        """
        candidates: list[MatchCandidate] = []
        rejected: list[RejectedPair] = []

        for a, b in itertools.combinations(contracts, 2):
            result = self._evaluate_pair(a, b)
            if isinstance(result, MatchCandidate):
                candidates.append(result)
            else:
                rejected.append(result)

        return candidates, rejected

    # -- pairwise evaluation ----------------------------------------------------

    def _evaluate_pair(
        self, a: OptionContract, b: OptionContract
    ) -> MatchCandidate | RejectedPair:
        # Order by expiry so "short" is always the earlier-expiring leg --
        # this is the strategy's premise (Section 2), not an arbitrary
        # convention, so getting this backwards would silently invert every
        # downstream P&L calculation in Phase 5.
        if a.expiry_timestamp < b.expiry_timestamp:
            short, long_ = a, b
        elif b.expiry_timestamp < a.expiry_timestamp:
            short, long_ = b, a
        else:
            return self._reject(a, b, RejectionReason.SAME_EXPIRY_NO_TIME_ADVANTAGE,
                                 "Identical expiry timestamps -- no expiry-time advantage exists "
                                 "for the short/long structure this strategy requires.")

        if short.instrument_id == long_.instrument_id and short.exchange == long_.exchange:
            return self._reject(a, b, RejectionReason.IDENTICAL_INSTRUMENT,
                                 "Same exchange and instrument_id -- not a pair.")

        # -- Prerequisite checks (Section 6, items 1-2) --------------------------

        if short.underlying != long_.underlying:
            return self._reject(short, long_, RejectionReason.DIFFERENT_UNDERLYING,
                                 f"{short.underlying} vs {long_.underlying}")

        if short.option_type != long_.option_type:
            return self._reject(short, long_, RejectionReason.DIFFERENT_OPTION_TYPE,
                                 f"{short.option_type.value} vs {long_.option_type.value}")

        # -- Structural checks (Section C, items 1-7) ----------------------------

        # Section C.5: vanilla vs Turbo/knockout must never be paired even
        # if strike/underlying/type all match on the surface.
        if short.option_variant != long_.option_variant:
            return self._reject(short, long_, RejectionReason.DIFFERENT_OPTION_VARIANT,
                                 f"{short.option_variant.value} vs {long_.option_variant.value}")

        # Section C: settlement method (cash vs physical) must match --
        # different payoff mechanics entirely otherwise.
        if short.settlement_method != long_.settlement_method:
            return self._reject(short, long_, RejectionReason.DIFFERENT_SETTLEMENT_METHOD,
                                 f"{short.settlement_method.value} vs {long_.settlement_method.value}")

        # Section C.2: settlement price *formula* must match, not just the
        # method. Two "cash-settled" contracts computed from a 30-min TWAP
        # vs. last-traded-price are not the same payoff distribution even
        # at an identical nominal strike -- this is the single biggest
        # source of false-positive arbitrage called out in the whole doc.
        if short.settlement_price_formula != long_.settlement_price_formula:
            return self._reject(
                short, long_, RejectionReason.DIFFERENT_SETTLEMENT_FORMULA,
                f"{short.settlement_price_formula!r} vs {long_.settlement_price_formula!r} -- "
                f"see architecture.md Section C.2: this alone can manufacture a fake edge.",
            )

        # -- Strike matching with configurable tolerance (Section 6) --------------

        strike_diff = abs(short.strike - long_.strike)
        reference_strike = max(short.strike, long_.strike)
        strike_diff_pct = (strike_diff / reference_strike) if reference_strike > 0 else Decimal("999")

        if strike_diff_pct > self._config.strike_tolerance_pct:
            return self._reject(
                short, long_, RejectionReason.STRIKE_OUT_OF_TOLERANCE,
                f"strike diff {strike_diff} ({strike_diff_pct:.4%}) exceeds tolerance "
                f"{self._config.strike_tolerance_pct:.2%} -- per Section 6, reject rather "
                f"than assume equivalence; do not silently interpolate.",
            )

        # -- Confidence scoring ------------------------------------------------------

        confidence = Decimal("1.0")
        notes: list[str] = []

        if strike_diff_pct > 0:
            penalty = (strike_diff_pct * 100) * self._config.confidence_penalty_per_pct
            confidence = max(Decimal("0"), confidence - penalty)
            notes.append(
                f"Strike mismatch {strike_diff} ({strike_diff_pct:.4%}) -- confidence reduced "
                f"from 1.0 to {confidence:.3f}. This is an interpolated/lower-confidence match, "
                f"not an exact one."
            )

        if short.contract_multiplier != long_.contract_multiplier:
            notes.append(
                f"Contract multiplier differs ({short.contract_multiplier} vs "
                f"{long_.contract_multiplier}) -- per Section C.4, size to notional-equivalent, "
                f"never contract-count-equivalent, downstream."
            )

        if short.settlement_currency != long_.settlement_currency:
            notes.append(
                f"Settlement currency differs ({short.settlement_currency} vs "
                f"{long_.settlement_currency}) -- per Section C.3, verify this isn't an "
                f"unhedged FX/stablecoin-basis difference masquerading as edge."
            )

        same_exchange = short.exchange == long_.exchange
        expiry_gap = long_.expiry_timestamp - short.expiry_timestamp

        classification = self._classify(short, long_, same_exchange, strike_diff_pct)

        pair_id = f"{short.exchange}:{short.instrument_id}__{long_.exchange}:{long_.instrument_id}"

        return MatchCandidate(
            pair_id=pair_id,
            short_contract=short,
            long_contract=long_,
            match_confidence=confidence,
            classification=classification,
            strike_diff=strike_diff,
            expiry_gap=expiry_gap,
            same_exchange=same_exchange,
            notes=tuple(notes),
        )

    # -- classification (Section D.5) ------------------------------------------

    def _classify(
        self,
        short: OptionContract,
        long_: OptionContract,
        same_exchange: bool,
        strike_diff_pct: Decimal,
    ) -> Classification:
        """
        Never defaults to a specific named arbitrage type -- classification
        must be earned by ruling out structural explanations first (Section
        D.5's explicit instruction). This function only distinguishes the
        categories this matching engine actually has enough information to
        tell apart; Phase 5's EV engine will refine "unclassified" further
        once pricing data is available (e.g. distinguishing IV mispricing
        from a fair spread).
        """
        exact_strike = strike_diff_pct == 0

        if same_exchange and exact_strike:
            # Per the Phase 2 MVP (architecture.md Section J): this is the
            # first thing worth testing -- Delta's own D1/D2/weekly chain
            # self-matched against itself.
            return Classification.SAME_EXCHANGE_CALENDAR_SPREAD

        if not same_exchange and exact_strike:
            # Section D.5: whether this is truly an intraday "expiry
            # arbitrage" (same calendar date, different clock time) or a
            # calendar-day difference depends on comparing the *dates*, not
            # just declaring it one or the other from the pair alone.
            if short.expiry_timestamp.date() == long_.expiry_timestamp.date():
                return Classification.CROSS_EXCHANGE_EXPIRY_ARBITRAGE
            return Classification.CROSS_EXCHANGE_CALENDAR_SPREAD

        if not exact_strike:
            # Different (but in-tolerance) strikes -- relative-value
            # structure, not a same-strike expiry play.
            return Classification.OPTIONS_RELATIVE_VALUE_ARBITRAGE

        return Classification.UNCLASSIFIED

    # -- rejection helper -----------------------------------------------------

    @staticmethod
    def _reject(
        a: OptionContract, b: OptionContract, reason: RejectionReason, detail: str
    ) -> RejectedPair:
        return RejectedPair(
            contract_a_id=f"{a.exchange}:{a.instrument_id}",
            contract_b_id=f"{b.exchange}:{b.instrument_id}",
            reason=reason,
            detail=detail,
        )
