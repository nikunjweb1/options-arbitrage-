"""
Shark Exchange public WebSocket client -- real-time market data feed.

SCOPE, ENFORCED: this file only ever connects to Shark's PUBLIC market-data
socket. It never touches an "-uds-" (User Data Stream) host, never sends an
api-key or signature, and has no code path that can place, edit, or cancel
an order. Duplicated from shark_ws_capture.py's host-refusal rule rather than
imported, so this file's safety property doesn't depend on another file
staying correct.

CORRECTION, 2026-08-26 -- THE EARLIER "FIRE HOSE, NO SUBSCRIBE NEEDED"
CLAIM IN THIS DOCSTRING WAS WRONG. Stated here plainly rather than silently
edited away, since acting on that wrong claim is exactly why every prior
"zero events received" debugging round (see CONNECTION FIX HISTORY below)
went looking at headers/transport/cookies instead of the actual cause. The
original capture that led to that claim happened to start recording
mid-session, after the browser had already sent its subscribe calls --
so no outgoing subscribe frame was visible, and the (wrong) conclusion was
that none existed. A fresh capture starting DevTools recording BEFORE
navigating to the page (2026-08-26) caught the real sequence:

    -> 42["subscribe",{"params":["BTC_USDT@indexPrice","ETH_USDT@indexPrice"]}]
    <- 42["indexPrice",{"indexPrice":"78929.55636322","baseCoin":"BTC","quoteCoin":"USDT"}]
    -> 42["unsubscribe",{"params":[""]}]
    -> 42["subscribe",{"params":["BTC_USDT_27AUG26@ticker"]}]
    <- 42["ticker",{"symbol":"BTC-27AUG26-78750-C-USDT","bidPrice":"690",...}]

CONFIRMED subscribe protocol, replacing the old claim entirely:
  - Event name: "subscribe" (and "unsubscribe" with the same param shape).
  - Payload shape: {"params": [<channel string>, ...]} -- an array, so one
    call can subscribe multiple channels at once (the indexPrice example
    above subscribes BTC and ETH in a single call).
  - Index price channel format: "{BASE}_{QUOTE}@indexPrice"
    e.g. "BTC_USDT@indexPrice"
  - Ticker channel format: "{BASE}_{QUOTE}_{EXPIRY:DDMMMYY}@ticker"
    e.g. "BTC_USDT_27AUG26@ticker"
    IMPORTANT: this is PER-EXPIRY, not per-strike or per-contract. A single
    subscribe to one expiry's ticker channel streams ticker events for
    EVERY strike and both option types (C and P) at that expiry -- this is
    why earlier captures showed many different strikes streaming from what
    looked like one connection: it was one subscription, not many. This
    also means subscribing is cheap: one call per expiry date you care
    about covers the whole chain for that date, not one call per contract.

STATUS AS OF 2026-08-23/26 -- CONFIRMED AGAINST REAL DATA:
Event names and payload shapes below are confirmed -- captured via Chrome
DevTools Network -> WS -> Messages tab on a real, logged-in session at
https://sharkexchange.in/options/btcusdt, connected to
wss://fawss-options.sharkexchange.in/socket.io/.

Confirmed event #1: "ticker"
  42["ticker",{"symbol":"BTC-27AUG26-78750-C-USDT","bidPrice":"690",
    "bidSize":"4","bidIv":"0.3661","askPrice":"695","askSize":"3.91",
    "askIv":"0.3692","lastPrice":"690","highPrice24h":"1710", ...}]
  Field list below is CONFIRMED-PRESENT, not necessarily CONFIRMED-COMPLETE
  -- the captured frame was truncated by the DevTools display before the
  full payload printed.

  Symbol format (confirmed from multiple examples):
    {BASE}-{EXPIRY:DDMMMYY}-{STRIKE}-{C|P}-{QUOTE}
    e.g. "BTC-27AUG26-78750-C-USDT" -> BTC, 27 Aug 2026, strike 78750, Call, USDT

Confirmed event #2: "orderBook"
  42["orderBook",{"bids":[["560","1.68"],["555","1.65"],...]}]
  Same caveat as before: "asks" key and symbol attribution for this event
  remain UNCONFIRMED (truncated in every capture so far) -- counted but not
  yet dispatched into a MarketSnapshot.

Confirmed event #3: "indexPrice" (complete, not truncated)
  42["indexPrice",{"indexPrice":"78929.55636322","baseCoin":"BTC","quoteCoin":"USDT"}]
  Market-wide, not per-instrument -- exposed via a separate callback
  (on_index_price), not folded into MarketSnapshot, since db/schema.sql's
  `instruments` table assumes every row is a real option contract.

CONNECTION FIX HISTORY, FLAGGED HONESTLY (most recent first):

  2026-08-26 -- ROOT CAUSE FOUND AND FIXED: see CORRECTION at the top of
  this docstring. Every "zero events" symptom in every entry below this one
  was, in hindsight, this same root cause -- no subscribe call was ever
  being sent, at any point, by this client's earlier versions. The
  Cookie-header and handle_sigint fixes below were real, correctly-reasoned
  fixes for real, separate problems they each targeted (see their own
  entries), but neither of them was ever going to fix the zero-events
  symptom, because that symptom's actual cause was simpler than any of the
  hypotheses being tested for it.

  2026-08-26 -- FIX: added handle_sigint=False to the socketio.Client()
  constructor. Root cause, confirmed against a real crash on a Windows end-
  user run of scanner/shark_delta_screen.py: python-socketio's Client
  installs its own SIGINT handler by default, which assumes it owns the
  main thread via a foreground sio.wait() call. This client instead runs
  sio.wait() inside a background thread while the caller's main thread does
  its own time.sleep() -- exactly the "running the Client in a thread"
  scenario python-socketio's own maintainer confirmed causes a synthesized
  KeyboardInterrupt on disconnect/reconnect (github.com/miguelgrinberg/
  python-socketio issues #414 and #453). This was a REAL, separate bug from
  the zero-events issue -- kept, still correct.

  2026-08-25 -- FIX ATTEMPT: optional Cookie header, sourced from
  SHARK_WS_COOKIE env var. Kept in place (harmless, additive, opt-in) even
  though the actual zero-events cause turned out to be the missing
  subscribe call, not a missing cookie -- a required cookie is still a
  plausible requirement for some OTHER purpose (e.g. rate-limit
  attribution) that just wasn't the thing breaking this particular symptom.

  2026-08-25 -- FIX: transports changed from ["websocket"] to
  ["polling", "websocket"], matching the real browser's connection
  sequence. Kept -- still correct, still needed for a stable connection,
  just not sufficient by itself for receiving data (see CORRECTION above).

  2026-08-23/24 -- FIX ATTEMPT: explicit Origin header. Kept -- still
  correct, still not sufficient by itself.

RESOLVED, 2026-08-24: Shark options settlement TIME is confirmed (01:30 PM
IST / 08:00 UTC) -- architecture.md Section M.6, read directly off Shark's
own options contract-details page. What remains unconfirmed is the
Delivery Price *construction* (which index, what averaging window) -- see
parse_shark_symbol's docstring.

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


def format_ticker_channel(base_coin: str, quote_coin: str, expiry_ddmmmyy: str) -> str:
    """
    Builds a confirmed ticker-channel string, e.g. format_ticker_channel(
    "BTC", "USDT", "27AUG26") -> "BTC_USDT_27AUG26@ticker". expiry_ddmmmyy
    must already be in Shark's DDMMMYY format (e.g. "27AUG26") -- this
    function does not reformat a date object, since the caller (typically
    working from a Delta expiry or a manual date) is responsible for
    knowing which calendar date it means; silently reformatting here risks
    an off-by-one-day bug being invisible at the call site.
    """
    return f"{base_coin.upper()}_{quote_coin.upper()}_{expiry_ddmmmyy.upper()}@ticker"


def format_index_price_channel(base_coin: str, quote_coin: str) -> str:
    """Builds a confirmed index-price-channel string, e.g.
    format_index_price_channel("BTC", "USDT") -> "BTC_USDT@indexPrice"."""
    return f"{base_coin.upper()}_{quote_coin.upper()}@indexPrice"


class SharkWebSocketClient:
    """
    Public-data-only WebSocket client for Shark Exchange options.

    Parses the three confirmed event types (ticker, orderBook, indexPrice).
    Requires an explicit subscribe call per channel -- see subscribe_ticker()/
    subscribe_index_price() below and the module docstring's CORRECTION for
    why this wasn't always known to be necessary.
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

        # Channels requested via subscribe_ticker()/subscribe_index_price()
        # before a connection exists (or across a reconnect) are queued here
        # and (re-)sent once connected -- see _on_connect below. Without
        # this, calling subscribe_ticker() before wait_until_connected()
        # returns True would silently do nothing.
        self._pending_channels: set[str] = set()
        self._pending_lock = threading.Lock()

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
            handle_sigint=False,
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

    def subscribe_ticker(self, base_coin: str, quote_coin: str, expiry_ddmmmyy: str) -> None:
        """
        Subscribes to ALL strikes/types for one expiry date -- see module
        docstring's CONFIRMED subscribe protocol note on why this is
        per-expiry, not per-contract. Safe to call before the connection is
        established (queued, see _pending_channels) or after (sent
        immediately).
        """
        channel = format_ticker_channel(base_coin, quote_coin, expiry_ddmmmyy)
        self._subscribe_channel(channel)

    def subscribe_index_price(self, base_coin: str, quote_coin: str) -> None:
        channel = format_index_price_channel(base_coin, quote_coin)
        self._subscribe_channel(channel)

    def _subscribe_channel(self, channel: str) -> None:
        with self._pending_lock:
            self._pending_channels.add(channel)
        if self._sio is not None and self._sio.connected:
            self._send_subscribe([channel])

    def _send_subscribe(self, channels: list[str]) -> None:
        # CONFIRMED shape, per module docstring: event "subscribe", payload
        # {"params": [...]}.
        try:
            self._sio.emit("subscribe", {"params": channels})
            logger.info("Subscribed to %d Shark channel(s): %s", len(channels), channels)
        except Exception as exc:  # noqa: BLE001
            logger.error("Shark subscribe emit failed for %s: %s", channels, exc)

    # -- internal: connection lifecycle ----------------------------------

    def _run(self) -> None:
        url = f"https://{self._host}"
        try:
            connect_headers = {"Origin": self._origin}

            import os
            shark_cookie = os.environ.get("SHARK_WS_COOKIE", "").strip()
            if shark_cookie:
                connect_headers["Cookie"] = shark_cookie
                logger.info("Shark WS: sending SHARK_WS_COOKIE from environment (value not logged).")
            else:
                logger.debug("Shark WS: no SHARK_WS_COOKIE set -- connecting without a Cookie header.")

            self._sio.connect(
                url,
                transports=["polling", "websocket"],
                headers=connect_headers,
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
            # Re-send every requested channel on (re)connect -- covers both
            # the first connection and any reconnect after a drop, so a
            # caller's earlier subscribe_ticker()/subscribe_index_price()
            # calls survive a disconnect without needing to be repeated by
            # hand.
            with self._pending_lock:
                channels = list(self._pending_channels)
            if channels:
                self._send_subscribe(channels)

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
            logger.warning("ticker symbol %r did not match expected pattern, dropping.", symbol)
            return None

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
