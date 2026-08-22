"""
Phase 6 tests for backtest/engine.py.

Same fixture-based approach as tests/test_ev_engine.py -- these prove the
engine's status logic (every gap reason, the happy path, fee-cap behavior,
and deterministic legging-failure simulation) before backtest/run_backtest.py
is trusted to run it against real historical `market_data`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.engine import LeanBacktester
from backtest.schemas import HistoricalTick
from matching.schemas import Classification, MatchCandidate
from normalization.schemas import OptionContract, OptionType, OptionVariant, SettlementMethod

_NOW = datetime.now(timezone.utc)


def _make_contract(instrument_id: str, strike: str, settlement_ts: datetime, multiplier: str = "0.001") -> OptionContract:
    return OptionContract(
        exchange="delta_india", underlying="BTC", base_asset="BTC", quote_asset="USD",
        option_type=OptionType.CALL, option_variant=OptionVariant.VANILLA,
        strike=Decimal(strike), expiry_timestamp=settlement_ts, settlement_timestamp=settlement_ts,
        settlement_method=SettlementMethod.CASH, settlement_price_formula="30min_twap_index",
        contract_multiplier=Decimal(multiplier), lot_size=Decimal("1"), tick_size=Decimal("0.5"),
        quote_currency="USD", settlement_currency="USDT", contract_symbol=f"C-BTC-{strike}",
        instrument_id=instrument_id, is_european=True,
    )


def _make_candidate(pair_id: str, short: OptionContract, long_: OptionContract) -> MatchCandidate:
    return MatchCandidate(
        pair_id=pair_id, short_contract=short, long_contract=long_, match_confidence=Decimal("1.0"),
        classification=Classification.SAME_EXCHANGE_CALENDAR_SPREAD,
        strike_diff=abs(long_.strike - short.strike),
        expiry_gap=long_.expiry_timestamp - short.expiry_timestamp, same_exchange=True,
    )


def _engine(**overrides) -> LeanBacktester:
    base = dict(
        short_taker_fee_pct=Decimal("0.0005"), long_taker_fee_pct=Decimal("0.0005"),
        settlement_fee_pct=Decimal("0.0005"), fee_cap_pct_of_premium=Decimal("0.075"),
        zero_fee_on_otm_settlement=True,
    )
    base.update(overrides)
    return LeanBacktester(**base)


class TestLeanBacktesterGapReporting:
    """Per architecture.md Section G.1 item 2: gaps are reported, never filled in."""

    def test_not_yet_settled(self) -> None:
        short = _make_contract("s1", "65000", _NOW + timedelta(hours=5))
        long_ = _make_contract("l1", "65000", _NOW + timedelta(days=8))
        candidate = _make_candidate("p1", short, long_)
        result = _engine().simulate_pair(candidate, [], [], _NOW)
        assert result.status == "not_yet_settled"

    def test_gap_no_entry_data_when_ticks_never_overlap(self) -> None:
        short = _make_contract("s2", "65000", _NOW - timedelta(hours=2))
        long_ = _make_contract("l2", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p2", short, long_)
        short_ticks = [HistoricalTick(ts=_NOW - timedelta(days=2), best_bid=Decimal("10"), best_ask=Decimal("11"), index_price=Decimal("65000"))]
        long_ticks = [HistoricalTick(ts=_NOW - timedelta(days=2, hours=5), best_bid=Decimal("20"), best_ask=Decimal("21"), index_price=Decimal("65000"))]
        result = _engine().simulate_pair(candidate, short_ticks, long_ticks, _NOW)
        assert result.status == "gap_no_entry_data"

    def test_gap_no_settlement_data_when_no_index_price_near_settlement(self) -> None:
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s3", "65000", settlement_ts)
        long_ = _make_contract("l3", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p3", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("300"), best_ask=Decimal("310"), index_price=Decimal("65000"))]
        long_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("500"), best_ask=Decimal("520"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts + timedelta(minutes=5), best_bid=Decimal("510"), best_ask=Decimal("515"), index_price=None),
        ]
        result = _engine().simulate_pair(candidate, short_ticks, long_ticks, _NOW)
        assert result.status == "gap_no_settlement_data"

    def test_gap_no_exit_data_when_no_long_bid_near_settlement(self) -> None:
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s4", "65000", settlement_ts)
        long_ = _make_contract("l4", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p4", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("300"), best_ask=Decimal("310"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts - timedelta(minutes=10), best_bid=None, best_ask=None, index_price=Decimal("66000")),
        ]
        long_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("500"), best_ask=Decimal("520"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts + timedelta(hours=10), best_bid=Decimal("900"), best_ask=Decimal("920"), index_price=Decimal("66000")),
        ]
        result = _engine().simulate_pair(candidate, short_ticks, long_ticks, _NOW)
        assert result.status == "gap_no_exit_data"


class TestLeanBacktesterCompletedTrades:
    def test_otm_settlement_waives_settlement_fee_and_matches_formula(self) -> None:
        """Section G.1 item 3, non-negotiable: zero settlement fee on OTM."""
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s5", "65000", settlement_ts)
        long_ = _make_contract("l5", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p5", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("2.00"), best_ask=Decimal("2.10"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts - timedelta(minutes=10), best_bid=None, best_ask=None, index_price=Decimal("64000")),
        ]
        long_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("2.90"), best_ask=Decimal("3.00"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts + timedelta(minutes=5), best_bid=Decimal("1.20"), best_ask=Decimal("1.30"), index_price=Decimal("64000")),
        ]
        result = _engine().simulate_pair(candidate, short_ticks, long_ticks, _NOW)

        assert result.status == "completed"
        assert result.short_payoff == Decimal("0")
        assert result.settlement_fee == Decimal("0")

        expected_net_entry = (Decimal("2.00") - Decimal("3.00")) - Decimal("2.00") * Decimal("0.0005") - Decimal("3.00") * Decimal("0.0005")
        assert result.net_entry_cost == expected_net_entry
        expected_exit_fee = Decimal("1.20") * Decimal("0.0005")
        assert result.long_exit_fee == expected_exit_fee
        expected_pnl = expected_net_entry - Decimal("0") - Decimal("0") + Decimal("1.20") - expected_exit_fee
        assert result.realized_pnl == expected_pnl

    def test_itm_settlement_charges_capped_settlement_fee(self) -> None:
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s6", "65000", settlement_ts)
        long_ = _make_contract("l6", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p6", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("2.00"), best_ask=Decimal("2.10"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts - timedelta(minutes=10), best_bid=None, best_ask=None, index_price=Decimal("73000")),
        ]
        long_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("2.90"), best_ask=Decimal("3.00"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts + timedelta(minutes=5), best_bid=Decimal("9.00"), best_ask=Decimal("9.30"), index_price=Decimal("73000")),
        ]
        result = _engine().simulate_pair(candidate, short_ticks, long_ticks, _NOW)

        assert result.status == "completed"
        assert result.short_payoff > Decimal("0")
        assert result.settlement_fee > Decimal("0")
        cap = abs(Decimal("2.00")) * Decimal("0.075")
        assert result.settlement_fee <= cap


class TestLeanBacktesterLeggingFailure:
    """Section G.1 item 4: legging failure simulated, not ignored."""

    def test_forced_legging_failure_rate_always_fails(self) -> None:
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s7", "65000", settlement_ts)
        long_ = _make_contract("l7", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p7", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("2.00"), best_ask=Decimal("2.10"), index_price=Decimal("65000"))]
        long_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("2.90"), best_ask=Decimal("3.00"), index_price=Decimal("65000"))]

        engine = _engine(legging_failure_rate=Decimal("1.0"))
        result = engine.simulate_pair(candidate, short_ticks, long_ticks, _NOW)

        assert result.status == "legging_failed"
        assert result.realized_pnl is not None and result.realized_pnl < 0

    def test_legging_failure_decision_is_deterministic(self) -> None:
        """Same pair_id must always produce the same legging-failure decision
        across separate runs -- reproducibility, not true randomness."""
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s8", "65000", settlement_ts)
        long_ = _make_contract("l8", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p8", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("2.00"), best_ask=Decimal("2.10"), index_price=Decimal("65000"))]
        long_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("2.90"), best_ask=Decimal("3.00"), index_price=Decimal("65000"))]

        engine = _engine(legging_failure_rate=Decimal("0.05"))
        result_a = engine.simulate_pair(candidate, short_ticks, long_ticks, _NOW)
        result_b = engine.simulate_pair(candidate, short_ticks, long_ticks, _NOW)
        assert result_a.status == result_b.status
