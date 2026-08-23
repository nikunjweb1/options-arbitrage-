"""
Shark Exchange public WebSocket client -- real-time market data feed.

SCOPE, ENFORCED: this file only ever connects to Shark's PUBLIC market-data
socket. It never touches an "-uds-" (User Data Stream) host, never sends an
api-key or signature, and has no code path that can place, edit, or cancel
an order. See exchange_adapters/shark_ws_capture.py's module docstring for
the reasoning -- the same host-refusal rule is duplicated here rather than
imported, so this file's safety property doesn't depend on another file
staying correct.

STATUS UPDATE (2026-08-23): _parse_ticker / _parse_depth / _parse_index are
no longer stubs. Real payloads were captured via browser DevTools (Network ->
WS -> Messages) against a live connection to
`fawss-options.sharkexchange.in` and inspected directly -- not guessed, not
assumed-by-analogy from Delta's field names, per this project's own rule
(see architecture.md Section C, ev_engine.py Bug #2).

CONFIRMED from real captured frames:
  - Engine.IO v4 framing: "42" prefix + JSON array [event_name, payload],
    e.g. `42["ticker",{...}]`. Bare "2"/"3" and "2probe"/"3probe" frames are
    Engine.IO ping/pong and upgrade-probe control frames, not app events --
    correctly ignored by python-socketio itself, never reach this file's
    handlers.
  - "ticker" event payload confirmed fields: symbol, bidPrice, bidSize,
    bidIv, askPrice, askSize, askIv, lastPrice, highPrice24h, lowPrice24h.
  - "orderBook" event payload confirmed fields: bids (list of [price, size]
    string pairs). An "asks" array is almost certainly present (mirrors
    "bids") but was cut off in every captured frame before it could be
    read -- see OPEN ITEM below.
  - "indexPrice" event payload confirmed fields (small enough to capture in
    full): indexPrice, baseCoin, quoteCoin.

OPEN ITEM, NOT YET CONFIRMED -- do not assume these exist or use these names:
  - Every captured "ticker" and "orderBook" frame was truncated (DevTools
    row preview, not the full JSON) after the fields listed above. There is
    almost certainly more in the real payload -- e.g. volume/open-interest
    fields on ticker, an "asks" array on orderBook, and possibly a
    timestamp. Until a full, untruncated frame is captured (click the row in
    DevTools -> Payload/Preview shows the full JSON, or add
    `--dump-full-json` handling to shark_ws_capture.py), this parser reads
    only the fields confirmed above and leaves the rest unmapped rather than
    guessing. `_RAW_UNPARSED_KEYS_SEEN` below is populated at runtime so you
    can log/inspect exactly what additional keys show up once real traffic
    flows, without having to re-open DevTools.
  - No timestamp field was observed in any captured frame. Until confirmed
    otherwise, MarketSnapshot.timestamp is stamped with ingestion time
    (datetime.now(timezone.utc)), same fallback delta_ws.py uses when a
    feed doesn't provide its own timestamp -- NOT a claim that this is the
    exchange's own event time.
  - Whether an explicit subscribe event is required (and its shape) is
    STILL unconfirmed -- _send_subscribe below remains a stub. The captured
    ticker/orderBook/indexPrice frames arrived without this file ever having
    sent a subscribe message, which suggests the public feed may just push
    a default symbol set unsolicited -- but that's an observation, not a
    confirmed protocol fact, until checked deliberately (e.g. via
    shark_ws_capture.py --subscribe-event against a *different* symbol than
    whatever loads by default on the page).

Protocol note (independently verifiable, not a guess): the captured URLs
(https://fawss-options.sharkexchange.in/socket.io/...) are Engine.IO v4 /
Socket.IO endpoints, same family as shark_ws_capture.py already documented.
This client uses python-socketio's Client for the same reason that capture
script does -- it handles the Engine.IO handshake and sid negotiation
correctly on its own; nothing here hardcodes or reuses a captured sid.

Design (mirrors delta_ws.py's public API on purpose, so the two adapters
are interchangeable from the caller's point of view):
  - Runs the socketio client in a background thread.
  - subscribe(symbols) / start() / stop() / wait_until_connected() -- same
    signatures as DeltaWebSocketClient.
  - Reconnects with backoff; python-socketio has its own built-in
    reconnection, configured here rather than hand-rolled.
  - Delivers parsed MarketSnapshot objects to a caller-supplied callback,
    same as delta_ws.py -- this file does not know about SQLite.

Usage:
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

# Fields confirmed present on a real captured "ticker" frame (see module
# docstring). Anything else that shows up is logged, not silently dropped,
# via _RAW_UNPARSED_KEYS_SEEN.
_CONFIRMED_TICKER_FIELDS = {
    "symbol", "bidPrice", "bidSize", "bidIv", "askPrice", "askSize",
    "askIv", "lastPrice", "highPrice24h", "lowPrice24h",
}
_CONFIRMED_ORDERBOOK_FIELDS = {"bids", "asks"}
_CONFIRMED_INDEXPRICE_FIELDS = {"indexPrice", "baseCoin", "quoteCoin"}


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

    Ticker, orderBook (bids side), and indexPrice events are parsed against
    real captured payloads (see module docstring). The orderBook "asks" side
    and any fields beyond what's listed in _CONFIRMED_*_FIELDS are NOT yet
    confirmed -- see OPEN ITEM in the module docstring before trusting this
    for anything beyond the confirmed fields.
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

        # Populated at runtime with any JSON keys seen on ticker/orderBook/
        # indexPrice frames that are NOT in the confirmed field sets above.
        # Inspect this (e.g. from a REPL or a debug log line) to find out
        # what the truncated DevTools captures were hiding, without having
        # to go back to the browser.
        self._raw_unparsed_keys_seen: set[str] = set()

        self._sio = None  # socketio.Client, created lazily in start()
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
        Adds symbols to the tracked set. NOTE: real captured traffic arrived
        WITHOUT this client ever sending a subscribe message (see module
        docstring OPEN ITEM) -- so for now this only tracks symbols for
        filtering in _on_message; it does not attempt to send an unconfirmed
        subscribe frame. If/when a real subscribe event name is confirmed
        via shark_ws_capture.py, wire it in here and in _send_subscribe.
        """
        with self._symbols_lock:
            self._symbols.update(symbols)

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

        @sio.event
        def connect_error(data):
            logger.error("Shark WebSocket connect_error: %s", data)
            self._connected_event.clear()

        @sio.event
        def disconnect():
            logger.warning("Shark WebSocket disconnected.")
            self._connected_event.clear()

        @sio.on("ticker")
        def on_ticker(data):
            self._dispatch("ticker", data, self._parse_ticker)

        @sio.on("orderBook")
        def on_orderbook(data):
            self._dispatch("orderBook", data, self._parse_depth)

        @sio.on("indexPrice")
        def on_index_price(data):
            self._dispatch("indexPrice", data, self._parse_index)

        # Anything outside the three confirmed event names above still
        # arrives here so nothing is silently dropped while more of the
        # protocol gets confirmed.
        @sio.on("*")
        def catch_all(event, data=None):
            if event not in ("ticker", "orderBook", "indexPrice"):
                logger.debug("Unhandled Shark WS event %r: %s", event, str(data)[:300])

    def _dispatch(self, event_name: str, data, parser) -> None:
        try:
            snapshot = parser(data)
        except Exception:  # noqa: BLE001 -- fail closed: log, don't crash the socket thread
            logger.exception("Failed to parse Shark %r payload: %s", event_name, str(data)[:300])
            return
        if snapshot is not None:
            self._on_snapshot(snapshot)

    def _send_subscribe(self, symbols: list[str]) -> None:
        raise NotImplementedError(
            "Subscribe message shape is unconfirmed for Shark's public feed -- "
            "real traffic was observed without ever sending one. See module "
            "docstring OPEN ITEM before implementing this."
        )

    # -- internal: message parsing -------------------------------------------

    @staticmethod
    def _dec_or_none(v) -> Decimal | None:
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None

    def _track_unparsed_keys(self, data: dict, confirmed: set[str]) -> None:
        extra = set(data.keys()) - confirmed
        if extra and not extra.issubset(self._raw_unparsed_keys_seen):
            self._raw_unparsed_keys_seen |= extra
            logger.info(
                "Shark WS: new unmapped field(s) seen, not yet parsed: %s "
                "(add these to shark_ws.py once you know what they mean)",
                sorted(extra),
            )

    def _parse_ticker(self, data: dict) -> MarketSnapshot | None:
        """
        Confirmed against a real captured frame (see module docstring). Only
        maps fields in _CONFIRMED_TICKER_FIELDS -- anything else present in
        `data` is logged via _track_unparsed_keys, not guessed at.
        """
        if not isinstance(data, dict) or "symbol" not in data:
            return None
        self._track_unparsed_keys(data, _CONFIRMED_TICKER_FIELDS)

        symbol = data["symbol"]
        with self._symbols_lock:
            tracked = self._symbols
        if tracked and symbol not in tracked:
            return None  # filtered client-side until a real subscribe exists

        best_bid = self._dec_or_none(data.get("bidPrice"))
        best_ask = self._dec_or_none(data.get("askPrice"))
        bid_size = self._dec_or_none(data.get("bidSize"))
        ask_size = self._dec_or_none(data.get("askSize"))

        # A 0/0 bid is a real, meaningful "no bid" state on this feed (see
        # the captured 86000-C frame: bidPrice "0", bidSize "0") -- keep it
        # as Decimal("0"), not None, so is_executable() correctly reports
        # False rather than looking like a missing-field parse failure.
        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            last_price=self._dec_or_none(data.get("lastPrice")),
            # highPrice24h/lowPrice24h are confirmed fields but MarketSnapshot
            # has no dedicated slot for them -- not forced into an unrelated
            # field (e.g. mark_price) just to use them. Add a real field to
            # MarketSnapshot if these turn out to matter downstream.
            iv=self._dec_or_none(data.get("askIv") or data.get("bidIv")),
        )

    def _parse_depth(self, data: dict) -> MarketSnapshot | None:
        """
        Confirmed for the "bids" side only (see module docstring OPEN ITEM --
        "asks" was never observed in an untruncated capture). Returns a
        MarketSnapshot with book_levels populated from bids; best_ask/ask_size
        are left None until "asks" is confirmed, which means is_executable()
        will correctly report False for depth-only snapshots -- by design,
        not an oversight, per architecture.md's fail-closed / executable-
        price-only rule (Section A.1). Do not patch this to fabricate an ask
        side.
        """
        if not isinstance(data, dict) or "bids" not in data:
            return None
        self._track_unparsed_keys(data, _CONFIRMED_ORDERBOOK_FIELDS)

        bids_raw = data.get("bids") or []
        book_levels: list[tuple[Decimal, Decimal]] = []
        for level in bids_raw:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            price = self._dec_or_none(level[0])
            size = self._dec_or_none(level[1])
            if price is not None and size is not None:
                book_levels.append((price, size))

        if not book_levels:
            return None

        best_bid_price, best_bid_size = book_levels[0]

        # No instrument_id is present on captured orderBook frames -- unlike
        # "ticker", which carries "symbol". Until confirmed otherwise, this
        # snapshot cannot be safely attributed to a specific instrument, so
        # instrument_id is left as an explicit placeholder rather than
        # guessed from whatever ticker was last seen (that coupling would be
        # a silent correctness bug if orderBook and ticker frames for
        # different symbols interleave). Fix this once a real frame confirms
        # whether orderBook carries its own symbol field.
        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id="UNKNOWN_ORDERBOOK_SYMBOL",  # see docstring above
            best_bid=best_bid_price,
            best_ask=None,
            bid_size=best_bid_size,
            ask_size=None,
            book_levels=book_levels,
        )

    def _parse_index(self, data: dict) -> MarketSnapshot | None:
        """Confirmed against a real, fully-captured "indexPrice" frame."""
        if not isinstance(data, dict) or "indexPrice" not in data:
            return None
        self._track_unparsed_keys(data, _CONFIRMED_INDEXPRICE_FIELDS)

        base = data.get("baseCoin", "")
        quote = data.get("quoteCoin", "")
        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id=f"{base}-{quote}-INDEX",
            best_bid=None,
            best_ask=None,
            bid_size=None,
            ask_size=None,
            index_price=self._dec_or_none(data.get("indexPrice")),
        )
