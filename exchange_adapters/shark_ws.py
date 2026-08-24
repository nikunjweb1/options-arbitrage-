"""
Shark Exchange public WebSocket client -- real-time market data feed.

SCOPE, ENFORCED: this file only ever connects to Shark's PUBLIC market-data
socket. It never touches an "-uds-" (User Data Stream) host, never sends an
api-key or signature, and has no code path that can place, edit, or cancel
an order. Duplicated from shark_ws_capture.py's host-refusal rule rather than
imported, so this file's safety property doesn't depend on another file
staying correct.

STATUS AS OF 2026-08-23 -- CONFIRMED AGAINST REAL DATA:
Unlike the previous version of this file, the event names and payload
shapes below ARE now confirmed -- captured via Chrome DevTools Network ->
WS -> Messages tab on a real, logged-in session at
https://sharkexchange.in/options/btcusdt, connected to
wss://fawss-options.sharkexchange.in/socket.io/ with transport=websocket
(the actual upgraded connection, not the polling fallback).

Three real Socket.IO event names were observed, unprompted -- no subscribe
message was sent by the browser before data arrived. This is a "fire hose"
feed: connecting appears to be sufficient to receive updates for many/all
option contracts at once, not just ones explicitly subscribed to. (This is
DIFFERENT from delta_ws.py, which does require an explicit channel
subscribe -- don't assume the two adapters work identically here.)

Confirmed event #1: "ticker"
  42["ticker",{"symbol":"BTC-24AUG26-86000-C-USDT","bidPrice":"0",
    "bidSize":"0","bidIv":"0","askPrice":"5","askSize":"34.69",
    "askIv":"1.0268","lastPrice":"5","highPrice24h":"35",
    "lowPrice24h":"5", ...}]
  The captured frame was truncated by DevTools display (~580-640 chars
  shown, actual message may be longer) -- fields after lowPrice24h are
  UNKNOWN and not parsed below. Do not guess at them (e.g. do not assume
  openInterest/volume/greeks field names by analogy to Delta's schema --
  Shark's REST docs already show different field-naming conventions than
  Delta's, e.g. Shark's own order objects use "orderAmount" where Delta
  uses different naming, so there's no reason Shark's WS ticker would
  reuse Delta's names either).

  Symbol format (confirmed from multiple examples):
    {BASE}-{EXPIRY:DDMMMYY}-{STRIKE}-{C|P}-{QUOTE}
    e.g. "BTC-24AUG26-86000-C-USDT" -> BTC, 24 Aug 2026, strike 86000, Call, USDT

Confirmed event #2: "orderBook"
  42["orderBook",{"bids":[["560","1.68"],["555","1.65"],["550","4.11"],...]}]
  IMPORTANT CAVEAT: the captured frame shows a "bids" key but was truncated
  before any "asks" key (if present) became visible. It's also UNCONFIRMED
  whether this message includes a "symbol" field further in (truncated) or
  whether it implicitly refers to whatever contract the page currently has
  selected -- if the latter, this event may not be safely usable in a
  multi-contract collector without a confirmed way to know which instrument
  it belongs to. Treat _parse_order_book's output as provisional until a
  full, untruncated capture confirms both of these.

Confirmed event #3: "indexPrice" (complete, not truncated)
  42["indexPrice",{"indexPrice":"77242.1492131","baseCoin":"BTC","quoteCoin":"USDT"}]
  This is a market-wide index value, not per-instrument -- exposed via a
  separate callback (on_index_price), not folded into MarketSnapshot.

OPEN DISCREPANCY, FLAGGED HONESTLY:
exchange_adapters/shark_ws_capture.py, run via the Python socketio client
against the same host, received ZERO events over 180s. The browser, on the
same host, received a continuous stream unprompted. Plausible causes (not
yet tested): (a) the server checks the Origin/Referer header and the Python
client didn't send one matching https://sharkexchange.in, (b) the Python
client's default transport list didn't upgrade to websocket within the
capture window and data may only flow on the upgraded transport, (c) some
other browser-only signal (e.g. a fingerprint/cookie) is required. This
client sends an explicit Origin header and forces websocket-only transport
as an attempt to close that gap -- but that fix itself is UNVERIFIED until
someone runs this against the real host and confirms events arrive. Do not
assume it works without testing.

Usage:
    pip install "python-socketio[client]" --break-system-packages
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from normalization.schemas import MarketSnapshot

logger = logging.getLogger("shark_ws")

MarketSnapshotCallback = Callable[[MarketSnapshot], None]
IndexPriceCallback = Callable[[str, str, Decimal], None]  # (base_coin, quote_coin, index_price)

_ALLOWED_HOST_SUBSTRINGS_MUST_NOT_CONTAIN = "uds"

# Confirmed format: BTC-24AUG26-86000-C-USDT
_SYMBOL_RE = re.compile(
    r"^(?P<base>[A-Z0-9]+)-(?P<expiry>\d{2}[A-Z]{3}\d{2})-(?P<strike>[\d.]+)-(?P<cp>[CP])-(?P<quote>[A-Z0-9]+)$"
)


def _refuse_if_uds(host: str) -> None:
    if _ALLOWED_HOST_SUBSTRINGS_MUST_NOT_CONTAIN in host.lower():
        raise ValueError(
            f"Refusing to connect to {host!r} -- 'uds' hosts are almost "
            "certainly account-authenticated (User Data Stream) channels, "
            "not public market data. See this file's module docstring."
        )


def _dec_or_none(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


class SharkWebSocketClient:
    """
    Public-data-only WebSocket client for Shark Exchange options.

    Parses the three confirmed event types (ticker, orderBook, indexPrice).
    See module docstring for exactly what is and isn't confirmed about each.
    """

    def __init__(
        self,
        host: str,
        on_snapshot: MarketSnapshotCallback,
        on_index_price: IndexPriceCallback | None = None,
        origin: str = "https://sharkexchange.in",
        reconnect_backoff_base_sec: float = 1.0,
        reconnect_backoff_max_sec: float = 30.0,
        reconnect_max_attempts: int = 0,  # 0 = unlimited
    ) -> None:
        _refuse_if_uds(host)

        self._host = host
        self._on_snapshot = on_snapshot
        self._on_index_price = on_index_price
        self._origin = origin
        self._reconnect_backoff_base = reconnect_backoff_base_sec
        self._reconnect_backoff_max = reconnect_backoff_max_sec
        self._reconnect_max_attempts = reconnect_max_attempts

        self._sio = None
        self._thread: threading.Thread | None = None
        self._connected_event = threading.Event()

        # Diagnostics: counts every event actually received, by type, so a
        # caller (or a quick manual check) can tell at a glance whether real
        # data is flowing -- without this, a silent parse failure could look
        # identical to "no data at all", which is exactly the ambiguity this
        # project has already been bitten by once (Delta ev_engine Bug #2).
        self.event_counts: dict[str, int] = {}
        self._counts_lock = threading.Lock()

    # -- public API -----------------------------------------------------

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

        self._thread = threading.Thread(target=self._run, daemon=True, name="shark-ws")
        self._thread.start()

    def stop(self) -> None:
        if self._sio is not None:
            self._sio.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def wait_until_connected(self, timeout_sec: float = 15.0) -> bool:
        return self._connected_event.wait(timeout=timeout_sec)

    # -- internal: connection lifecycle ----------------------------------

    def _run(self) -> None:
        url = f"https://{self._host}"
        try:
            # UNVERIFIED FIX (see module docstring): explicit Origin header +
            # websocket-only transport, attempting to close the gap where the
            # plain-Python capture got zero events but the real browser
            # (which sends these automatically) got a continuous stream.
            self._sio.connect(
                url,
                transports=["websocket"],
                headers={"Origin": self._origin},
                wait_timeout=15,
            )
            self._sio.wait()
        except Exception as exc:  # noqa: BLE001
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
            self._count("ticker")
            snapshot = self._parse_ticker(data)
            if snapshot is not None:
                self._on_snapshot(snapshot)

        @sio.on("orderBook")
        def on_order_book(data):
            self._count("orderBook")
            # See module docstring's caveat -- symbol attribution for this
            # event is UNCONFIRMED, so it is logged/counted but not yet
            # dispatched into a MarketSnapshot. Wire this up only after
            # confirming (from an untruncated capture) which instrument
            # each orderBook message belongs to.
            logger.debug("orderBook received (not yet dispatched, see docstring): %s", str(data)[:200])

        @sio.on("indexPrice")
        def on_index_price(data):
            self._count("indexPrice")
            if self._on_index_price is None:
                return
            base = data.get("baseCoin")
            quote = data.get("quoteCoin")
            price = _dec_or_none(data.get("indexPrice"))
            if base and quote and price is not None:
                self._on_index_price(base, quote, price)

        @sio.on("*")
        def catch_all(event, data=None):
            # Anything NOT one of the three confirmed events above lands
            # here -- logged so an unexpected/new event type is visible
            # rather than silently dropped.
            if event not in ("ticker", "orderBook", "indexPrice"):
                self._count(f"unhandled:{event}")
                logger.info("Unhandled Shark WS event %r: %s", event, str(data)[:200])

    def _count(self, key: str) -> None:
        with self._counts_lock:
            self.event_counts[key] = self.event_counts.get(key, 0) + 1

    # -- parsing (confirmed fields only) ---------------------------------

    def _parse_ticker(self, data: dict) -> MarketSnapshot | None:
        symbol = data.get("symbol")
        if not symbol:
            logger.warning("ticker event missing symbol, dropping: %s", str(data)[:200])
            return None

        m = _SYMBOL_RE.match(symbol)
        if not m:
            # Don't guess at a malformed/unrecognized symbol shape -- log
            # and drop rather than silently mis-attributing data.
            logger.warning("ticker symbol %r did not match expected pattern, dropping.", symbol)
            return None

        bid_iv = _dec_or_none(data.get("bidIv"))
        ask_iv = _dec_or_none(data.get("askIv"))
        # SCHEMA NOTE: MarketSnapshot.iv is a single field, but Shark's ticker
        # gives separate bidIv/askIv. There's no confirmed "correct" way to
        # collapse two numbers into one here, so deliberately leaving iv=None
        # rather than picking one arbitrarily (e.g. defaulting to askIv)
        # without a documented reason -- see architecture.md's repeated point
        # about not silently choosing a value that "looks close enough."
        # Callers needing IV should read bid_iv/ask_iv from raw_extra instead.

        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id=symbol,
            best_bid=_dec_or_none(data.get("bidPrice")),
            best_ask=_dec_or_none(data.get("askPrice")),
            bid_size=_dec_or_none(data.get("bidSize")),
            ask_size=_dec_or_none(data.get("askSize")),
            last_price=_dec_or_none(data.get("lastPrice")),
            iv=None,  # see note above
        )

    @staticmethod
    def parse_symbol(symbol: str) -> dict | None:
        """
        Splits a confirmed Shark option symbol into components.
        Returns None if the symbol doesn't match the confirmed pattern.
        NOTE: expiry is returned as the raw "DDMMMYY" string, not parsed into
        a datetime -- the settlement TIME (not just date) and timezone are
        still unconfirmed for Shark options (architecture.md Section M.6 only
        confirmed this for futures/spot, not options specifically), so
        building a timezone-aware expiry_timestamp here would require
        guessing the settlement hour. Do that step only once confirmed.
        """
        m = _SYMBOL_RE.match(symbol)
        if not m:
            return None
        return {
            "base": m.group("base"),
            "expiry_raw": m.group("expiry"),
            "strike": Decimal(m.group("strike")),
            "option_type": "call" if m.group("cp") == "C" else "put",
            "quote": m.group("quote"),
        }
