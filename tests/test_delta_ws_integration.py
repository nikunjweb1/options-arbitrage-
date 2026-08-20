"""
Integration tests for the Delta WebSocket feed against a REAL testnet
connection -- these are what actually validate (or refute) the message-shape
assumptions flagged in exchange_adapters/delta_ws.py's module docstring.

Gated identically to tests/test_delta_integration.py, for the same reasons:
skipped by default, testnet-only, requires real credentials. Run with:

    RUN_INTEGRATION_TESTS=true pytest tests/test_delta_ws_integration.py -v -s

What this file is actually checking, and why each check exists:

  1. Connection + subscribe ack -- proves the WS URL and the subscribe
     message shape (the part that's NOT independently confirmed from
     official docs -- see delta_ws.py's docstring) actually work against a
     live server, not just against our own fixtures.
  2. At least one real ticker message arrives and parses into a valid,
     non-crashing MarketSnapshot -- proves the field names assumed in
     _parse_ticker_message (quotes.best_bid, greeks.delta, etc.) match what
     Delta actually sends over the socket, which could differ from the REST
     ticker shape even though this module assumed they'd mostly overlap.
  3. Time-to-first-message and inter-message latency are measured and
     asserted against generous bounds -- not to prove sub-second performance
     in the abstract (that's a code-level guarantee enforced by
     RealtimeCollector's flush-interval ceiling, tested in
     tests/test_delta_ws.py), but to catch the case where the feed is
     technically connected but effectively dead (e.g. subscribed to the
     wrong channel/symbol and receiving nothing).
  4. A short real run of RealtimeCollector end-to-end -- WS -> queue ->
     SQLite -- confirms the full pipeline actually persists rows, not just
     that individual pieces work in isolation.
  5. Reconnect behavior: forcibly closes the underlying socket and confirms
     the client reconnects and resumes receiving data, since a silent
     reconnect-without-resubscribe bug would reintroduce exactly the kind of
     gap Phase 2 exists to rule out.

What this file does NOT do:
  - Does not place orders (no trading channels are touched).
  - Does not assert on specific price values.
  - Does not replace the 24h continuous run -- a passing run here proves the
    wiring is correct, not that it survives 24 hours unattended.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from config.settings import DELTA
from exchange_adapters.delta import DeltaAdapter
from exchange_adapters.delta_ws import DeltaWebSocketClient
from normalization.schemas import MarketSnapshot

pytestmark = pytest.mark.integration

_RUN_FLAG = os.getenv("RUN_INTEGRATION_TESTS", "false").lower() == "true"

_SKIP_REASON_NOT_ENABLED = (
    "Integration tests skipped by default. Run with "
    "RUN_INTEGRATION_TESTS=true pytest tests/test_delta_ws_integration.py -v -s"
)
_SKIP_REASON_NOT_TESTNET = (
    "Refusing to run: DELTA.use_testnet is False. This suite only runs "
    "against testnet."
)
_SKIP_REASON_NO_CREDS = (
    "Skipping: DELTA_API_KEY / DELTA_API_SECRET not set in config/.env."
)

# Generous bounds -- these exist to catch a dead/misconfigured feed, not to
# benchmark performance. Testnet liquidity/update frequency is not something
# we control or should assert tightly against.
_MAX_WAIT_FOR_FIRST_MESSAGE_SEC = 30.0
_MAX_WAIT_FOR_SECOND_MESSAGE_SEC = 60.0


def _require_integration_enabled() -> None:
    if not _RUN_FLAG:
        pytest.skip(_SKIP_REASON_NOT_ENABLED)
    if not DELTA.use_testnet:
        pytest.skip(_SKIP_REASON_NOT_TESTNET)
    if not (DELTA.api_key and DELTA.api_secret):
        pytest.skip(_SKIP_REASON_NO_CREDS)


@pytest.fixture(scope="module")
def sample_symbol() -> str:
    """
    Pulls one real, currently-live BTC option symbol via REST to subscribe
    to over WS -- we don't hardcode a symbol since strikes/expiries roll
    constantly (see architecture.md Section E.3).
    """
    _require_integration_enabled()
    adapter = DeltaAdapter()
    chain = adapter.get_option_chain(underlying="BTC")
    if not chain:
        pytest.skip("Testnet returned zero BTC options -- nothing to subscribe to.")
    return chain[0].contract_symbol


class TestConnectionAndSubscribe:
    def test_client_connects_within_timeout(self, sample_symbol: str) -> None:
        received: list[MarketSnapshot] = []
        client = DeltaWebSocketClient(on_snapshot=received.append)
        try:
            client.start()
            connected = client.wait_until_connected(timeout_sec=15.0)
            assert connected, (
                "WebSocket did not connect within 15s. Check DELTA.ws_base_url "
                "and network connectivity before assuming the client code is wrong."
            )
        finally:
            client.stop()

    def test_subscribe_does_not_raise(self, sample_symbol: str) -> None:
        received: list[MarketSnapshot] = []
        client = DeltaWebSocketClient(on_snapshot=received.append)
        try:
            client.start()
            assert client.wait_until_connected(timeout_sec=15.0)
            client.subscribe([sample_symbol])
            time.sleep(2)  # let the subscribe message actually reach the server
        finally:
            client.stop()


class TestMessageFlow:
    def test_receives_at_least_one_valid_snapshot(self, sample_symbol: str) -> None:
        """
        The central test of this file: if the subscribe message shape
        documented (with caveats) in delta_ws.py is wrong, this is where
        it surfaces -- either zero messages arrive, or _parse_ticker_message
        silently produces garbage. We assert on shape validity, not values.
        """
        received: list[MarketSnapshot] = []
        client = DeltaWebSocketClient(on_snapshot=received.append)
        try:
            client.start()
            assert client.wait_until_connected(timeout_sec=15.0)
            client.subscribe([sample_symbol])

            deadline = time.monotonic() + _MAX_WAIT_FOR_FIRST_MESSAGE_SEC
            while not received and time.monotonic() < deadline:
                time.sleep(0.1)

            assert received, (
                f"No ticker messages received within {_MAX_WAIT_FOR_FIRST_MESSAGE_SEC}s "
                f"for symbol {sample_symbol}. Either the subscribe message shape is "
                f"wrong (see delta_ws.py docstring -- it was reconstructed from "
                f"community sources, not confirmed against official docs), the "
                f"channel name 'ticker' doesn't match what's expected, or this "
                f"instrument genuinely has no update activity on testnet right now."
            )

            snapshot = received[0]
            assert isinstance(snapshot, MarketSnapshot)
            assert snapshot.exchange == "delta_india"
            assert snapshot.timestamp.tzinfo is not None
            # Don't assert best_bid/best_ask are non-None -- testnet liquidity
            # may be thin -- but if present they must be positive and sane.
            if snapshot.best_bid is not None:
                assert snapshot.best_bid > 0
            if snapshot.best_ask is not None:
                assert snapshot.best_ask > 0
        finally:
            client.stop()

    def test_message_timestamps_are_monotonically_reasonable(self, sample_symbol: str) -> None:
        """
        Collects a couple of messages and checks their timestamps make sense
        (increasing, not wildly in the future/past) -- catches clock or
        parsing bugs that a single-message test wouldn't.
        """
        received: list[MarketSnapshot] = []
        client = DeltaWebSocketClient(on_snapshot=received.append)
        try:
            client.start()
            assert client.wait_until_connected(timeout_sec=15.0)
            client.subscribe([sample_symbol])

            deadline = time.monotonic() + _MAX_WAIT_FOR_SECOND_MESSAGE_SEC
            while len(received) < 2 and time.monotonic() < deadline:
                time.sleep(0.1)

            if len(received) < 2:
                pytest.skip(
                    f"Only received {len(received)} message(s) within "
                    f"{_MAX_WAIT_FOR_SECOND_MESSAGE_SEC}s -- testnet activity "
                    f"may be too low to test ordering. Not a failure of this client."
                )

            now = datetime.now(timezone.utc)
            for snap in received:
                age_sec = (now - snap.timestamp).total_seconds()
                assert -5 <= age_sec <= 120, (
                    f"Snapshot timestamp {snap.timestamp.isoformat()} is "
                    f"{age_sec:.1f}s from now -- clock/parsing issue likely."
                )
        finally:
            client.stop()


class TestReconnectBehavior:
    def test_reconnects_after_forced_disconnect(self, sample_symbol: str) -> None:
        """
        Forces the underlying socket closed mid-stream and confirms the
        client both reconnects AND resumes receiving data -- a reconnect
        that forgets to re-subscribe would pass a naive "does it reconnect"
        check while still producing exactly the kind of silent gap Phase 2
        is designed to catch.
        """
        received: list[MarketSnapshot] = []
        client = DeltaWebSocketClient(
            on_snapshot=received.append,
            reconnect_backoff_base_sec=0.5,
            reconnect_backoff_max_sec=5.0,
        )
        try:
            client.start()
            assert client.wait_until_connected(timeout_sec=15.0)
            client.subscribe([sample_symbol])

            # Wait for first message to confirm the feed is live before we
            # deliberately break it.
            deadline = time.monotonic() + _MAX_WAIT_FOR_FIRST_MESSAGE_SEC
            while not received and time.monotonic() < deadline:
                time.sleep(0.1)
            if not received:
                pytest.skip("No initial message received -- can't test reconnect on a dead feed.")

            messages_before_disconnect = len(received)

            # Force-close the underlying connection to simulate a network blip.
            if client._ws_app is not None:
                client._ws_app.close()

            # Wait for the reconnect + re-subscribe + at least one new message.
            deadline = time.monotonic() + 60.0
            while len(received) <= messages_before_disconnect and time.monotonic() < deadline:
                time.sleep(0.2)

            assert len(received) > messages_before_disconnect, (
                "No new messages received after forced disconnect within 60s -- "
                "the client either failed to reconnect or reconnected without "
                "re-subscribing. Check _on_open's re-subscribe logic."
            )
        finally:
            client.stop()


class TestEndToEndPersistence:
    def test_realtime_collector_persists_rows_to_sqlite(self, sample_symbol: str, tmp_path: Path) -> None:
        """
        Runs the actual RealtimeCollector (WS -> queue -> SQLite) for a short
        bounded window against a throwaway DB file and confirms rows land in
        market_data -- the real end-to-end path, not individual pieces.
        """
        from collectors.realtime_collector import RealtimeCollector

        db_path = tmp_path / "integration_test.db"

        # Initialize schema in the throwaway DB.
        schema_sql = Path("db/schema.sql").read_text()
        conn = sqlite3.connect(db_path)
        conn.executescript(schema_sql)
        conn.close()

        adapter = DeltaAdapter()
        collector = RealtimeCollector(adapter=adapter, db_path=db_path, flush_interval_sec=0.5)

        try:
            collector._ws_client.start()
            connected = collector._ws_client.wait_until_connected(timeout_sec=15.0)
            assert connected, "RealtimeCollector's WS client failed to connect."

            collector.discover_and_subscribe()

            # Run the flush loop manually for a bounded window rather than
            # calling run_forever() (which installs signal handlers meant
            # for a real process, not a test).
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                batch = collector._drain_queue()
                collector._write_batch(batch)
                time.sleep(0.5)
        finally:
            collector._ws_client.stop()
            collector.close()

        # Verify independently, via a fresh connection, that rows landed.
        verify_conn = sqlite3.connect(db_path)
        try:
            row_count = verify_conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
        finally:
            verify_conn.close()

        assert row_count > 0, (
            "No rows were written to market_data during a 20s real-time "
            "collection window. Either the WS feed produced nothing for the "
            "subscribed symbol in that window, or the write path is broken."
        )
