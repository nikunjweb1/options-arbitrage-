"""
Shark Exchange public WebSocket client -- real-time market data feed.

SCOPE, ENFORCED: this file only ever connects to Shark's PUBLIC market-data
socket. It never touches an "-uds-" (User Data Stream) host, never sends an
api-key or signature, and has no code path that can place, edit, or cancel
an order. See exchange_adapters/shark_ws_capture.py's module docstring for
the reasoning -- the same host-refusal rule is duplicated here rather than
imported, so this file's safety property doesn't depend on another file
staying correct.

HONESTY NOTE -- READ BEFORE USING THIS IN PRODUCTION:
Unlike delta_ws.py (whose channel name and message shape were corrected
against a real live testnet connection -- see that file's docstring), the
event names and payload shapes below are NOT YET CONFIRMED against a real
Shark connection. As of this writing, exchange_adapters/shark_ws_capture.py
has only been verified to reach the Engine.IO handshake (HTTP 200 on
/socket.io/?EIO=4&transport=polling); no actual ticker/orderbook payload has
been captured yet.

So, per this project's own established rule (ev_engine.py Bug #2,
delta_ws.py's channel correction -- every real bug here was found by looking
at real data, never guessed): the parsing methods below (_parse_ticker,
_parse_depth) are STUBS. They raise NotImplementedError with a pointer back
to this docstring, on purpose, rather than silently returning made-up
field mappings that look plausible but were never checked. Fill them in only
after running shark_ws_capture.py against the real host and inspecting a
real captured payload -- then delete this paragraph and replace it with the
same kind of "confirmed against live connection on <date>" note delta_ws.py
has.

Protocol note (independently verifiable, not a guess): the captured URLs
(https://fawss-options.sharkexchange.in/socket.io/...) are Engine.IO v4 /
Socket.IO endpoints, same family as shark_ws_capture.py already documented.
This client uses python-socketio's Client for the same reason that capture
script does -- it handles the Engine.IO handshake and sid negotiation
correctly on its own; nothing here hardcodes or reuses a captured sid.

What IS reused safely from shark_ws_capture.py's findings: the "uds" host
exclusion rule, and the base library choice (python-socketio). What is NOT
reused: any specific event name or payload shape, because none has been
observed yet.

Design (mirrors delta_ws.py's public API on purpose, so the two adapters
are interchangeable from the caller's point of view):
  - Runs the socketio client in a background thread.
  - subscribe(symbols) / start() / stop() / wait_until_connected() -- same
    signatures as DeltaWebSocketClient.
  - Reconnects with backoff; python-socketio has its own built-in
    reconnection, configured here rather than hand-rolled, since re-deriving
    Engine.IO's reconnect/backoff semantics by hand would be exactly the
    kind of untested guesswork this file is trying to avoid elsewhere.
  - Delivers parsed MarketSnapshot objects to a caller-supplied callback,
    same as delta_ws.py -- this file does not know about SQLite.

Usage once the parser stubs are filled in:
    pip install "python-socketio[client]" --break-system-packages
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from normalization.schemas import MarketSnapshot

logger = logging.getLogger("shark_ws")

MarketSnapshotCallback = Callable[[MarketSnapshot], None]

# Only public, non-account hosts are ever allowed here. Duplicated from
# shark_ws_capture.py deliberately -- see module docstring.
_ALLOWED_HOST_SUBSTRINGS_MUST_NOT_CONTAIN = "uds"


def _refuse_if_uds(host: str) -> None:
    if _ALLOWED_HOST_SUBSTRINGS_MUST_NOT_CONTAIN in host.lower():
        raise ValueError(
            f"Refusing to connect to {host!r} -- 'uds' hosts are almost "
            "certainly account-authenticated (User Data Stream) channels, "
            "not public market data. This client only connects to public "
            "feeds. See this file's module docstring."
        )


class SharkWebSocketClient:
    """
    Public-data-only WebSocket client for Shark Exchange.

    NOT YET FUNCTIONAL END-TO-END: connects successfully (Engine.IO
    handshake only, confirmed manually via browser DevTools so far -- see
    module docstring), but _parse_ticker/_parse_depth are stubs until a real
    payload has been captured and inspected. Wiring this into
    collectors/realtime_collector.py before then would silently produce no
    data (or worse, mis-parsed data) rather than a clear, actionable error --
    so the stubs raise loudly instead.
    """

    def __init__(
        self,
        host: str,
        on_snapshot: MarketSnapshotCallback,
        reconnect_backoff_base_sec: float = 1.0,
        reconnect_backoff_max_sec: float = 30.0,
        reconnect_max_attempts: int = 0,  # 0 = unlimited, matches delta_ws.py's always-retry behavior
    ) -> None:
        _refuse_if_uds(host)

        self._host = host
        self._on_snapshot = on_snapshot
        self._reconnect_backoff_base = reconnect_backoff_base_sec
        self._reconnect_backoff_max = reconnect_backoff_max_sec
        self._reconnect_max_attempts = reconnect_max_attempts

        self._symbols: set[str] = set()
        self._symbols_lock = threading.Lock()

        self._sio = None  # socketio.Client, created lazily in start() so
        # importing this module doesn't require python-socketio to be
        # installed unless it's actually used.
        self._thread: threading.Thread | None = None
        self._should_run = False
        self._connected_event = threading.Event()

    # -- public API (mirrors DeltaWebSocketClient) ---------------------------

    def start(self) -> None:
        try:
            import socketio
        except ImportError as exc:
            raise RuntimeError(
                'python-socketio is not installed. Run: '
                'pip install "python-socketio[client]" --break-system-packages'
            ) from exc

        if self._thread is not None:
            raise RuntimeError("SharkWebSocketClient already started.")

        self._sio = socketio.Client(
            logger=False,
            engineio_logger=False,
            reconnection=True,
            reconnection_attempts=self._reconnect_max_attempts,
            reconnection_delay=self._reconnect_backoff_base,
            reconnection_delay_max=self._reconnect_backoff_max,
        )
        self._register_handlers()

        self._should_run = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="shark-ws")
        self._thread.start()

    def stop(self) -> None:
        self._should_run = False
        if self._sio is not None:
            self._sio.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def subscribe(self, symbols: list[str]) -> None:
        """
        Adds symbols to the tracked set and, if already connected, attempts
        to (re-)subscribe. NOTE: whether an explicit subscribe event is even
        required, and what its name/payload should be, is UNCONFIRMED -- see
        module docstring and shark_ws_capture.py's --subscribe-event flag,
        which exists specifically to find this out. _send_subscribe below is
        a stub for the same reason _parse_ticker/_parse_depth are.
        """
        with self._symbols_lock:
            new_symbols = [s for s in symbols if s not in self._symbols]
            self._symbols.update(symbols)

        if new_symbols and self._connected_event.is_set():
            self._send_subscribe(new_symbols)

    def wait_until_connected(self, timeout_sec: float = 15.0) -> bool:
        return self._connected_event.wait(timeout=timeout_sec)

    # -- internal: connection lifecycle --------------------------------------

    def _run(self) -> None:
        url = f"https://{self._host}"
        try:
            self._sio.connect(url, wait_timeout=15)
            self._sio.wait()  # blocks until disconnected; socketio handles reconnection internally
        except Exception as exc:  # noqa: BLE001 -- report whatever the real failure was
            logger.error("Shark WebSocket connection failed: %s", exc)
            self._connected_event.clear()

    def _register_handlers(self) -> None:
        sio = self._sio

        @sio.event
        def connect():
            logger.info("Shark WebSocket connected to %s", self._host)
            self._connected_event.set()
            with self._symbols_lock:
                symbols = list(self._symbols)
            if symbols:
                self._send_subscribe(symbols)

        @sio.event
        def connect_error(data):
            logger.error("Shark WebSocket connect_error: %s", data)
            self._connected_event.clear()

        @sio.event
        def disconnect():
            logger.warning("Shark WebSocket disconnected.")
            self._connected_event.clear()

        # Catch-all so nothing is silently dropped before the real event
        # names are known -- once _parse_ticker/_parse_depth are filled in,
        # this should be replaced with explicit @sio.on("<real_event_name>")
        # handlers, matching delta_ws.py's explicit "v2/ticker" type filter
        # rather than a catch-all.
        @sio.on("*")
        def catch_all(event, data=None):
            self._on_message(event, data)

    def _send_subscribe(self, symbols: list[str]) -> None:
        raise NotImplementedError(
            "Subscribe message shape is unconfirmed for Shark's public feed. "
            "Run exchange_adapters/shark_ws_capture.py against the real host "
            "first (with and without --subscribe-event) to find out whether "
            "a subscribe step is even required, and what it looks like if so. "
            "See this file's module docstring."
        )

    # -- internal: message parsing (STUBS -- see module docstring) ----------

    def _on_message(self, event: str, data: dict | list | None) -> None:
        logger.debug("Shark WS event %r: %s", event, str(data)[:300])
        # Once real event names are known, dispatch here, e.g.:
        #   if event == "<real_ticker_event_name>":
        #       snapshot = self._parse_ticker(data)
        #       if snapshot is not None:
        #           self._on_snapshot(snapshot)
        # Left as a no-op dispatcher until shark_ws_capture.py output confirms
        # what event names actually arrive.

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
        STUB. Do not fill this in from guesswork or from Delta's field names
        assumed-by-analogy -- Shark's REST docs (docs.sharkexchange.in) show
        a completely different JSON shape for order/position objects than
        Delta uses (e.g. "orderAmount" vs Delta's quantity fields, "symbol"
        strings like "BTCUSDT"/"BTCINR" rather than Delta's product_id
        integers), so there's no reason to assume the WS ticker shape
        transfers either. Fill in from a real captured payload only.
        """
        raise NotImplementedError(
            "Shark ticker payload shape not yet observed. Capture real data "
            "with shark_ws_capture.py first. See this file's module docstring."
        )

    def _parse_depth(self, data: dict) -> MarketSnapshot | None:
        """STUB -- same reasoning as _parse_ticker."""
        raise NotImplementedError(
            "Shark depth/orderbook payload shape not yet observed. Capture "
            "real data with shark_ws_capture.py first."
        )
