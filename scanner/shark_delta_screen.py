"""
Shark (short leg) vs Delta (long leg) SCREENING scanner -- automated watch mode.

STRATEGY THIS IMPLEMENTS: docs/architecture.md Section D/M -- sell (short) a
BTC option on Shark (1:30 PM IST expiry), buy (long) the same-strike option
on Delta (5:30 PM IST expiry, same calendar day). Net entry credit
(Shark_bid - Delta_ask) is the entry economics per Section D.2.

WHY THIS IS A SCREENING TOOL, NOT A SIZED P&L ENGINE (be honest about the
gap): pricing/ev_engine.py's LeanEVEngine requires a confirmed
contract_multiplier for BOTH legs to produce real per-contract dollar P&L.
Shark's multiplier is NOT confirmed (architecture.md Section C: the "Min
Order Size" field renders client-side, unconfirmed as of this writing) --
per this project's explicit rule, that number is never guessed or
hardcoded. So this scanner does NOT call LeanEVEngine and does NOT produce
a dollar EV figure. For sized, tax-aware recommendations, use
pricing/manual_spread_finder.py once Shark's lot size is confirmed -- this
scanner's job is the FIRST-PASS screen: is there anything worth typing into
that tool right now, without you having to watch two browser tabs by hand.

NO ORDER PLACEMENT: this file only reads public market data (Shark) and
Delta's public ticker endpoint. Nothing here can place, size, or execute a
trade, in either single-run or --watch mode. Output is a ranked list /
notification for manual review and manual execution on both exchanges.

SPEED, 2026-08-26:
- The old default WS listen step is now OFF by default (--ws-listen-seconds
  0). Every real run so far has shown the WS connecting then immediately
  disconnecting with zero events received (see exchange_adapters/shark_ws.py's
  own CONNECTION FIX HISTORY) -- so it was 20+ seconds of guaranteed-dead
  time on every run, not a real IV data source yet. Pass
  --ws-listen-seconds N to re-enable it once that connection issue is
  actually fixed and confirmed delivering events.
- Shark REST orderbook calls (one per Delta-listed strike, ~40-75 per run)
  now run CONCURRENTLY via a thread pool instead of sequentially -- this was
  the dominant cost in every prior run's wall-clock time. Delta ticker calls
  are also parallelized the same way. Both APIs are public/no-auth GETs;
  concurrency here doesn't touch anything account-specific.
- STRIKE-BAND FILTERING (added 2026-08-26, later same day): live runs
  consistently show Shark only ever having real data for a narrow band of
  strikes near the current spot price (e.g. 6/42 on one real run), while
  the remaining strikes come back "Symbol expired" -- NOT because they
  actually expired (the expiry hadn't even started yet), but because
  Shark's own confirmed error message is reused for "never listed at all."
  Querying those was pure wasted round-trips: ~36/42 Shark calls on a
  typical run got nothing. --strike-band-pct (default 8.0) now filters
  which strikes get a Shark REST call AT ALL, based on distance from the
  live spot price -- itself read off Delta's own ticker data (underlying
  index/spot/futures, whichever is present), which was already being
  fetched for every contract anyway, so this adds zero extra network calls.
  Strikes outside the band are marked shark_error="skipped_out_of_band" --
  a DIFFERENT, more honest label than "expired", since this is a decision
  not to ask, not a report of what Shark said. Default band is 8% --
  chosen from the one real run seen so far (spot ~77250, live strikes
  spanning ~76000-79000, i.e. roughly +/-2.6%), giving about 3x margin
  around that observed band rather than cutting it razor-thin off one data
  point. Widen with --strike-band-pct if a live strike ever gets excluded
  (visible as "skipped_out_of_band" where you'd expect real data). If spot
  price can't be determined from any Delta ticker (all fields None), this
  filter is skipped entirely and every strike is queried, same as before --
  a failed band guess must never silently produce an incomplete-looking
  result set. If the band excludes every strike, that's also logged loudly
  and the scanner automatically falls back to querying everything, rather
  than silently returning zero results because the band was miscentered.

AUTOMATION, 2026-08-26: --watch runs the screen on a fixed interval forever
(Ctrl+C to stop), printing a one-line status each cycle and the full table
+ a Telegram alert ONLY when at least one strike actually clears the
net-credit bar -- so you can leave this running without it spamming you,
and get pinged the moment something real appears. This is the automation of
FINDING an opportunity; executing on it is still, deliberately, entirely
manual -- see NO ORDER PLACEMENT above.

Usage:
    python -m scanner.shark_delta_screen --underlying BTC --shark-expiry 27AUG26 --delta-date 2026-08-27
    python -m scanner.shark_delta_screen --underlying BTC --shark-expiry 27AUG26 --delta-date 2026-08-27 --watch --interval-sec 60
    python -m scanner.shark_delta_screen --underlying BTC --shark-expiry 27AUG26 --delta-date 2026-08-27 --strike-band-pct 10
"""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from exchange_adapters.delta import DeltaAdapter
from exchange_adapters.shark_rest_options import (
    SharkOptionsPublicClient,
    SharkOptionsRestError,
    SharkSymbolExpiredError,
)
from exchange_adapters.shark_ws import SharkWebSocketClient
from normalization.schemas import OptionType
from notifications.telegram_notifier import is_configured as is_telegram_configured, send_telegram_message

