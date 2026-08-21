"""
Output schema for the contract matching engine.

A MatchCandidate is the matching engine's verdict on a pair of OptionContract
records: whether they're a legitimate candidate pair, at what confidence,
under what classification (per docs/architecture.md Section D.5), and if
rejected, exactly why -- rejections are not silently dropped, they're
recorded, because a matching engine that only reports acceptances gives no
way to audit *why* something didn't match without re-running it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

from normalization.schemas import OptionContract


class Classification(str, Enum):
    """Per docs/architecture.md Section D.5. Never defaults to a guess --
    every value here must be earned by ruling out structural explanations
    first (see MatchingEngine._classify)."""

    CROSS_EXCHANGE_EXPIRY_ARBITRAGE = "cross_exchange_expiry_arbitrage"
    CROSS_EXCHANGE_CALENDAR_SPREAD = "cross_exchange_calendar_spread"
    SAME_EXCHANGE_CALENDAR_SPREAD = "same_exchange_calendar_spread"
    OPTIONS_RELATIVE_VALUE_ARBITRAGE = "options_relative_value_arbitrage"
    CROSS_EXCHANGE_OPTION_MISPRICING = "cross_exchange_option_mispricing"
    UNCLASSIFIED = "unclassified"  # matched structurally but doesn't fit a named category yet


class RejectionReason(str, Enum):
    """Per docs/architecture.md Section C's seven structural checks, plus
    the basic prerequisites (same underlying/option type)."""

    DIFFERENT_UNDERLYING = "different_underlying"
    DIFFERENT_OPTION_TYPE = "different_option_type"
    DIFFERENT_OPTION_VARIANT = "different_option_variant"  # e.g. vanilla vs turbo -- Section C.5
    DIFFERENT_SETTLEMENT_METHOD = "different_settlement_method"  # cash vs physical -- Section C
    DIFFERENT_SETTLEMENT_FORMULA = "different_settlement_price_formula"  # Section C.2
    STRIKE_OUT_OF_TOLERANCE = "strike_out_of_tolerance"  # Section 6: reject rather than assume equivalence
    SAME_EXPIRY_NO_TIME_ADVANTAGE = "same_expiry_no_time_advantage"  # short must genuinely expire before long
    IDENTICAL_INSTRUMENT = "identical_instrument"  # guards against self-matching a contract with itself


@dataclass(frozen=True)
class MatchCandidate:
    pair_id: str
    short_contract: OptionContract  # earlier expiry -- intended short leg
    long_contract: OptionContract   # later expiry -- intended long leg
    match_confidence: Decimal       # 1.0 = exact structural match, <1.0 = interpolated/lower confidence
    classification: Classification
    strike_diff: Decimal
    expiry_gap: timedelta
    same_exchange: bool
    notes: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class RejectedPair:
    """
    Recorded, not discarded -- see module docstring. Useful both for
    debugging the matcher and for architecture.md's "matcher correctly
    rejects deliberately-mismatched fixtures" Phase 3 exit criterion.
    """

    contract_a_id: str
    contract_b_id: str
    reason: RejectionReason
    detail: str
