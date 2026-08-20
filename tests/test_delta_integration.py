"""
Integration tests for DeltaAdapter against a REAL exchange (testnet by
default, per config/settings.py's DELTA.use_testnet).

These hit the network and are SKIPPED BY DEFAULT. Run them explicitly with:

    RUN_INTEGRATION_TESTS=true pytest tests/test_delta_integration.py -v -s

Why gated like this rather than just letting pytest discover and run them:
  - They depend on real credentials (config/.env -- see config/.env.example)
    and a live testnet connection, neither of which exist in CI or in a fresh
    clone.
  - They must never run silently against production keys. There is no
    "use_testnet=False" path exercised anywhere in this file -- if
    DELTA.use_testnet is False when this suite runs, every test aborts
    immediately (see the fixture below) rather than hitting production.
  - Per docs/architecture.md Phase 2 exit criteria ("24h of continuous,
    gap-free Delta options + underlying data captured and queryable"), this
    file is the first rung of that ladder: a single successful run here is
    necessary but not sufficient -- it doesn't replace the 24h continuous
    capture run, it just proves the adapter's wiring is correct before that
    longer run is worth doing.

What this file intentionally does NOT do:
  - Does not place, cancel, or modify any order. Those adapter methods are
    still hard-blocked by LIVE_TRADING=False regardless (see
    exchange_adapters/delta.py) and are out of scope until Phase 8.
  - Does not call get_positions()/get_balance() (they raise NotImplementedError
    by design in Phase 2 -- authenticated account endpoints aren't built yet).
  - Does not assert on price *values* (BTC price, IV levels, etc. are not
    stable enough to assert against) -- only on *shape*, *types*, and the
    settlement-mechanics claims from docs/architecture.md Section 0 that are
    supposed to be exchange-guaranteed regardless of market conditions.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
import requests

from config.settings import DELTA
from exchange_adapters.delta import DeltaAdapter, DeltaAdapterError
from normalization.schemas import MarketSnapshot, OptionContract

pytestmark = pytest.mark.integration

_RUN_FLAG = os.getenv("RUN_INTEGRATION_TESTS", "false").lower() == "true"

_SKIP_REASON_NOT_ENABLED = (
    "Integration tests skipped by default. Run with "
    "RUN_INTEGRATION_TESTS=true pytest tests/test_delta_integration.py -v -s"
)
_SKIP_REASON_NOT_TESTNET = (
    "Refusing to run integration tests: DELTA.use_testnet is False. "
    "This suite only runs against testnet -- set DELTA_USE_TESTNET=true in "
    "config/.env (see config/.env.example) before running these tests."
)
_SKIP_REASON_NO_CREDS = (
    "Skipping: DELTA_API_KEY / DELTA_API_SECRET not set in config/.env. "
    "Public endpoints exercised here don't strictly require auth, but the "
    "adapter is instantiated the same way production code will use it, so "
    "credentials should be present to catch auth wiring bugs too."
)


def _require_integration_enabled() -> None:
    if not _RUN_FLAG:
        pytest.skip(_SKIP_REASON_NOT_ENABLED)
    if not DELTA.use_testnet:
        pytest.skip(_SKIP_REASON_NOT_TESTNET)


@pytest.fixture(scope="module")
def live_adapter() -> DeltaAdapter:
    _require_integration_enabled()
    if not (DELTA.api_key and DELTA.api_secret):
        pytest.skip(_SKIP_REASON_NO_CREDS)
    return DeltaAdapter()


@pytest.fixture(scope="module")
def btc_option_chain(live_adapter: DeltaAdapter) -> list[OptionContract]:
    """
    Fetched once per test module run (not once per test) since it's a real
    network call and every test in this file that needs a live instrument
    can share it.
    """
    chain = live_adapter.get_option_chain(underlying="BTC")
    if not chain:
        pytest.skip(
            "Testnet returned zero BTC options -- either testnet has no "
            "active BTC options chain right now, or get_option_chain has a "
            "real bug. Investigate before assuming this is expected."
        )
    return chain


# ---------------------------------------------------------------------------
# Connectivity & instrument discovery
# ---------------------------------------------------------------------------


class TestConnectivityAndInstruments:
    def test_get_instruments_returns_nonempty_list(self, live_adapter: DeltaAdapter) -> None:
        instruments = live_adapter.get_instruments()
        assert isinstance(instruments, list)
        assert len(instruments) > 0, (
            "Testnet returned zero option instruments. Either testnet has no "
            "options listed right now (check delta.exchange status) or "
            "get_instruments()/the /v2/products endpoint is broken."
        )
        assert all(isinstance(c, OptionContract) for c in instruments)

    def test_get_instruments_produces_well_formed_contracts(
        self, live_adapter: DeltaAdapter
    ) -> None:
        """
        Every contract that survives normalization must satisfy
        OptionContract.__post_init__ (tz-aware datetimes, positive multiplier/
        lot size) -- if the API shape drifted from what delta.py assumes,
        this is where it would surface as a real exception, not a silent bad
        record making it into the DB.
        """
        instruments = live_adapter.get_instruments()
        for contract in instruments[:50]:  # sample; full list can be large
            assert contract.expiry_timestamp.tzinfo is not None
            assert contract.settlement_timestamp.tzinfo is not None
            assert contract.contract_multiplier > 0
            assert contract.lot_size > 0
            assert contract.exchange == "delta_india"

    def test_get_option_chain_filters_to_requested_underlying(
        self, btc_option_chain: list[OptionContract]
    ) -> None:
        assert all(c.underlying == "BTC" for c in btc_option_chain)
        assert all(c.option_type.value in ("call", "put") for c in btc_option_chain)


# ---------------------------------------------------------------------------
# Section 0's headline finding, checked empirically against live data
# ---------------------------------------------------------------------------


class TestSettlementMechanics:
    """
    These tests exist specifically to keep docs/architecture.md Section 0
    honest over time: it claims, from documentation, that every Delta
    options contract settles at a fixed 5:30 PM IST (12:00 UTC) clock time
    regardless of maturity. If Delta ever changes this, or if the docs were
    wrong, these tests should fail loudly rather than have the discrepancy
    discovered downstream in the matching engine.
    """

    def test_all_contracts_settle_at_fixed_1200_utc(
        self, btc_option_chain: list[OptionContract]
    ) -> None:
        distinct_settlement_times = {
            (c.settlement_timestamp.astimezone(timezone.utc).hour,
             c.settlement_timestamp.astimezone(timezone.utc).minute)
            for c in btc_option_chain
        }
        assert distinct_settlement_times == {(12, 0)}, (
            f"Expected every BTC option to settle at 12:00 UTC (5:30 PM IST) "
            f"per docs/architecture.md Section 0, but found distinct "
            f"settlement clock times: {distinct_settlement_times}. "
            f"If this fails, Section 0's central finding needs updating -- "
            f"this could mean the video's original 1:30 PM / 5:30 PM example "
            f"is achievable on Delta after all, which changes the whole "
            f"strategy design. Do not silently patch this test to pass; "
            f"investigate and update the architecture doc instead."
        )

    def test_distinct_settlement_dates_exist(
        self, btc_option_chain: list[OptionContract]
    ) -> None:
        """
        Confirms D1/D2/weekly/monthly maturities differ by *date*, which is
        the calendar-spread structure the Phase 2 MVP (architecture.md
        Section J) is designed to test.
        """
        distinct_dates = {c.settlement_timestamp.date() for c in btc_option_chain}
        assert len(distinct_dates) >= 2, (
            "Expected multiple distinct settlement dates (D1/D2/weekly/etc) "
            "in the live BTC option chain, found only one. Either testnet "
            "has limited listings right now, or something is collapsing "
            "distinct maturities into one date."
        )

    def test_all_contracts_are_cash_settled_european(
        self, btc_option_chain: list[OptionContract]
    ) -> None:
        from normalization.schemas import SettlementMethod

        assert all(c.settlement_method == SettlementMethod.CASH for c in btc_option_chain)
        assert all(c.is_european for c in btc_option_chain)


# ---------------------------------------------------------------------------
# Market data: ticker & orderbook
# ---------------------------------------------------------------------------


class TestMarketData:
    def test_get_ticker_returns_valid_snapshot_shape(
        self, live_adapter: DeltaAdapter, btc_option_chain: list[OptionContract]
    ) -> None:
        sample = btc_option_chain[0]
        ticker = live_adapter.get_ticker(sample.instrument_id)

        assert isinstance(ticker.snapshot, MarketSnapshot)
        assert ticker.snapshot.exchange == "delta_india"
        assert ticker.snapshot.instrument_id == sample.instrument_id
        # Per architecture.md Section A.1: mark/index price are allowed to be
        # None-tolerant for margin-only use, but we still expect the API to
        # generally return *something* for an actively listed instrument.
        # We do NOT assert exact values -- only that the fields are wired up.
        assert ticker.snapshot.timestamp.tzinfo is not None

    def test_get_ticker_executability_flag_is_consistent(
        self, live_adapter: DeltaAdapter, btc_option_chain: list[OptionContract]
    ) -> None:
        """
        Exercises MarketSnapshot.is_executable() against real data --
        this is the fail-closed gate the scanner (Phase 4) will rely on, so
        it needs to behave correctly against real thin/wide/missing books,
        not just fixtures.
        """
        sample = btc_option_chain[0]
        ticker = live_adapter.get_ticker(sample.instrument_id)
        snap = ticker.snapshot

        if snap.best_bid is not None and snap.best_ask is not None:
            assert snap.is_executable() is True
        else:
            assert snap.is_executable() is False

    def test_get_orderbook_returns_top_of_book(
        self, live_adapter: DeltaAdapter, btc_option_chain: list[OptionContract]
    ) -> None:
        sample = btc_option_chain[0]
        book = live_adapter.get_orderbook(sample.instrument_id)

        assert book.instrument_id == sample.instrument_id
        assert book.timestamp.tzinfo is not None
        # Per the known-open-item noted in exchange_adapters/delta.py: only
        # top-of-book (depth=1) is verified. Assert AT MOST one level per
        # side rather than asserting deeper levels exist, to avoid this test
        # silently passing on stale assumptions.
        assert len(book.bids) <= 1
        assert len(book.asks) <= 1
        if book.bids:
            price, size = book.bids[0]
            assert price > 0 and size >= 0
        if book.asks:
            price, size = book.asks[0]
            assert price > 0 and size >= 0


# ---------------------------------------------------------------------------
# Contract specification (feeds the matching engine's Section C checks)
# ---------------------------------------------------------------------------


class TestContractSpecification:
    def test_get_contract_specification_matches_instrument_list(
        self, live_adapter: DeltaAdapter, btc_option_chain: list[OptionContract]
    ) -> None:
        sample = btc_option_chain[0]
        spec = live_adapter.get_contract_specification(sample.instrument_id)

        assert spec.instrument_id == sample.instrument_id
        assert spec.exchange == "delta_india"
        # These two sources (the /v2/products list and the single-instrument
        # spec lookup) should agree -- if they don't, that's exactly the kind
        # of "contract-spec risk" architecture.md Section H calls out, and
        # the matching engine's confidence downgrade logic needs this to be
        # a real, working signal, not a hypothetical one.
        assert spec.contract_multiplier == sample.contract_multiplier
        assert spec.lot_size == sample.lot_size
        assert spec.settlement_method == sample.settlement_method
        assert spec.option_variant == sample.option_variant
        assert spec.fetched_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Error handling: confirm failures surface loudly, not silently
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_invalid_instrument_id_raises_not_silently_returns_empty(
        self, live_adapter: DeltaAdapter
    ) -> None:
        """
        Per architecture.md's fail-closed principle (Section A.1): a bad
        instrument ID should raise, not return a quietly-empty/zeroed
        snapshot that could get treated as a real (if boring) market state
        downstream.
        """
        with pytest.raises((DeltaAdapterError, requests.exceptions.RequestException)):
            live_adapter.get_ticker("not-a-real-instrument-id-0000000")

    def test_place_order_is_hard_blocked_even_in_integration_context(
        self, live_adapter: DeltaAdapter
    ) -> None:
        """
        Belt-and-suspenders: even though this is an integration test with
        real credentials against a real (test) exchange, place_order() must
        still refuse to run, because LIVE_TRADING is hardcoded False in
        config/settings.py regardless of environment. This test exists so
        that a future refactor of place_order() that accidentally removes
        the guard gets caught here, in the one test file that actually has
        working credentials to have placed a real order with.
        """
        from normalization.schemas import OptionType  # noqa: F401  (unused, kept for clarity)
        from decimal import Decimal
        from exchange_adapters.base import OrderRequest

        dummy_order = OrderRequest(
            instrument_id="irrelevant",
            side="buy",
            quantity=Decimal("1"),
            limit_price=Decimal("1"),
        )
        with pytest.raises(RuntimeError, match="LIVE_TRADING is False"):
            live_adapter.place_order(dummy_order)
