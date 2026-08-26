"""
Manual-trading assistant: finds the single best (or top N) currently-live,
entry-eligible spread and prints it as an actionable trade ticket.

WHY THIS EXISTS: per docs/architecture.md Section M.7/M.9, full automation of
the cross-exchange version of this strategy is blocked -- Shark has no
documented options order API at all, and CoinSwitch's options API is
request-only, not yet granted. Rather than wait on that, this script
reframes the deliverable: analyze everything this system already knows how
to analyze (matching, EV, net-credit gate, liquidity check, stress test) and
hand a human the single best real opportunity to execute manually on the
exchange's own UI. This does NOT place any order -- it only reads.

WHAT "BEST" MEANS HERE, explicitly, so it's never a black box:
  1. Must be entry_eligible: net_entry_cost > RISK.min_net_credit (Section M.2)
     -- never recommends a net-debit trade, per the video's own math.
  2. Must pass ev_engine.py's liquidity check (GAP #1): real resting size on
     both legs, not just a quote that exists.
  3. Must NOT already be expired -- see "BUG FOUND + FIXED" below. Neither
     leg's own expiry math nor the net-credit/liquidity checks above catch
     this; it needed its own explicit check.
  4. Ranked by score = expected_value * probability_of_profit (same
     heuristic run_pricing.py already uses -- not a new, undocumented
     formula invented just for this script).
  5. Surfaced, not filtered on: the +/-10% stress P&L (GAP #2). A candidate
     with a great grid-based EV but a negative stress_pnl is still shown --
     with that fact printed prominently -- rather than hidden or silently
     excluded, since deciding how much tail risk to accept is exactly the
     kind of judgment call this script exists to hand to a human, not make
     for them.

BUG FOUND + FIXED (2026-08-26): the first real run recommended a spread
whose short leg had ALREADY EXPIRED FIVE DAYS EARLIER, labeled "URGENT --
under 1 hour to short-leg expiry." Root cause: ev_engine.py computes
`time_to_T1_years = max((expiry - now), 0.0)` -- when expiry is in the past,
this clamps to exactly 0 rather than going negative, and nothing in this
script (or, it turns out, in pricing/run_pricing.py either -- see that
file's docstring, which references an "805 skipped: already expired" log
line that does not actually appear anywhere in its current code) checked
`short.expiry_timestamp > now` before evaluating a candidate at all. Zero
hours-to-expiry got interpreted as "about to expire" instead of "already
dead." This is a dangerous class of bug specifically for a tool whose whole
purpose is telling a human what to act on RIGHT NOW -- fixed by adding an
explicit, first-thing expiry check per candidate, independent of (and
before) any EV/liquidity computation, with its own clearly-labeled skip
reason so it's auditable, not silent.

WHAT THIS DOES NOT DO:
  - Does not place, modify, or cancel any order. LIVE_TRADING stays
    irrelevant to this script entirely -- it has no order-placement code
    path to gate in the first place.
  - Does not guarantee the printed prices are still available by the time
    you act on them -- see the data-freshness check below. This is a
    decision aid for manual execution, not a live quote stream.
  - Currently Delta-only in practice: candidate_pairs today only contains
    delta_india same-exchange pairs (Section J), since Shark/CoinSwitch have
    no working options execution path (Section M.7/M.9) and Shark's market
    data isn't wired into the collector yet (Section M.6). The "spread" this
    prints is a same-exchange Delta calendar spread, not the cross-exchange
    trade the source video describes, until that changes.
  - Is NOT tax advice. See _tax_adjusted() below: this is a disclosed,
    simplified approximation of general Indian crypto tax rules, not a
    personalized calculation, and the TDS treatment specifically for
    OPTIONS (as opposed to spot VDA transfers, which Section 194S was
    written for) is not officially clarified anywhere this project found.
    Verify with a real tax professional before trusting the after-tax
    number for an actual filing decision -- treat it as directionally
    useful, not exact.

TAX MODELING (added 2026-08-26, per explicit request to account for this):
  - Flat 30% tax on GAINS only (Section 115BBH), applied to expected_value
    when positive. No loss offset against other income/gains is modeled --
    if expected_value is negative, tax_on_gains is 0 (you don't get a tax
    "credit" for an expected loss, and this project isn't attempting to
    model set-off rules across a whole portfolio, only this one trade).
  - 1% TDS (Section 194S) estimated on total transacted notional (both legs'
    entry value) -- a genuine approximation, not a confirmed rule for
    options specifically. Flagged in every ticket, not silently applied.
  - net_profit_after_tax = expected_value - tax_on_gains - tds_estimate.
    TDS is a withholding, not a final tax -- it's credited against your
    actual year-end tax liability, not lost. It's still subtracted here
    because it affects near-term cash flow, which matters for someone
    manually trading with limited capital -- but it's not "gone money"
    the way tax_on_gains is.

Usage:
    python -m pricing.best_trade --underlying BTC
    python -m pricing.best_trade --underlying BTC --top-n 3
    python -m pricing.best_trade --underlying BTC --allow-negative-stress
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from config.settings import DB, RISK
from db.loaders import get_candidate_pairs, get_contract
from pricing.ev_engine import EVResult, InsufficientDataError, LeanEVEngine
from pricing.run_pricing import _fee_pct_for, _load_latest_snapshot, _row_to_candidate

logger = logging.getLogger("pricing.best_trade")

# If the freshest market_data tick used for either leg is older than this,
# the ticket is still printed but with a loud staleness warning -- per the
# module docstring, this is a decision aid, not a live feed, and a human
# about to act on it needs to know if the collector has fallen behind.
_STALE_DATA_WARNING_SEC = 30

# See "TAX MODELING" in the module docstring above -- a disclosed
# approximation, not personalized tax advice.
_INDIA_FLAT_TAX_RATE = Decimal("0.30")
_INDIA_TDS_RATE = Decimal("0.01")


def _seconds_stale(ts: datetime) -> float:
    return (datetime.now(timezone.utc) - ts).total_seconds()


@dataclass(frozen=True)
class TaxEstimate:
    tax_on_gains: Decimal
    tds_estimate: Decimal
    net_profit_after_tax: Decimal
    transacted_notional: Decimal


def _tax_adjusted(result: EVResult) -> TaxEstimate:
    """See "TAX MODELING" in this module's docstring for exactly what is
    and isn't modeled here, and why it's an approximation, not advice."""
    tax_on_gains = (
        result.expected_value * _INDIA_FLAT_TAX_RATE
        if result.expected_value > 0
        else Decimal("0")
    )
    transacted_notional = result.short_bid_scaled + result.long_ask_scaled
    tds_estimate = transacted_notional * _INDIA_TDS_RATE
    net_profit_after_tax = result.expected_value - tax_on_gains - tds_estimate
    return TaxEstimate(
        tax_on_gains=tax_on_gains,
        tds_estimate=tds_estimate,
        net_profit_after_tax=net_profit_after_tax,
        transacted_notional=transacted_notional,
    )


@dataclass(frozen=True)
class Recommendation:
    pair_id: str
    result: EVResult
    candidate: object
    short_snapshot_ts: datetime
    long_snapshot_ts: datetime
    tax: TaxEstimate
    stress_clean: bool


def compute_recommendations(
    conn: sqlite3.Connection,
    underlying: str,
    *,
    min_confidence: float = 0.9,
    allow_negative_stress: bool = False,
) -> tuple[list[Recommendation], int]:
    """
    Shared implementation for both the CLI (main(), below) and the
    dashboard API (dashboard/backend/app.py's /api/best-trade). Kept as a
    single function deliberately -- the CLI and the API showing DIFFERENT
    recommendations because the logic was copy-pasted and drifted apart
    would be a much worse bug than either one being temporarily unavailable.

    Returns (recommendations sorted best-first, count of expired-and-skipped
    candidates).
    """
    candidate_rows = get_candidate_pairs(conn, min_confidence=min_confidence)
    filtered = []
    for row in candidate_rows:
        short = get_contract(conn, row["short_exchange"], row["short_instrument_id"])
        if short is not None and short.underlying == underlying:
            filtered.append(row)
    candidate_rows = filtered

    now = datetime.now(timezone.utc)
    eligible: list[Recommendation] = []
    skipped_expired = 0

    for row in candidate_rows:
        candidate = _row_to_candidate(conn, row)
        if candidate is None:
            continue

        # BUG FIX (see module docstring): this MUST happen before any EV
        # computation -- time_to_short_expiry_hours clamps to 0 for
        # already-expired contracts and looks identical to "about to
        # expire" otherwise.
        if candidate.short_contract.expiry_timestamp <= now:
            skipped_expired += 1
            continue

        short_snapshot = _load_latest_snapshot(conn, candidate.short_contract.exchange, candidate.short_contract.instrument_id)
        long_snapshot = _load_latest_snapshot(conn, candidate.long_contract.exchange, candidate.long_contract.instrument_id)
        if short_snapshot is None or long_snapshot is None:
            continue

        engine = LeanEVEngine(
            short_taker_fee_pct=_fee_pct_for(candidate.short_contract.exchange),
            long_taker_fee_pct=_fee_pct_for(candidate.long_contract.exchange),
            min_contract_size=RISK.min_liquidity,
        )

        try:
            result = engine.evaluate(candidate, short_snapshot, long_snapshot)
        except InsufficientDataError:
            continue

        if result.net_entry_cost <= RISK.min_net_credit:
            continue  # Section M.2: never recommend a net-debit trade, full stop.

        stress_clean = result.stress_pnl_down_10pct >= 0 and result.stress_pnl_up_10pct >= 0
        eligible.append(
            Recommendation(
                pair_id=row["pair_id"],
                result=result,
                candidate=candidate,
                short_snapshot_ts=short_snapshot.timestamp,
                long_snapshot_ts=long_snapshot.timestamp,
                tax=_tax_adjusted(result),
                stress_clean=stress_clean,
            )
        )

    def sort_key(rec: Recommendation):
        score = rec.result.expected_value * rec.result.probability_of_profit
        if allow_negative_stress:
            return score
        return (rec.stress_clean, score)

    eligible.sort(key=sort_key, reverse=True)
    return eligible, skipped_expired


def _print_ticket(rank: int, rec: Recommendation) -> None:
    pair_id, result, candidate = rec.pair_id, rec.result, rec.candidate
    short = candidate.short_contract
    long_ = candidate.long_contract

    short_age = _seconds_stale(rec.short_snapshot_ts)
    long_age = _seconds_stale(rec.long_snapshot_ts)
    stale = short_age > _STALE_DATA_WARNING_SEC or long_age > _STALE_DATA_WARNING_SEC

    hrs = result.time_to_short_expiry_hours
    # hrs is guaranteed > 0 here -- the caller filters out already-expired
    # candidates before this function is ever called (see the BUG FOUND +
    # FIXED note in the module docstring). Still worded to never claim
    # certainty past what the (possibly stale) data actually supports.
    urgency = "URGENT -- under 1 hour to short-leg expiry" if hrs < 1 else \
              "soon -- under 6 hours" if hrs < 6 else \
              f"{hrs:.1f}h remaining"

    print(f"\n{'=' * 78}")
    print(f"#{rank}  {pair_id}")
    print(f"{'=' * 78}")

    if stale:
        print(f"  !! DATA STALENESS WARNING: short leg quote is {short_age:.0f}s old, "
              f"long leg quote is {long_age:.0f}s old (warn threshold: {_STALE_DATA_WARNING_SEC}s). "
              f"Re-check live prices on the exchange before acting -- do not trust these "
              f"numbers blindly if the collector has stalled. A stale quote can also mean the "
              f"'hours remaining' figure above is now wrong even though this candidate wasn't "
              f"expired at evaluation time -- if the staleness exceeds the hours remaining, "
              f"treat this ticket as unusable until you refresh the data.")

    print(f"\n  LEG 1 -- SELL (short) -- close automatically at expiry, no action needed then")
    print(f"    Exchange:      {short.exchange}")
    print(f"    Symbol:        {short.contract_symbol}")
    print(f"    Strike:        {short.strike}  {short.option_type.value.upper()}")
    print(f"    Expiry (T1):   {short.expiry_timestamp.isoformat()}  ({urgency})")
    print(f"    Sell at bid:   {result.short_bid_used}  (per-contract: {result.short_bid_scaled})")
    print(f"    Resting size seen: {result.short_bid_size} contracts")

    print(f"\n  LEG 2 -- BUY (long) -- MUST BE MANUALLY CLOSED AT T1 (see below)")
    print(f"    Exchange:      {long_.exchange}")
    print(f"    Symbol:        {long_.contract_symbol}")
    print(f"    Strike:        {long_.strike}  {long_.option_type.value.upper()}")
    print(f"    Expiry (T2):   {long_.expiry_timestamp.isoformat()}")
    print(f"    Buy at ask:    {result.long_ask_used}  (per-contract: {result.long_ask_scaled})")
    print(f"    Resting size seen: {result.long_ask_size} contracts")

    if short.contract_multiplier != long_.contract_multiplier:
        print(f"\n  !! CONTRACT MULTIPLIER MISMATCH: short leg = {short.contract_multiplier}, "
              f"long leg = {long_.contract_multiplier}. Size your quantities to NOTIONAL-"
              f"EQUIVALENT, not a 1:1 contract count, or you will be under/over-hedged. "
              f"See architecture.md Section C.4.")
    else:
        print(f"\n  Contract multiplier (both legs): {short.contract_multiplier} -- 1:1 contract "
              f"count is correctly hedged.")

    print(f"\n  ECONOMICS")
    print(f"    Net entry credit:     {result.net_entry_cost}  (fees included: {result.fees_total})")
    print(f"    Expected value (EV):  {result.expected_value}")
    print(f"    P(profit), 21x3 grid: {float(result.probability_of_profit):.0%}")
    print(f"    Worst case (in grid): {result.worst_case_pnl}")
    print(f"    Best case (in grid):  {result.best_case_pnl}")
    print(f"    Stress -10% spot:     {result.stress_pnl_down_10pct}"
          f"{'  << NEGATIVE, outside the normal grid range -- read this' if result.stress_pnl_down_10pct < 0 else ''}")
    print(f"    Stress +10% spot:     {result.stress_pnl_up_10pct}"
          f"{'  << NEGATIVE, outside the normal grid range -- read this' if result.stress_pnl_up_10pct < 0 else ''}")

    print(f"\n  AFTER-TAX ESTIMATE (India, APPROXIMATE -- see module docstring, not tax advice)")
    print(f"    Transacted notional (both legs): {rec.tax.transacted_notional}")
    print(f"    Tax on gains (30% flat, EV>0 only): -{rec.tax.tax_on_gains}")
    print(f"    TDS estimate (1% of notional, 194S, options treatment UNCONFIRMED): -{rec.tax.tds_estimate}")
    print(f"    Net profit after tax + TDS:       {rec.tax.net_profit_after_tax}")

    print(f"\n  MANUAL EXECUTION CHECKLIST (per architecture.md Section M.1/M.3):")
    print(f"    [ ] 1. Place both legs as close to simultaneously as you can manage manually --")
    print(f"           the longer the gap, the more legging risk (Section H).")
    print(f"    [ ] 2. Confirm BOTH fills before considering the position open.")
    print(f"    [ ] 3. Set a reminder for {short.expiry_timestamp.isoformat()} (short leg expiry, T1).")
    print(f"    [ ] 4. AT THAT EXACT TIME: manually sell/close the long leg ({long_.contract_symbol}).")
    print(f"           This is Exit A -- do not hold it to its own {long_.expiry_timestamp.date()} expiry.")
    print(f"           Forgetting this step is the video's own \"Scenario 4\" -- an unhedged,")
    print(f"           decaying position, not an arbitrage. See Section M.5.")


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Find and print the best currently-live spread(s) for manual execution.")
    parser.add_argument("--underlying", default="BTC")
    parser.add_argument("--top-n", type=int, default=1, help="How many ranked tickets to print (default 1).")
    parser.add_argument("--min-confidence", type=float, default=0.9,
                         help="Higher default than run_pricing.py's 0.5 -- a manual trade ticket "
                              "should favor exact/high-confidence structural matches, not "
                              "interpolated ones, since a human is about to act on it directly.")
    parser.add_argument("--allow-negative-stress", action="store_true",
                         help="By default, candidates with a negative +/-10%% stress P&L on "
                              "EITHER side are still shown but pushed to the bottom of the "
                              "ranking. Pass this to rank purely by score instead.")
    args = parser.parse_args()

    if not DB.sqlite_path.exists():
        logger.error("No database found at %s.", DB.sqlite_path)
        return 1

    conn = sqlite3.connect(DB.sqlite_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row

    eligible, skipped_expired = compute_recommendations(
        conn, args.underlying,
        min_confidence=args.min_confidence,
        allow_negative_stress=args.allow_negative_stress,
    )
    conn.close()

    if skipped_expired:
        print(f"\n({skipped_expired} candidate(s) skipped: short leg already expired -- "
              f"stale candidate_pairs/matcher data. Consider re-running "
              f"matching/run_matcher.py against fresh instruments if this number is large.)")

    if not eligible:
        print(f"\nNo entry-eligible (net-credit, liquid, non-expired) spreads found for "
              f"{args.underlying} right now. This is a real, honest 'nothing to trade' "
              f"result -- not an error. Re-run in a few minutes, or check that the collector "
              f"(collectors/run_realtime.py) is actually running and current.")
        return 0

    print(f"\nFound {len(eligible)} entry-eligible spread(s) for {args.underlying}. "
          f"Showing top {min(args.top_n, len(eligible))}.")
    if not args.allow_negative_stress:
        clean_count = sum(1 for rec in eligible if rec.stress_clean)
        print(f"({clean_count}/{len(eligible)} pass the +/-10% stress check cleanly -- those are ranked first.)")

    for i, rec in enumerate(eligible[: args.top_n], start=1):
        _print_ticket(i, rec)

    print(f"\n{'=' * 78}")
    print("Reminder: this tool only analyzes and reports. Nothing here places an order.")
    print("You execute both legs manually, on the exchange's own UI, using the details above.")
    print("After-tax figures are an approximation, not tax advice -- see module docstring.")
    print(f"{'=' * 78}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
