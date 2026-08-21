"""
Phase 3 exit criterion tests, per docs/architecture.md Section I:

    "matcher correctly rejects deliberately-mismatched fixtures (different
    multiplier, different settlement method) and correctly accepts genuine
    matches, in a test suite."

Fixtures are self-matched Delta-style contracts first (per the Phase 2 MVP,
Section J: D1 vs D2 vs weekly, same exchange), then cross-exchange fixtures
to exercise the structural-check rejection paths.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from matching.engine import MatchingConfig, MatchingEngine
from matching.schemas import Classification, RejectionReason
from normalization.schemas import (
    OptionContract,
    OptionType,
    OptionVariant,
    SettlementMethod,
)

_D1 = datetime(2026, 12, 20, 12, 0, 0, tzinfo=timezone.utc)  # 5:30 PM IST
_D2 = datetime(2026, 12, 21, 12, 0, 0, tzinfo=timezone.utc)
_WEEKLY = datetime(2026, 12, 25, 12, 0, 0, tzinfo=timezone.utc)


def _make_contract(**overrides) -> OptionContract:
    base = dict(
        exchange="delta_india",
        underlying="BTC",
        base_asset="BTC",
        quote_asset="USD",
        option_type=OptionType.CALL,
        option_variant=OptionVariant.VANILLA,
        strike=Decimal("65000"),
        expiry_timestamp=_D1,
        settlement_timestamp=_D1,
        settlement_method=SettlementMethod.CASH,
        settlement_price_formula="30min_twap_index",
        contract_multiplier=Decimal("0.001"),
        lot_size=Decimal("1"),
        tick_size=Decimal("0.5"),
        quote_currency="USD",
        settlement_currency="USDT",
        contract_symbol="C-BTC-65000-201226",
        instrument_id="1",
        is_european=True,
    )
    base.update(overrides)
    return OptionContract(**base)


@pytest.fixture
def engine() -> MatchingEngine:
    return MatchingEngine()


# ---------------------------------------------------------------------------
# Genuine matches -- must be ACCEPTED
# ---------------------------------------------------------------------------


class TestGenuineMatches:
    def test_same_exchange_d1_vs_d2_same_strike_accepted(self, engine: MatchingEngine) -> None:
        """Core Phase 2 MVP case: Delta's own D1 vs D2, same strike."""
        d1 = _make_contract(instrument_id="1", expiry_timestamp=_D1, settlement_timestamp=_D1)
        d2 = _make_contract(instrument_id="2", expiry_timestamp=_D2, settlement_timestamp=_D2)

        candidates, rejected = engine.find_candidates([d1, d2])

        assert len(rejected) == 0
        assert len(candidates) == 1
        c = candidates[0]
        assert c.short_contract.instrument_id == "1"  # earlier expiry is short
        assert c.long_contract.instrument_id == "2"
        assert c.match_confidence == Decimal("1.0")
        assert c.classification == Classification.SAME_EXCHANGE_CALENDAR_SPREAD
        assert c.same_exchange is True
        assert c.expiry_gap == timedelta(days=1)

    def test_cross_exchange_same_calendar_date_classified_as_expiry_arbitrage(
        self, engine: MatchingEngine
    ) -> None:
        """
        Two exchanges, same underlying/strike/settlement mechanics, expiring
        on the SAME calendar date but different clock times -- this is the
        one case that would actually validate the original video's premise,
        per architecture.md Section 0 / D.5.
        """
        early = _make_contract(
            exchange="hypothetical_exchange_a", instrument_id="A1",
            expiry_timestamp=datetime(2026, 12, 20, 8, 0, 0, tzinfo=timezone.utc),  # 1:30 PM IST
            settlement_timestamp=datetime(2026, 12, 20, 8, 0, 0, tzinfo=timezone.utc),
        )
        late = _make_contract(
            exchange="delta_india", instrument_id="1",
            expiry_timestamp=_D1,  # 5:30 PM IST, same calendar date
            settlement_timestamp=_D1,
        )

        candidates, rejected = engine.find_candidates([early, late])

        assert len(rejected) == 0
        assert len(candidates) == 1
        assert candidates[0].classification == Classification.CROSS_EXCHANGE_EXPIRY_ARBITRAGE
        assert candidates[0].same_exchange is False

    def test_cross_exchange_different_calendar_date_classified_as_calendar_spread(
        self, engine: MatchingEngine
    ) -> None:
        d1_a = _make_contract(exchange="exchange_a", instrument_id="A1", expiry_timestamp=_D1, settlement_timestamp=_D1)
        weekly_b = _make_contract(
            exchange="exchange_b", instrument_id="B1", expiry_timestamp=_WEEKLY, settlement_timestamp=_WEEKLY
        )

        candidates, rejected = engine.find_candidates([d1_a, weekly_b])

        assert len(rejected) == 0
        assert candidates[0].classification == Classification.CROSS_EXCHANGE_CALENDAR_SPREAD

    def test_slightly_off_strike_within_tolerance_accepted_at_reduced_confidence(
        self, engine: MatchingEngine
    ) -> None:
        d1 = _make_contract(instrument_id="1", strike=Decimal("65000"), expiry_timestamp=_D1, settlement_timestamp=_D1)
        d2 = _make_contract(instrument_id="2", strike=Decimal("65100"), expiry_timestamp=_D2, settlement_timestamp=_D2)
        # 65100 vs 65000 = 0.154% diff, within default 1% tolerance

        candidates, rejected = engine.find_candidates([d1, d2])

        assert len(rejected) == 0
        assert len(candidates) == 1
        assert candidates[0].match_confidence < Decimal("1.0")
        assert candidates[0].match_confidence >= Decimal("0.5")
        assert candidates[0].classification == Classification.OPTIONS_RELATIVE_VALUE_ARBITRAGE
        assert any("Strike mismatch" in note for note in candidates[0].notes)


