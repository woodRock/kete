"""Export transactions as CSV, an hledger journal or a Beancount ledger.

Plain-text accounting layout:
    Assets:<Account>          each kete account (credit cards and loans are Liabilities:)
    Expenses:<Category>       money out, by category (Expenses:Uncategorised if none)
    Income:<Category>         money in
    Assets:Receivable:<Name>  the part of a payment someone owes you (kete owe)
    Equity:Transfers          a transfer whose other side isn't in kete
A paired transfer is written once, from one of your accounts to the other.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3

FORMATS = ("csv", "hledger", "beancount")
CSV_COLUMNS = ("id", "date", "account", "amount", "payee", "description", "particulars", "code",
               "reference", "category", "transfer", "shared", "pending")


def _rows(conn: sqlite3.Connection, date_from: str | None, date_to: str | None, account: str | None) -> list[dict]:
    sql = """SELECT t.*, a.key AS account, a.label AS account_label, a.type AS account_type,
                    (SELECT json_group_array(json_array(person, cents)) FROM shares s WHERE s.tx_id = t.id) AS shares_json
             FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE 1=1"""
    params: list = []
    if date_from:
        sql += " AND t.posted_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND t.posted_date <= ?"
        params.append(date_to)
    if account:
        sql += " AND a.key = ?"
        params.append(account.lower())
    return [dict(r) for r in conn.execute(sql + " ORDER BY t.posted_date, t.id", params)]


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([r["id"], r["posted_date"], r["account"], f"{r['amount_cents'] / 100:.2f}", r["payee"],
                    r["description"], r["particulars"], r["code"], r["reference"], r["category"] or "",
                    int(bool(r["is_transfer"] or r["transfer_pair_id"])), f"{r['shared_cents'] / 100:.2f}",
                    r["pending"]])
    return buf.getvalue()


def _component(name: str) -> str:
    """'eating out & bars' -> 'Eating-Out-Bars': valid in both hledger and Beancount."""
    words = re.findall(r"[A-Za-z0-9]+", name)
    s = "-".join(w[:1].upper() + w[1:] for w in words) or "Other"
    return s if s[0].isalpha() else "X" + s


def _asset(r: dict) -> str:
    root = "Liabilities" if r["account_type"] in ("credit", "loan") else "Assets"
    return f"{root}:{_component(r['account'])}"


def _postings(r: dict, by_id: dict[int, dict]) -> list[tuple[str, int]] | None:
    """[(account, cents)] summing to zero, or None to skip (the other leg of a written pair)."""
    cents = r["amount_cents"]
    own = _asset(r)
    if r["transfer_pair_id"]:
        other = by_id.get(r["transfer_pair_id"])
        if other is None:
            return [(own, cents), ("Equity:Transfers", -cents)]
        if cents > 0:  # written with the outgoing leg
            return None
        return [(own, cents), (_asset(other), -cents)]
    if r["is_transfer"]:
        return [(own, cents), ("Equity:Transfers", -cents)]
    postings = [(own, cents)]
    shared = 0
    for person, share in json.loads(r["shares_json"] or "[]"):
        postings.append((f"Assets:Receivable:{_component(person)}", share))
        shared += share
    rest = -cents - shared
    if rest:
        root = "Expenses" if cents < 0 else "Income"
        postings.append((f"{root}:{_component(r['category'] or 'Uncategorised')}", rest))
    return postings


def _amount(cents: int) -> str:
    return f"{cents / 100:.2f} NZD"


def _narration(r: dict) -> tuple[str, str]:
    payee = " ".join(r["payee"].split()) or r["description"] or "(no payee)"
    note = " ".join(x for x in (r["particulars"], r["code"], r["reference"]) if x)
    return payee, note


def to_hledger(rows: list[dict]) -> str:
    by_id = {r["id"]: r for r in rows}
    out = ["; Exported by kete. Amounts in NZD.", ""]
    for r in rows:
        postings = _postings(r, by_id)
        if postings is None:
            continue
        payee, note = _narration(r)
        status = "! " if r["pending"] else ""
        head = f"{r['posted_date']} {status}{payee.replace(';', ',')}"
        if note:
            head += f"  ; {note}"
        out.append(head)
        out.append(f"    ; kete-id: {r['id']}")
        width = max(len(a) for a, _ in postings) + 4
        out += [f"    {a:<{width}}{_amount(c):>14}" for a, c in postings]
        out.append("")
    return "\n".join(out)


def _bean_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_beancount(rows: list[dict]) -> str:
    by_id = {r["id"]: r for r in rows}
    entries = []
    opened: dict[str, str] = {}
    for r in rows:
        postings = _postings(r, by_id)
        if postings is None:
            continue
        for a, _ in postings:
            opened.setdefault(a, r["posted_date"])
        payee, note = _narration(r)
        flag = "!" if r["pending"] else "*"
        lines = [f"{r['posted_date']} {flag} {_bean_str(payee)} {_bean_str(note)}",
                 f"  kete-id: {_bean_str(str(r['id']))}"]
        width = max(len(a) for a, _ in postings) + 4
        lines += [f"  {a:<{width}}{_amount(c):>14}" for a, c in postings]
        entries.append("\n".join(lines))
    head = ['; Exported by kete. Amounts in NZD.', 'option "operating_currency" "NZD"', ""]
    head += [f"{d} open {a}" for a, d in sorted(opened.items(), key=lambda x: (x[1], x[0]))]
    return "\n".join(head) + "\n\n" + "\n\n".join(entries) + "\n"


def export(conn: sqlite3.Connection, fmt: str, date_from: str | None = None, date_to: str | None = None,
           account: str | None = None) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    rows = _rows(conn, date_from, date_to, account)
    return {"csv": to_csv, "hledger": to_hledger, "beancount": to_beancount}[fmt](rows)
