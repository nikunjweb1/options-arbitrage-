"""
CLI: finds the best manually-tradeable cross-exchange calendar spread.

WHY THIS EXISTS: as of 2026-08-24, neither Shark's market-data WebSocket
(exchange_adapters/shark_ws.py -- connects, but data reliability is still
being fixed, see that file's CONNECTION FIX HISTORY) nor a CoinSwitch
adapter of any kind exist yet. A fully-automated cross-exchange scanner has
nothing real to read on the short-leg side. But the underlying strategy
doesn't require automation to be tradeable -- the source video this project
is based on shows a human doing exactly this by eye: watch Shark/CoinSwitch's
own website for a strike's price, watch Delta's chain for the same strike,
and act if the spread looks good. This script is that same workflow, minus
the arithmetic and same-strike-hunting -- you paste in what you're already
looking at on Shark/CoinSwitch's website, and this pulls Delta's live
matching side automatically and does the net-credit math (now also: fees,
liquidity, and an Indian VDA tax estimate) for every strike you give it,
instantly, correctly, every time -- computer-check rather than
computer-execute.

This script places no orders and has no execution path. It is a
recommendation report for a human to act on manually.

PIVOT, 2026-08-26: results now PERSIST to the `manual_recommendations`
table (see db/schema.sql) instead of only printing to a terminal, so the
dashboard (dashboard/backend/app.py) can serve them -- this is the point
where "run a script and read the terminal" becomes "check the website
before you trade." Every recommendation this script computes gets written,
not just the ones that clear the net-credit bar -- so the dashboard can
show "we checked X, none were good enough" as honestly as it can show a
real opportunity, consistent with this project's fail-closed/show-your-work
principle elsewhere.

LIQUIDITY, 2026-08-26: net_entry_cost alone doesn't tell you how MUCH you
can trade at that price. This now also computes max_safe_contracts =
min(short_size_input, long_ask_size_live) -- the largest size where BOTH
legs can actually fill at the quoted price. The short-leg size is
user-typed (see ManualQuote.size below) since there's no live feed to read
it from; the long-leg size is real, live-fetched from Delta's order book.
A high net_entry_cost with a tiny max_safe_contracts is a real trap this
project wants to surface, not hide -- see the WARNING printed for low-
liquidity recommendations.

TAX, 2026-08-26: every recommendation now also reports an Indian VDA tax
estimate via pricing/tax.py. Per that module's own docstring: NOT TAX
ADVICE, a documented-assumptions estimate for comparing candidates, confirm
with a CA before trusting it for a real filing/trading decision.

NOTIFICATION, 2026-08-26: alongside the dashboard, net-credit results from
each run are also sent as a Telegram alert (see
notifications/telegram_notifier.py) -- the dashboard is best for browsing/
comparing many candidates side by side, but a phone notification is what
actually gets seen the moment a real opportunity appears rather than only
when you happen to have the dashboard open. Best-effort: if Telegram isn't
configured (see that module's docstring for 3-minute setup) or the API call
fails, this prints a clear notice and the script's actual output/exit
behavior is unaffected either way.

USAGE:
    Create a small JSON/CSV of what you're seeing on Shark or CoinSwitch's
    site right now, one row per option you want checked, e.g.:

        [
          {"exchange": "shark", "underlying": "BTC", "option_type": "call",
           "strike": "67000", "expiry": "2026-08-28", "bid": "205", "ask": "215",
           "size": "2.5"},
          {"exchange": "coinswitch", "underlying": "BTC", "option_type": "put",
           "strike": "68000", "expiry": "2026-08-28", "bid": "180", "ask": "190",
           "size": "1.0"}
        ]

    "size" is optional -- if omitted, max_safe_contracts and the liquidity
    warning are skipped for that row (printed/persisted as unknown, not
    guessed at).

    Save as manual_quotes.json, then:

        python -m pricing.manual_spread_finder --quotes manual_quotes.json

    For each row, this fetches Delta's live chain for the same
    underlying/option_type/strike, finds every Delta contract that SETTLES
    STRICTLY AFTER the short leg's actual settlement instant (short leg's
    date + 1:30 PM IST -- see _short_leg_settlement_instant_utc -- NOT just
    "a later calendar date"), computes net_entry_cost for sell-short/buy-
    long, a liquidity-capped size, and a tax estimate, then prints AND
    persists a ranked, human-readable report.

    No expiry match on Delta at that exact strike? Prints the closest
    available strikes instead, rather than silently skipping -- so you know
    whether "no candidate" means "genuinely nothing available" or "close but
    not exact, check manually."
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
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

from config.settings import DB, DELTA
from exchange_adapters.delta import DeltaAdapter, DeltaAdapterError
from normalization.schemas import OptionType
from notifications.telegram_notifier import (
    format_recommendation_alert,
    is_configured as is_telegram_configured,
    send_telegram_message,
)
from pricing.tax import estimate_vda_tax


@dataclass
class ManualQuote:
    exchange: str
    underlying: str
    option_type: OptionType
    strike: Decimal
    expiry: date
    bid: Decimal
    ask: Decimal
    size: Decimal | None  # user-typed order-book size at `bid` -- see module docstring's LIQUIDITY note
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
            size_raw = row.get("size")
            quotes.append(
                ManualQuote(
                    exchange=str(row["exchange"]).strip().lower(),
                    underlying=str(row["underlying"]).strip().upper(),
                    option_type=OptionType(str(row["option_type"]).strip().lower()),
                    strike=Decimal(str(row["strike"])),
                    expiry=date.fromisoformat(str(row["expiry"])),
                    bid=Decimal(str(row["bid"])),
                    ask=Decimal(str(row["ask"])),
                    size=Decimal(str(size_raw)) if size_raw not in (None, "") else None,
                    raw_row=row,
                )
            )
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise SystemExit(
                f"Row {i} in '{path}' is malformed ({exc}). Row was: {row}\n"
                f"Expected keys: exchange, underlying, option_type (call/put), "
                f"strike, expiry (YYYY-MM-DD), bid, ask. Optional: size."
            )
    return quotes


@dataclass
class Recommendation:
    quote: ManualQuote
    delta_instrument_id: str
    delta_expiry: datetime
    delta_bid: Decimal
    delta_ask: Decimal
    delta_ask_size: Decimal | None
    net_entry_cost: Decimal
    gap_hours: float
    max_safe_contracts: Decimal | None
    gross_profit_estimate: Decimal
    tax_owed_estimate: Decimal
    net_profit_after_tax_estimate: Decimal
    tds_withheld_estimate: Decimal


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

        max_safe = None
        if quote.size is not None and snap.ask_size is not None:
            max_safe = min(quote.size, snap.ask_size)

        tax = estimate_vda_tax(gross_profit=net_entry_cost, contract_value_for_tds=quote.bid)

        recs.append(
            Recommendation(
                quote=quote,
                delta_instrument_id=contract.instrument_id,
                delta_expiry=contract.expiry_timestamp,
                delta_bid=snap.best_bid,
                delta_ask=snap.best_ask,
                delta_ask_size=snap.ask_size,
                net_entry_cost=net_entry_cost,
                gap_hours=gap_hours,
                max_safe_contracts=max_safe,
                gross_profit_estimate=tax.gross_profit,
                tax_owed_estimate=tax.tax_owed_estimate,
                net_profit_after_tax_estimate=tax.net_profit_after_tax,
                tds_withheld_estimate=tax.tds_withheld_estimate,
            )
        )

    return recs


def _persist_recommendations(recs: list[Recommendation]) -> int:
    if not recs:
        return 0
    try:
        conn = sqlite3.connect(DB.sqlite_path)
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                str(uuid.uuid4()), now,
                r.quote.exchange, r.quote.underlying, r.quote.option_type.value,
                str(r.quote.strike), r.quote.expiry.isoformat(), str(r.quote.bid),
                "delta_india", r.delta_instrument_id, r.delta_expiry.isoformat(),
                str(r.delta_ask), str(r.delta_ask_size) if r.delta_ask_size is not None else None,
                str(r.net_entry_cost), int(r.net_entry_cost > 0),
                str(r.quote.size) if r.quote.size is not None else None,
                str(r.max_safe_contracts) if r.max_safe_contracts is not None else None,
                str(r.gross_profit_estimate), str(r.tax_owed_estimate),
                str(r.net_profit_after_tax_estimate), str(r.tds_withheld_estimate),
            )
            for r in recs
        ]
        conn.executemany(
            """
            INSERT INTO manual_recommendations (
                recommendation_id, ts, short_exchange, short_underlying, short_option_type,
                short_strike, short_expiry_date, short_bid_input,
                long_exchange, long_instrument_id, long_expiry_ts,
                long_ask_live, long_ask_size_live,
                net_entry_cost, entry_eligible,
                short_size_input, max_safe_contracts,
                gross_profit_estimate, tax_owed_estimate,
                net_profit_after_tax_estimate, tds_withheld_estimate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        conn.close()
        return len(rows)
    except sqlite3.Error as exc:
        print(f"  WARNING: could not persist recommendations to the DB ({exc}) -- "
              f"terminal report below is still accurate, but won't appear on the dashboard.", file=sys.stderr)
        return 0


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
        "NOTE: tax figures are an ESTIMATE per documented assumptions in pricing/tax.py, "
        "NOT tax advice -- confirm with a CA before relying on them.\n"
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

    written = _persist_recommendations(all_recs)
    if all_recs:
        print(f"Persisted {written}/{len(all_recs)} recommendation(s) to the dashboard DB.\n")

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
        liquidity_note = (
            f"max safely-fillable size ~{r.max_safe_contracts} contracts"
            if r.max_safe_contracts is not None
            else "liquidity UNKNOWN (no size given for the manual leg, or Delta reported no ask_size)"
        )
        print(
            f"    -> MANUAL ACTION: SELL 1x {r.quote.underlying} {r.quote.option_type.value} "
            f"{r.quote.strike} on {r.quote.exchange} @ ~{r.quote.bid} (expiry {r.quote.expiry}), "
            f"BUY 1x same strike on delta_india @ ~{r.delta_ask} (expiry {r.delta_expiry.date()}, "
            f"instrument {r.delta_instrument_id}). Net credit ~{r.net_entry_cost:.4f} per contract "
            f"before slippage. Liquidity: {liquidity_note}. "
            f"Est. after-tax profit per contract (NOT tax advice): ~{r.net_profit_after_tax_estimate:.4f} "
            f"(tax ~{r.tax_owed_estimate:.4f}, TDS withheld ~{r.tds_withheld_estimate:.4f}). "
            f"VERIFY LIVE PRICES ON BOTH EXCHANGES before executing -- this quote may be stale "
            f"by the time you read this.\n"
        )
        if r.max_safe_contracts is not None and r.max_safe_contracts < Decimal("1"):
            print(f"    !! LOW LIQUIDITY WARNING: max safely-fillable size is under 1 contract "
                  f"({r.max_safe_contracts}) -- the size that looks tradeable on paper may not "
                  f"actually fill at this price.\n")

    # NOTIFICATION, 2026-08-26: alongside the dashboard, send a Telegram
    # alert for the net-credit candidates found in THIS run, so a real
    # opportunity is visible even when the dashboard isn't open. Best-effort
    # only -- see notifications/telegram_notifier.py's docstring for why
    # this never raises or changes this script's exit behavior either way.
    if is_telegram_configured():
        alert_text = format_recommendation_alert(credit_recs)
        sent = send_telegram_message(alert_text)
        print(f"Telegram notification {'sent' if sent else 'FAILED to send'} for {len(credit_recs[:5])} candidate(s).")
    else:
        print(
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not in config/.env) -- "
            "skipping notification. See notifications/telegram_notifier.py's docstring for 3-minute setup."
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
