"""
Shared SQLite read helpers -- pulled out of matching/run_matcher.py so
pricing/run_ev.py (and anything else that needs to read real captured data)
doesn't duplicate the row-parsing logic. Read-only; nothing here writes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

from normalization.schemas import (
    OptionContract,
    OptionType,
    OptionVariant,
    SettlementMethod,
)


def row_to_contract(row: sqlite3.Row) -> OptionContract | None:
    """Parses one `instruments` table row into an OptionContract. Returns
    None (and lets the caller decide whether to log) on malformed data rather
    than raising -- a single bad row should not abort a batch read."""
    try:
        return OptionContract(
            exchange=row["exchange"],
            underlying=row["underlying"],
            base_asset=row["underlying"],
            quote_asset=row["quote_currency"],
            option_type=OptionType(row["option_type"]),
            option_variant=OptionVariant(row["option_variant"]),
            strike=Decimal(row["strike"]),
            expiry_timestamp=datetime.fromisoformat(row["expiry_ts"]),
            settlement_timestamp=datetime.fromisoformat(row["settlement_ts"]),
            settlement_method=SettlementMethod(row["settlement_method"]),
            settlement_price_formula=row["settlement_price_formula"],
            contract_multiplier=Decimal(row["contract_multiplier"]),
            lot_size=Decimal(row["lot_size"]),
            tick_size=Decimal(row["tick_size"]),
            quote_currency=row["quote_currency"],
            settlement_currency=row["settlement_currency"],
            contract_symbol=row["symbol"],
            instrument_id=row["instrument_id"],
            is_european=bool(row["is_european"]),
        )
    except (ValueError, InvalidOperation, TypeError):
        return None


def get_contract(conn: sqlite3.Connection, exchange: str, instrument_id: str) -> OptionContract | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM instruments WHERE exchange = ? AND instrument_id = ?",
        (exchange, instrument_id),
    ).fetchone()
    if row is None:
        return None
    return row_to_contract(row)


def get_candidate_pairs(
    conn: sqlite3.Connection, min_confidence: float = 0.0
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM candidate_pairs WHERE CAST(match_confidence AS REAL) >= ? ORDER BY created_at DESC",
        (min_confidence,),
    ).fetchall()
