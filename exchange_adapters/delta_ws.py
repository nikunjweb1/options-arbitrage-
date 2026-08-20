"""
Delta Exchange India WebSocket client -- real-time market data feed.

IMPORTANT HONESTY NOTE (read before trusting this in production):
Delta's official WebSocket documentation lives behind a "Websocket Feed" /
"Subscribing to Channels" section of docs.delta.exchange that could not be
fully retrieved during this research pass (the page is too large to fetch in
one piece). What IS confirmed directly from the official docs table of
contents is the list of public channel names this file relies on:

    ticker, ob_l1, ob_l2, ob_updates, trades, mark_price, candlesticks,
    spot_price, spot_30mtwap_price, funding_rate, product_updates, system_status

The exact subscribe/unsubscribe JSON message shape used below --

    {"type": "subscribe", "payload": {"channels": [{"name": ..., "symbols": [...]}]}}

-- is reconstructed from two independent community sources (a public Python
client and a blog walkthrough), not copied verbatim from the official docs
page, because that page's relevant section didn't fit in the fetch. Both
sources agree with each other, which is reasonable but not the same as
reading it directly off docs.delta.exchange.

**Before relying on this for anything beyond Phase 2 data collection,
validate the actual message shapes against a live testnet connection** (see
tests/test_delta_ws_integration.py, gated the same way as the REST
integration suite) and update this docstring with what's actually observed.
Do not silently assume the shape below is exactly right.

Design:
  - Runs in a background thread (websocket-client's run_forever loop).
  - Subscribes to the "ticker" channel for a configurable symbol list --
    this is the channel that carries best_bid/best_ask/greeks/IV per the
    REST ticker schema, so parsing reuses the same field names as
    exchange_adapters/delta.py's REST ticker parsing wherever the shapes
    plausibly overlap.
  - Reconnects automatically with backoff on disconnect, and re-subscribes
    to the last-known symbol list on reconnect -- a silent reconnect that
    forgets subscriptions would reintroduce exactly the kind of gap Phase 2
    is trying to eliminate.
  - Delivers parsed MarketSnapshot objects to a caller-supplied callback;
    this file does not know about SQLite at all, by design -- see
    collectors/realtime_collector.py for the piece that writes to the DB.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

import websocket

from config.settings import DELTA
from normalization.schemas import MarketSnapshot

logger = logging.getLogger("delta_ws")

MarketSnapshotCallback = Callable[[MarketSnapshot], None]


class DeltaWebSocketClient:
    def __init__(
        self,
        on_snapshot: MarketSnapshotCallback,
        reconnect_backoff_base_sec: float = 1.0,
        reconnect_backoff_max_sec: float = 30.0,
        ping_interval_sec: int = 20,
    ) -> None:
        self._on_snapshot = on_snapshot
        self._ws_url = DELTA.ws_base_url
        self._reconnect_backoff_base = reconnect_backoff_base_sec
        self._reconnect_backoff_max = reconnect_backoff_max_sec
        self._ping_interval = ping_interval_sec

        self._symbols: set[str] = set()
        self._symbols_lock = threading.Lock()

        self._ws_app: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._should_run = False
        self._connected_event = threading.Event()

        self._reconnect_attempt = 0

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("DeltaWebSocketClient already started.")
        self._should_run = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="delta-ws")
        self._thread.start()

    def stop(self) -> None:
        self._should_run = False
        if self._ws_app is not None:
            self._ws_app.close()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def subscribe(self, symbols: list[str]) -> None:
        """
        Adds symbols to the tracked set and, if already connected, sends a
        subscribe message immediately. On reconnect, the full tracked set is
        always re-subscribed -- see _on_open.
        """
        with self._symbols_lock:
            new_symbols = [s for s in symbols if s not in self._symbols]
            self._symbols.update(symbols)

        if new_symbols and self._ws_app is not None and self._connected_event.is_set():
            self._send_subscribe(new_symbols)

    def wait_until_connected(self, timeout_sec: float = 15.0) -> bool:
        return self._connected_event.wait(timeout=timeout_sec)

    # -- internal: connection lifecycle -------------------------------------

    def _run_loop(self) -> None:
        while self._should_run:
            self._connected_event.clear()
            self._ws_app = websocket.WebSocketApp(
                self._ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._ws_app.run_forever(ping_interval=self._ping_interval)
            except Exception as exc:  # noqa: BLE001 -- keep the reconnect loop alive regardless of failure shape
                logger.error("WebSocket run_forever raised: %s", exc)

            if not self._should_run:
                break

            backoff = min(
                self._reconnect_backoff_base * (2 ** self._reconnect_attempt),
                self._reconnect_backoff_max,
            )
            self._reconnect_attempt += 1
            logger.warning("WebSocket disconnected -- reconnecting in %.1fs (attempt %d).",
                            backoff, self._reconnect_attempt)
            time.sleep(backoff)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        logger.info("WebSocket connected to %s", self._ws_url)
        self._reconnect_attempt = 0
        self._connected_event.set()
        with self._symbols_lock:
            symbols = list(self._symbols)
        if symbols:
            self._send_subscribe(symbols)

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code, close_msg) -> None:
        logger.warning("WebSocket closed: code=%s msg=%s", close_status_code, close_msg)
        self._connected_event.clear()

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        logger.error("WebSocket error: %s", error)

    def _send_subscribe(self, symbols: list[str]) -> None:
        # Message shape per the community-sourced format documented in this
        # module's docstring -- see the honesty note above before trusting
        # this blindly against production.
        payload = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": "ticker", "symbols": symbols},
                ]
            },
        }
        if self._ws_app is not None:
            self._ws_app.send(json.dumps(payload))
            logger.info("Subscribed to ticker channel for %d symbol(s).", len(symbols))

    # -- internal: message parsing --------------------------------------------

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Received non-JSON WebSocket message, ignoring: %r", message[:200])
            return

        msg_type = data.get("type")
        if msg_type != "ticker":
            # Subscription acks, heartbeats, and other channel types are
            # ignored here -- this client only cares about ticker snapshots.
            return

        snapshot = self._parse_ticker_message(data)
        if snapshot is not None:
            self._on_snapshot(snapshot)

    @staticmethod
    def _dec_or_none(v) -> Decimal | None:
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None

    def _parse_ticker_message(self, data: dict) -> MarketSnapshot | None:
        symbol = data.get("symbol")
        if not symbol:
            logger.warning("Ticker message missing symbol field, dropping: %r", data)
            return None

        quotes = data.get("quotes", {})
        greeks = data.get("greeks", {})

        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="delta_india",
            instrument_id=str(data.get("product_id", symbol)),
            best_bid=self._dec_or_none(quotes.get("best_bid")),
            best_ask=self._dec_or_none(quotes.get("best_ask")),
            bid_size=self._dec_or_none(quotes.get("bid_size")),
            ask_size=self._dec_or_none(quotes.get("ask_size")),
            last_price=self._dec_or_none(data.get("close")),
            mark_price=self._dec_or_none(data.get("mark_price")),
            index_price=self._dec_or_none(data.get("spot_price")),
            iv=self._dec_or_none(data.get("mark_vol")),
            delta=self._dec_or_none(greeks.get("delta")),
            gamma=self._dec_or_none(greeks.get("gamma")),
            theta=self._dec_or_none(greeks.get("theta")),
            vega=self._dec_or_none(greeks.get("vega")),
            open_interest=self._dec_or_none(data.get("oi")),
            volume_24h=self._dec_or_none(data.get("volume")),
            underlying_spot=self._dec_or_none(data.get("spot_price")),
            underlying_index=self._dec_or_none(data.get("spot_price")),
            underlying_futures=None,
            funding_rate=None,
        )
