"""
Shark (short leg) vs Delta (long leg) SCREENING scanner.

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
a dollar EV figure.

What it DOES do, and why it's still useful: both exchanges quote premiums
in raw per-1-BTC terms (confirmed for Delta via ev_engine.py's Bug #2
finding; Shark's captured numbers are the same scale -- e.g. a BTC~$77k
underlying next to option premiums in the tens-to-thousands range). A raw
premium/IV comparison at matching strikes is a fair, apples-to-apples
SCREEN for which candidates are worth a closer manual look -- it tells you
where Shark's leg looks rich relative to Delta's, which is the actual
economic signal this strategy trades on (Section M.1's "if one option is
relatively overpriced, sell that one"). It does NOT tell you the exact
dollar P&L of a specific position size -- that still requires Shark's real
lot size, which you can read directly from your own logged-in Shark UI
before sizing any manual trade.

NO ORDER PLACEMENT: this file only reads public market data (Shark) and
Delta's public ticker endpoint. Nothing here can place, size, or execute a
trade. Output is a ranked list for manual review and manual execution on
both exchanges.

Usage:
    python -m scanner.shark_delta_screen --underlying BTC --shark-expiry 25AUG26 --delta-date 2026-08-25
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from exchange_adapters.delta import DeltaAdapter
from exchange_adapters.shark_rest_options import SharkOptionsPublicClient, SharkOptionsRestError
from exchange_adapters.shark_ws import SharkWebSocketClient
from normalization.schemas import OptionType

logger = logging.getLogger("shark_delta_screen")


@dataclass
class ScreenResult:
    strike: Decimal
    option_type: str
    shark_symbol: str
    shark_bid: Decimal | None
    shark_bid_size: Decimal | None
    shark_iv: Decimal | None  # from WS ticker, if captured during the run
    delta_symbol: str
    delta_ask: Decimal | None
    delta_ask_size: Decimal | None
    delta_iv: Decimal | None
    raw_net_credit: Decimal | None  # shark_bid - delta_ask, RAW per-1-BTC terms, NOT sized
    iv_divergence: Decimal | None  # shark_iv - delta_iv, positive = short leg richer in vol terms

    @property
    def entry_eligible_raw(self) -> bool:
        """Same net-credit gate as architecture.md Section M.2, applied to
        RAW (unsized) terms. This tells you the DIRECTION is right, not the
        real dollar magnitude -- see module docstring."""
        return self.raw_net_credit is not None and self.raw_net_credit > 0


def _build_shark_symbol(underlying: str, expiry_ddmmmyy: str, strike: Decimal, is_call: bool) -> str:
    """Per exchange_adapters/shark_ws.py's confirmed symbol format."""
    cp = "C" if is_call else "P"
    strike_str = str(int(strike)) if strike == strike.to_integral_value() else str(strike)
    return f"{underlying}-{expiry_ddmmmyy.upper()}-{strike_str}-{cp}-USDT"


