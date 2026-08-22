"""
Phase 6 tests for backtest/engine.py.

Same fixture-based approach as tests/test_ev_engine.py -- these prove the
engine's status logic (every gap reason, the happy path, fee-cap behavior,
and deterministic legging-failure simulation) before backtest/run_backtest.py
is trusted to run it against real historical `market_data`.

NOTE on fixture premium magnitudes (2026-08-23): earlier versions of these
tests used small dollar amounts (e.g. Decimal("2.00")) for best_bid/best_ask,
written as if premiums were already contract-scaled. The real backtest run
against live data proved that's wrong -- premiums are raw, per-1-BTC-terms,
same scale as spot/strike (see backtest/engine.py's "BUG FOUND + FIXED" note
and pricing/ev_engine.py's Bug #2). Fixtures below now use realistically raw
premium magnitudes (hundreds to low-thousands, matching real Delta ticker
data observed via pricing/diagnose_pair.py) so these tests actually exercise
the contract_multiplier scaling path instead of numbers small enough to make
the bug invisible.
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
        short_ticks = [HistoricalTick(ts=_NOW - timedelta(days=2), best_bid=Decimal("300"), best_ask=Decimal("310"), index_price=Decimal("65000"))]
        long_ticks = [HistoricalTick(ts=_NOW - timedelta(days=2, hours=5), best_bid=Decimal("500"), best_ask=Decimal("520"), index_price=Decimal("65000"))]
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
        """
        Section G.1 item 3, non-negotiable: zero settlement fee on OTM.
        Also exercises the contract_multiplier premium-scaling fix directly:
        raw quoted premiums (hundreds of dollars, realistic per-1-BTC scale)
        must be scaled by 0.001 before entering net_entry_cost/realized_pnl.
        """
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s5", "65000", settlement_ts)
        long_ = _make_contract("l5", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p5", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_bid_raw, long_ask_raw = Decimal("200"), Decimal("300")
        short_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=short_bid_raw, best_ask=Decimal("210"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts - timedelta(minutes=10), best_bid=None, best_ask=None, index_price=Decimal("64000")),
        ]
        long_exit_bid_raw = Decimal("120")
        long_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("290"), best_ask=long_ask_raw, index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts + timedelta(minutes=5), best_bid=long_exit_bid_raw, best_ask=Decimal("130"), index_price=Decimal("64000")),
        ]
        result = _engine().simulate_pair(candidate, short_ticks, long_ticks, _NOW)

        assert result.status == "completed"
        assert result.short_payoff == Decimal("0")
        assert result.settlement_fee == Decimal("0")

        multiplier = Decimal("0.001")
        short_bid_scaled = short_bid_raw * multiplier
        long_ask_scaled = long_ask_raw * multiplier
        expected_net_entry = (short_bid_scaled - long_ask_scaled) - short_bid_scaled * Decimal("0.0005") - long_ask_scaled * Decimal("0.0005")
        assert result.net_entry_cost == expected_net_entry

        long_exit_scaled = long_exit_bid_raw * multiplier
        expected_exit_fee = long_exit_scaled * Decimal("0.0005")
        assert result.long_exit_price == long_exit_scaled
        assert result.long_exit_fee == expected_exit_fee

        expected_pnl = expected_net_entry - Decimal("0") - Decimal("0") + long_exit_scaled - expected_exit_fee
        assert result.realized_pnl == expected_pnl

    def test_itm_settlement_charges_capped_settlement_fee(self) -> None:
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s6", "65000", settlement_ts)
        long_ = _make_contract("l6", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p6", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("200"), best_ask=Decimal("210"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts - timedelta(minutes=10), best_bid=None, best_ask=None, index_price=Decimal("73000")),
        ]
        long_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("290"), best_ask=Decimal("300"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts + timedelta(minutes=5), best_bid=Decimal("900"), best_ask=Decimal("930"), index_price=Decimal("73000")),
        ]
        result = _engine().simulate_pair(candidate, short_ticks, long_ticks, _NOW)

        assert result.status == "completed"
        assert result.short_payoff > Decimal("0")
        assert result.settlement_fee > Decimal("0")
        short_bid_scaled = Decimal("200") * Decimal("0.001")
        cap = abs(short_bid_scaled) * Decimal("0.075")
        assert result.settlement_fee <= cap
        # Sanity ceiling: for a 0.001-multiplier contract, no completed-trade
        # dollar figure should be in the hundreds/thousands -- this is the
        # regression guard against the premium-scaling bug reappearing.
        assert result.settlement_fee < Decimal("10")


class TestLeanBacktesterLeggingFailure:
    """Section G.1 item 4: legging failure simulated, not ignored."""

    def test_forced_legging_failure_rate_always_fails(self) -> None:
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s7", "65000", settlement_ts)
        long_ = _make_contract("l7", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p7", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("200"), best_ask=Decimal("210"), index_price=Decimal("65000"))]
        long_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("290"), best_ask=Decimal("300"), index_price=Decimal("65000"))]

        engine = _engine(legging_failure_rate=Decimal("1.0"))
        result = engine.simulate_pair(candidate, short_ticks, long_ticks, _NOW)

        assert result.status == "legging_failed"
        assert result.realized_pnl is not None and result.realized_pnl < 0
        # Regression guard: legging-failure loss must also be contract-scaled,
        # not the raw hundreds-of-dollars premium magnitude.
        assert abs(result.realized_pnl) < Decimal("10")

    def test_legging_failure_decision_is_deterministic(self) -> None:
        """Same pair_id must always produce the same legging-failure decision
        across separate runs -- reproducibility, not true randomness."""
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s8", "65000", settlement_ts)
        long_ = _make_contract("l8", "65000", _NOW + timedelta(days=6))
        candidate = _make_candidate("p8", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("200"), best_ask=Decimal("210"), index_price=Decimal("65000"))]
        long_ticks = [HistoricalTick(ts=entry_ts, best_bid=Decimal("290"), best_ask=Decimal("300"), index_price=Decimal("65000"))]

        engine = _engine(legging_failure_rate=Decimal("0.05"))
        result_a = engine.simulate_pair(candidate, short_ticks, long_ticks, _NOW)
        result_b = engine.simulate_pair(candidate, short_ticks, long_ticks, _NOW)
        assert result_a.status == result_b.status


class TestPremiumScalingUnitConsistency:
    """
    Regression tests for the 2026-08-23 bug: net_entry_cost, long_exit_price,
    and the legging-failure loss were computed from raw (unscaled) premiums,
    while short_payoff/settlement_fee were correctly scaled -- producing an
    implausible $2848 avg pnl/trade on the first real backtest run. See
    backtest/engine.py's "BUG FOUND + FIXED" docstring note.
    """

    def test_completed_trade_pnl_stays_in_plausible_range_for_small_multiplier(self) -> None:
        """
        For a 0.001 BTC contract, no single completed trade's realized_pnl
        should land in the hundreds or thousands of dollars given realistic
        (hundreds-of-dollars, raw-scale) quoted premiums -- that magnitude
        would indicate the unit-mismatch bug is back.
        """
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s9", "65000", settlement_ts, multiplier="0.001")
        long_ = _make_contract("l9", "65000", _NOW + timedelta(days=6), multiplier="0.001")
        candidate = _make_candidate("p9", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("250"), best_ask=Decimal("260"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts - timedelta(minutes=10), best_bid=None, best_ask=None, index_price=Decimal("65500")),
        ]
        long_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("340"), best_ask=Decimal("350"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts + timedelta(minutes=5), best_bid=Decimal("400"), best_ask=Decimal("410"), index_price=Decimal("65500")),
        ]
        result = _engine(legging_failure_rate=Decimal("0.0")).simulate_pair(candidate, short_ticks, long_ticks, _NOW)

        assert result.status == "completed"
        assert result.realized_pnl is not None
        assert abs(result.realized_pnl) < Decimal("10"), (
            f"realized_pnl={result.realized_pnl} is implausibly large for a "
            f"0.001-multiplier contract with ~$250-400 raw premiums -- check "
            f"that contract_multiplier scaling wasn't dropped again."
        )

    def test_different_multipliers_per_leg_scaled_independently(self) -> None:
        """Section C.4: never assume both legs share one multiplier."""
        settlement_ts = _NOW - timedelta(hours=2)
        short = _make_contract("s10", "65000", settlement_ts, multiplier="0.001")
        long_ = _make_contract("l10", "65000", _NOW + timedelta(days=6), multiplier="0.01")
        candidate = _make_candidate("p10", short, long_)
        entry_ts = settlement_ts - timedelta(days=1)
        short_bid_raw, long_ask_raw = Decimal("200"), Decimal("300")
        short_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=short_bid_raw, best_ask=Decimal("210"), index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts - timedelta(minutes=10), best_bid=None, best_ask=None, index_price=Decimal("64000")),
        ]
        long_ticks = [
            HistoricalTick(ts=entry_ts, best_bid=Decimal("290"), best_ask=long_ask_raw, index_price=Decimal("65000")),
            HistoricalTick(ts=settlement_ts + timedelta(minutes=5), best_bid=Decimal("120"), best_ask=Decimal("130"), index_price=Decimal("64000")),
        ]
        result = _engine(legging_failure_rate=Decimal("0.0")).simulate_pair(candidate, short_ticks, long_ticks, _NOW)

        assert result.status == "completed"
        expected_net_entry = (
            (short_bid_raw * Decimal("0.001")) - (long_ask_raw * Decimal("0.01"))
            - (short_bid_raw * Decimal("0.001")) * Decimal("0.0005")
            - (long_ask_raw * Decimal("0.01")) * Decimal("0.0005")
        )
        assert result.net_entry_cost == expected_net_entry


class TestFindEntryPerformance:
    """
    Regression tests for the O(n*m) -> O(n log m) fix in
    LeanBacktester._find_entry (2026-08-22).

    NOTE (2026-08-23): the first version of these tests used periodic tick
    spacing for both legs, which -- by construction -- aliases back into the
    tolerance window at regular intervals (e.g. offsetting by tolerance+1s
    on a 60s grid still lands a match one step over). Fixed by using widely
    SEPARATED time ranges for the "no match" case (a large gap between the
    two legs' entire tick ranges, not just an offset within a shared grid),
    which guarantees no match regardless of internal spacing.
    """

    def test_large_no_match_input_completes_quickly(self) -> None:
        """
        5,000 ticks per leg, in two entirely separate time ranges (a
        multi-day gap between the short leg's tick range and the long leg's
        tick range) -- guarantees zero valid matches regardless of internal
        spacing, unlike offsetting within a shared periodic grid. This is
        still the worst case for the original nested loop (full n*m scan
        before giving up); should now resolve via bisect in well under a
        second.
        """
        import time

        short_base = _NOW - timedelta(days=60)
        long_base = _NOW - timedelta(days=30)  # 30-day gap, far beyond the 60s tolerance

        short_ticks = [
            HistoricalTick(ts=short_base + timedelta(seconds=17 * i), best_bid=Decimal("200"), best_ask=Decimal("210"), index_price=Decimal("65000"))
            for i in range(5000)
        ]
        long_ticks = [
            HistoricalTick(ts=long_base + timedelta(seconds=23 * i), best_bid=Decimal("290"), best_ask=Decimal("300"), index_price=Decimal("65000"))
            for i in range(5000)
        ]

        start = time.monotonic()
        result = LeanBacktester._find_entry(short_ticks, long_ticks)
        elapsed = time.monotonic() - start

        assert result == (None, None)
        assert elapsed < 1.0, (
            f"_find_entry took {elapsed:.2f}s on a 5,000x5,000 no-match input -- "
            f"expected well under 1s with O(n log m) bisect search."
        )

    def test_finds_correct_match_in_large_input(self) -> None:
        """
        Correctness check: bisect must find the right tick pair. Uses
        irregular (non-arithmetic-progression) spacing for the background
        ticks specifically to avoid the periodic-aliasing issue that broke
        the original version of this test -- see class docstring.
        """
        base = _NOW - timedelta(days=30)

        # Irregular spacing (varying step sizes via a simple non-periodic
        # pattern) so no unintended alignment occurs between the two lists.
        short_ticks = []
        t = base
        for i in range(2000):
            short_ticks.append(HistoricalTick(ts=t, best_bid=Decimal("200"), best_ask=Decimal("210"), index_price=Decimal("65000")))
            t += timedelta(seconds=90 + (i % 7))  # step varies 90-96s, non-periodic relative to any fixed offset

        long_ticks = []
        t = base + timedelta(days=5)  # entire background range far from short_ticks' range
        for i in range(2000):
            long_ticks.append(HistoricalTick(ts=t, best_bid=Decimal("290"), best_ask=Decimal("300"), index_price=Decimal("65000")))
            t += timedelta(seconds=110 + (i % 5))

        # Plant exactly one real match: a long tick 30 seconds after a
        # specific short tick, distinguishable by a unique price, inserted
        # into the (otherwise far-away) long_ticks list.
        planted_short = short_ticks[1000]
        planted_long = HistoricalTick(
            ts=planted_short.ts + timedelta(seconds=30),
            best_bid=Decimal("999.99"), best_ask=Decimal("888.88"), index_price=Decimal("65000"),
        )
        long_ticks.append(planted_long)
        long_ticks.sort(key=lambda tick: tick.ts)

        found_short, found_long = LeanBacktester._find_entry(short_ticks, long_ticks)

        assert found_short is not None and found_long is not None
        assert found_short.ts == planted_short.ts
        assert found_long.best_ask == Decimal("888.88")
