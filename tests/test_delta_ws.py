"""
Unit tests for the WebSocket ticker-message parsing and the flush-interval
safety ceiling. No network -- these test pure parsing/validation logic.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from exchange_adapters.delta_ws import DeltaWebSocketClient
from normalization.schemas import MarketSnapshot


@pytest.fixture
def received_snapshots() -> list[MarketSnapshot]:
    return []


@pytest.fixture
def client(received_snapshots: list[MarketSnapshot]) -> DeltaWebSocketClient:
    return DeltaWebSocketClient(on_snapshot=received_snapshots.append)


def _fixture_ticker_message(**overrides) -> dict:
    base = {
        "type": "ticker",
        "symbol": "C-BTC-65000-201226",
        "product_id": 12345,
        "close": 95.5,
        "mark_price": "96.0",
        "spot_price": "65200.00",
        "mark_vol": "0.55",
        "oi": "1200",
        "volume": "300",
        "quotes": {
            "best_bid": "95.0",
            "best_ask": "97.0",
            "bid_size": "10",
            "ask_size": "8",
        },
        "greeks": {
            "delta": "0.42",
            "gamma": "0.01",
            "theta": "-1.2",
            "vega": "3.4",
        },
    }
    base.update(overrides)
    return base


def test_parse_ticker_message_produces_valid_snapshot(client: DeltaWebSocketClient) -> None:
    msg = _fixture_ticker_message()
    snapshot = client._parse_ticker_message(msg)

    assert snapshot is not None
    assert isinstance(snapshot, MarketSnapshot)
    assert snapshot.exchange == "delta_india"
    assert snapshot.instrument_id == "12345"
    assert snapshot.best_bid == Decimal("95.0")
    assert snapshot.best_ask == Decimal("97.0")
    assert snapshot.delta == Decimal("0.42")
    assert snapshot.is_executable() is True


def test_parse_ticker_message_missing_symbol_returns_none(client: DeltaWebSocketClient) -> None:
    msg = _fixture_ticker_message()
    del msg["symbol"]
    del msg["product_id"]
    assert client._parse_ticker_message(msg) is None


def test_parse_ticker_message_missing_quotes_produces_non_executable_snapshot(
    client: DeltaWebSocketClient,
) -> None:
    msg = _fixture_ticker_message(quotes={})
    snapshot = client._parse_ticker_message(msg)
    assert snapshot is not None
    assert snapshot.best_bid is None
    assert snapshot.best_ask is None
    assert snapshot.is_executable() is False


def test_parse_ticker_message_malformed_numeric_field_does_not_crash(
    client: DeltaWebSocketClient,
) -> None:
    msg = _fixture_ticker_message()
    msg["quotes"]["best_bid"] = "not-a-number"
    snapshot = client._parse_ticker_message(msg)
    assert snapshot is not None
    assert snapshot.best_bid is None  # falls back to None rather than raising


def test_on_message_ignores_non_ticker_types(
    client: DeltaWebSocketClient, received_snapshots: list[MarketSnapshot]
) -> None:
    import json

    ack_message = json.dumps({"type": "subscriptions", "channels": []})
    client._on_message(MagicMock(), ack_message)
    assert received_snapshots == []


def test_on_message_dispatches_ticker_to_callback(
    client: DeltaWebSocketClient, received_snapshots: list[MarketSnapshot]
) -> None:
    import json

    msg = json.dumps(_fixture_ticker_message())
    client._on_message(MagicMock(), msg)
    assert len(received_snapshots) == 1
    assert received_snapshots[0].instrument_id == "12345"


def test_on_message_handles_non_json_gracefully(
    client: DeltaWebSocketClient, received_snapshots: list[MarketSnapshot]
) -> None:
    client._on_message(MagicMock(), "not json at all")
    assert received_snapshots == []


def test_flush_interval_ceiling_enforced() -> None:
    from collectors.realtime_collector import RealtimeCollector, _MAX_FLUSH_INTERVAL_SEC
    from exchange_adapters.delta import DeltaAdapter

    with pytest.raises(ValueError, match="hard ceiling"):
        RealtimeCollector(adapter=DeltaAdapter(), flush_interval_sec=_MAX_FLUSH_INTERVAL_SEC + 0.5)
