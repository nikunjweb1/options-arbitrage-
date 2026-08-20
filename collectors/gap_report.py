"""
Gap-report tool: verifies the Phase 2 exit criterion --

    "24h of continuous, gap-free Delta options + underlying data captured
    and queryable."

This does NOT run the collector. It reads whatever is already in
market_data and reports, per instrument, every gap between consecutive
ticks larger than a configurable threshold (default: 3x the configured
ticker_poll_interval_sec, to allow for normal jitter/retries without
flagging every minor delay as a "gap").

Usage:
    python -m collectors.gap_report
    python -m collectors.gap_report --threshold-sec 300
    python -m collectors.gap_report --since "2026-08-19T00:00:00"

Exit code is 0 if no gaps found above threshold, 1 otherwise -- so this can
be used as a pass/fail check, e.g. at the end of a 24h validation run.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from config.settings import COLLECTOR, DB


def _fetch_timestamps(conn: sqlite3.Connection, since: str | None) -> dict[str, list[datetime]]:
    query = "SELECT instrument_id, ts FROM market_data"
    params: tuple = ()
    if since:
        query += " WHERE ts >= ?"
        params = (since,)
    query += " ORDER BY instrument_id, ts"

    by_instrument: dict[str, list[datetime]] = {}
    for instrument_id, ts_raw in conn.execute(query, params):
        ts = datetime.fromisoformat(ts_raw)
        by_instrument.setdefault(instrument_id, []).append(ts)
    return by_instrument


def find_gaps(
    by_instrument: dict[str, list[datetime]], threshold: timedelta
) -> dict[str, list[tuple[datetime, datetime, timedelta]]]:
    gaps: dict[str, list[tuple[datetime, datetime, timedelta]]] = {}
    for instrument_id, timestamps in by_instrument.items():
        instrument_gaps = []
        for prev, curr in zip(timestamps, timestamps[1:]):
            delta = curr - prev
            if delta > threshold:
                instrument_gaps.append((prev, curr, delta))
        if instrument_gaps:
            gaps[instrument_id] = instrument_gaps
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description="Report gaps in collected market_data")
    parser.add_argument(
        "--threshold-sec", type=float, default=None,
        help="Gap threshold in seconds (default: 3x COLLECTOR.ticker_poll_interval_sec).",
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="Only consider data from this ISO8601 timestamp onward.",
    )
    args = parser.parse_args()

    threshold_sec = args.threshold_sec or (COLLECTOR.ticker_poll_interval_sec * 3)
    threshold = timedelta(seconds=threshold_sec)

    if not DB.sqlite_path.exists():
        print(f"No database found at {DB.sqlite_path}. Run db/init_db.py and the collector first.")
        return 1

    conn = sqlite3.connect(DB.sqlite_path)
    try:
        by_instrument = _fetch_timestamps(conn, args.since)
    finally:
        conn.close()

    if not by_instrument:
        print("No market_data rows found for the given window. Nothing to report.")
        return 1

    total_instruments = len(by_instrument)
    total_ticks = sum(len(ts) for ts in by_instrument.values())

    span_start = min(min(ts) for ts in by_instrument.values())
    span_end = max(max(ts) for ts in by_instrument.values())
    span_hours = (span_end - span_start).total_seconds() / 3600

    print(f"Instruments with data: {total_instruments}")
    print(f"Total ticks collected: {total_ticks}")
    print(f"Coverage window:       {span_start.isoformat()} -> {span_end.isoformat()} ({span_hours:.2f}h)")
    print(f"Gap threshold:         {threshold_sec:.0f}s ({threshold_sec / COLLECTOR.ticker_poll_interval_sec:.1f}x poll interval)")
    print()

    gaps = find_gaps(by_instrument, threshold)

    if not gaps:
        print(f"PASS: no gaps above {threshold_sec:.0f}s found across {total_instruments} instruments.")
        return 0

    total_gap_count = sum(len(g) for g in gaps.values())
    print(f"FAIL: {total_gap_count} gap(s) found across {len(gaps)} instrument(s):\n")
    for instrument_id, instrument_gaps in sorted(gaps.items()):
        print(f"  {instrument_id}: {len(instrument_gaps)} gap(s)")
        for prev, curr, delta in instrument_gaps[:5]:  # cap detail per instrument for readability
            print(f"      {prev.isoformat()} -> {curr.isoformat()}  ({delta})")
        if len(instrument_gaps) > 5:
            print(f"      ... and {len(instrument_gaps) - 5} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
