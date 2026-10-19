"""Read-only queries shared by the CLI (--json) and the TUI. Money is returned in dollars."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

NOT_TRANSFER = "t.is_transfer = 0 AND t.transfer_pair_id IS NULL"


def dollars(cents: int | None) -> float | None:
    return None if cents is None else round(cents / 100, 2)


_TRAILING_NUM = re.compile(r"(\s+\d+)+$")


def payee_key(payee: str) -> str:
    """'Some Merchant 21' and 'Some Merchant 15' are the same merchant (Westpac appends a day number)."""
    return _TRAILING_NUM.sub("", " ".join(payee.split())).lower()


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def prev_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


def latest_balance(conn: sqlite3.Connection, account_id: int) -> dict | None:
    """Most recent anchor (OFX ledger balance or a manual entry) rolled forward by later txns."""
    anchor = conn.execute(
        """SELECT as_of, balance_cents, source FROM balances WHERE account_id = ?
           ORDER BY as_of DESC, source = 'manual' LIMIT 1""",
        (account_id,),
    ).fetchone()
    if not anchor:
        return None
    after = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0), MAX(posted_date) FROM transactions "
        "WHERE account_id = ? AND posted_date > ?",
        (account_id, anchor["as_of"]),
    ).fetchone()
    return {
        "balance": dollars(anchor["balance_cents"] + after[0]),
        "as_of": after[1] or anchor["as_of"],
        "anchor_date": anchor["as_of"],
        "anchor_source": anchor["source"],
    }


def accounts(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for a in conn.execute("SELECT * FROM accounts ORDER BY id"):
        stats = conn.execute(
            """SELECT COUNT(*) n, MIN(posted_date) first, MAX(posted_date) last
               FROM transactions WHERE account_id = ?""",
            (a["id"],),
        ).fetchone()
        imp = conn.execute(
            "SELECT MAX(imported_at) FROM imports WHERE account_id = ?", (a["id"],)
        ).fetchone()[0]
        bal = latest_balance(conn, a["id"])
        days = (date.today() - date.fromisoformat(stats["last"])).days if stats["last"] else None
        out.append({
            "key": a["key"],
            "label": a["label"],
            "number_masked": a["number_masked"],
            "type": a["type"],
            "currency": a["currency"],
            "transactions": stats["n"],
            "first_date": stats["first"],
            "last_date": stats["last"],
            "days_since_last": days,
            "last_imported": imp,
            "balance": bal["balance"] if bal else None,
            "balance_as_of": bal["as_of"] if bal else None,
        })
    return out


def transactions(
    conn: sqlite3.Connection,
    account: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    search: str | None = None,
    uncategorised: bool = False,
    limit: int | None = None,
) -> list[dict]:
    sql = """SELECT t.id, a.key AS account, t.posted_date AS date, t.amount_cents, t.payee,
                    t.description, t.particulars, t.code, t.reference, t.category,
                    t.category_source, t.is_transfer, t.transfer_pair_id, t.source, t.pending
             FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE 1=1"""
    params: list = []
    if account:
        sql += " AND a.key = ?"
        params.append(account.lower())
    if date_from:
        sql += " AND t.posted_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND t.posted_date <= ?"
        params.append(date_to)
    if category:
        sql += " AND lower(t.category) = lower(?)"
        params.append(category)
    if uncategorised:
        sql += f" AND t.category IS NULL AND {NOT_TRANSFER}"
    if search:
        like = f"%{search}%"
        sql += """ AND (t.payee LIKE ? OR t.description LIKE ? OR t.particulars LIKE ?
                   OR t.code LIKE ? OR t.reference LIKE ?)"""
        params += [like] * 5
    sql += " ORDER BY t.posted_date DESC, t.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["amount"] = dollars(d.pop("amount_cents"))
        d["is_transfer"] = bool(d["is_transfer"] or d["transfer_pair_id"])
        d["pending"] = bool(d["pending"])
        rows.append(d)
    return rows


def raw_row(conn: sqlite3.Connection, tx_id: int) -> dict:
    r = conn.execute("SELECT raw_json FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return json.loads(r["raw_json"]) if r else {}


def spend(conn: sqlite3.Connection, month: str | None = None, by: str = "cat",
          account: str | None = None) -> dict:
    month = month or current_month()
    start, end = _month_bounds(month)
    sql = f"""SELECT t.amount_cents, t.category, t.payee FROM transactions t
              JOIN accounts a ON a.id = t.account_id
              WHERE t.posted_date >= ? AND t.posted_date < ? AND t.amount_cents < 0
              AND {NOT_TRANSFER}"""
    params: list = [start, end]
    if account:
        sql += " AND a.key = ?"
        params.append(account.lower())
    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    labels: dict[str, str] = {}
    for r in conn.execute(sql, params):
        if by == "payee":
            k = payee_key(r["payee"])
            labels.setdefault(k, _TRAILING_NUM.sub("", " ".join(r["payee"].split())))
        else:
            k = r["category"] or "Uncategorised"
            labels[k] = k
        totals[k] -= r["amount_cents"]
        counts[k] += 1
    rows = sorted(
        ({"name": labels[k], "total": dollars(v), "count": counts[k]} for k, v in totals.items()),
        key=lambda x: -x["total"],
    )
    return {"month": month, "by": by, "total": dollars(sum(totals.values())), "rows": rows}


def cashflow(conn: sqlite3.Connection, months: int = 12) -> list[dict]:
    end = current_month()
    keys = [end]
    for _ in range(months - 1):
        keys.append(prev_month(keys[-1]))
    keys.reverse()
    rows = {
        r["m"]: r
        for r in conn.execute(
            f"""SELECT substr(t.posted_date, 1, 7) m,
                       SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents ELSE 0 END) income,
                       -SUM(CASE WHEN t.amount_cents < 0 THEN t.amount_cents ELSE 0 END) spend
                FROM transactions t WHERE {NOT_TRANSFER} AND t.posted_date >= ?
                GROUP BY m""",
            (keys[0] + "-01",),
        )
    }
    out = []
    for k in keys:
        r = rows.get(k)
        inc, sp = (r["income"], r["spend"]) if r else (0, 0)
        out.append({"month": k, "income": dollars(inc), "spend": dollars(sp), "net": dollars(inc - sp)})
    return out


def stale(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    return [a for a in accounts(conn) if a["days_since_last"] is None or a["days_since_last"] > days]


CADENCES = (("weekly", 7, 1), ("fortnightly", 14, 2), ("monthly", 30.4, 4),
            ("quarterly", 91, 7), ("yearly", 365, 15))


def subscriptions(conn: sqlite3.Connection, min_count: int = 3) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    names: dict[str, str] = {}
    for r in conn.execute(
        f"""SELECT t.posted_date, t.amount_cents, t.payee, t.category FROM transactions t
            WHERE t.amount_cents < 0 AND t.pending = 0 AND {NOT_TRANSFER} ORDER BY t.posted_date"""
    ):
        k = payee_key(r["payee"])
        groups[k].append(r)
        names.setdefault(k, _TRAILING_NUM.sub("", " ".join(r["payee"].split())))

    today = date.today()
    out = []
    for k, rows in groups.items():
        by_day: dict[str, int] = {}
        for r in rows:  # several charges on one day count as one occurrence
            by_day[r["posted_date"]] = by_day.get(r["posted_date"], 0) + r["amount_cents"]
        days = sorted(by_day)
        if len(days) < min_count:
            continue
        gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(days, days[1:])]
        med_gap = median(gaps)
        cadence = next((c for c in CADENCES if abs(med_gap - c[1]) <= c[2]), None)
        if not cadence:
            continue
        name, period, tol = cadence
        regular = sum(1 for g in gaps if abs(g - period) <= tol * 2) / len(gaps)
        amounts = [-v for v in by_day.values()]
        med_amt = median(amounts)
        similar = sum(1 for a in amounts if abs(a - med_amt) <= max(0.25 * med_amt, 100)) / len(amounts)
        if regular < 0.6 or similar < 0.6:
            continue
        last = date.fromisoformat(days[-1])
        next_due = last + timedelta(days=round(period))
        out.append({
            "payee": names[k],
            "cadence": name,
            "typical_amount": dollars(int(med_amt)),
            "last_amount": dollars(amounts[-1]),
            "count": len(days),
            "last_date": days[-1],
            "next_expected": next_due.isoformat(),
            "active": (today - last).days <= period * 2 + tol,
            "category": rows[-1]["category"],
            "monthly_cost": dollars(int(med_amt * 30.4 / period)),
        })
    out.sort(key=lambda s: (not s["active"], -s["monthly_cost"]))
    return out


def import_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = []
    for r in conn.execute(
        """SELECT i.*, a.key AS account FROM imports i LEFT JOIN accounts a ON a.id = i.account_id
           ORDER BY i.id DESC LIMIT ?""",
        (limit,),
    ):
        d = dict(r)
        d["warnings"] = json.loads(d["warnings"] or "[]")
        d.pop("sha256")
        rows.append(d)
    return rows


def categories(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT category FROM transactions WHERE category IS NOT NULL ORDER BY 1"
    )]
