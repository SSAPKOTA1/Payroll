"""SQLite persistence for manual overrides."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "overrides.db"


def _conn():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS overrides (
                company      TEXT NOT NULL,
                employee_id  TEXT NOT NULL,
                year         INTEGER NOT NULL,
                month        INTEGER NOT NULL,
                paid_override   INTEGER,   -- 1=paid, 0=not paid, NULL=auto
                gross_override  REAL,
                net_override    REAL,
                paid_amount_override REAL,
                PRIMARY KEY (company, employee_id, year, month)
            )
        """)


def get_override(company: str, employee_id: str, year: int, month: int) -> dict:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM overrides WHERE company=? AND employee_id=? AND year=? AND month=?",
            (company, employee_id, year, month)
        ).fetchone()
    if row:
        return dict(row)
    return {}


def set_override(company: str, employee_id: str, year: int, month: int, **kwargs):
    allowed = {"paid_override", "gross_override", "net_override", "paid_amount_override"}
    kwargs = {k: v for k, v in kwargs.items() if k in allowed}
    if not kwargs:
        return
    with _conn() as con:
        existing = con.execute(
            "SELECT 1 FROM overrides WHERE company=? AND employee_id=? AND year=? AND month=?",
            (company, employee_id, year, month)
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            con.execute(
                f"UPDATE overrides SET {sets} WHERE company=? AND employee_id=? AND year=? AND month=?",
                (*kwargs.values(), company, employee_id, year, month)
            )
        else:
            cols = "company, employee_id, year, month, " + ", ".join(kwargs.keys())
            placeholders = "?, ?, ?, ?, " + ", ".join("?" * len(kwargs))
            con.execute(
                f"INSERT INTO overrides ({cols}) VALUES ({placeholders})",
                (company, employee_id, year, month, *kwargs.values())
            )


def clear_override(company: str, employee_id: str, year: int, month: int):
    with _conn() as con:
        con.execute(
            "DELETE FROM overrides WHERE company=? AND employee_id=? AND year=? AND month=?",
            (company, employee_id, year, month)
        )


def get_all_overrides_for_month(company: str, year: int, month: int) -> dict:
    """Return {employee_id: override_dict} for a given company/month."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM overrides WHERE company=? AND year=? AND month=?",
            (company, year, month)
        ).fetchall()
    return {row["employee_id"]: dict(row) for row in rows}
