"""
Shark Exchange WebSocket client -- real-time market data + (eventually)
authenticated order/position updates.

CONFIRMATION STATUS -- more unconfirmed than exchange_adapters/delta_ws.py
was at the equivalent point, and that file's own docstring is the template
for how this one is written: confirmed pieces stated plainly, unconfirmed
pieces flagged loudly rather than guessed silently.

CONFIRMED (from docs.sharkexchange.in's sidebar navigation structure):
  - There IS a documented WebSocket surface, split into:
      * Public Web Sockets (market data, presumably no auth -- matches the
        REST pattern where /v1/market/* endpoints are public)
      * Authenticated Web Sockets, gated behind a Listen Key lifecycle:
        Create Listen Key / Get Listen Key / Update Listen Key / Delete
        Listen Key -- this is the same pattern Binance, MEXC, and HashKey
        Global all use (POST to mint a key, PUT/GET to keep it alive,
        DELETE to close it, then connect wss://.../ws?listenKey=... or
        similar and the key scopes the authenticated stream to your
        account without putting your API secret on the wire per-message).

NOT CONFIRMED -- genuinely unknown, not guessed, despite two full-page
fetch attempts and a targeted search (2026-08-23) that all failed to
surface this specific content (the docs page is a single very large
Slate-style doc that truncates before reaching the WebSocket section in
every fetch attempted):
  - The actual `wss://` base URL.
  - The exact REST paths for the Create/Get/Update/Delete Listen Key calls
    (guessed below as a Binance-style `/v1/userDataStream` convention --
    this is a PLACEHOLDER, not a confirmed path, and is clearly marked as
    such at its point of use).
  - Public channel names (e.g. whatever Shark calls its ticker/depth/trade
    streams) -- delta_ws.py's own history is the cautionary tale here: the
    "obvious" guess ("ticker") was wrong for Delta (the working name was
    "v2/ticker", only discovered by live-testing). Assume the same risk
    applies here and do NOT trust any channel name in this file until it's
    been confirmed against a live connection.
  - Message envelope shape (subscribe/unsubscribe JSON format, ping/pong
    convention) -- Shark's REST responses use a plain JSON body (not an
    envelope like CoinSwitch HFT's {"retCode", "retMsg", "result"}), so the
    WS envelope is presented below as a guess following that same plain
    convention, but this is unverified.

FASTEST PATH TO CONFIRMING THE MISSING PIECES: open sharkexchange.in in a
browser, log in, open DevTools -> Network tab -> filter "WS", visit a live
options/futures price page, and copy the exact wss:// URL and the first few
subscribe/message frames from the live connection. That is strictly more
reliable than anything further web research can produce here, since it's
Shark's own official UI talking to Shark's own backend.

Until ws_url is supplied and the channel name(s)/message shapes below are
corrected against a real connection, treat this file as a scaffold with the
reconnect/threading logic ready to go, not as something that will
successfully receive real data yet.
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

from normalization.schemas import MarketSnapshot

logger = logging.getLogger("shark_ws")

MarketSnapshotCallback = Callable[[MarketSnapshot], None]

# PLACEHOLDER -- not confirmed. Best guess at a name for Shark's ticker
# stream, following the REST endpoint's naming (ticker24Hr). Verify against
# a live connection (see module docstring) before trusting this.
_TICKER_CHANNEL_GUESS = "ticker24Hr"


class SharkWebSocketClient:
    """
    Structurally mirrors exchange_adapters/delta_ws.py's DeltaWebSocketClient
    (background thread, auto-reconnect with backoff, re-subscribe on
    reconnect, caller-supplied MarketSnapshot callback) so the two clients
    are interchangeable from the collector's point of view. The one required
    difference: `ws_url` must be passed in explicitly rather than read from
    a config singleton, since it isn't confirmed yet -- see module docstring.
    """

    def __init__(
        self,
        ws_url: str,
        on_snapshot: MarketSnapshotCallback,
        reconnect_backoff_base_sec: float = 1.0,
        reconnect_backoff_max_sec: float = 30.0,
        ping_interval_sec: int = 20,
    ) -> None:
        if not ws_url:
            raise ValueError(
                "ws_url is required and not confirmed for Shark yet -- capture it from "
                "browser DevTools (Network tab, filter WS) against a live sharkexchange.in "
                "session. See this module's docstring."
            )
        self._on_snapshot = on_snapshot
        self._ws_url = ws_url
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

    # -- public API -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("SharkWebSocketClient already started.")
        self._should_run = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="shark-ws")
        self._thread.start()

    def stop(self) -> None:
        self._should_run = False
        if self._ws_app is not None:
            self._ws_app.close()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def subscribe(self, symbols: list[str]) -> None:
        with self._symbols_lock:
            new_symbols = [s for s in symbols if s not in self._symbols]
            self._symbols.update(symbols)
        if new_symbols and self._ws_app is not None and self._connected_event.is_set():
            self._send_subscribe(new_symbols)

    def wait_until_connected(self, timeout_sec: float = 15.0) -> bool:
        return self._connected_event.wait(timeout=timeout_sec)

    # -- internal: connection lifecycle (identical pattern to delta_ws.py) --

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
            logger.warning("Shark WebSocket disconnected -- reconnecting in %.1fs (attempt %d).",
                            backoff, self._reconnect_attempt)
            time.sleep(backoff)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        logger.info("Shark WebSocket connected to %s", self._ws_url)
        self._reconnect_attempt = 0
        self._connected_event.set()
        with self._symbols_lock:
            symbols = list(self._symbols)
        if symbols:
            self._send_subscribe(symbols)

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code, close_msg) -> None:
        logger.warning("Shark WebSocket closed: code=%s msg=%s", close_status_code, close_msg)
        self._connected_event.clear()

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        logger.error("Shark WebSocket error: %s", error)

    def _send_subscribe(self, symbols: list[str]) -> None:
        # UNCONFIRMED message shape -- guessed following the same
        # {"type": ..., "payload": {...}} envelope Delta uses, since no real
        # Shark subscribe frame was captured. REPLACE once confirmed via
        # DevTools (see module docstring) -- do not trust this as-is.
        payload = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": _TICKER_CHANNEL_GUESS, "symbols": symbols},
                ]
            },
        }
        if self._ws_app is not None:
            self._ws_app.send(json.dumps(payload))
            logger.info(
                "Sent (UNCONFIRMED shape) subscribe for channel=%s, %d symbol(s). "
                "Verify Shark actually acknowledges this before trusting data flow.",
                _TICKER_CHANNEL_GUESS, len(symbols),
            )

    # -- internal: message parsing -----------------------------------------

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Received non-JSON Shark WebSocket message, ignoring: %r", message[:200])
            return

        # UNCONFIRMED filter -- see _send_subscribe's caveat. Log unknown
        # message shapes at INFO (not silently dropped) while this is being
        # bootstrapped, so real frames can be inspected and this file
        # corrected accordingly.
        msg_type = data.get("type")
        if msg_type != _TICKER_CHANNEL_GUESS:
            logger.info("Shark WS message with unrecognized type=%r, contents=%r -- inspect and "
                        "update this client's parsing once real shapes are known.", msg_type, data)
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
        """
        UNCONFIRMED field mapping -- guessed from the REST /v1/market/
        ticker24Hr and /v1/order/place-order response field names
        (symbol, bidPrice/askPrice-style, baseAsset/quoteAsset) as the most
        plausible analogues, since no real WS ticker payload was captured.
        Correct this against a real message before trusting any output.
        """
        symbol = data.get("symbol")
        if not symbol:
            logger.warning("Shark ticker message missing symbol field, dropping: %r", data)
            return None

        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id=symbol,
            best_bid=self._dec_or_none(data.get("bidPrice")),
            best_ask=self._dec_or_none(data.get("askPrice")),
            bid_size=self._dec_or_none(data.get("bidQty")),
            ask_size=self._dec_or_none(data.get("askQty")),
            last_price=self._dec_or_none(data.get("lastPrice")),
            mark_price=self._dec_or_none(data.get("markPrice")),
            index_price=self._dec_or_none(data.get("indexPrice")),
            iv=None,  # not confirmed present on Shark's ticker payload at all
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            open_interest=self._dec_or_none(data.get("openInterest")),
            volume_24h=self._dec_or_none(data.get("volume")),
            underlying_spot=self._dec_or_none(data.get("indexPrice")),
            underlying_index=self._dec_or_none(data.get("indexPrice")),
            underlying_futures=None,
            funding_rate=self._dec_or_none(data.get("fundingRate")),
        )


# -- Listen Key lifecycle (Authenticated WebSocket) --------------------------
#
# PLACEHOLDER PATHS -- not confirmed. Guessed following the Binance/MEXC
# convention (`/v1/userDataStream` for POST/PUT/DELETE) since Shark's docs
# sidebar confirms this feature EXISTS (Create/Get/Update/Delete Listen Key
# are real section headers) but the actual paths were never reached in any
# fetch attempt. Confirm via DevTools or by requesting docs from Shark
# support before relying on these for anything real.

_LISTEN_KEY_PATH_GUESS = "/v1/userDataStream"


def create_listen_key(adapter) -> str:
    """Takes a SharkAdapter (for its signed _post helper) -- PLACEHOLDER path, unconfirmed."""
    resp = adapter._post(_LISTEN_KEY_PATH_GUESS)
    return resp["listenKey"]


def keepalive_listen_key(adapter, listen_key: str) -> None:
    raise NotImplementedError(
        "Needs a signed PUT helper on SharkAdapter (not yet implemented) and a confirmed "
        "path -- see module docstring."
    )


def delete_listen_key(adapter, listen_key: str) -> None:
    adapter._delete(_LISTEN_KEY_PATH_GUESS, {"listenKey": listen_key})
