"""SQLite schema and connection helpers. One file is the single source of truth."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,        -- short name used in filenames / --account
    label         TEXT NOT NULL,
    number        TEXT,                        -- full number, used to match OFX ACCTID
    number_masked TEXT,
    type          TEXT NOT NULL DEFAULT 'everyday',
    currency      TEXT NOT NULL DEFAULT 'NZD'
);

CREATE UNIQUE INDEX IF NOT EXISTS accounts_number ON accounts(number);

CREATE TABLE IF NOT EXISTS imports (
    id           INTEGER PRIMARY KEY,
    filename     TEXT NOT NULL,
    sha256       TEXT NOT NULL UNIQUE,
    format       TEXT NOT NULL,
    account_id   INTEGER REFERENCES accounts(id),
    imported_at  TEXT NOT NULL,
    date_from    TEXT,
    date_to      TEXT,
    rows_new     INTEGER NOT NULL DEFAULT 0,
    rows_dup     INTEGER NOT NULL DEFAULT 0,
    archived_to  TEXT,
    warnings     TEXT NOT NULL DEFAULT '[]'    -- JSON list of strings
);

CREATE TABLE IF NOT EXISTS transactions (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    posted_date      TEXT NOT NULL,            -- ISO yyyy-mm-dd
    amount_cents     INTEGER NOT NULL,         -- signed, debits negative
    payee            TEXT NOT NULL DEFAULT '', -- Westpac "Other Party" / OFX NAME
    description      TEXT NOT NULL DEFAULT '', -- Westpac "Description" (DEBIT, ...) / OFX MEMO
    particulars      TEXT NOT NULL DEFAULT '',
    code             TEXT NOT NULL DEFAULT '', -- Westpac "Analysis Code"
    reference        TEXT NOT NULL DEFAULT '',
    fitid            TEXT,
    dedupe_key       TEXT NOT NULL UNIQUE,
    source           TEXT NOT NULL,            -- westpac-ofx | westpac-csv | akahu
    import_id        INTEGER REFERENCES imports(id),
    raw_json         TEXT NOT NULL,
    category         TEXT,
    category_source  TEXT,                     -- rule | manual
    rule_index       INTEGER,
    is_transfer      INTEGER NOT NULL DEFAULT 0,
    pending          INTEGER NOT NULL DEFAULT 0, -- card auth not yet posted
    transfer_pair_id INTEGER
);
CREATE INDEX IF NOT EXISTS tx_account_date ON transactions(account_id, posted_date);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(posted_date);

CREATE TABLE IF NOT EXISTS balances (
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    as_of         TEXT NOT NULL,               -- end of this day
    balance_cents INTEGER NOT NULL,
    source        TEXT NOT NULL,               -- westpac-ofx | manual
    PRIMARY KEY (account_id, as_of, source)
);

-- Remembered answers to "which account is this CSV?" keyed by a filename pattern.
CREATE TABLE IF NOT EXISTS file_mappings (
    pattern    TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id)
);
"""

# Accounts created on first import. Any other filename prefix becomes a new account.
DEFAULT_ACCOUNTS = {
    "everyday": ("Everyday", "everyday"),
    "savings": ("Savings", "savings"),
    "credit": ("Credit card", "credit"),
}


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        if "pending" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_or_create_account(conn: sqlite3.Connection, key: str) -> int:
    key = key.lower()
    row = conn.execute("SELECT id FROM accounts WHERE key = ?", (key,)).fetchone()
    if row:
        return row["id"]
    label, type_ = DEFAULT_ACCOUNTS.get(key, (key.title(), "everyday"))
    cur = conn.execute(
        "INSERT INTO accounts (key, label, type) VALUES (?, ?, ?)", (key, label, type_)
    )
    return cur.lastrowid


def find_account(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM accounts WHERE key = ?", (key.lower(),)).fetchone()


def mask_number(number: str) -> str:
    """0100-0012345-03 -> 0100-***2345-03; keeps enough to recognise the account."""
    digits = number.replace(" ", "")
    parts = digits.split("-")
    if len(parts) >= 3:
        mid = parts[1]
        parts[1] = "*" * max(len(mid) - 4, 0) + mid[-4:]
        return "-".join(parts)
    return "*" * max(len(digits) - 4, 0) + digits[-4:]
