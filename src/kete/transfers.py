"""Pair the two legs of a move between tracked accounts so neither counts as income or spend."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import date

from .db import mask_number

ACCOUNT_RE = re.compile(r"\b((?:\d{2}-)?\d{4}-\d{7}-\d{2,3})\b")  # [bank-]branch-account-suffix
MAX_DAYS = 3


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def detect(conn: sqlite3.Connection, max_days: int = MAX_DAYS) -> list[dict]:
    numbers = {r["id"]: r["number"] for r in conn.execute("SELECT id, number FROM accounts")}
    keys = {r["id"]: r["key"] for r in conn.execute("SELECT id, key FROM accounts")}
    base = """SELECT id, account_id, posted_date, amount_cents, payee FROM transactions
              WHERE transfer_pair_id IS NULL AND category_source IS NOT 'manual'"""
    outs = conn.execute(base + " AND amount_cents < 0").fetchall()
    ins = conn.execute(base + " AND amount_cents > 0").fetchall()
    ins_by_amount: dict[int, list] = {}
    for r in ins:
        ins_by_amount.setdefault(r["amount_cents"], []).append(r)

    def named(row) -> str | None:
        m = ACCOUNT_RE.search(row["payee"] or "")
        return m.group(1) if m else None

    def refers_elsewhere(row, counterpart_id) -> bool:
        """The payee names an account number, we know the counterpart's number, and they differ."""
        n, known = named(row), numbers.get(counterpart_id)
        return bool(n and known and n != known)

    raw = []
    for o in outs:
        for i in ins_by_amount.get(-o["amount_cents"], []):
            if i["account_id"] == o["account_id"]:
                continue
            gap = _days(o["posted_date"], i["posted_date"])
            if gap > max_days:
                continue
            # "To X" and "From X": money went A -> X -> B via an untracked account, not A -> B.
            if named(o) and named(o) == named(i):
                continue
            raw.append((gap, o, i))

    # Numbers we don't know yet: a clear majority of candidates naming the same number wins.
    votes: dict[int, Counter] = {}
    for _, o, i in raw:
        if named(o):
            votes.setdefault(i["account_id"], Counter())[named(o)] += 1
        if named(i):
            votes.setdefault(o["account_id"], Counter())[named(i)] += 1
    for acct, counter in votes.items():
        if numbers.get(acct) is None:
            (top, n), total = counter.most_common(1)[0], sum(counter.values())
            if n >= 3 and n / total > 0.6:
                numbers[acct] = top

    candidates = []
    for gap, o, i in raw:
        if refers_elsewhere(o, i["account_id"]) or refers_elsewhere(i, o["account_id"]):
            continue
        score = int(bool(named(o))) + int(bool(named(i)))
        candidates.append((gap, -score, o["id"], i["id"], o, i))

    candidates.sort(key=lambda c: c[:4])
    used: set[int] = set()
    pairs = []
    for gap, _, oid, iid, o, i in candidates:
        if oid in used or iid in used:
            continue
        used.update((oid, iid))
        pairs.append({
            "out_id": oid,
            "in_id": iid,
            "amount": -o["amount_cents"] / 100,
            "from_account": keys[o["account_id"]],
            "to_account": keys[i["account_id"]],
            "out_date": o["posted_date"],
            "in_date": i["posted_date"],
            "days_apart": gap,
            "out_payee": o["payee"],
            "in_payee": i["payee"],
        })
    pairs.sort(key=lambda p: p["out_date"])
    return pairs


def apply(conn: sqlite3.Connection, pairs: list[dict]) -> int:
    for p in pairs:
        conn.execute(
            """UPDATE transactions SET transfer_pair_id = ?, is_transfer = 1, category = 'Transfer',
               category_source = 'pair' WHERE id IN (?, ?)""",
            (p["out_id"], p["out_id"], p["in_id"]),
        )
        _learn_numbers(conn, p)
    conn.commit()
    return len(pairs)


def _learn_numbers(conn: sqlite3.Connection, p: dict) -> None:
    """'To 0100-...-04' on the outgoing leg is the receiving account's number, and vice versa."""
    for payee, key in ((p["out_payee"], p["to_account"]), (p["in_payee"], p["from_account"])):
        m = ACCOUNT_RE.search(payee or "")
        if m:
            conn.execute(
                """UPDATE accounts SET number = ?, number_masked = ? WHERE key = ? AND number IS NULL
                   AND NOT EXISTS (SELECT 1 FROM accounts b WHERE b.number = ?)""",
                (m.group(1), mask_number(m.group(1)), key, m.group(1)),
            )


def unpair(conn: sqlite3.Connection, tx_id: int) -> None:
    row = conn.execute("SELECT transfer_pair_id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if row and row["transfer_pair_id"]:
        conn.execute(
            """UPDATE transactions SET transfer_pair_id = NULL, is_transfer = 0, category = NULL,
               category_source = NULL WHERE transfer_pair_id = ?""",
            (row["transfer_pair_id"],),
        )
        conn.commit()
