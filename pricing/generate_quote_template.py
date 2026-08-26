"""
CLI: generates a pre-filled manual_quotes.json template for
pricing/manual_spread_finder.py.

WHY THIS EXISTS: during a live 1:30 PM IST window, hand-typing each strike,
expiry, and option_type into JSON is exactly the kind of friction that eats
into an already-short window. This script pulls Delta's live chain for
today's date, picks a spread of strikes around the current spot price, and
writes out a manual_quotes.json skeleton with strike/expiry/option_type
already filled in and bid/ask left as null -- so during the actual live
window, you only need to fill in the two numbers you're already reading off
Shark or CoinSwitch's screen, not retype the whole row.

USAGE:
    python -m pricing.generate_quote_template --underlying BTC --exchange shark
    python -m pricing.generate_quote_template --underlying BTC --exchange coinswitch --strike-range-pct 5 --strikes-per-side 4

    Writes manual_quotes_template.json (or --out <path>). Open it, fill in
    each row's "bid"/"ask" from what you see on Shark/CoinSwitch's site,
    delete any rows you don't have a live quote for, save, then run:

        python -m pricing.manual_spread_finder --quotes manual_quotes_template.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timezone
from decimal import Decimal
from pathlib import Path

from exchange_adapters.delta import DeltaAdapter, DeltaAdapterError
from normalization.schemas import OptionType


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a manual_quotes.json template (strikes/expiries pre-filled from Delta's live chain)."
    )
    parser.add_argument("--underlying", default="BTC")
    parser.add_argument("--exchange", required=True, choices=["shark", "coinswitch"],
                         help="Which exchange you'll be reading quotes off of -- just fills the 'exchange' field, doesn't fetch from it.")
    parser.add_argument("--strike-range-pct", type=float, default=3.0,
                         help="How far above/below spot to include strikes, as a %% of spot (default 3%%).")
    parser.add_argument("--strikes-per-side", type=int, default=5,
                         help="Max number of strikes to include on each side of spot (default 5, to keep the template a manageable size to fill in live).")
    parser.add_argument("--expiry-date", type=str, default=None,
                         help="Short leg's settlement date, YYYY-MM-DD. Defaults to today (UTC date).")
    parser.add_argument("--out", type=Path, default=Path("manual_quotes_template.json"))
    args = parser.parse_args()

    expiry_date = date.fromisoformat(args.expiry_date) if args.expiry_date else date.today()

    adapter = DeltaAdapter()
    try:
        chain = adapter.get_option_chain(args.underlying, expiry=None)
    except DeltaAdapterError as exc:
        print(f"Could not fetch Delta's chain: {exc}", file=sys.stderr)
        return 1

    # Spot price: use any contract's underlying reference via a live ticker
    # call rather than trusting a stale field on the chain response -- the
    # chain endpoint (per exchange_adapters/delta.py's own documented bug
    # history) has been wrong about non-strike/expiry fields before.
    same_day_contracts = [c for c in chain if c.expiry_timestamp.date() == expiry_date]
    if not same_day_contracts:
        print(
            f"No Delta contracts found expiring on {expiry_date}. "
            f"Delta may not have a same-day contract for this date -- check manually, "
            f"or pass --expiry-date for a date you know exists.",
            file=sys.stderr,
        )
        return 1

    try:
        probe_ticker = adapter.get_ticker(same_day_contracts[0].instrument_id)
    except DeltaAdapterError as exc:
        print(f"Could not fetch a live ticker to determine spot price: {exc}", file=sys.stderr)
        return 1

    spot = probe_ticker.snapshot.underlying_spot or probe_ticker.snapshot.mark_price
    if spot is None:
        print("Could not determine current spot price from Delta's ticker data.", file=sys.stderr)
        return 1

    print(f"Spot ~{spot} (from Delta's live ticker). Using strikes within ±{args.strike_range_pct}% for {expiry_date}.")

    lower_bound = spot * (Decimal("1") - Decimal(str(args.strike_range_pct)) / 100)
    upper_bound = spot * (Decimal("1") + Decimal(str(args.strike_range_pct)) / 100)

    calls_in_range = sorted(
        {c.strike for c in same_day_contracts if c.option_type == OptionType.CALL and lower_bound <= c.strike <= upper_bound}
    )
    puts_in_range = sorted(
        {c.strike for c in same_day_contracts if c.option_type == OptionType.PUT and lower_bound <= c.strike <= upper_bound}
    )

    # Nearest-to-spot strikes first, capped at --strikes-per-side per side --
    # deliberately not "every strike in range", since a huge template is
    # exactly the friction this script exists to avoid during a live window.
    calls_sorted_by_distance = sorted(calls_in_range, key=lambda s: abs(s - spot))[: args.strikes_per_side]
    puts_sorted_by_distance = sorted(puts_in_range, key=lambda s: abs(s - spot))[: args.strikes_per_side]

    rows = []
    for strike in sorted(calls_sorted_by_distance):
        rows.append({
            "exchange": args.exchange, "underlying": args.underlying, "option_type": "call",
            "strike": str(strike), "expiry": expiry_date.isoformat(),
            "bid": None, "ask": None,
        })
    for strike in sorted(puts_sorted_by_distance):
        rows.append({
            "exchange": args.exchange, "underlying": args.underlying, "option_type": "put",
            "strike": str(strike), "expiry": expiry_date.isoformat(),
            "bid": None, "ask": None,
        })

    if not rows:
        print(
            f"No strikes found within ±{args.strike_range_pct}% of spot ({spot}) for {expiry_date}. "
            f"Try a wider --strike-range-pct.",
            file=sys.stderr,
        )
        return 1

    args.out.write_text(json.dumps(rows, indent=2))
    print(f"Wrote {len(rows)} row(s) ({len(calls_sorted_by_distance)} calls, {len(puts_sorted_by_distance)} puts) to {args.out}.")
    print(
        f"\nNEXT: open {args.out}, fill in each row's \"bid\"/\"ask\" from {args.exchange}'s "
        f"live screen (delete any row you don't have a live quote for -- null bid/ask will "
        f"error, not silently skip, when manual_spread_finder.py reads it), save, then run:\n"
        f"    python -m pricing.manual_spread_finder --quotes {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
