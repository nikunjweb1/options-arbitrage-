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

CONFIRMED NOT TO EXIST (tested directly, 2026-08-24) -- do not re-try these
guesses, they were reasonable but wrong:
    GET /v1/exchange/exchangeInfo        -> 404 {"message":"Cannot GET
      /v1/exchange/exchangeInfo","error":"Not Found","statusCode":404}
    GET /v1/market/ticker24Hr?symbol=... -> same 404 shape
  So this host does NOT simply mirror api.sharkexchange.in's documented
  /v1/market/* and /v1/exchange/* path patterns for every endpoint -- only
  orderBook (confirmed above) is known to exist here. Don't assume path-name
  symmetry with the futures API going forward; each endpoint needs its own
  real confirmation.

  The more promising lead for get_option_chain(), still untested: the
  actual endpoint NAMES seen firing from the live options page's Fetch/XHR
  panel earlier this session -- basePairs, delivery-times, options-tier-info,
  instrument-info, and a paginated list?page=...&pageSize=...&isPast=...
  request. Those are real observed request names, not guesses by analogy,
  so they're worth testing on this host (and were originally seen without a
  confirmed host at all -- confirming they live on api-options.sharkexchange.in
  specifically, the same way orderBook did, is the next concrete step).

CRITICAL BUG FOUND AND FIXED 2026-08-26: this endpoint returns application
errors wrapped INSIDE an HTTP 200 response, not as a real 4xx/5xx status
code. Confirmed example, symbol whose expiry had already passed (it was
past 1:30 PM IST, so that day's Shark options had settled):

    GET /v1/market/orderBook?symbol=BTC-26AUG26-79000-C-USDT
    HTTP status: 200 OK (!)
    Body: {"response":{"message":"Symbol expired.","error":"Internal Server
           Error","statusCode":500}, "status":500, "options":{},
           "message":"Symbol expired.","name":"InternalServerErrorException"}

The original version of get_orderbook_raw() only checked resp.raise_for_status()
(which saw 200, so raised nothing), then get_orderbook_snapshot() looked for
a "bids" key that doesn't exist in this error envelope, got None, and
silently returned an empty MarketSnapshot -- indistinguishable from "this
symbol genuinely has no bids right now." A full scanner run against 60
strikes produced 60 silent empty results with zero exceptions logged, which
looked exactly like "no liquidity anywhere" when the real story was "every
symbol's expiry had already passed, tell the caller that." Fixed below by
checking for this envelope shape explicitly and raising with the real
message, rather than only trusting the transport-level HTTP status code.
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
    """Raised on any failure calling api-options.sharkexchange.in --
    including the HTTP-200-wrapped-error case documented above. Callers
    should treat this as 'we don't know the real orderbook state', never
    as 'confirmed empty book'."""


class SharkSymbolExpiredError(SharkOptionsRestError):
    """Specifically: the requested symbol's expiry has already passed
    (Shark's own "Symbol expired." message). Distinguished from other
    SharkOptionsRestError cases because a caller (e.g. the scanner) may
    want to react differently -- e.g. stop trying that expiry entirely
    rather than retrying, versus a transient network error which might be
    worth retrying."""


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
        JSON response ONLY if it looks like a real orderbook (has a "bids"
        or "asks" key). Raises SharkOptionsRestError (or the more specific
        SharkSymbolExpiredError) for the HTTP-200-wrapped-error case --
        see this module's CRITICAL BUG note. Never silently returns an
        error envelope as if it were real data.
        """
        url = f"{_BASE_URL}/v1/market/orderBook"
        try:
            resp = self._session.get(url, params={"symbol": symbol}, timeout=_REQUEST_TIMEOUT_SEC)
        except requests.RequestException as exc:
            raise SharkOptionsRestError(f"GET {url} (symbol={symbol}) failed: {exc}") from exc

        # Real HTTP-level failure -- still worth checking even though the
        # confirmed error case above uses 200, in case OTHER failure modes
        # (rate limiting, auth, etc.) use real status codes instead.
        if resp.status_code != 200:
            raise SharkOptionsRestError(
                f"GET {url} (symbol={symbol}) returned HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise SharkOptionsRestError(f"GET {url} (symbol={symbol}) returned non-JSON body: {resp.text[:300]}") from exc

        # THE FIX: detect Shark's HTTP-200-wrapped-error envelope. Confirmed
        # shape has a top-level "statusCode" and/or "error"/"name" key and
        # NO "bids"/"asks" keys -- a real orderbook response never has
        # these. Checking for the error shape explicitly (rather than just
        # "bids" is missing) so a genuinely different-but-valid response
        # shape wouldn't be misclassified as this specific known error.
        if "bids" not in body and "asks" not in body:
            err_message = body.get("message") or body.get("response", {}).get("message") if isinstance(body.get("response"), dict) else body.get("message")
            status_code = body.get("statusCode") or (body.get("response") or {}).get("statusCode") if isinstance(body.get("response"), dict) else body.get("statusCode")
            if err_message and "expired" in str(err_message).lower():
                raise SharkSymbolExpiredError(
                    f"Shark symbol {symbol!r}: {err_message!r} (wrapped in HTTP 200, statusCode={status_code})"
                )
            raise SharkOptionsRestError(
                f"Shark orderBook response for {symbol!r} has neither 'bids' nor 'asks' and isn't a "
                f"recognized error shape either -- raw body: {str(body)[:300]}"
            )

        return body

    def get_orderbook_snapshot(self, symbol: str) -> MarketSnapshot:
        """
        Same data as get_orderbook_raw(), normalized into a MarketSnapshot.
        Raises (does not silently return empty) if get_orderbook_raw()
        couldn't get real data -- see that method and this module's
        CRITICAL BUG note.
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
