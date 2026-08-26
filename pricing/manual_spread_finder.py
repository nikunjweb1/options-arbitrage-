"""
CLI: finds the best manually-tradeable cross-exchange calendar spread.

WHY THIS EXISTS: as of 2026-08-24, neither Shark's market-data WebSocket
(exchange_adapters/shark_ws.py -- connects, but zero events arrive, an
active unresolved bug) nor a CoinSwitch adapter of any kind exist yet. A
fully-automated cross-exchange scanner has nothing real to read on the short
-leg side. But the underlying strategy doesn't require automation to be
tradeable -- the source video this project is based on shows a human doing
exactly this by eye: watch Shark/CoinSwitch's own website for a strike's
price, watch Delta's chain for the same strike, and act if the spread looks
good. This script is that same workflow, minus the arithmetic and
same-strike-hunting -- you paste in what you're already looking at on
Shark/CoinSwitch's website, and this pulls Delta's live matching side
automatically and does the net-credit math for every strike you give it,
instantly, correctly, every time -- computer-check rather than
computer-execute.

This script places no orders and has no execution path. It is a
recommendation report for a human to act on manually, matching where this
project actually is: pipeline proven end-to-end on Delta-only data
(pricing/run_pricing.py), but the real edge (per docs/architecture.md's own
Section D.5 analysis and this session's live run showing 92/92 Delta-only
same-exchange calendar spreads are net-debit) requires a genuine
cross-exchange IV difference, which needs a second exchange's real quote.

USAGE:
    Create a small JSON/CSV of what you're seeing on Shark or CoinSwitch's
    site right now, one row per option you want checked, e.g.:

        [
          {"exchange": "shark", "underlying": "BTC", "option_type": "call",
           "strike": "67000", "expiry": "2026-08-28", "bid": "205", "ask": "215"},
          {"exchange": "coinswitch", "underlying": "BTC", "option_type": "put",
           "strike": "68000", "expiry": "2026-08-28", "bid": "180", "ask": "190"}
        ]

    Save as manual_quotes.json, then:

        python -m pricing.manual_spread_finder --quotes manual_quotes.json

    For each row, this fetches Delta's live chain for the same
    underlying/option_type/strike, finds every Delta contract that SETTLES
    STRICTLY AFTER the short leg's actual settlement instant (short leg's
    date + 1:30 PM IST -- see _short_leg_settlement_instant_utc -- NOT just
    "a later calendar date"), computes net_entry_cost for sell-short/buy-
    long, and prints a ranked, human-readable report. This correctly
    includes SAME-DAY Delta contracts (which settle 5:30 PM IST, 4 hours
    later) -- same-day is the strategy's actual intended shape, not an edge
    case to exclude.

    No expiry match on Delta at that exact strike? Prints the closest
    available strikes instead, rather than silently skipping -- so you know
    whether "no candidate" means "genuinely nothing available" or "close but
    not exact, check manually."
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Per docs/architecture.md Section 0 (confirmed against Delta's own docs) and
# this session's screenshot evidence (CoinSwitch/Shark both showing ~27-30min
# to expiry at 1:02-1:00 PM IST, i.e. settling ~1:30 PM IST): Shark/CoinSwitch
# options settle at 1:30 PM IST. This is used to build the SHORT leg's exact
# settlement instant from its date, since the manual quote input only
# captures a date, not a time -- without this, "same day, 4 hours later" (the
# strategy's actual shape) was being misidentified as "no same-day match" and
# skipped, see this file's fix history / commit message.
_IST_OFFSET = timedelta(hours=5, minutes=30)
_SHARK_COINSWITCH_SETTLEMENT_TIME_IST = time(13, 30)

from config.settings import DELTA
from exchange_adapters.delta import DeltaAdapter, DeltaAdapterError
from normalization.schemas import OptionType


@dataclass
class ManualQuote:
    exchange: str
    underlying: str
    option_type: OptionType
    strike: Decimal
    expiry: date
    bid: Decimal
    ask: Decimal
    raw_row: dict  # kept for error messages that quote the original input back


def _load_quotes(path: Path) -> list[ManualQuote]:
    try:
        rows = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"'{path}' is not valid JSON: {exc}")
    if not isinstance(rows, list):
        raise SystemExit(f"'{path}' must contain a JSON list of quote objects, see this file's module docstring for the shape.")

    quotes: list[ManualQuote] = []
    for i, row in enumerate(rows):
        try:
            quotes.append(
                ManualQuote(
                    exchange=str(row["exchange"]).strip().lower(),
                    underlying=str(row["underlying"]).strip().upper(),
                    option_type=OptionType(str(row["option_type"]).strip().lower()),
                    strike=Decimal(str(row["strike"])),
                    expiry=date.fromisoformat(str(row["expiry"])),
                    bid=Decimal(str(row["bid"])),
                    ask=Decimal(str(row["ask"])),
                    raw_row=row,
                )
            )
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise SystemExit(
                f"Row {i} in '{path}' is malformed ({exc}). Row was: {row}\n"
                f"Expected keys: exchange, underlying, option_type (call/put), "
                f"strike, expiry (YYYY-MM-DD), bid, ask."
            )
    return quotes


@dataclass
class Recommendation:
    quote: ManualQuote
    delta_instrument_id: str
    delta_expiry: datetime
    delta_bid: Decimal
    delta_ask: Decimal
    net_entry_cost: Decimal
    gap_hours: float


def _short_leg_settlement_instant_utc(quote_date: date) -> datetime:
    """Builds the short leg's actual settlement instant (date + 1:30 PM IST,
    converted to UTC) -- NOT just a date. Comparing full instants (not just
    calendar dates) is what correctly allows same-day Delta contracts
    (which settle 5:30 PM IST, 4 hours later) to qualify as valid long legs
    -- this is the strategy's actual, intended shape, per the source video
    and docs/architecture.md Section 0."""
    naive_ist = datetime.combine(quote_date, _SHARK_COINSWITCH_SETTLEMENT_TIME_IST)
    return (naive_ist - _IST_OFFSET).replace(tzinfo=timezone.utc)


def _fee_adjusted(price: Decimal, fee_pct: Decimal, *, is_sell: bool) -> Decimal:
    """Sell side: you receive less than the quoted bid (fee comes out of
    proceeds). Buy side: you pay more than the quoted ask (fee added on
    top). Same convention as pricing/ev_engine.py's net_entry_cost math,
    kept consistent here deliberately rather than reinventing fee handling."""
    fee = price * fee_pct
    return price - fee if is_sell else price + fee


def find_candidates_for_quote(adapter: DeltaAdapter, quote: ManualQuote) -> list[Recommendation]:
    """
    Manual quote = the SHORT leg (sell side, since it's on the earlier-
    settling exchange per the strategy's core mechanism -- Shark/CoinSwitch
    settle earlier than Delta on any shared date). Delta's matching chain =
    candidate LONG legs. Only same underlying + same option_type + same
    strike + Delta expiry strictly after the manual quote's expiry are
    considered -- anything else isn't the strategy this project targets.
    """
    try:
        chain = adapter.get_option_chain(quote.underlying)
    except DeltaAdapterError as exc:
        print(f"  Delta API error while fetching {quote.underlying} chain: {exc}", file=sys.stderr)
        return []

    same_strike_type = [
        c for c in chain
        if c.option_type == quote.option_type and c.strike == quote.strike
    ]

    manual_fee_pct = Decimal("0.001")  # unverified placeholder for Shark/CoinSwitch, see manual_fee_pct note below
    delta_fee_pct = DELTA.fee_schedule.taker_fee_pct

    short_settlement_instant = _short_leg_settlement_instant_utc(quote.expiry)

    recs: list[Recommendation] = []
    for contract in same_strike_type:
        if contract.expiry_timestamp <= short_settlement_instant:
            continue  # long leg must settle strictly after the short leg's actual settlement instant
            # (was: same-day Delta contracts (settling 4hrs later at 5:30 PM
            # IST) were being wrongly excluded here by a date-only
            # comparison -- same-day IS the strategy's real shape, see the
            # helper function's docstring above)
        try:
            ticker = adapter.get_ticker(contract.instrument_id)
        except DeltaAdapterError as exc:
            print(f"  Skipping Delta instrument {contract.instrument_id}: {exc}", file=sys.stderr)
            continue
        snap = ticker.snapshot
        if snap.best_bid is None or snap.best_ask is None:
            continue

        short_net = _fee_adjusted(quote.bid, manual_fee_pct, is_sell=True)
        long_net = _fee_adjusted(snap.best_ask, delta_fee_pct, is_sell=False)
        net_entry_cost = short_net - long_net
        gap_hours = (contract.expiry_timestamp - short_settlement_instant).total_seconds() / 3600

        recs.append(
            Recommendation(
                quote=quote,
                delta_instrument_id=contract.instrument_id,
                delta_expiry=contract.expiry_timestamp,
                delta_bid=snap.best_bid,
                delta_ask=snap.best_ask,
                net_entry_cost=net_entry_cost,
                gap_hours=gap_hours,
            )
        )

    return recs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cross-reference manually-entered Shark/CoinSwitch quotes against Delta's live chain."
    )
    parser.add_argument("--quotes", required=True, type=Path, help="Path to a JSON file of manual quotes (see module docstring for shape).")
    args = parser.parse_args()

    if not args.quotes.exists():
        print(f"'{args.quotes}' not found. See this file's module docstring for the expected format.", file=sys.stderr)
        return 1

    quotes = _load_quotes(args.quotes)
    print(f"Loaded {len(quotes)} manual quote(s) from {args.quotes}.")
    print(
        "NOTE: manual_fee_pct=0.10% used for the Shark/CoinSwitch leg below is an "
        "UNVERIFIED placeholder (see manual_spread_finder.py) -- check the exchange's "
        "actual fee page before trusting net_entry_cost to the last decimal.\n"
    )

    adapter = DeltaAdapter()
    all_recs: list[Recommendation] = []
    for quote in quotes:
        print(f"Checking {quote.exchange} {quote.underlying} {quote.option_type.value} {quote.strike} "
              f"(expiry {quote.expiry}, bid={quote.bid}/ask={quote.ask}) against Delta's chain...")
        recs = find_candidates_for_quote(adapter, quote)
        if not recs:
            print("  No Delta contract found at this exact strike settling after the short leg. "
                  "Check the strike exists on Delta at all, or try an adjacent strike manually.")
        all_recs.extend(recs)
        print()

    if not all_recs:
        print("No candidates found across any input row. Nothing to recommend.")
        return 0

    all_recs.sort(key=lambda r: r.net_entry_cost, reverse=True)
    credit_recs = [r for r in all_recs if r.net_entry_cost > 0]

    print("=" * 78)
    print(f"RESULT: {len(all_recs)} candidate(s) checked, {len(credit_recs)} are net-credit.")
    print("=" * 78)

    if not credit_recs:
        print(
            "\nNone of the checked candidates are net-credit right now. Per this project's "
            "hard rule (Section M.2), a net-debit entry has unbounded downside risk and "
            "should NOT be manually traded, regardless of how good the EV looks on paper."
        )
        print("\nAll checked candidates, for reference (best net_entry_cost first):")
        for r in all_recs[:10]:
            _print_rec(r)
        return 0

    print(f"\nBest {min(5, len(credit_recs))} net-credit candidate(s) -- these are safe to consider manually:\n")
    for r in credit_recs[:5]:
        _print_rec(r)
        print(
            f"    -> MANUAL ACTION: SELL 1x {r.quote.underlying} {r.quote.option_type.value} "
            f"{r.quote.strike} on {r.quote.exchange} @ ~{r.quote.bid} (expiry {r.quote.expiry}), "
            f"BUY 1x same strike on delta_india @ ~{r.delta_ask} (expiry {r.delta_expiry.date()}, "
            f"instrument {r.delta_instrument_id}). Net credit ~{r.net_entry_cost:.4f} per contract "
            f"before slippage. VERIFY LIVE PRICES ON BOTH EXCHANGES before executing -- "
            f"this quote may be stale by the time you read this.\n"
        )

    return 0


def _print_rec(r: Recommendation) -> None:
    print(
        f"  net_entry_cost={r.net_entry_cost:+.4f}  gap={r.gap_hours:.1f}h  "
        f"{r.quote.exchange}:{r.quote.strike}{r.quote.option_type.value[0].upper()} "
        f"(exp {r.quote.expiry}) -> delta_india:{r.delta_instrument_id} "
        f"(exp {r.delta_expiry.date()}, bid={r.delta_bid}/ask={r.delta_ask})"
    )


if __name__ == "__main__":
    sys.exit(main())