def run_screen(
    underlying: str,
    shark_expiry_ddmmmyy: str,
    delta_expiry_date: datetime,
    shark_ws_listen_seconds: int = 20,
) -> list[ScreenResult]:
    """
    1. Pull Delta's real option chain for `underlying` on `delta_expiry_date`
       (existing, working DeltaAdapter -- no guessing).
    2. For each Delta strike, build the corresponding Shark symbol (confirmed
       format) and try the confirmed Shark REST orderbook endpoint.
    3. Briefly listen to Shark's WS ticker feed to pick up IV for whichever
       symbols happen to stream during the listen window (fire-hose, not
       request/response -- see shark_ws.py's docstring; not every symbol is
       guaranteed to appear in a short window).
    4. Compute raw net credit and IV divergence per strike, rank, return.
    """
    delta = DeltaAdapter()
    shark_rest = SharkOptionsPublicClient()

    delta_contracts = delta.get_option_chain(underlying, expiry=delta_expiry_date)
    logger.info("Delta: %d contracts found for %s on %s", len(delta_contracts), underlying, delta_expiry_date.date())

    # Passive WS listen for IV -- best-effort, not required for the core
    # screen. shark_ws.py's MarketSnapshot.iv is populated from Shark's own
    # askIv field (documented fallback, see that file) -- used directly here.
    shark_iv_by_symbol: dict[str, Decimal] = {}
    shark_bid_by_symbol: dict[str, tuple[Decimal | None, Decimal | None]] = {}

    def _on_snapshot(snapshot):
        if snapshot.iv is not None:
            shark_iv_by_symbol[snapshot.instrument_id] = snapshot.iv
        if snapshot.best_bid is not None:
            shark_bid_by_symbol[snapshot.instrument_id] = (snapshot.best_bid, snapshot.bid_size)

    ws_client = SharkWebSocketClient(host="fawss-options.sharkexchange.in", on_snapshot=_on_snapshot)
    try:
        ws_client.start()
        ws_client.wait_until_connected(timeout_sec=10)
        import time
        time.sleep(shark_ws_listen_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Shark WS listen failed (continuing with REST-only screen): %s", exc)
    finally:
        ws_client.stop()

    results: list[ScreenResult] = []

    for contract in delta_contracts:
        try:
            delta_ticker = delta.get_ticker(contract.instrument_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Delta ticker fetch failed for %s: %s", contract.instrument_id, exc)
            continue

        is_call = contract.option_type == OptionType.CALL
        shark_symbol = _build_shark_symbol(underlying, shark_expiry_ddmmmyy, contract.strike, is_call)

        # REST orderbook is the primary source (confirmed reliable, request/
        # response). WS snapshot is used only to fill in shark_bid if the
        # REST call fails and the fire-hose happened to catch this symbol
        # during the listen window -- REST is preferred since it's guaranteed
        # per-symbol, not best-effort.
        shark_bid: Decimal | None = None
        shark_bid_size: Decimal | None = None
        try:
            ob = shark_rest.get_orderbook_snapshot(shark_symbol)
            shark_bid = ob.best_bid
            shark_bid_size = ob.bid_size
        except SharkOptionsRestError as exc:
            logger.debug("No Shark orderbook for %s (strike may not be listed there): %s", shark_symbol, exc)
            if shark_symbol in shark_bid_by_symbol:
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
            )
        )

    results.sort(key=lambda r: (r.raw_net_credit is None, -(r.raw_net_credit or Decimal("-999999"))))
    return results


def print_results(results: list[ScreenResult]) -> None:
    eligible = [r for r in results if r.entry_eligible_raw]
    print(f"\n{len(eligible)}/{len(results)} strikes show a positive RAW net credit (screening signal only -- see module docstring for why this isn't sized dollar P&L).\n")
    print(f"{'Strike':>10} {'Type':<5} {'Shark bid':>10} {'Shark sz':>9} {'Delta ask':>10} {'Delta sz':>9} {'Raw credit':>11} {'IV div':>8}")
    for r in results:
        print(
            f"{r.strike!s:>10} {r.option_type:<5} "
            f"{r.shark_bid if r.shark_bid is not None else '-':>10} "
            f"{r.shark_bid_size if r.shark_bid_size is not None else '-':>9} "
            f"{r.delta_ask if r.delta_ask is not None else '-':>10} "
            f"{r.delta_ask_size if r.delta_ask_size is not None else '-':>9} "
            f"{r.raw_net_credit if r.raw_net_credit is not None else '-':>11} "
            f"{r.iv_divergence if r.iv_divergence is not None else '-':>8}"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="BTC")
    parser.add_argument("--shark-expiry", required=True, help="e.g. 25AUG26 -- must match Shark's real listed expiry")
    parser.add_argument("--delta-date", required=True, help="e.g. 2026-08-25 -- must match Delta's real listed expiry date")
    parser.add_argument("--listen-seconds", type=int, default=20)
    args = parser.parse_args()

    delta_expiry = datetime.strptime(args.delta_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    results = run_screen(args.underlying, args.shark_expiry, delta_expiry, args.listen_seconds)
    print_results(results)