logger = logging.getLogger("shark_delta_screen")

_MAX_WORKERS = 10  # concurrent REST calls per side -- polite to both APIs, still a large speedup over sequential
_DEFAULT_STRIKE_BAND_PCT = 8.0  # see module docstring's STRIKE-BAND FILTERING note


def _infer_spot_price(delta_tickers: dict) -> Decimal | None:
    """
    Pulls a spot-ish reference price out of whichever Delta ticker snapshot
    has one, preferring underlying_index (the actual settlement-relevant
    reference) over underlying_spot over underlying_futures. Returns None
    if not a single ticker had any of these -- callers must treat that as
    "can't band-filter, query everything" per module docstring, not as
    "spot is 0".
    """
    for field in ("underlying_index", "underlying_spot", "underlying_futures"):
        for ticker in delta_tickers.values():
            val = getattr(ticker.snapshot, field, None)
            if val is not None:
                return val
    return None


@dataclass
class ScreenResult:
    strike: Decimal
    option_type: str
    shark_symbol: str
    shark_bid: Decimal | None
    shark_bid_size: Decimal | None
    shark_iv: Decimal | None
    delta_symbol: str
    delta_ask: Decimal | None
    delta_ask_size: Decimal | None
    delta_iv: Decimal | None
    raw_net_credit: Decimal | None
    iv_divergence: Decimal | None
    shark_error: str | None = None  # e.g. "expired", "skipped_out_of_band" -- surfaced, not hidden

    @property
    def entry_eligible_raw(self) -> bool:
        """Same net-credit gate as architecture.md Section M.2, applied to
        RAW (unsized) terms. Direction, not magnitude -- see module
        docstring."""
        return self.raw_net_credit is not None and self.raw_net_credit > 0


def _build_shark_symbol(underlying: str, expiry_ddmmmyy: str, strike: Decimal, is_call: bool) -> str:
    """Per exchange_adapters/shark_ws.py's confirmed symbol format."""
    cp = "C" if is_call else "P"
    strike_str = str(int(strike)) if strike == strike.to_integral_value() else str(strike)
    return f"{underlying}-{expiry_ddmmmyy.upper()}-{strike_str}-{cp}-USDT"


