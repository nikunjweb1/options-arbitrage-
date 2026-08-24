"""
Shark Exchange OPTIONS public REST client -- market data only.

SCOPE, ENFORCED: this file only calls public, unauthenticated market-data
endpoints. No api-key, no signature, no order placement, no account access
of any kind lives in this file -- see architecture.md's scope boundary and
this project's repeated stance on live trading.

CONFIRMED (2026-08-24, via live browser DevTools capture, Headers + Response
both verified -- not guessed):

  Host:   https://api-options.sharkexchange.in
  This is a DIFFERENT host from https://api.sharkexchange.in, which
  docs.sharkexchange.in documents and which was independently confirmed
  (via SharkAdapter.probe_exchange_info() on the phase2/shark-adapter
  branch) to return FUTURES-ONLY data. This "-options" host is not
  mentioned anywhere in the public docs -- it was only found by capturing
  real Network traffic from the live options page.

  Confirmed endpoint:
    GET /v1/market/orderBook?symbol={SYMBOL}
    Example: GET https://api-options.sharkexchange.in/v1/market/orderBook?symbol=BTC-25AUG26-78000-C-USDT
    -> 200 OK, no api-key/signature required (plain GET with only a query param)
    Confirmed response shape (full, not truncated):
      {
        "symbol": "BTC-25AUG26-78000-C-USDT",
        "bids": [["1030", "2"], ["1025", "2"], ...],   -- [price_str, size_str], descending
        "asks": [["1115", "0.72"], ["1120", "9.56"], ...] -- [price_str, size_str], ascending
      }
    This directly resolves the ambiguity flagged in shark_ws.py's docstring
    about whether "asks" exists and whether orderbook data carries a symbol
    -- confirmed yes to both, for this REST endpoint specifically. (The
    WebSocket "orderBook" event's own shape is still a separate, not-yet-
    independently-confirmed question -- don't assume the WS event matches
    this REST shape just because they share a name.)

STRONGLY SUSPECTED BUT NOT YET CONFIRMED -- do not call these without
testing first:
  Given /v1/market/orderBook exists here with the same path shape as
  api.sharkexchange.in's documented futures endpoints, it's a reasonable
  guess (not a confirmed fact) that sibling endpoints exist on this same
  host following the same /v1/market/* pattern:
    GET /v1/market/ticker24Hr?symbol=...
    GET /v1/market/klines?symbol=...
    GET /v1/market/aggTrade?symbol=...
  And possibly, for the option-chain-listing gap that's been open all
  session:
    GET /v1/exchange/exchangeInfo  (an OPTIONS-scoped version, distinct
      from api.sharkexchange.in's confirmed-futures-only version of the
      same path)
  NONE of these are implemented below. Test each one for real (hit the URL
  directly, or capture it from a live DevTools session) before adding it --
  per this project's own rule, guessed-but-untested endpoints don't get
  wired into working code, they get flagged as the next thing to check.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from normalization.schemas import MarketSnapshot

logger = logging.getLogger("shark_rest_options")

_BASE_URL = "https://api-options.sharkexchange.in"
_REQUEST_TIMEOUT_SEC = 10


class SharkOptionsRestError(RuntimeError):
    """Raised on any failure calling api-options.sharkexchange.in."""


def _dec_or_none(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


class SharkOptionsPublicClient:
    """
    Public (no-auth) REST client for Shark's options-specific market data
    host. Deliberately has no constructor arguments for api_key/api_secret
    -- there is no code path in this class that could use them, by design.
    """

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def get_orderbook_raw(self, symbol: str) -> dict:
        """
        Confirmed endpoint -- see module docstring. Returns the raw parsed
        JSON response, exactly as the server sent it.
        """
        url = f"{_BASE_URL}/v1/market/orderBook"
        try:
            resp = self._session.get(url, params={"symbol": symbol}, timeout=_REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise SharkOptionsRestError(f"GET {url} (symbol={symbol}) failed: {exc}") from exc

    def get_orderbook_snapshot(self, symbol: str) -> MarketSnapshot:
        """
        Same data as get_orderbook_raw(), normalized into a MarketSnapshot.
        best_bid/best_ask are the top of the confirmed bids/asks arrays
        (bids are given descending, asks ascending -- confirmed from the
        real captured example in the module docstring, where bids started
        at 1030 and descended, asks started at 1115 and ascended).
        """
        raw = self.get_orderbook_raw(symbol)

        bids_raw = raw.get("bids") or []
        asks_raw = raw.get("asks") or []

        book_levels = [
            (_dec_or_none(p), _dec_or_none(s))
            for p, s in bids_raw
            if _dec_or_none(p) is not None and _dec_or_none(s) is not None
        ]

        best_bid, bid_size = (_dec_or_none(bids_raw[0][0]), _dec_or_none(bids_raw[0][1])) if bids_raw else (None, None)
        best_ask, ask_size = (_dec_or_none(asks_raw[0][0]), _dec_or_none(asks_raw[0][1])) if asks_raw else (None, None)

        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            exchange="shark",
            instrument_id=raw.get("symbol", symbol),
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            book_levels=book_levels,
        )
