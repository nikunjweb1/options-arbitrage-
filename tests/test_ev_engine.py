"""
Phase 5 tests for pricing/black_scholes.py and pricing/ev_engine.py.

Per docs/architecture.md Section A.1 ("No component downstream is trusted
until the component upstream is validated against real data") and the same
pattern tests/test_matching_engine.py already established for Phase 3: the
math has to be proven against fixtures before pricing/run_pricing.py is
trusted to run it against live candidate_pairs.

These are fixture-based unit tests, not a replacement for actually running
pricing/run_pricing.py against live Delta data -- that's still required to
satisfy the Phase 5 exit criterion in architecture.md ("using real bid/ask
pulled live, not backfilled").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from matching.schemas import Classification, MatchCandidate
from normalization.schemas import (
    MarketSnapshot,
    OptionContract,
    OptionType,
    OptionVariant,
    SettlementMethod,
)
from pricing.black_scholes import OptionKind, black_scholes_price, settlement_payoff
from pricing.ev_engine import InsufficientDataError, LeanEVEngine

_NOW = datetime.now(timezone.utc)
_T1 = _NOW + timedelta(days=1)   # short leg expiry
_T2 = _NOW + timedelta(days=8)   # long leg expiry


def _make_contract(**overrides) -> OptionContract:
    base = dict(
        exchange="delta_india",
        underlying="BTC",
        base_asset="BTC",
        quote_asset="USD",
        option_type=OptionType.CALL,
        option_variant=OptionVariant.VANILLA,
        strike=Decimal("65000"),
        expiry_timestamp=_T1,
        settlement_timestamp=_T1,
        settlement_method=SettlementMethod.CASH,
        settlement_price_formula="30min_twap_index",
        contract_multiplier=Decimal("0.001"),
        lot_size=Decimal("1"),
        tick_size=Decimal("0.5"),
        quote_currency="USD",
        settlement_currency="USDT",
        contract_symbol="C-BTC-65000",
        instrument_id="1",
        is_european=True,
    )
    base.update(overrides)
    return OptionContract(**base)


def _make_snapshot(**overrides) -> MarketSnapshot:
    base = dict(
        timestamp=_NOW,
        exchange="delta_india",
        instrument_id="1",
        best_bid=Decimal("120"),
        best_ask=Decimal("125"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        iv=Decimal("0.65"),
        underlying_spot=Decimal("65000"),
        underlying_index=Decimal("65000"),
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def _make_candidate(short: OptionContract, long_: OptionContract) -> MatchCandidate:
    return MatchCandidate(
        pair_id="p1",
        short_contract=short,
        long_contract=long_,
        match_confidence=Decimal("1.0"),
        classification=Classification.SAME_EXCHANGE_CALENDAR_SPREAD,
        strike_diff=abs(long_.strike - short.strike),
        expiry_gap=long_.expiry_timestamp - short.expiry_timestamp,
        same_exchange=True,
    )


# ---------------------------------------------------------------------------
# pricing/black_scholes.py
# ---------------------------------------------------------------------------


class TestBlackScholes:
    def test_call_price_positive_and_above_intrinsic_when_time_and_vol_present(self) -> None:
        price = black_scholes_price(
            spot=Decimal("65000"), strike=Decimal("65000"),
            time_to_expiry_years=7 / 365, volatility=0.65, risk_free_rate=0.0,
            option_kind=OptionKind.CALL,
        )
        # ATM option with time value should be strictly positive
        assert price > Decimal("0")

    def test_put_price_positive_atm(self) -> None:
        price = black_scholes_price(
            spot=Decimal("65000"), strike=Decimal("65000"),
            time_to_expiry_years=7 / 365, volatility=0.65, risk_free_rate=0.0,
            option_kind=OptionKind.PUT,
        )
        assert price > Decimal("0")

    def test_zero_time_to_expiry_returns_intrinsic(self) -> None:
        price = black_scholes_price(
            spot=Decimal("66000"), strike=Decimal("65000"),
            time_to_expiry_years=0.0, volatility=0.65, risk_free_rate=0.0,
            option_kind=OptionKind.CALL,
        )
        assert price == Decimal("1000")

    def test_zero_volatility_returns_intrinsic(self) -> None:
        price = black_scholes_price(
            spot=Decimal("64000"), strike=Decimal("65000"),
            time_to_expiry_years=7 / 365, volatility=0.0, risk_free_rate=0.0,
            option_kind=OptionKind.PUT,
        )
        assert price == Decimal("1000")

    def test_deep_otm_call_near_zero(self) -> None:
        price = black_scholes_price(
            spot=Decimal("65000"), strike=Decimal("200000"),
            time_to_expiry_years=1 / 365, volatility=0.5, risk_free_rate=0.0,
            option_kind=OptionKind.CALL,
        )
        assert price < Decimal("1")

    def test_settlement_payoff_call(self) -> None:
        assert settlement_payoff(Decimal("66000"), Decimal("65000"), OptionKind.CALL) == Decimal("1000")
        assert settlement_payoff(Decimal("64000"), Decimal("65000"), OptionKind.CALL) == Decimal("0")

    def test_settlement_payoff_put(self) -> None:
        assert settlement_payoff(Decimal("64000"), Decimal("65000"), OptionKind.PUT) == Decimal("1000")
        assert settlement_payoff(Decimal("66000"), Decimal("65000"), OptionKind.PUT) == Decimal("0")


# ---------------------------------------------------------------------------
# pricing/ev_engine.py
# ---------------------------------------------------------------------------


class TestLeanEVEngineFailClosed:
    """Per architecture.md Section A.1: fail-closed on missing executable data."""

    def test_raises_when_short_snapshot_not_executable(self) -> None:
        short = _make_contract(instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1)
        long_ = _make_contract(instrument_id="2", expiry_timestamp=_T2, settlement_timestamp=_T2)
        candidate = _make_candidate(short, long_)

        short_snap = _make_snapshot(instrument_id="1", best_bid=None)  # not executable
        long_snap = _make_snapshot(instrument_id="2")

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        with pytest.raises(InsufficientDataError):
            engine.evaluate(candidate, short_snap, long_snap)

    def test_raises_when_long_snapshot_not_executable(self) -> None:
        short = _make_contract(instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1)
        long_ = _make_contract(instrument_id="2", expiry_timestamp=_T2, settlement_timestamp=_T2)
        candidate = _make_candidate(short, long_)

        short_snap = _make_snapshot(instrument_id="1")
        long_snap = _make_snapshot(instrument_id="2", best_ask=None)  # not executable

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        with pytest.raises(InsufficientDataError):
            engine.evaluate(candidate, short_snap, long_snap)

    def test_raises_when_no_spot_or_index_price_available(self) -> None:
        short = _make_contract(instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1)
        long_ = _make_contract(instrument_id="2", expiry_timestamp=_T2, settlement_timestamp=_T2)
        candidate = _make_candidate(short, long_)

        short_snap = _make_snapshot(instrument_id="1", underlying_spot=None, underlying_index=None)
        long_snap = _make_snapshot(instrument_id="2")

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        with pytest.raises(InsufficientDataError):
            engine.evaluate(candidate, short_snap, long_snap)


class TestLeanEVEngineScenarioGrid:
    def test_basic_evaluate_produces_full_scenario_grid_with_ev_within_bounds(self) -> None:
        short = _make_contract(instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1)
        long_ = _make_contract(instrument_id="2", strike=Decimal("65000"), expiry_timestamp=_T2, settlement_timestamp=_T2)
        candidate = _make_candidate(short, long_)

        short_snap = _make_snapshot(instrument_id="1", best_bid=Decimal("300"), best_ask=Decimal("310"))
        long_snap = _make_snapshot(instrument_id="2", best_bid=Decimal("500"), best_ask=Decimal("520"))

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        result = engine.evaluate(candidate, short_snap, long_snap)

        # 21 price points x 3 IV-shock points, per ev_engine.py's documented
        # grid (widened from 5 points -- see "GRID RESOLUTION FIX" in the
        # module docstring).
        assert result.scenario_count == 63
        assert Decimal("0") <= result.probability_of_profit <= Decimal("1")
        # EV is a weighted average of the scenario P&Ls, so it must fall
        # within [worst_case, best_case] -- this is a structural guarantee,
        # not a tuned expectation, so it's a good regression check.
        assert result.worst_case_pnl <= result.expected_value <= result.best_case_pnl
        assert result.pair_id == "p1"

    def test_net_entry_cost_matches_documented_formula(self) -> None:
        """Section D.2: Net entry cost = (B_short - A_long) - fees(short) - fees(long)."""
        short = _make_contract(instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1)
        long_ = _make_contract(instrument_id="2", strike=Decimal("65000"), expiry_timestamp=_T2, settlement_timestamp=_T2)
        candidate = _make_candidate(short, long_)

        short_bid, long_ask = Decimal("300"), Decimal("310")
        short_fee_pct, long_fee_pct = Decimal("0.001"), Decimal("0.002")

        short_snap = _make_snapshot(instrument_id="1", best_bid=short_bid, best_ask=Decimal("305"))
        long_snap = _make_snapshot(instrument_id="2", best_bid=Decimal("305"), best_ask=long_ask)

        engine = LeanEVEngine(short_taker_fee_pct=short_fee_pct, long_taker_fee_pct=long_fee_pct)
        result = engine.evaluate(candidate, short_snap, long_snap)

        expected_fees = short_bid * short_fee_pct + long_ask * long_fee_pct
        expected_net_entry = (short_bid - long_ask) - expected_fees

        assert result.fees_total == expected_fees
        assert result.net_entry_cost == expected_net_entry
        assert result.short_bid_used == short_bid
        assert result.long_ask_used == long_ask

    def test_missing_iv_falls_back_to_conservative_default_without_crashing(self) -> None:
        """ev_engine.py falls back to an 80% flat vol assumption when a
        snapshot has no IV -- must not raise or divide by zero."""
        short = _make_contract(instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1)
        long_ = _make_contract(instrument_id="2", strike=Decimal("65000"), expiry_timestamp=_T2, settlement_timestamp=_T2)
        candidate = _make_candidate(short, long_)

        short_snap = _make_snapshot(instrument_id="1", iv=None)
        long_snap = _make_snapshot(instrument_id="2", iv=None)

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        result = engine.evaluate(candidate, short_snap, long_snap)

        assert result.scenario_count == 63
        assert result.model_notes  # always carries the lean-model disclosure


class TestGridResolution:
    """
    Regression tests for the coarse-grid P(profit) collapse found on the
    first real re-run against 330 live-priced candidates after the
    contract_multiplier fix (2026-08-22): every single one landed at
    P(profit) EXACTLY 0.0 or 1.0, with zero candidates strictly in between.
    Root cause and fix are in ev_engine.py's "GRID RESOLUTION FIX" docstring
    note -- summary: short-dated options have a small sigma_move, and the
    original 5-point price grid was too coarse to land any scenario near a
    typical candidate's payoff breakeven.
    """

    def test_short_dated_close_to_the_money_candidate_produces_fractional_probability(self) -> None:
        """
        Constructs a short-dated (6-hour), near-the-money candidate sized so
        a real trade would plausibly win in some price scenarios and lose in
        others -- the exact shape of candidate that produced an incorrect
        hard 0/1 split on real data before the grid was widened.
        """
        short_expiry = _NOW + timedelta(hours=6)
        long_expiry = _NOW + timedelta(hours=150)

        short = _make_contract(
            instrument_id="1", strike=Decimal("65000"),
            expiry_timestamp=short_expiry, settlement_timestamp=short_expiry,
            contract_multiplier=Decimal("0.001"),
        )
        long_ = _make_contract(
            instrument_id="2", strike=Decimal("65000"),
            expiry_timestamp=long_expiry, settlement_timestamp=long_expiry,
            contract_multiplier=Decimal("0.001"),
        )
        candidate = _make_candidate(short, long_)

        # Small net debit, spot sitting exactly at the strike (maximally
        # sensitive to which side of ATM a given scenario lands on) --
        # realistic short-dated Delta-scale premiums and IV.
        short_snap = _make_snapshot(
            instrument_id="1", best_bid=Decimal("2.00"), best_ask=Decimal("2.05"),
            iv=Decimal("0.50"), underlying_spot=Decimal("65000"), underlying_index=Decimal("65000"),
        )
        long_snap = _make_snapshot(
            instrument_id="2", best_bid=Decimal("2.90"), best_ask=Decimal("2.95"),
            iv=Decimal("0.50"), underlying_spot=Decimal("65000"), underlying_index=Decimal("65000"),
        )

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        result = engine.evaluate(candidate, short_snap, long_snap)

        assert Decimal("0") < result.probability_of_profit < Decimal("1"), (
            f"P(profit)={result.probability_of_profit} landed exactly at 0 or 1 for a "
            f"near-the-money, short-dated candidate -- this is the exact failure mode "
            f"the 21-point grid was supposed to fix. sigma_move={result.sigma_move}, "
            f"hrs_to_expiry={result.time_to_short_expiry_hours}."
        )

    def test_scenario_count_reflects_widened_grid(self) -> None:
        short = _make_contract(instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1)
        long_ = _make_contract(instrument_id="2", strike=Decimal("65000"), expiry_timestamp=_T2, settlement_timestamp=_T2)
        candidate = _make_candidate(short, long_)

        short_snap = _make_snapshot(instrument_id="1")
        long_snap = _make_snapshot(instrument_id="2")

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        result = engine.evaluate(candidate, short_snap, long_snap)

        assert result.scenario_count == 63  # 21 price points x 3 IV-shock points


class TestLeanEVEngineUnitConsistency:
    """
    Regression tests for the contract_multiplier unit-mismatch bug found
    during the first live Phase 5 run against all 1,504 real candidates
    (2026-08-21). Before the fix, settlement_payoff() and
    black_scholes_price() were combined with net_entry_cost with no
    contract_multiplier applied, mixing "dollars per contract" (real
    exchange premiums) with "dollars per 1 unit of underlying" (raw
    intrinsic/BS values) -- roughly a 1/contract_multiplier scale error.

    Symptoms this produced on the real run: EV magnitudes ~5x the net entry
    cost on a short-dated options calendar spread, and P(profit)=1.0 exactly
    for every one of the top 20 ranked candidates. See pricing/ev_engine.py's
    module docstring "BUG FOUND + FIXED" note for the full writeup.
    """

    def test_ev_stays_within_a_sane_multiple_of_net_entry_cost(self) -> None:
        """
        Before the fix, this exact scenario produced EV ~3124 against a net
        entry cost of ~-1.00 (a ~3000x blowup) because the unscaled payoff/
        repricing terms swamped the correctly-scaled entry economics. After
        the fix, EV should sit within a small, sane multiple of the entry
        cost -- this is a regression guard against that specific unit
        mismatch reappearing, not a claim about what "sane" EV should be in
        general.
        """
        short = _make_contract(
            instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1,
            contract_multiplier=Decimal("0.001"),
        )
        long_ = _make_contract(
            instrument_id="2", strike=Decimal("65000"), expiry_timestamp=_T2, settlement_timestamp=_T2,
            contract_multiplier=Decimal("0.001"),
        )
        candidate = _make_candidate(short, long_)

        # Realistic Delta-scale premiums for a 0.001 BTC contract -- small
        # dollar amounts, not thousands, and consistent with the multiplier
        # above (this is the shape real live quotes actually take).
        short_snap = _make_snapshot(
            instrument_id="1", best_bid=Decimal("2.00"), best_ask=Decimal("2.10"), iv=Decimal("1.20"),
        )
        long_snap = _make_snapshot(
            instrument_id="2", best_bid=Decimal("2.90"), best_ask=Decimal("3.00"), iv=Decimal("1.20"),
        )

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        result = engine.evaluate(candidate, short_snap, long_snap)

        assert abs(result.expected_value) < abs(result.net_entry_cost) * 20, (
            f"EV={result.expected_value} looks disproportionate to "
            f"net_entry_cost={result.net_entry_cost} -- check that "
            f"contract_multiplier is being applied to short_payoff and v_long."
        )

    def test_exact_strike_calendar_spread_shows_genuine_tail_risk(self) -> None:
        """
        For an exact-strike same-exchange calendar spread, the long leg's
        Black-Scholes value at T1 can never fall below its own intrinsic
        value, which (at matching strikes) equals the short leg's
        settlement payoff. So (v_long - short_payoff) -- the long leg's
        remaining time value -- is structurally >= 0 in every scenario,
        regardless of contract_multiplier. That's expected, real calendar-
        spread behavior, not a bug on its own.

        What the multiplier bug did was inflate that time-value term to a
        scale that swamped a realistically-sized net debit in every one of
        the grid scenarios, making every exact-strike candidate look
        risk-free. With premiums and payoffs on the same (correct) scale,
        a real net debit should be able to exceed the shrinking time value
        in the tail scenarios (large moves away from the strike), producing
        a genuine loss -- which is the actual, bounded risk this trade
        carries in real markets.
        """
        short = _make_contract(
            instrument_id="1", expiry_timestamp=_T1, settlement_timestamp=_T1,
            contract_multiplier=Decimal("0.001"),
        )
        long_ = _make_contract(
            instrument_id="2", strike=Decimal("65000"), expiry_timestamp=_T2, settlement_timestamp=_T2,
            contract_multiplier=Decimal("0.001"),
        )
        candidate = _make_candidate(short, long_)

        short_snap = _make_snapshot(
            instrument_id="1", best_bid=Decimal("2.00"), best_ask=Decimal("2.10"), iv=Decimal("1.20"),
        )
        long_snap = _make_snapshot(
            instrument_id="2", best_bid=Decimal("2.90"), best_ask=Decimal("3.00"), iv=Decimal("1.20"),
        )

        engine = LeanEVEngine(short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"))
        result = engine.evaluate(candidate, short_snap, long_snap)

        assert result.worst_case_pnl < Decimal("0"), (
            "A debit-funded exact-strike calendar spread should show at "
            "least one losing scenario in the grid -- P(profit)=1.0 here "
            "would indicate the unit-mismatch artifact is back."
        )
        assert result.probability_of_profit < Decimal("1.0")
