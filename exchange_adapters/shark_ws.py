"""
Shark Exchange WebSocket client -- real-time OPTIONS market data.

CONFIRMATION STATUS -- major update 2026-08-23, now with the connection
host independently confirmed (previously inferred, then proven wrong by a
live 404 -- see below). This file was rewritten against REAL frames
captured directly from a live sharkexchange.in options session via browser
DevTools (Network tab -> WS -> Messages/Headers sub-tabs), while the option
chain for BTC-USDT was actively streaming. What follows is genuinely
confirmed, not inferred, except where explicitly marked otherwise.

CONFIRMED, from real captured frames:
  - Transport protocol is standard Engine.IO v4 + Socket.IO v4 -- NOT a
    raw/custom WebSocket protocol. This matters: a plain `websocket-client`
    connection (what this file used before, and what delta_ws.py correctly
    uses for Delta's genuinely-raw WS) CANNOT correctly speak this protocol
    -- it doesn't do the Engine.IO handshake, upgrade dance, or Socket.IO
    packet-type framing. This file uses the `python-socketio` client
    library instead, which speaks this protocol properly.
  - Confirmed handshake, in order (captured via DevTools XHR-polling
    frames before the WS upgrade): Engine.IO open packet
    `0{"sid":"...","upgrades":["websocket"],"pingInterval":180000,
    "pingTimeout":60000,...}`, then Socket.IO connect packet
    `40{"sid":"..."}` -- both handled automatically by python-socketio,
    documented here only so the shape is understood if something needs
    debugging by hand later.
  - CONFIRMED WS HOST (2026-08-23, via Headers tab of a live 101-status
    connection, replacing the earlier inferred-and-wrong
    `https://api.sharkexchange.in`):

        wss://fawss-options.sharkexchange.in/socket.io/?EIO=4&transport=websocket&sid=...

    This is a genuinely SEPARATE host from Shark's REST API host
    (api.sharkexchange.in) and from the futures WS host -- see the next
    point. The earlier guess assumed WS shared the REST host, which was
    wrong; options market data lives on its own dedicated subdomain.
  - BONUS FINDING from the same Headers capture: FOUR distinct WS hosts
    were observed connecting simultaneously on the live options page, not
    one:
        wss://fawss-options.sharkexchange.in       -- options public market data (this file uses this one)
        wss://fawss.sharkexchange.in                -- futures/general public market data (no "-options")
        wss://fawss-uds-options.sharkexchange.in    -- options AUTHENTICATED stream ("uds" = User Data
                                                          Stream, same terminology Binance uses for
                                                          order/position/balance update streams)
        wss://fawss-uds.sharkexchange.in            -- futures/general authenticated stream
    This is genuinely useful beyond just fixing the connection: it confirms
    where the authenticated order/position-update socket for options will
    live once that's built (fawss-uds-options.sharkexchange.in), and
    strongly suggests it's the real-time counterpart to the Listen Key
    lifecycle referenced in docs.sharkexchange.in's sidebar nav (Create/
    Get/Update/Delete Listen Key) -- i.e. mint a listen key via REST, then
    connect to fawss-uds-options.sharkexchange.in using it, following the
    same pattern this module's own bottom section already sketches (see
    the Listen Key placeholder functions -- still unconfirmed paths, but
    now with a confirmed destination host to connect the pieces to).
  - Confirmed real event names (captured live, multiple examples of each):
      * "ticker"     -- per-instrument live quote update
      * "orderBook"  -- per-instrument order book update
      * "indexPrice" -- underlying spot/index price update
  - Confirmed real options symbol format:
      "BTC-24AUG26-73000-P-USDT"  (BTC put, strike 73000, expires 24 Aug 2026, USDT-settled)
      "BTC-24AUG26-75000-C-USDT"  (BTC call, strike 75000, same expiry)
    Pattern: {BASE}-{DD}{MMM}{YY}-{STRIKE}-{C|P}-{QUOTE}
    This is the confirmed symbol format needed for get_option_chain() /
    get_ticker() / place_order() on shark.py -- see that file's updated
    docstring.
  - Confirmed "ticker" event fields (from a real captured payload,
    field list below is exactly what was visible in the captured frame --
    the frame was truncated in the DevTools panel before the full payload
    printed, so treat this as CONFIRMED-PRESENT, not CONFIRMED-COMPLETE):
      symbol, bidPrice, bidSize, bidIv, askPrice, askSize, askIv,
      lastPrice, highPrice24h, lowPrice24h, ... (truncated -- likely
      continues with markPrice/markIv/greeks/openInterest/volume based on
      what a usable options ticker needs, but NOT confirmed -- treat any
      field not in the list above as unconfirmed until independently seen).
  - Confirmed "orderBook" event fields (also truncated in capture):
      {"bids": [[price_str, size_str], [price_str, size_str], ...], ...}
      "asks" almost certainly exists symmetrically but was cut off in the
      captured frame -- NOT independently confirmed, treated as probable.
  - Confirmed "indexPrice" event, FULL payload (this one was short enough
    to capture completely):
      {"indexPrice": "77242.1492131", "baseCoin": "BTC", "quoteCoin": "USDT"}

STILL NOT CONFIRMED:
  - Whether a "subscribe" call is even required. No outgoing subscribe
    frame was visible in the captured session -- ticker/orderBook/
    indexPrice events for multiple different strikes were streaming
    without an observed subscribe emit. Two explanations, both plausible:
    (a) the subscribe happened before DevTools started recording, or
    (b) this socket just broadcasts all live options data for the loaded
    underlying with no per-symbol subscribe step at all. This file emits a
    "subscribe" event as a reasonable, low-risk attempt (harmless if
    ignored) but does NOT rely on it being necessary -- the on_ticker
    handler processes any ticker event received regardless of whether an
    explicit subscribe was acknowledged.
  - The Listen Key REST paths for the authenticated fawss-uds-options
    stream -- see this module's bottom section, still placeholder.
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

# CONFIRMED 2026-08-23 via live DevTools Headers capture (Request URL of a
# real 101 Switching Protocols connection). Replaces the earlier inferred
# `https://api.sharkexchange.in`, which returned a live 404 -- options
# market data runs on its own dedicated subdomain, separate from the REST
# API host and from the futures WS host. See module docstring's BONUS
# FINDING note for the other three hosts observed alongside this one.
SHARK_OPTIONS_WS_URL = "https://fawss-options.sharkexchange.in"

# Authenticated counterpart, confirmed to exist (same Headers capture) but
# not yet wired up -- needs a Listen Key first. See module docstring.
SHARK_OPTIONS_UDS_WS_URL = "https://fawss-uds-options.sharkexchange.in"


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
        self._sio.connect(self._ws_url, transports=["websocket"], wait_timeout=15)

    def stop(self) -> None:
        if self._sio.connected:
            self._sio.disconnect()

    def subscribe(self, symbols: list[str]) -> None:
        """
        Emits a "subscribe" event with the given symbols -- see module
        docstring's caveat that this wasn't confirmed necessary in the
        captured session. Harmless either way: if Shark ignores this event,
        ticker/orderBook/indexPrice events still get processed by this
        client's handlers regardless.
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
        # NOT confirmed (see module docstring) -- no outgoing subscribe
        # frame was captured to copy exactly.
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
        # Not yet wired into MarketSnapshot -- MarketSnapshot per
        # normalization/schemas.py is a top-of-book (best_bid/best_ask)
        # shape, not a full depth shape. Logged at DEBUG for now; revisit
        # if/when full-depth data is actually needed (e.g. for slippage
        # modeling -- see backtest/replay.py's KNOWN LIMITATION -- SLIPPAGE
        # note, which is exactly the kind of thing full depth could help
        # close eventually).
        logger.debug("orderBook event received (not yet parsed into a snapshot): %r", data)

    def _on_index_price(self, data: dict) -> None:
        # Full confirmed shape: {"indexPrice": ..., "baseCoin": ..., "quoteCoin": ...}
        # Not currently forwarded anywhere -- collectors/realtime_collector.py
        # (or equivalent) would need a separate index-price sink, since
        # MarketSnapshot's underlying_index field is populated per-instrument
        # in _parse_ticker below, not as its own event stream. Logged for
        # now so this data isn't silently dropped without a trace.
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
        """
        Field mapping here uses ONLY confirmed-present fields from the real
        captured frame (see module docstring). Fields not confirmed present
        (mark_price, greeks, open_interest, volume) are left None rather
        than guessed at a field name that might not exist -- consistent
        with this codebase's fail-closed principle elsewhere (e.g.
        pricing/ev_engine.py's InsufficientDataError).
        """
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
            mark_price=None,  # not confirmed present -- see docstring
            index_price=None,  # delivered via separate "indexPrice" event, not per-ticker -- see _on_index_price
            iv=self._dec_or_none(data.get("askIv")),  # using ask-side IV as the confirmed-present IV field;
                                                        # bidIv also exists but MarketSnapshot has one iv slot --
                                                        # revisit which side is more appropriate once this is
                                                        # actually consumed by pricing/ev_engine.py for Shark legs.
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
#
# PLACEHOLDER PATHS -- not confirmed. Guessed following the Binance/MEXC
# convention (`/v1/userDataStream` for POST/PUT/DELETE) since Shark's docs
# sidebar confirms this feature EXISTS (Create/Get/Update/Delete Listen Key
# are real section headers) but the actual REST paths were never reached in
# any doc fetch attempt. What IS now confirmed is the destination host to
# connect to once a listen key is minted: SHARK_OPTIONS_UDS_WS_URL above.

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
