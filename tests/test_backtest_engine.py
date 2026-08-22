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


class TestFindEntryPerformance:
    """
    Regression tests for the O(n*m) -> O(n log m) fix in
    LeanBacktester._find_entry (2026-08-22), found when
    `python -m backtest.run_backtest --underlying BTC` slowed to a crawl on
    real data -- candidates with thousands of ticks per leg, especially ones
    with NO match at all (the original nested loop's worst case: full n*m
    scan before giving up), were taking seconds each.
    """

    def test_large_no_match_input_completes_quickly(self) -> None:
        """
        5,000 ticks per leg, deliberately interleaved so NO short/long tick
        pair falls within the entry tolerance window -- this is exactly the
        worst case that made the original nested loop scan the full n*m
        product. Should now resolve via bisect in well under a second; the
        naive O(n*m) version of this same input (25 million comparisons)
        would take multiple seconds in pure Python.
        """
        import time

        base = _NOW - timedelta(days=30)
        tolerance = timedelta(seconds=60)

        # Short ticks every 60s starting at base; long ticks every 60s but
        # offset by (tolerance + 1s) so no pair ever lands inside the window.
        short_ticks = [
            HistoricalTick(ts=base + timedelta(seconds=60 * i), best_bid=Decimal("2.00"), best_ask=Decimal("2.10"), index_price=Decimal("65000"))
            for i in range(5000)
        ]
        long_ticks = [
            HistoricalTick(
                ts=base + timedelta(seconds=60 * i) + tolerance + timedelta(seconds=1),
                best_bid=Decimal("2.90"), best_ask=Decimal("3.00"), index_price=Decimal("65000"),
            )
            for i in range(5000)
        ]

        start = time.monotonic()
        result = LeanBacktester._find_entry(short_ticks, long_ticks)
        elapsed = time.monotonic() - start

        assert result == (None, None)
        assert elapsed < 1.0, (
            f"_find_entry took {elapsed:.2f}s on a 5,000x5,000 no-match input -- "
            f"expected well under 1s with O(n log m) bisect search. If this is "
            f"slow again, check that the nested-loop O(n*m) version wasn't "
            f"reintroduced."
        )

    def test_finds_correct_match_in_large_input(self) -> None:
        """
        Correctness check alongside the performance check: bisect must still
        find the right tick pair, not just be fast. Plants exactly one valid
        match in the middle of large, otherwise-non-matching lists.
        """
        base = _NOW - timedelta(days=30)

        short_ticks = [
            HistoricalTick(ts=base + timedelta(minutes=5 * i), best_bid=Decimal("2.00"), best_ask=Decimal("2.10"), index_price=Decimal("65000"))
            for i in range(2000)
        ]
        long_ticks = [
            HistoricalTick(ts=base + timedelta(minutes=5 * i) + timedelta(hours=1), best_bid=Decimal("2.90"), best_ask=Decimal("3.00"), index_price=Decimal("65000"))
            for i in range(2000)
        ]

        # Plant one real match: a long tick 30 seconds after short tick #1000
        # (well within the 60s tolerance), distinguishable by a unique price.
        planted_short = short_ticks[1000]
        planted_long = HistoricalTick(
            ts=planted_short.ts + timedelta(seconds=30),
            best_bid=Decimal("999.99"), best_ask=Decimal("888.88"), index_price=Decimal("65000"),
        )
        long_ticks.insert(1000, planted_long)
        long_ticks.sort(key=lambda t: t.ts)

        found_short, found_long = LeanBacktester._find_entry(short_ticks, long_ticks)

        assert found_short is not None and found_long is not None
        assert found_short.ts == planted_short.ts
        assert found_long.best_ask == Decimal("888.88")
