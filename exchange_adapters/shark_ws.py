"""
Shark Exchange public WebSocket client -- real-time market data feed.

SCOPE, ENFORCED: this file only ever connects to Shark's PUBLIC market-data
socket. It never touches an "-uds-" (User Data Stream) host, never sends an
api-key or signature, and has no code path that can place, edit, or cancel
an order. Duplicated from shark_ws_capture.py's host-refusal rule rather than
imported, so this file's safety property doesn't depend on another file
staying correct.

STATUS AS OF 2026-08-23 -- CONFIRMED AGAINST REAL DATA:
Event names and payload shapes below are confirmed -- captured via Chrome
DevTools Network -> WS -> Messages tab on a real, logged-in session at
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
  UNKNOWN and not parsed below. Do not guess at them.

  Symbol format (confirmed from multiple examples):
    {BASE}-{EXPIRY:DDMMMYY}-{STRIKE}-{C|P}-{QUOTE}
    e.g. "BTC-24AUG26-86000-C-USDT" -> BTC, 24 Aug 2026, strike 86000, Call, USDT

Confirmed event #2: "orderBook"
  42["orderBook",{"bids":[["560","1.68"],["555","1.65"],["550","4.11"],...]}]
  IMPORTANT CAVEAT: the captured frame shows a "bids" key but was truncated
  before any "asks" key (if present) became visible. It's also UNCONFIRMED
  whether this message includes a "symbol" field further in (truncated) or
  whether it implicitly refers to whatever contract the page currently has
  selected. This event is therefore counted (see event_counts) but
  deliberately NOT dispatched into a MarketSnapshot -- wire it up only after
  a full, untruncated capture confirms both of these.

Confirmed event #3: "indexPrice" (complete, not truncated)
  42["indexPrice",{"indexPrice":"77242.1492131","baseCoin":"BTC","quoteCoin":"USDT"}]
  This is a market-wide index value, not per-instrument -- exposed via a
  separate callback (on_index_price), not folded into MarketSnapshot. It
  isn't an option instrument (no strike/expiry/type), and db/schema.sql's
  `instruments` table has CHECK constraints that assume every row is a real
  option contract -- forcing an index price into that table would mean
  fabricating an option_type/strike that doesn't exist.

CONNECTION FIX HISTORY, FLAGGED HONESTLY (most recent first):

  2026-08-25 -- FIX: transports changed from ["websocket"] to
  ["polling", "websocket"]. The original browser capture (very first
  DevTools session in this investigation) shows the real connection
  sequence is POLLING FIRST -- dozens of `transport=polling` XHR requests
  establish a session and obtain a `sid`, and only THEN does the client
  upgrade to `transport=websocket` using that sid. Forcing
  transports=["websocket"] skips that handshake and connects directly via
  WS from a cold start. Live-tested 2026-08-24/25 with websocket-only: the
  connection succeeded at the TCP/TLS/Engine.IO level but then disconnected
  almost immediately / delivered zero events in the brief time it stayed
  open -- the well-known Socket.IO server-side pattern of validating that a
  websocket upgrade references a `sid` already established via a prior
  polling request, and dropping connections that skip straight to
  websocket. See _run()'s inline comment for the full reasoning. This fix
  is UNVERIFIED until re-tested against the real host -- if event_counts
  stays all-zero (or the connection still drops immediately) after this
  change, that specific hypothesis was wrong and the next thing to check is
  whether a Cookie header (real browser session) is required, which neither
  fix attempt so far has sent.

  2026-08-23/24 -- FIX ATTEMPT: explicit Origin header added
  (headers={"Origin": self._origin}), attempting to close the gap where an
  earlier plain-Python capture received zero events but the real browser
  (which sends this automatically) received a continuous stream. Kept
  alongside the polling-first fix above since both address different parts
  of "look like the real browser's connection" -- if only one turns out to
  matter, the polling-first fix is more likely to be it, per the reasoning
  above, but there's no cost to keeping both.

RESOLVED, 2026-08-24 (previously an open item in an earlier draft of this
file): whether the settlement TIME for Shark *options* specifically (not
just futures/spot) is confirmed. It is -- architecture.md Section M.6
records this as read directly off Shark's own options contract-details page
("Delivery Time: 01:30 PM"), not a futures/spot page and not inferred by
analogy. parse_shark_symbol() below uses that confirmed value (08:00 UTC)
when building each OptionContract's settlement_timestamp. What remains
genuinely unconfirmed is the Delivery Price *construction* (which index,
what averaging window) -- a different question from the settlement clock --
see parse_shark_symbol's own docstring and architecture.md Section M.6.

Usage:
    pip install "python-socketio[client]" --break-system-packages
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, time as dt_time, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from normalization.schemas import (
    MarketSnapshot,
    OptionContract,
    OptionType,
    OptionVariant,
    SettlementMethod,
)

logger = logging.getLogger("shark_ws")

MarketSnapshotCallback = Callable[[MarketSnapshot], None]
IndexPriceCallback = Callable[[str, str, Decimal], None]  # (base_coin, quote_coin, index_price)

_ALLOWED_HOST_SUBSTRINGS_MUST_NOT_CONTAIN = "uds"

# Confirmed format: BTC-24AUG26-86000-C-USDT
_SYMBOL_RE = re.compile(
    r"^(?P<base>[A-Z0-9]+)-(?P<expiry>\d{2}[A-Z]{3}\d{2})-(?P<strike>[\d.]+)-(?P<cp>[CP])-(?P<quote>[A-Z0-9]+)$"
)
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Confirmed 2026-08-23/24, architecture.md Section M.6 -- see module
# docstring's RESOLVED note above.
_SHARK_SETTLEMENT_TIME_UTC = dt_time(8, 0, 0)


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


def parse_shark_symbol(symbol: str) -> OptionContract | None:
    """
    Parses a confirmed Shark option symbol (e.g. "BTC-24AUG26-73000-C-USDT")
    into a normalized OptionContract, for use by collectors/realtime_collector.py
    to register a new instrument the first time a ticker event mentions it.

    The symbol PATTERN and the settlement TIME are both confirmed (see
    module docstring). Several other fields are NOT confirmed and are
    flagged loudly here rather than guessed silently, same pattern as
    DeltaConfig.fee_schedule's source_url note in config/settings.py:

      - contract_multiplier / lot_size: Shark's contract-details page has a
        "Min Order Size" field but it renders client-side and hasn't been
        read from a real logged-in session yet (architecture.md Section C's
        open item). Defaulted to Decimal("1") -- OptionContract requires a
        positive value, so this can't be left as None. DO NOT use this
        default for any real position-sizing or cross-exchange
        multiplier-ratio check (Section C.4) until the real figure is
        confirmed.
      - is_european: not stated anywhere in Shark's docs. Defaulted True as
        a documented assumption (standard for cash-settled crypto index
        options, matching Delta's confirmed convention), not a fact.
      - tick_size: not documented. Defaulted to Decimal("0.5") as an
        observationally-plausible placeholder from captured order-book
        spacing -- not a verified tick-size rule. Do not use for real order
        price rounding.
      - settlement_price_formula: recorded as a named "UNCONFIRMED"
        placeholder string, not a real formula name -- architecture.md
        Section M.6 found Shark's Delivery Price *construction* (which
        index, what averaging window) undocumented. Only the settlement
        *time* and the overall P&L formula *shape* are confirmed, not this.

    Returns None (fail closed) if `symbol` doesn't match the confirmed
    pattern -- an unparseable symbol must never silently become a malformed
    OptionContract.
    """
    m = _SYMBOL_RE.match(symbol)
    if not m:
        return None

    underlying = m.group("base")
    quote = m.group("quote")
    opt_type = OptionType.CALL if m.group("cp") == "C" else OptionType.PUT

    try:
        strike = Decimal(m.group("strike"))
    except (InvalidOperation, ValueError):
        return None

    expiry_raw = m.group("expiry")  # e.g. "24AUG26"
    try:
        day = int(expiry_raw[:2])
        month = _MONTHS[expiry_raw[2:5]]
        year = 2000 + int(expiry_raw[5:7])
        expiry_date = datetime(year, month, day, tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None

    settlement_dt = datetime.combine(
        expiry_date.date(), _SHARK_SETTLEMENT_TIME_UTC, tzinfo=timezone.utc
    )

    return OptionContract(
        exchange="shark",
        underlying=underlying,
        base_asset=underlying,
        quote_asset=quote,
        option_type=opt_type,
        option_variant=OptionVariant.VANILLA,  # ASSUMED -- see docstring
        strike=strike,
        expiry_timestamp=expiry_date,
        settlement_timestamp=settlement_dt,  # confirmed clock, see module docstring RESOLVED note
        settlement_method=SettlementMethod.CASH,  # confirmed, architecture.md Section M.6
        settlement_price_formula="shark_delivery_price_UNCONFIRMED_construction",
        contract_multiplier=Decimal("1"),  # UNVERIFIED -- see docstring
        lot_size=Decimal("1"),             # UNVERIFIED -- see docstring
        tick_size=Decimal("0.5"),          # UNVERIFIED -- see docstring
        quote_currency=quote,
        settlement_currency="INR",  # confirmed, architecture.md Section M.6: USDT-quoted, INR-settled
        contract_symbol=symbol,
        instrument_id=symbol,
        is_european=True,  # ASSUMED -- see docstring
    )


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
        # Check this first if realtime_collector.py's shark_snapshots_received
        # stat stays at zero -- it tells you whether the socket is even
        # receiving frames at all vs. receiving them but failing to parse.
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
            # FIX 2026-08-25 (was: transports=["websocket"]): the real
            # browser capture (DevTools Network tab, very first capture in
            # this investigation) shows the actual connection sequence is
            # POLLING FIRST -- dozens of `transport=polling` XHR requests
            # establish a session and obtain a `sid`, and only THEN does the
            # client upgrade to `transport=websocket` using that sid. Forcing
            # transports=["websocket"] skips that polling handshake and
            # connects directly via WS from a cold start.
            #
            # Live-tested 2026-08-24/25: with websocket-only, the connection
            # succeeded (TCP/TLS/Engine.IO handshake all completed) but then
            # disconnected almost immediately, and/or delivered zero events
            # during the brief time it stayed open. This is the well-known
            # Socket.IO server-side pattern of validating that a websocket
            # upgrade request references a `sid` already established via a
            # prior polling request -- a from-scratch direct-to-websocket
            # connect looks anomalous and gets dropped, even though the
            # initial handshake response looks fine.
            #
            # This was tried BEFORE the Origin-header fix (see module
            # docstring's CONNECTION FIX HISTORY) and is a separate, likely
            # more fundamental cause of the same symptom -- both fixes are
            # kept together since they address different parts of "look
            # like the real browser's connection", but if only one turns
            # out to matter, it's more likely this one.
            self._sio.connect(
                url,
                transports=["polling", "websocket"],
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

        # SCHEMA NOTE: MarketSnapshot.iv is a single field, but Shark's ticker
        # gives separate bidIv/askIv. There's no confirmed "correct" way to
        # collapse two numbers into one here, so this deliberately falls back
        # to askIv (arbitrary but documented, unlike silently picking one and
        # calling it "iv" with no explanation) only when it's present;
        # otherwise leaves iv=None rather than fabricating a number.
        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id=symbol,
            best_bid=_dec_or_none(data.get("bidPrice")),
            best_ask=_dec_or_none(data.get("askPrice")),
            bid_size=_dec_or_none(data.get("bidSize")),
            ask_size=_dec_or_none(data.get("askSize")),
            last_price=_dec_or_none(data.get("lastPrice")),
            iv=_dec_or_none(data.get("askIv")),
        )

    @staticmethod
    def parse_symbol(symbol: str) -> dict | None:
        """
        Splits a confirmed Shark option symbol into raw components (strings/
        Decimal, not a full OptionContract). Returns None if the symbol
        doesn't match the confirmed pattern.

        For a full OptionContract (with settlement_timestamp, exchange
        defaults, and the UNVERIFIED-but-required placeholder fields
        documented there), use the module-level parse_shark_symbol()
        function instead -- that's what collectors/realtime_collector.py
        calls to register new instruments from live ticks.
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