# ---------------------------------------------------------------------------
# Deliberately-mismatched fixtures -- must be REJECTED, per Phase 3 exit criterion
# ---------------------------------------------------------------------------


class TestDeliberateMismatchesRejected:
    def test_different_underlying_rejected(self, engine: MatchingEngine) -> None:
        btc = _make_contract(instrument_id="1", underlying="BTC")
        eth = _make_contract(instrument_id="2", underlying="ETH", expiry_timestamp=_D2, settlement_timestamp=_D2)

        candidates, rejected = engine.find_candidates([btc, eth])

        assert len(candidates) == 0
        assert len(rejected) == 1
        assert rejected[0].reason == RejectionReason.DIFFERENT_UNDERLYING

    def test_different_option_type_rejected(self, engine: MatchingEngine) -> None:
        call = _make_contract(instrument_id="1", option_type=OptionType.CALL)
        put = _make_contract(
            instrument_id="2", option_type=OptionType.PUT, expiry_timestamp=_D2, settlement_timestamp=_D2
        )

        candidates, rejected = engine.find_candidates([call, put])

        assert len(candidates) == 0
        assert rejected[0].reason == RejectionReason.DIFFERENT_OPTION_TYPE

    def test_vanilla_vs_turbo_variant_rejected(self, engine: MatchingEngine) -> None:
        """Guards against exactly the failure mode in Section C.5."""
        vanilla = _make_contract(instrument_id="1", option_variant=OptionVariant.VANILLA)
        turbo = _make_contract(
            instrument_id="2", option_variant=OptionVariant.TURBO,
            expiry_timestamp=_D2, settlement_timestamp=_D2,
        )

        candidates, rejected = engine.find_candidates([vanilla, turbo])

        assert len(candidates) == 0
        assert rejected[0].reason == RejectionReason.DIFFERENT_OPTION_VARIANT

    def test_different_settlement_method_rejected(self, engine: MatchingEngine) -> None:
        cash = _make_contract(instrument_id="1", settlement_method=SettlementMethod.CASH)
        physical = _make_contract(
            instrument_id="2", settlement_method=SettlementMethod.PHYSICAL,
            expiry_timestamp=_D2, settlement_timestamp=_D2,
        )

        candidates, rejected = engine.find_candidates([cash, physical])

        assert len(candidates) == 0
        assert rejected[0].reason == RejectionReason.DIFFERENT_SETTLEMENT_METHOD

    def test_different_settlement_formula_rejected(self, engine: MatchingEngine) -> None:
        """
        The single most important rejection per Section C.2 -- a TWAP-based
        settlement vs. a last-traded-price settlement is a different payoff
        distribution even at an identical strike and nominal expiry date.
        """
        twap = _make_contract(instrument_id="1", settlement_price_formula="30min_twap_index")
        last_price = _make_contract(
            instrument_id="2", settlement_price_formula="last_traded_price",
            expiry_timestamp=_D2, settlement_timestamp=_D2,
        )

        candidates, rejected = engine.find_candidates([twap, last_price])

        assert len(candidates) == 0
        assert rejected[0].reason == RejectionReason.DIFFERENT_SETTLEMENT_FORMULA

    def test_strike_beyond_tolerance_rejected(self, engine: MatchingEngine) -> None:
        """Per Section 6: 63000 vs 63250-style gaps must not be silently
        treated as equivalent."""
        d1 = _make_contract(instrument_id="1", strike=Decimal("63000"), expiry_timestamp=_D1, settlement_timestamp=_D1)
        d2 = _make_contract(instrument_id="2", strike=Decimal("70000"), expiry_timestamp=_D2, settlement_timestamp=_D2)
        # ~10% strike diff, well beyond default 1% tolerance

        candidates, rejected = engine.find_candidates([d1, d2])

        assert len(candidates) == 0
        assert rejected[0].reason == RejectionReason.STRIKE_OUT_OF_TOLERANCE

    def test_identical_expiry_rejected(self, engine: MatchingEngine) -> None:
        a = _make_contract(instrument_id="1", expiry_timestamp=_D1, settlement_timestamp=_D1)
        b = _make_contract(instrument_id="2", expiry_timestamp=_D1, settlement_timestamp=_D1)

        candidates, rejected = engine.find_candidates([a, b])

        assert len(candidates) == 0
        assert rejected[0].reason == RejectionReason.SAME_EXPIRY_NO_TIME_ADVANTAGE

    def test_identical_instrument_not_matched_against_itself(self, engine: MatchingEngine) -> None:
        a = _make_contract(instrument_id="1")
        candidates, rejected = engine.find_candidates([a])
        assert candidates == []
        assert rejected == []  # itertools.combinations of a 1-element list yields no pairs at all


