"""Money other people owe you: part of a payment that was theirs, and their repayments.

A share on a payment (cents > 0) says "Sam owes me $40 of this $80 dinner"; a share on an
incoming payment (cents < 0) says "this $40 was Sam paying me back". transactions.shared_cents
holds the total per transaction, so spending and income totals count only your own part.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime


class ShareError(Exception):
    pass


def parse_amount_spec(spec: str | None, total_cents: int) -> int:
    """'40', '$40.50', 'half', '1/3', '25%' or None (everything) -> cents of |total|."""
    whole = abs(total_cents)
    if not spec or spec.lower() in ("all", "full"):
        return whole
    s = spec.strip().lower().replace("$", "")
    if s == "half":
        return round(whole / 2)
    if m := re.fullmatch(r"(\d+)/(\d+)", s):
        if int(m.group(2)) == 0:
            raise ShareError("can't divide by zero")
        return round(whole * int(m.group(1)) / int(m.group(2)))
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)%", s):
        return round(whole * float(m.group(1)) / 100)
    try:
        return round(float(s) * 100)
    except ValueError:
        raise ShareError(f"amount must be dollars, half, 1/3 or 25%, not {spec!r}") from None


def _sync(conn: sqlite3.Connection, tx_id: int) -> None:
    conn.execute("""UPDATE transactions SET shared_cents =
                    (SELECT COALESCE(SUM(cents), 0) FROM shares WHERE tx_id = ?) WHERE id = ?""",
                 (tx_id, tx_id))


def set_share(conn: sqlite3.Connection, tx_id: int, person: str, spec: str | None = None,
              note: str = "") -> dict:
    """Record (or replace) one person's part of a transaction. Payments: what they owe you.
    Incoming money: what they paid back."""
    person = " ".join(person.split())
    if not person:
        raise ShareError("who? give a name")
    tx = conn.execute("SELECT amount_cents, is_transfer, transfer_pair_id FROM transactions WHERE id = ?",
                      (tx_id,)).fetchone()
    if tx is None:
        raise ShareError(f"no transaction {tx_id}")
    if tx["is_transfer"] or tx["transfer_pair_id"]:
        raise ShareError("that's a transfer between your own accounts")
    cents = parse_amount_spec(spec, tx["amount_cents"])
    others = conn.execute("SELECT COALESCE(SUM(ABS(cents)), 0) FROM shares WHERE tx_id = ? AND lower(person) != lower(?)",
                          (tx_id, person)).fetchone()[0]
    if cents <= 0 or cents + others > abs(tx["amount_cents"]):
        raise ShareError(f"amount must be between $0.01 and ${(abs(tx['amount_cents']) - others) / 100:,.2f}")
    existing = conn.execute("SELECT person FROM shares WHERE tx_id = ? AND lower(person) = lower(?)",
                            (tx_id, person)).fetchone()
    person = existing["person"] if existing else _canonical(conn, person)
    signed = cents if tx["amount_cents"] < 0 else -cents
    conn.execute("INSERT OR REPLACE INTO shares (tx_id, person, cents, note, created_at) VALUES (?, ?, ?, ?, ?)",
                 (tx_id, person, signed, note, datetime.now().isoformat(timespec="seconds")))
    _sync(conn, tx_id)
    return {"tx_id": tx_id, "person": person, "amount": signed / 100,
            "kind": "owes you" if signed > 0 else "paid you back"}


def _canonical(conn: sqlite3.Connection, person: str) -> str:
    """'sam' reuses an existing 'Sam'."""
    row = conn.execute("SELECT person FROM shares WHERE lower(person) = lower(?) LIMIT 1", (person,)).fetchone()
    return row["person"] if row else person


def remove_share(conn: sqlite3.Connection, tx_id: int, person: str | None = None) -> int:
    if person:
        n = conn.execute("DELETE FROM shares WHERE tx_id = ? AND lower(person) = lower(?)", (tx_id, person)).rowcount
    else:
        n = conn.execute("DELETE FROM shares WHERE tx_id = ?", (tx_id,)).rowcount
    _sync(conn, tx_id)
    return n


def balances(conn: sqlite3.Connection, include_settled: bool = False) -> list[dict]:
    """Per person: what they owe you now (owed − repaid). Positive means they owe you."""
    out = []
    for r in conn.execute(
        """SELECT person, SUM(CASE WHEN cents > 0 THEN cents ELSE 0 END) owed,
                  -SUM(CASE WHEN cents < 0 THEN cents ELSE 0 END) repaid, COUNT(*) n,
                  MAX(t.posted_date) last
           FROM shares s JOIN transactions t ON t.id = s.tx_id
           GROUP BY lower(person) ORDER BY SUM(cents) DESC"""):
        balance = (r["owed"] - r["repaid"]) / 100
        if balance or include_settled:
            out.append({"person": r["person"], "owed": r["owed"] / 100, "repaid": r["repaid"] / 100,
                        "balance": round(balance, 2), "entries": r["n"], "last_date": r["last"]})
    return out


def entries(conn: sqlite3.Connection, person: str | None = None) -> list[dict]:
    sql = """SELECT s.tx_id, s.person, s.cents, s.note, t.posted_date, t.amount_cents, t.payee, a.key AS account
             FROM shares s JOIN transactions t ON t.id = s.tx_id JOIN accounts a ON a.id = t.account_id"""
    params: list = []
    if person:
        sql += " WHERE lower(s.person) = lower(?)"
        params.append(person)
    sql += " ORDER BY t.posted_date DESC, s.tx_id DESC"
    return [{"tx_id": r["tx_id"], "person": r["person"], "date": r["posted_date"], "account": r["account"],
             "payee": r["payee"], "amount": r["amount_cents"] / 100, "share": r["cents"] / 100,
             "kind": "owes you" if r["cents"] > 0 else "paid you back", "note": r["note"]}
            for r in conn.execute(sql, params)]
