"""
Shark Exchange WebSocket client -- real-time OPTIONS market data.

CONFIRMATION STATUS -- major update 2026-08-23/24, now with the connection
host independently confirmed (previously inferred, then proven wrong by a
live 404 -- see below), and a live smoke test run against it (2026-08-24)
that connects successfully but has not yet received data -- see the
ZERO-DATA INVESTIGATION note near the bottom, which is the active open
question as of this revision.

CONFIRMED, from real captured frames:
  - Transport protocol is standard Engine.IO v4 + Socket.IO v4 -- NOT a
    raw/custom WebSocket protocol. This file uses the `python-socketio`
    client library, which speaks this protocol properly (a plain
    `websocket-client` connection, correct for Delta's genuinely-raw WS in
    delta_ws.py, CANNOT correctly speak Engine.IO/Socket.IO framing).
  - Confirmed handshake, in order (captured via DevTools XHR-polling
    frames before the WS upgrade): Engine.IO open packet
    `0{"sid":"...","upgrades":["websocket"],"pingInterval":180000,
    "pingTimeout":60000,...}`, then Socket.IO connect packet
    `40{"sid":"..."}` -- both handled automatically by python-socketio.
  - CONFIRMED WS HOST (2026-08-23, via Headers tab of a live 101-status
    connection, replacing an earlier inferred-and-wrong
    `https://api.sharkexchange.in`, which returned a live 404):

        wss://fawss-options.sharkexchange.in/socket.io/?EIO=4&transport=websocket&sid=...

    Independently RE-CONFIRMED 2026-08-24: connecting to this host with
    python-socketio succeeds (101 Switching Protocols equivalent, stable
    connection, clean 20s session) -- so the host itself is solid. The
    open question now (see below) is why no events arrived, not whether
    the connection works.
  - BONUS FINDING from the same Headers capture: FOUR distinct WS hosts
    run simultaneously on the live options page:
        wss://fawss-options.sharkexchange.in       -- options public market data (this file uses this one)
        wss://fawss.sharkexchange.in                -- futures/general public market data (no "-options")
        wss://fawss-uds-options.sharkexchange.in    -- options AUTHENTICATED stream ("uds" = User Data Stream)
        wss://fawss-uds.sharkexchange.in            -- futures/general authenticated stream
    Confirms where the authenticated order/position-update socket for
    options lives once built, likely the live counterpart to the Listen
    Key REST lifecycle referenced in docs.sharkexchange.in's sidebar (see
    this file's bottom section).
  - Confirmed real event names (captured live, multiple examples of each):
      * "ticker"     -- per-instrument live quote update
      * "orderBook"  -- per-instrument order book update
      * "indexPrice" -- underlying spot/index price update
  - Confirmed real options symbol format:
      "BTC-24AUG26-73000-P-USDT"  (BTC put, strike 73000, expires 24 Aug 2026, USDT-settled)
    Pattern: {BASE}-{DD}{MMM}{YY}-{STRIKE}-{C|P}-{QUOTE}
  - Confirmed "ticker" event fields (CONFIRMED-PRESENT, not necessarily
    CONFIRMED-COMPLETE -- the captured frame was truncated in the DevTools
    panel before the full payload printed):
      symbol, bidPrice, bidSize, bidIv, askPrice, askSize, askIv,
      lastPrice, highPrice24h, lowPrice24h, ...
  - Confirmed "orderBook" event fields (also truncated in capture):
      {"bids": [[price_str, size_str], ...], ...}  ("asks" assumed symmetric, not independently confirmed)
  - Confirmed "indexPrice" event, FULL payload:
      {"indexPrice": "77242.1492131", "baseCoin": "BTC", "quoteCoin": "USDT"}

ZERO-DATA INVESTIGATION (active, 2026-08-24): a live smoke test against
SHARK_OPTIONS_WS_URL connected successfully and stayed open for 20 seconds,
but zero ticker/orderBook/indexPrice events arrived, despite subscribing to
a symbol ("BTC-24AUG26-73000-P-USDT") that was confirmed streaming live
moments earlier in a real browser session against the same host. Two
live hypotheses, not yet disambiguated, being tried in order of
cost-to-test:
  1. (TRIED THIS REVISION) Origin/Referer header mismatch. Real exchanges
     commonly gate event PUSH logic on request headers looking like a
     genuine browser tab, even when the initial handshake/upgrade succeeds
     regardless of headers (handshake success != authorized to receive
     data). This revision adds Origin/Referer headers matching a real
     sharkexchange.in options page to connect() -- see start()'s inline
     comment. If this alone fixes it, the fix was this cheap.
  2. (NOT YET TRIED) The "subscribe" event name/payload shape is wrong. No
     outgoing subscribe frame was ever captured directly -- it's possible
     the real page sends something differently-named/shaped, or sends it
     as part of the initial connection (e.g. Socket.IO auth/query
     payload) rather than as a post-connect emit. If headers alone don't
     fix it, the next step is a fresh DevTools capture of the EXACT
     outgoing (upward-arrow) frames sent in the few hundred ms right after
     a fresh page load's "40{sid:...}" connect ack -- previous captures in
     this investigation only caught steady-state ping/pong frames, not the
     initial subscribe (if one exists).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

import socketio

from normalization.schemas import MarketSnapshot

logger = logging.getLogger("shark_ws")

MarketSnapshotCallback = Callable[[MarketSnapshot], None]

# CONFIRMED 2026-08-23 via live DevTools Headers capture, RE-CONFIRMED
# 2026-08-24 via a live successful connection. Replaces the earlier
# inferred `https://api.sharkexchange.in`, which returned a live 404.
SHARK_OPTIONS_WS_URL = "https://fawss-options.sharkexchange.in"

# Authenticated counterpart, confirmed to exist (same Headers capture) but
# not yet wired up -- needs a Listen Key first. See module docstring.
SHARK_OPTIONS_UDS_WS_URL = "https://fawss-uds-options.sharkexchange.in"

# Matches a real browser tab's headers when loading the BTC options chain.
# Added 2026-08-24 as the first attempt at fixing the ZERO-DATA
# INVESTIGATION described in the module docstring -- see start()'s comment.
_BROWSER_MATCHING_HEADERS = {
    "Origin": "https://sharkexchange.in",
    "Referer": "https://sharkexchange.in/options/btcusdt",
}


class SharkWebSocketClient:
    """
    Thin wrapper around python-socketio's Client, giving this class the same
    public shape (start/stop/subscribe/wait_until_connected) as
    delta_ws.py's DeltaWebSocketClient for interchangeability, even though
    the underlying transport mechanics are different (Socket.IO vs raw WS)
    and python-socketio handles reconnection internally rather than needing
    our own backoff loop.
    """

    def __init__(
        self,
        on_snapshot: MarketSnapshotCallback,
        ws_url: str = SHARK_OPTIONS_WS_URL,
        reconnection_attempts: int = 0,  # 0 = retry forever, matches delta_ws.py's never-give-up behavior
        reconnection_delay: float = 1.0,
        reconnection_delay_max: float = 30.0,
    ) -> None:
        self._on_snapshot = on_snapshot
        self._ws_url = ws_url
        self._sio = socketio.Client(
            reconnection=True,
            reconnection_attempts=reconnection_attempts,
            reconnection_delay=reconnection_delay,
            reconnection_delay_max=reconnection_delay_max,
            logger=False,
            engineio_logger=False,
        )
        self._pending_symbols: set[str] = set()

        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)
        self._sio.on("ticker", self._on_ticker)
        self._sio.on("orderBook", self._on_order_book)
        self._sio.on("indexPrice", self._on_index_price)

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        # transport=["websocket"] skips the polling handshake step and
        # connects directly via WS -- confirmed acceptable since the real
        # session's Engine.IO open packet advertised "upgrades":["websocket"].
        #
        # headers=_BROWSER_MATCHING_HEADERS added 2026-08-24: see module
        # docstring's ZERO-DATA INVESTIGATION note. A first live test
        # connected successfully but received zero events; this is attempt
        # #1 at fixing that (Origin/Referer header gating on server-side
        # push logic, distinct from handshake-level auth). If a subsequent
        # test still receives zero events, this is NOT the cause and
        # attempt #2 (verifying the subscribe shape) is next.
        self._sio.connect(
            self._ws_url, transports=["websocket"], wait_timeout=15, headers=_BROWSER_MATCHING_HEADERS
        )

    def stop(self) -> None:
        if self._sio.connected:
            self._sio.disconnect()

    def subscribe(self, symbols: list[str]) -> None:
        """
        Emits a "subscribe" event with the given symbols -- see module
        docstring's ZERO-DATA INVESTIGATION note: this shape is NOT
        confirmed, and is the leading suspect if header-matching alone
        doesn't fix the zero-data issue. Harmless either way: if Shark
        ignores this event, ticker/orderBook/indexPrice events (if they
        arrive at all) still get processed by this client's handlers
        regardless of whether this specific emit was meaningful.
        """
        self._pending_symbols.update(symbols)
        if self._sio.connected:
            self._send_subscribe(symbols)

    def wait_until_connected(self, timeout_sec: float = 15.0) -> bool:
        return self._sio.connected

    # -- internal: socketio event handlers -----------------------------------

    def _on_connect(self) -> None:
        logger.info("Shark Socket.IO connected to %s", self._ws_url)
        if self._pending_symbols:
            self._send_subscribe(list(self._pending_symbols))

    def _on_disconnect(self) -> None:
        logger.warning("Shark Socket.IO disconnected -- python-socketio will auto-reconnect.")

    def _send_subscribe(self, symbols: list[str]) -> None:
        # Event name/payload shape here is a reasonable-convention attempt,
        # NOT confirmed (see module docstring's ZERO-DATA INVESTIGATION) --
        # no outgoing subscribe frame was captured to copy exactly.
        try:
            self._sio.emit("subscribe", {"symbols": symbols})
            logger.info("Emitted subscribe for %d symbol(s) (shape unconfirmed, see docstring).", len(symbols))
        except Exception as exc:  # noqa: BLE001 -- don't let an unconfirmed emit shape crash the client
            logger.warning("subscribe emit failed (non-fatal, unconfirmed shape): %s", exc)

    def _on_ticker(self, data: dict) -> None:
        snapshot = self._parse_ticker(data)
        if snapshot is not None:
            self._on_snapshot(snapshot)

    def _on_order_book(self, data: dict) -> None:
        logger.debug("orderBook event received (not yet parsed into a snapshot): %r", data)

    def _on_index_price(self, data: dict) -> None:
        logger.debug("indexPrice event received (not yet forwarded): %r", data)

    @staticmethod
    def _dec_or_none(v) -> Decimal | None:
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None

    def _parse_ticker(self, data: dict) -> MarketSnapshot | None:
        symbol = data.get("symbol")
        if not symbol:
            logger.warning("Shark ticker event missing symbol field, dropping: %r", data)
            return None

        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id=symbol,
            best_bid=self._dec_or_none(data.get("bidPrice")),
            best_ask=self._dec_or_none(data.get("askPrice")),
            bid_size=self._dec_or_none(data.get("bidSize")),
            ask_size=self._dec_or_none(data.get("askSize")),
            last_price=self._dec_or_none(data.get("lastPrice")),
            mark_price=None,
            index_price=None,
            iv=self._dec_or_none(data.get("askIv")),
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            open_interest=None,
            volume_24h=None,
            underlying_spot=None,
            underlying_index=None,
            underlying_futures=None,
            funding_rate=None,
        )


# -- Listen Key lifecycle (Authenticated WebSocket, fawss-uds-options) ------
_LISTEN_KEY_PATH_GUESS = "/v1/userDataStream"


def create_listen_key(adapter) -> str:
    resp = adapter._post(_LISTEN_KEY_PATH_GUESS)
    return resp["listenKey"]


def keepalive_listen_key(adapter, listen_key: str) -> None:
    raise NotImplementedError(
        "Needs a signed PUT helper on SharkAdapter (not yet implemented) and a confirmed "
        "path -- see module docstring."
    )


def delete_listen_key(adapter, listen_key: str) -> None:
    adapter._delete(_LISTEN_KEY_PATH_GUESS, {"listenKey": listen_key})