def _fetch_shark_bid(shark_rest: SharkOptionsPublicClient, symbol: str) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Returns (bid, bid_size, error_label). error_label is a short string
    ('expired', 'error') when the call didn't succeed -- never silently
    conflated with 'genuinely no bids', per the CRITICAL BUG fix in
    shark_rest_options.py."""
    try:
        ob = shark_rest.get_orderbook_snapshot(symbol)
        return ob.best_bid, ob.bid_size, None
    except SharkSymbolExpiredError:
        return None, None, "expired"
    except SharkOptionsRestError as exc:
        logger.debug("Shark orderbook error for %s: %s", symbol, exc)
        return None, None, "error"


def run_screen(
    underlying: str,
    shark_expiry_ddmmmyy: str,
    delta_expiry_date: datetime,
    ws_listen_seconds: int = 0,
    strike_band_pct: float = _DEFAULT_STRIKE_BAND_PCT,
) -> list[ScreenResult]:
    delta = DeltaAdapter()
    shark_rest = SharkOptionsPublicClient()

    delta_contracts = delta.get_option_chain(underlying, expiry=delta_expiry_date)
    logger.info("Delta: %d contracts found for %s on %s", len(delta_contracts), underlying, delta_expiry_date.date())

    shark_iv_by_symbol: dict[str, Decimal] = {}
    shark_bid_by_symbol: dict[str, tuple[Decimal | None, Decimal | None]] = {}

    if ws_listen_seconds > 0:
        def _on_snapshot(snapshot):
            if snapshot.iv is not None:
                shark_iv_by_symbol[snapshot.instrument_id] = snapshot.iv
            if snapshot.best_bid is not None:
                shark_bid_by_symbol[snapshot.instrument_id] = (snapshot.best_bid, snapshot.bid_size)

        ws_client = SharkWebSocketClient(host="fawss-options.sharkexchange.in", on_snapshot=_on_snapshot)
        try:
            ws_client.start()
            ws_client.wait_until_connected(timeout_sec=10)
            time.sleep(ws_listen_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shark WS listen failed (continuing with REST-only screen): %s", exc)
        finally:
            ws_client.stop()

    # Fetch Delta side CONCURRENTLY -- see module docstring's SPEED note.
    # Every Delta contract still gets a ticker (Delta's API isn't the
    # bottleneck, and we need delta_ask for every strike regardless of
    # whether Shark ends up being queried for it).
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        delta_futures = {
            pool.submit(delta.get_ticker, c.instrument_id): c for c in delta_contracts
        }
        delta_tickers: dict[str, object] = {}
        for fut in as_completed(delta_futures):
            contract = delta_futures[fut]
            try:
                delta_tickers[contract.instrument_id] = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Delta ticker fetch failed for %s: %s", contract.instrument_id, exc)

    shark_symbol_for = {}
    for contract in delta_contracts:
        is_call = contract.option_type == OptionType.CALL
        shark_symbol_for[contract.instrument_id] = _build_shark_symbol(
            underlying, shark_expiry_ddmmmyy, contract.strike, is_call
        )

    # STRIKE-BAND FILTERING -- see module docstring. Only decide WHICH
    # strikes to ask Shark about; never changes what Delta already told us.
    spot_price = _infer_spot_price(delta_tickers)
    in_band_ids: set[str]
    if spot_price is None:
        logger.warning(
            "Could not infer a spot price from any Delta ticker (no "
            "underlying_index/underlying_spot/underlying_futures present on "
            "any snapshot) -- querying Shark for all %d strikes, strike-band "
            "filtering skipped this run.", len(delta_contracts),
        )
        in_band_ids = set(shark_symbol_for.keys())
    else:
        band_frac = Decimal(str(strike_band_pct)) / Decimal("100")
        lo = spot_price * (Decimal("1") - band_frac)
        hi = spot_price * (Decimal("1") + band_frac)
        in_band_ids = {
            c.instrument_id for c in delta_contracts
            if lo <= c.strike <= hi
        }
        if not in_band_ids:
            logger.warning(
                "Strike band [%s, %s] (spot=%s, +/-%.1f%%) excluded ALL %d "
                "strikes -- band is likely miscentered or too tight. Falling "
                "back to querying every strike this run rather than "
                "returning zero results silently. Consider a larger "
                "--strike-band-pct if this recurs.",
                lo, hi, spot_price, strike_band_pct, len(delta_contracts),
            )
            in_band_ids = set(shark_symbol_for.keys())
        else:
            skipped = len(delta_contracts) - len(in_band_ids)
            logger.info(
                "Strike band [%s, %s] (spot~%s, +/-%.1f%%): querying Shark for "
                "%d/%d strikes, skipping %d out-of-band strikes (no network call).",
                lo, hi, spot_price, strike_band_pct, len(in_band_ids), len(delta_contracts), skipped,
            )

    # Fetch Shark side CONCURRENTLY, in-band strikes only.
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        shark_futures = {
            pool.submit(_fetch_shark_bid, shark_rest, sym): instrument_id
            for instrument_id, sym in shark_symbol_for.items()
            if instrument_id in in_band_ids
        }
        shark_results: dict[str, tuple[Decimal | None, Decimal | None, str | None]] = {}
        for fut in as_completed(shark_futures):
            instrument_id = shark_futures[fut]
            shark_results[instrument_id] = fut.result()

    results: list[ScreenResult] = []
    expired_count = 0
    skipped_count = 0

    for contract in delta_contracts:
        delta_ticker = delta_tickers.get(contract.instrument_id)
        if delta_ticker is None:
            continue

        shark_symbol = shark_symbol_for[contract.instrument_id]

        if contract.instrument_id not in in_band_ids:
            shark_bid, shark_bid_size, shark_error = None, None, "skipped_out_of_band"
            skipped_count += 1
        else:
            shark_bid, shark_bid_size, shark_error = shark_results.get(contract.instrument_id, (None, None, "error"))
            if shark_error == "expired":
                expired_count += 1
            elif shark_error is None and shark_bid is None and shark_symbol in shark_bid_by_symbol:
                shark_bid, shark_bid_size = shark_bid_by_symbol[shark_symbol]

        delta_ask = delta_ticker.snapshot.best_ask
        delta_ask_size = delta_ticker.snapshot.ask_size
        delta_iv = delta_ticker.snapshot.iv
        shark_iv = shark_iv_by_symbol.get(shark_symbol)

        raw_net_credit = (shark_bid - delta_ask) if (shark_bid is not None and delta_ask is not None) else None
        iv_divergence = (shark_iv - delta_iv) if (shark_iv is not None and delta_iv is not None) else None

        results.append(
            ScreenResult(
                strike=contract.strike,
                option_type=contract.option_type.value,
                shark_symbol=shark_symbol,
                shark_bid=shark_bid,
                shark_bid_size=shark_bid_size,
                shark_iv=shark_iv,
                delta_symbol=contract.contract_symbol,
                delta_ask=delta_ask,
                delta_ask_size=delta_ask_size,
                delta_iv=delta_iv,
                raw_net_credit=raw_net_credit,
                iv_divergence=iv_divergence,
                shark_error=shark_error,
            )
        )

    queried_count = len(in_band_ids)
    if expired_count > 0 and expired_count == queried_count:
        logger.warning(
            "ALL %d Shark-queried strikes came back 'Symbol expired' for expiry %s -- "
            "that whole expiry has settled on Shark's side. Try the next expiry date.",
            expired_count, shark_expiry_ddmmmyy,
        )
    elif expired_count > 0:
        logger.info("%d/%d Shark-queried strikes expired (rest are live). %d strikes skipped (out of band).",
                    expired_count, queried_count, skipped_count)
    elif skipped_count > 0:
        logger.info("%d strikes skipped (out of band), %d queried.", skipped_count, queried_count)

    results.sort(key=lambda r: (r.raw_net_credit is None, -(r.raw_net_credit or Decimal("-999999"))))
    return results


def print_results(results: list[ScreenResult], full: bool = True) -> None:
    eligible = [r for r in results if r.entry_eligible_raw]
    have_data = [r for r in results if r.shark_bid is not None]
    print(
        f"\n{len(eligible)}/{len(results)} strikes show a positive RAW net credit "
        f"({len(have_data)}/{len(results)} had live Shark data) -- screening signal only.\n"
    )
    if not full and not eligible:
        return
    print(f"{'Strike':>10} {'Type':<5} {'Shark bid':>10} {'Shark sz':>9} {'Delta ask':>10} {'Delta sz':>9} {'Raw credit':>11} {'IV div':>8}")
    rows = eligible if (eligible and not full) else results
    for r in rows:
        print(
            f"{r.strike!s:>10} {r.option_type:<5} "
            f"{r.shark_bid if r.shark_bid is not None else '-':>10} "
            f"{r.shark_bid_size if r.shark_bid_size is not None else '-':>9} "
            f"{r.delta_ask if r.delta_ask is not None else '-':>10} "
            f"{r.delta_ask_size if r.delta_ask_size is not None else '-':>9} "
            f"{r.raw_net_credit if r.raw_net_credit is not None else '-':>11} "
            f"{r.iv_divergence if r.iv_divergence is not None else '-':>8}"
        )


def _format_telegram_alert(results: list[ScreenResult], underlying: str) -> str:
    eligible = [r for r in results if r.entry_eligible_raw]
    lines = [f"\U0001F4B0 {underlying}: {len(eligible)} RAW net-credit strike(s) found (screening only, not sized):\n"]
    for r in eligible[:5]:
        lines.append(
            f"<b>{r.strike} {r.option_type.upper()}</b> -- Shark bid {r.shark_bid} vs Delta ask {r.delta_ask} "
            f"= <b>+{r.raw_net_credit}</b> raw credit\n"
            f"  Shark symbol: {r.shark_symbol}\n"
        )
    lines.append(
        "\u26A0\uFE0F Raw/unsized screen -- verify live prices and Shark's real lot size before "
        "sizing a trade. Run pricing.manual_spread_finder for a full tax/liquidity-aware check."
    )
    return "\n".join(lines)


def run_watch(
    underlying: str,
    shark_expiry: str,
    delta_expiry: datetime,
    interval_sec: int,
    ws_listen_seconds: int,
    strike_band_pct: float,
) -> None:
    print(f"Watching {underlying} every {interval_sec}s (Ctrl+C to stop). Full table + Telegram alert only when something clears the net-credit bar.\n")
    cycle = 0
    while True:
        cycle += 1
        started = time.monotonic()
        try:
            results = run_screen(underlying, shark_expiry, delta_expiry, ws_listen_seconds, strike_band_pct)
        except Exception as exc:  # noqa: BLE001
            print(f"[cycle {cycle}] ERROR: {exc}")
            time.sleep(interval_sec)
            continue

        eligible = [r for r in results if r.entry_eligible_raw]
        elapsed = time.monotonic() - started
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        if eligible:
            print(f"\n[cycle {cycle}, {ts}, {elapsed:.1f}s] *** {len(eligible)} opportunity(ies) found ***")
            print_results(results, full=False)
            if is_telegram_configured():
                sent = send_telegram_message(_format_telegram_alert(results, underlying))
                print(f"Telegram alert {'sent' if sent else 'FAILED'}.")
            else:
                print("Telegram not configured -- see notifications/telegram_notifier.py for 3-minute setup.")
        else:
            print(f"[cycle {cycle}, {ts}, {elapsed:.1f}s] 0/{len(results)} eligible. Nothing to report.")

        time.sleep(max(0, interval_sec - elapsed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="BTC")
    parser.add_argument("--shark-expiry", required=True, help="e.g. 27AUG26 -- must match Shark's real listed expiry")
    parser.add_argument("--delta-date", required=True, help="e.g. 2026-08-27 -- must match Delta's real listed expiry date")
    parser.add_argument("--ws-listen-seconds", type=int, default=0, help="Listen to Shark's WS for IV data before scanning. Default 0 (off) -- see module docstring's SPEED note.")
    parser.add_argument("--strike-band-pct", type=float, default=_DEFAULT_STRIKE_BAND_PCT,
                         help=f"Only query Shark for strikes within this %% of live spot price. Default {_DEFAULT_STRIKE_BAND_PCT}. See module docstring's STRIKE-BAND FILTERING note.")
    parser.add_argument("--watch", action="store_true", help="Run continuously on an interval instead of once.")
    parser.add_argument("--interval-sec", type=int, default=60, help="Only used with --watch.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    delta_expiry = datetime.strptime(args.delta_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if args.watch:
        run_watch(args.underlying, args.shark_expiry, delta_expiry, args.interval_sec, args.ws_listen_seconds, args.strike_band_pct)
    else:
        results = run_screen(args.underlying, args.shark_expiry, delta_expiry, args.ws_listen_seconds, args.strike_band_pct)
        print_results(results)
