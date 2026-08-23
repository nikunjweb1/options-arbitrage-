"""
Tests for pricing/run_pricing.py's Section M.2 MIN_NET_CREDIT gate.

Per docs/architecture.md Section M.2: net_entry_cost > RISK.min_net_credit
is a hard DO_NOT_ENTER gate, not a ranking preference. is_entry_eligible()
is the one function that decides this -- everything else (the ranked
display filter, the persisted `signals.entry_eligible` column) is wiring
around this single decision, so this is the one function that most needs a
direct test independent of any live DB or network call.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from pricing.run_pricing import _ensure_entry_eligible_column, is_entry_eligible


class TestIsEntryEligible:
    def test_positive_net_credit_above_threshold_is_eligible(self) -> None:
        assert is_entry_eligible(Decimal("5.00"), Decimal("0.0")) is True

    def test_net_debit_is_never_eligible(self) -> None:
        assert is_entry_eligible(Decimal("-5.00"), Decimal("0.0")) is False

    def test_exactly_zero_is_not_a_real_credit(self) -> None:
        """Section M.2 requires a real net CREDIT -- net_entry_cost must be
        strictly greater than min_net_credit, not merely non-negative."""
        assert is_entry_eligible(Decimal("0.00"), Decimal("0.0")) is False

    def test_safety_margin_above_zero_is_respected(self) -> None:
        """A small positive credit can still fail the gate if MIN_NET_CREDIT
        is configured above zero as a safety margin."""
        assert is_entry_eligible(Decimal("0.50"), Decimal("1.0")) is False
        assert is_entry_eligible(Decimal("1.50"), Decimal("1.0")) is True


class TestEnsureEntryEligibleColumnMigration:
    def test_adds_column_to_a_pre_migration_signals_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY, ts TEXT NOT NULL, pair_id TEXT NOT NULL,
                net_entry_cost TEXT NOT NULL, expected_value TEXT NOT NULL, expected_profit TEXT NOT NULL,
                prob_of_profit TEXT NOT NULL, var_95 TEXT, expected_shortfall TEXT, required_margin TEXT,
                score TEXT NOT NULL, score_breakdown TEXT
            )
            """
        )
        _ensure_entry_eligible_column(conn)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()]
        assert "entry_eligible" in columns

    def test_is_idempotent_on_an_already_migrated_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY, ts TEXT NOT NULL, pair_id TEXT NOT NULL,
                net_entry_cost TEXT NOT NULL, expected_value TEXT NOT NULL, expected_profit TEXT NOT NULL,
                prob_of_profit TEXT NOT NULL, var_95 TEXT, expected_shortfall TEXT, required_margin TEXT,
                score TEXT NOT NULL, score_breakdown TEXT,
                entry_eligible INTEGER NOT NULL DEFAULT 0 CHECK (entry_eligible IN (0, 1))
            )
            """
        )
        # Must not raise even though the column already exists.
        _ensure_entry_eligible_column(conn)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()]
        assert columns.count("entry_eligible") == 1
