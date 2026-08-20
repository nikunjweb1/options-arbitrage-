"""
Initializes the local SQLite database from db/schema.sql.

Usage:
    python db/init_db.py

Safe to re-run: every table is created with CREATE TABLE IF NOT EXISTS, so
this never drops or overwrites existing data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from config.settings import DB


def init_db() -> None:
    db_path = DB.sqlite_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
        print(f"Database initialized at {db_path.resolve()}")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
