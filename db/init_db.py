"""
Initializes the local SQLite database from db/schema.sql.

Usage (must be run as a module from the repo root, not as a direct script --
running `python db/init_db.py` sets sys.path[0] to db/ instead of the repo
root, which breaks the `from config.settings import DB` import below):
    python -m db.init_db

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
