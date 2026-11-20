"""Seed data: a single ``employees`` table with 15 synthetic rows.

Teams are Sales, Engineering and Marketing. Exactly five employees are on
the Engineering team so the row-filter demo has an easy number to check.
All names and figures are invented.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    team        TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    salary      INTEGER NOT NULL,
    last_raise  TEXT    NOT NULL   -- ISO date of the most recent raise
);
"""

EMPLOYEES = [
    # id, name,              team,          role,                     salary, last_raise
    (1,  "Priya Raman",      "Engineering", "Staff Engineer",         187000, "2026-02-14"),
    (2,  "Diego Alvarez",    "Engineering", "Senior Engineer",        162000, "2025-11-03"),
    (3,  "Hana Kobayashi",   "Engineering", "Engineer II",            138000, "2026-04-21"),
    (4,  "Samuel Okafor",    "Engineering", "Engineer I",             121000, "2025-09-15"),
    (5,  "Lena Fischer",     "Engineering", "Site Reliability Eng.",  154000, "2026-01-30"),
    (6,  "Marcus Lee",       "Sales",       "Account Executive",      98000,  "2025-12-01"),
    (7,  "Aisha Bello",      "Sales",       "Senior Account Exec.",   124000, "2026-03-08"),
    (8,  "Tomasz Nowak",     "Sales",       "Sales Engineer",         131000, "2025-10-19"),
    (9,  "Grace Whitfield",  "Sales",       "Sales Development Rep",  72000,  "2026-05-02"),
    (10, "Rafael Souza",     "Sales",       "Regional Sales Lead",    142000, "2025-08-27"),
    (11, "Mei Chen",         "Marketing",   "Content Lead",           109000, "2026-02-02"),
    (12, "Oliver Grant",     "Marketing",   "Growth Marketer",        97000,  "2025-11-20"),
    (13, "Zara Hussain",     "Marketing",   "Brand Designer",         91000,  "2026-04-04"),
    (14, "Noah Petrov",      "Marketing",   "Marketing Analyst",      88000,  "2025-12-12"),
    (15, "Isabella Rossi",   "Marketing",   "Product Marketing Mgr",  128000, "2026-03-25"),
]


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection with row access by column name."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def seed(conn: sqlite3.Connection) -> None:
    """Create and populate the employees table (idempotent)."""
    conn.executescript(SCHEMA)
    count = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO employees (id, name, team, role, salary, last_raise) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            EMPLOYEES,
        )
        conn.commit()


def seeded_connection(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = connect(path)
    seed(conn)
    return conn
