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
symbol genuinely has no bids right now." Fixed below by checking for this
envelope shape explicitly and raising with the real message, rather than
only trusting the transport-level HTTP status code.

THREE MORE ENDPOINTS CONFIRMED 2026-08-28 (real DevTools capture, Headers +
Response both verified for each):

  GET /v1/exchange/basePairs
    -> 200, no auth, array of per-underlying config, e.g.:
      {"baseCoin":"BTC","quoteCoin":"USDT","displayName":"BTC-USDT",
       "makerFeePercentage":0.015,"takerFeePercentage":0.02, ...}
    Real, API-sourced fee percentages -- more authoritative than the
    support-doc-derived formulas previously in architecture.md Section M.6.
    Note the icon URL under this and other Shark options responses points
    at storage.googleapis.com/pi42-dev-static/... -- strong evidence Shark's
    OPTIONS backend specifically runs on Pi42's infrastructure (white-label),
    independent of whatever Shark's futures stack (api.sharkexchange.in) is.

  GET /v1/exchange/delivery-times?baseCoin={BASE}&quoteCoin={QUOTE}
    -> 200, no auth, response body is a flat JSON array of epoch-millisecond
    expiry timestamps, e.g.:
      [1787904000000,1787990400000,1788076800000,1788508800000,...]
    THIS REPLACES GUESSING/HAND-TYPING --shark-expiry: a caller can fetch
    this directly and pick the next expiry >= now. Consecutive early entries
    are exactly 86400000ms (24h) apart, consistent with a fixed daily
    settlement time (not yet independently re-confirmed as exactly 1:30 PM
    IST from THIS endpoint specifically -- that figure comes from
    architecture.md Section M.6's separate confirmation).

  POST /v1/exchange/instrument-info-symbol
    -> 201, no auth. Real captured response for symbol="BTC-28AUG26-79750-C-USDT":
      {"symbol":"BTC-28AUG26-79750-C-USDT","strikePrice":79750,"baseCoin":"BTC",
       "quoteCoin":"USDT","settleCoin":"INR","optionsType":"Call",
       "launchTime":1787796300000,"deliveryTime":1787904000000,
       "deliveryFeeRate":0.015,
       "priceFilter":{"maxPrice":"1110000","minPrice":"5","tickSize":"5",
         "quoteCoinPrecision":4,"settleCoinPrecision":2,"pricePrecision":2},
       "lotSizeFilter":{"qtyStep":"0.01","maxOrderQty":"500",
         "minOrderQty":"0.01","quantityPrecision":2},
       "lastPrice":260,"change24h":-5.454546,"markPrice":281.91531579}
    THIS RESOLVES THE CONTRACT-MULTIPLIER QUESTION that's been flagged
    UNCONFIRMED since architecture.md Section C: lotSizeFilter shows
    quantity is specified DIRECTLY IN BTC (0.01 BTC minimum, 0.01 BTC step)
    -- same convention as Delta (per ev_engine.py's Bug #2 finding that
    Delta quotes/sizes in raw BTC terms too). There is no separate
    "contract = N BTC" abstraction to multiply by; a caller sizing a Shark
    order works directly in BTC quantity, same units as everywhere else in
    this codebase. tickSize is confirmed as "5" (price units), NOT the 0.5
    placeholder that had been assumed elsewhere.

    UNCONFIRMED CAVEAT: the request PAYLOAD for this POST was not captured
    (only Headers were visible, not the Payload tab) -- get_instrument_info()
    below assumes a `{"symbol": "..."}` JSON body as the obvious shape, but
    this is an assumption, not a confirmed fact like everything else in this
    docstring. If it 4xxs, capture the real Payload tab before changing the
    guess to something else.
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
    should treat this as 'we don't know the real state', never as
    'confirmed empty/absent'."""


class SharkSymbolExpiredError(SharkOptionsRestError):
    """Specifically: the requested symbol's expiry has already passed
    (Shark's own "Symbol expired." message)."""


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

    # -- orderbook (confirmed 2026-08-24) --------------------------------

    def get_orderbook_raw(self, symbol: str) -> dict:
        """
        Confirmed endpoint -- see module docstring. Returns the raw parsed
        JSON response ONLY if it looks like a real orderbook (has a "bids"
        or "asks" key). Raises SharkOptionsRestError (or the more specific
        SharkSymbolExpiredError) for the HTTP-200-wrapped-error case.
        """
        url = f"{_BASE_URL}/v1/market/orderBook"
        try:
            resp = self._session.get(url, params={"symbol": symbol}, timeout=_REQUEST_TIMEOUT_SEC)
        except requests.RequestException as exc:
            raise SharkOptionsRestError(f"GET {url} (symbol={symbol}) failed: {exc}") from exc

        if resp.status_code != 200:
            raise SharkOptionsRestError(
                f"GET {url} (symbol={symbol}) returned HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise SharkOptionsRestError(f"GET {url} (symbol={symbol}) returned non-JSON body: {resp.text[:300]}") from exc

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
        best_bid/best_ask are the top of the confirmed bids/asks arrays
        (bids descending, asks ascending -- confirmed from real captures).
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

    # -- newly confirmed 2026-08-28 ---------------------------------------

    def get_base_pairs(self) -> list[dict]:
        """
        Confirmed endpoint -- see module docstring. Returns the raw list,
        e.g. one entry has baseCoin="BTC", makerFeePercentage=0.015,
        takerFeePercentage=0.02, etc. No transformation applied here so
        callers see exactly what the API returns.
        """
        url = f"{_BASE_URL}/v1/exchange/basePairs"
        try:
            resp = self._session.get(url, timeout=_REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise SharkOptionsRestError(f"GET {url} failed: {exc}") from exc
        except ValueError as exc:
            raise SharkOptionsRestError(f"GET {url} returned non-JSON body") from exc

    def get_delivery_times(self, base_coin: str, quote_coin: str) -> list[datetime]:
        """
        Confirmed endpoint -- see module docstring. Returns real listed
        expiry timestamps as timezone-aware UTC datetimes, sorted ascending.
        Replaces guessing/hand-typing an expiry date/label.
        """
        url = f"{_BASE_URL}/v1/exchange/delivery-times"
        try:
            resp = self._session.get(
                url, params={"baseCoin": base_coin, "quoteCoin": quote_coin}, timeout=_REQUEST_TIMEOUT_SEC
            )
            resp.raise_for_status()
            raw_list = resp.json()
        except requests.RequestException as exc:
            raise SharkOptionsRestError(f"GET {url} failed: {exc}") from exc
        except ValueError as exc:
            raise SharkOptionsRestError(f"GET {url} returned non-JSON body") from exc

        if not isinstance(raw_list, list):
            raise SharkOptionsRestError(
                f"GET {url} expected a JSON array of epoch-ms timestamps, got: {str(raw_list)[:200]}"
            )

        return sorted(datetime.fromtimestamp(ms / 1000, tz=timezone.utc) for ms in raw_list)

    def get_next_delivery_time(self, base_coin: str, quote_coin: str, after: datetime | None = None) -> datetime:
        """Convenience wrapper: the earliest confirmed delivery time that is
        still in the future (>= `after`, default now). Raises
        SharkOptionsRestError if none are found (e.g. delivery-times list
        was empty or all entries are in the past)."""
        after = after or datetime.now(timezone.utc)
        for dt in self.get_delivery_times(base_coin, quote_coin):
            if dt >= after:
                return dt
        raise SharkOptionsRestError(
            f"No delivery time >= {after.isoformat()} found for {base_coin}/{quote_coin} -- "
            "Shark may not have listed the next expiry yet."
        )

    def get_instrument_info(self, symbol: str) -> dict:
        """
        Confirmed endpoint (response shape), UNCONFIRMED request payload --
        see module docstring's caveat. Assumes POST body {"symbol": symbol}.
        Real response includes priceFilter.tickSize and
        lotSizeFilter.{qtyStep,minOrderQty,maxOrderQty} -- see module
        docstring for why this resolves the contract-multiplier question
        (quantity is directly in BTC, no separate multiplier).
        """
        url = f"{_BASE_URL}/v1/exchange/instrument-info-symbol"
        try:
            resp = self._session.post(url, json={"symbol": symbol}, timeout=_REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise SharkOptionsRestError(
                f"POST {url} (symbol={symbol}) failed: {exc} -- NOTE: request payload shape is "
                f"UNCONFIRMED for this endpoint (see module docstring), this may be a wrong-body "
                f"issue rather than a real absence of data."
            ) from exc
        except ValueError as exc:
            raise SharkOptionsRestError(f"POST {url} (symbol={symbol}) returned non-JSON body") from exc

    def get_min_order_qty_btc(self, symbol: str) -> Decimal | None:
        """Convenience wrapper over get_instrument_info(): the confirmed
        minimum order size in BTC for a given symbol, or None if the field
        is missing from the response (should not happen per the confirmed
        response shape, but not assumed blindly)."""
        info = self.get_instrument_info(symbol)
        lot = info.get("lotSizeFilter") or {}
        return _dec_or_none(lot.get("minOrderQty"))

    def get_tick_size(self, symbol: str) -> Decimal | None:
        """Convenience wrapper over get_instrument_info(): confirmed tick
        size ("5" in the captured example), or None if missing."""
        info = self.get_instrument_info(symbol)
        pf = info.get("priceFilter") or {}
        return _dec_or_none(pf.get("tickSize"))
