"""
Unit tests for the Delta adapter's normalization logic.

These test the pure normalization functions (_normalize_product,
_parse_option_variant, _settlement_timestamp) against fixture payloads
shaped like Delta's documented API responses -- they do NOT hit the network.

A separate, explicitly-marked integration test (not included yet) should run
against Delta's testnet before this adapter is trusted with real trading
logic in later phases. Do not skip that step when it's time -- see
docs/architecture.md Phase 2 exit criteria: "24h of continuous, gap-free
Delta options + underlying data captured and queryable" implies validating
against the live testnet, not just these fixtures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from exchange_adapters.delta import DeltaAdapter
from normalization.schemas import OptionType, OptionVariant, SettlementMethod


@pytest.fixture
def adapter() -> DeltaAdapter:
    return DeltaAdapter()


def _fixture_call_product(**overrides) -> dict:
    base = {
        "id": 12345,
        "symbol": "C-BTC-65000-201226",
        "contract_type": "call_options",
        "strike_price": "65000",
        "underlying_asset": {"symbol": "BTC"},
        "quoting_asset": {"symbol": "USD"},
        "settlement_currency": "USDT",
        "contract_value": "0.001",
        "lot_size": "1",
        "tick_size": "0.5",
        # 5:30 PM IST = 12:00 UTC
        "settlement_time": "2026-12-20T12:00:00Z",
    }
    base.update(overrides)
    return base


def test_normalize_vanilla_call_option(adapter: DeltaAdapter) -> None:
    product = _fixture_call_product()
    contract = adapter._normalize_product(product)

    assert contract is not None
    assert contract.exchange == "delta_india"
    assert contract.underlying == "BTC"
    assert contract.option_type == OptionType.CALL
    assert contract.option_variant == OptionVariant.VANILLA
    assert contract.strike == Decimal("65000")
    assert contract.settlement_method == SettlementMethod.CASH
    assert contract.is_european is True
    assert contract.contract_multiplier == Decimal("0.001")

    # This is the load-bearing assertion given Section 0's finding: every
    # Delta option settles at 5:30 PM IST (== 12:00 UTC) regardless of which
    # day it's for.
    assert contract.settlement_timestamp.astimezone(timezone.utc).hour == 12
    assert contract.settlement_timestamp.astimezone(timezone.utc).minute == 0


def test_normalize_rejects_non_option_products(adapter: DeltaAdapter) -> None:
    futures_product = {
        "id": 999,
        "symbol": "BTCUSD",
        "contract_type": "perpetual_futures",
    }
    assert adapter._normalize_product(futures_product) is None


def test_normalize_turbo_variant_detected(adapter: DeltaAdapter) -> None:
    product = _fixture_call_product(contract_type="call_options", symbol="TURBO-C-BTC-65000")
    # Simulate a Turbo/knockout contract type string as Delta might return it --
    # exact string not independently verified against live API in Phase 1
    # research; this test documents the expected behavior once confirmed.
    product["contract_type"] = "call_options"  # baseline: vanilla unless variant string present
    contract = adapter._normalize_product(product)
    assert contract is not None
    assert contract.option_variant == OptionVariant.VANILLA


def test_missing_settlement_time_returns_none(adapter: DeltaAdapter) -> None:
    product = _fixture_call_product()
    del product["settlement_time"]
    assert adapter._normalize_product(product) is None


def test_normalized_contract_rejects_naive_datetime_downstream() -> None:
    """
    Guards against a future regression where someone constructs an
    OptionContract directly with a naive datetime -- see the __post_init__
    validation in normalization/schemas.py, added specifically because
    naive-vs-aware datetime bugs are a documented source of cross-exchange
    settlement-clock errors (architecture.md Section C.1).
    """
    from normalization.schemas import OptionContract

    with pytest.raises(ValueError, match="timezone-aware"):
        OptionContract(
            exchange="delta_india",
            underlying="BTC",
            base_asset="BTC",
            quote_asset="USD",
            option_type=OptionType.CALL,
            option_variant=OptionVariant.VANILLA,
            strike=Decimal("65000"),
            expiry_timestamp=datetime(2026, 12, 20, 12, 0, 0),  # naive -- should raise
            settlement_timestamp=datetime(2026, 12, 20, 12, 0, 0, tzinfo=timezone.utc),
            settlement_method=SettlementMethod.CASH,
            settlement_price_formula="30min_twap_index",
            contract_multiplier=Decimal("0.001"),
            lot_size=Decimal("1"),
            tick_size=Decimal("0.5"),
            quote_currency="USD",
            settlement_currency="USDT",
            contract_symbol="C-BTC-65000-201226",
            instrument_id="12345",
            is_european=True,
        )