# ---------------------------------------------------------------------------
# Cross-cutting: multiplier/currency mismatches noted but not rejected
# ---------------------------------------------------------------------------


class TestNotedButNotRejected:
    def test_different_multiplier_accepted_with_note(self, engine: MatchingEngine) -> None:
        """
        Per Section C.4: a multiplier mismatch is a sizing concern for
        Phase 5, not grounds for rejection at the matching stage -- it gets
        surfaced as a note so downstream sizing logic can't silently ignore
        it, but the pair itself may still be structurally valid.
        """
        d1 = _make_contract(instrument_id="1", contract_multiplier=Decimal("0.001"))
        d2 = _make_contract(
            instrument_id="2", contract_multiplier=Decimal("1"),
            expiry_timestamp=_D2, settlement_timestamp=_D2,
        )

        candidates, rejected = engine.find_candidates([d1, d2])

        assert len(rejected) == 0
        assert len(candidates) == 1
        assert any("multiplier" in note.lower() for note in candidates[0].notes)

    def test_different_settlement_currency_accepted_with_note(self, engine: MatchingEngine) -> None:
        d1 = _make_contract(instrument_id="1", settlement_currency="USDT")
        d2 = _make_contract(
            instrument_id="2", settlement_currency="USD",
            expiry_timestamp=_D2, settlement_timestamp=_D2,
        )

        candidates, rejected = engine.find_candidates([d1, d2])

        assert len(rejected) == 0
        assert any("currency" in note.lower() for note in candidates[0].notes)


# ---------------------------------------------------------------------------
# Configurable tolerance
# ---------------------------------------------------------------------------


class TestConfigurableTolerance:
    def test_tighter_tolerance_rejects_what_default_would_accept(self) -> None:
        strict_engine = MatchingEngine(config=MatchingConfig(strike_tolerance_pct=Decimal("0.001")))
        d1 = _make_contract(instrument_id="1", strike=Decimal("65000"), expiry_timestamp=_D1, settlement_timestamp=_D1)
        d2 = _make_contract(instrument_id="2", strike=Decimal("65100"), expiry_timestamp=_D2, settlement_timestamp=_D2)
        # 0.154% diff -- within default 1%, but beyond a 0.1% strict tolerance

        candidates, rejected = strict_engine.find_candidates([d1, d2])

        assert len(candidates) == 0
        assert rejected[0].reason == RejectionReason.STRIKE_OUT_OF_TOLERANCE
