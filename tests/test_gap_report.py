"""
Unit tests for collectors/gap_report.py's gap-detection logic.

Pure function tests, no network, no real SQLite file -- just exercises
find_gaps() against constructed timestamp sequences.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from collectors.gap_report import find_gaps


def _dt(minutes_offset: int) -> datetime:
    base = datetime(2026, 8, 19, 0, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minutes_offset)


def test_no_gaps_when_evenly_spaced() -> None:
    timestamps = [_dt(0), _dt(1), _dt(2), _dt(3)]
    gaps = find_gaps({"INSTR-1": timestamps}, threshold=timedelta(minutes=2))
    assert gaps == {}


def test_detects_single_gap() -> None:
    # 30-minute gap between the 2nd and 3rd tick
    timestamps = [_dt(0), _dt(1), _dt(31), _dt(32)]
    gaps = find_gaps({"INSTR-1": timestamps}, threshold=timedelta(minutes=5))

    assert "INSTR-1" in gaps
    assert len(gaps["INSTR-1"]) == 1
    prev, curr, delta = gaps["INSTR-1"][0]
    assert prev == _dt(1)
    assert curr == _dt(31)
    assert delta == timedelta(minutes=30)


def test_multiple_instruments_independent() -> None:
    by_instrument = {
        "INSTR-CLEAN": [_dt(0), _dt(1), _dt(2)],
        "INSTR-GAPPY": [_dt(0), _dt(50)],
    }
    gaps = find_gaps(by_instrument, threshold=timedelta(minutes=5))
    assert "INSTR-CLEAN" not in gaps
    assert "INSTR-GAPPY" in gaps


def test_empty_input_produces_no_gaps() -> None:
    assert find_gaps({}, threshold=timedelta(minutes=5)) == {}


def test_single_tick_produces_no_gaps() -> None:
    # No consecutive pair to compare -- a single tick can't have a gap.
    gaps = find_gaps({"INSTR-1": [_dt(0)]}, threshold=timedelta(minutes=5))
    assert gaps == {}
