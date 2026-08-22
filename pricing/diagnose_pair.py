"""
Diagnostic tool: dumps the raw exchange response and our normalized view for
one specific candidate pair, side by side.

Built specifically to investigate the implausible-premium finding from the
Phase 5 grid-fix re-run (2026-08-22): pairs showing net_entry_cost far larger
than the notional value contract_multiplier implies (e.g. a $265 credit on a
~$65-notional 0.001 BTC contract -- physically impossible for a vanilla
option premium). This prints exactly what get_ticker() returned, exactly
what URL/instrument_id was requested, and the contract spec on file, so we
can tell whether this is (a) a bad instrument_id/symbol being sent to the
ticker endpoint, (b) genuinely broken/crossed testnet liquidity, or (c) a
units mistake somewhere else we haven't found yet -- without guessing.

Usage:
    python -m pricing.diagnose_pair delta_india:195293__delta_india:183947
"""

from __future__ import annotations

import sqlite3
import sys

from config.settings import DB, DELTA
from db.loaders import get_contract
from exchange_adapters.delta import DeltaAdapter


def diagnose(pair_id: str) -> int:
    parts = pair_id.split("__")
    if len(parts) != 2:
        print(f"Malformed pair_id: {pair_id!r} -- expected 'exchange:instrument_id__exchange:instrument_id'")
        return 1

    (short_exchange, short_id), (long_exchange, long_id) = (p.split(":", 1) for p in parts)

    if not DB.sqlite_path.exists():
        print(f"No database found at {DB.sqlite_path}.")
        return 1

    conn = sqlite3.connect(DB.sqlite_path)

    for label, exchange, instrument_id in [
        ("SHORT (earlier expiry)", short_exchange, short_id),
        ("LONG (later expiry)", long_exchange, long_id),
    ]:
        print(f"\n{'=' * 70}\n{label}: {exchange}:{instrument_id}\n{'=' * 70}")

        contract = get_contract(conn, exchange, instrument_id)
        if contract is None:
            print(f"  !! No instrument row found for {exchange}:{instrument_id} in `instruments`.")
            continue

        print(f"  Normalized instrument row:")
        print(f"    contract_symbol      = {contract.contract_symbol!r}")
        print(f"    instrument_id        = {contract.instrument_id!r}")
        print(f"    strike               = {contract.strike}")
        print(f"    contract_multiplier  = {contract.contract_multiplier}  <- notional per contract = strike * this, roughly")
        print(f"    expiry_timestamp     = {contract.expiry_timestamp.isoformat()}")
        print(f"    settlement_currency  = {contract.settlement_currency}")

        if exchange != "delta_india":
            print(f"  (Skipping live fetch -- diagnostic tool currently wired for delta_india only.)")
            continue

        adapter = DeltaAdapter()

        print(f"\n  Live get_ticker(instrument_id={instrument_id!r}) [numeric ID, as currently called]:")
        try:
            ticker_by_id = adapter.get_ticker(instrument_id)
            snap = ticker_by_id.snapshot
            print(f"    best_bid={snap.best_bid}  best_ask={snap.best_ask}  "
                  f"mark_price={snap.mark_price}  underlying_spot={snap.underlying_spot}")
        except Exception as exc:
            print(f"    !! Request failed: {exc}")

        print(f"\n  Live get_ticker(instrument_id={contract.contract_symbol!r}) [symbol, for comparison]:")
        try:
            ticker_by_symbol = adapter.get_ticker(contract.contract_symbol)
            snap2 = ticker_by_symbol.snapshot
            print(f"    best_bid={snap2.best_bid}  best_ask={snap2.best_ask}  "
                  f"mark_price={snap2.mark_price}  underlying_spot={snap2.underlying_spot}")
        except Exception as exc:
            print(f"    !! Request failed: {exc}")

        # Sanity check: premium should never exceed notional for a vanilla long option.
        notional = contract.strike * contract.contract_multiplier
        print(f"\n  Sanity check: approx notional per contract = strike * multiplier = {notional}")
        print(f"  (A real vanilla option premium should sit well under this, not exceed it.)")

    conn.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m pricing.diagnose_pair <pair_id>")
        sys.exit(1)
    sys.exit(diagnose(sys.argv[1]))
