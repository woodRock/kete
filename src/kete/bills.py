"""Bills you name yourself. Detection (queries.subscriptions) needs regular days, one payee
spelling and a settled price; a named bill needs none of those:

    kete bills add Internet --match telco --min 80 --max 150
    kete bills add "AI app" --match aico --match aicorp

Its next date is the latest matching payment plus one cycle, and its amount is the latest
payment, so a price change shows straight away. Named bills replace any detected subscription
they match.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

PERIODS = {"weekly": 7, "fortnightly": 14, "monthly": 30.4, "quarterly": 91, "yearly": 365}


class BillError(Exception):
    pass


def add(conn: sqlite3.Connection, name: str, match: list[str], cadence: str = "monthly",
        amount_min: float | None = None, amount_max: float | None = None) -> dict:
    name = " ".join(name.split())
    match = [" ".join(m.split()).lower() for m in match if m.strip()]
    if not name or not match:
        raise BillError("a bill needs a name and at least one --match text")
    if cadence not in PERIODS:
        raise BillError(f"cadence must be one of {', '.join(PERIODS)}")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise BillError("--min is more than --max")
    conn.execute(
        """INSERT INTO bills (name, match, amount_min_cents, amount_max_cents, cadence, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET match = excluded.match, amount_min_cents = excluded.amount_min_cents,
             amount_max_cents = excluded.amount_max_cents, cadence = excluded.cadence""",
        (name, json.dumps(match), None if amount_min is None else round(amount_min * 100),
         None if amount_max is None else round(amount_max * 100), cadence,
         datetime.now().isoformat(timespec="seconds")))
    return next(b for b in declared(conn) if b["name"] == name)


def remove(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("DELETE FROM bills WHERE lower(name) = lower(?)", (name,)).rowcount > 0


def declared(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute("SELECT * FROM bills ORDER BY name").fetchall()
    except sqlite3.OperationalError:  # read-only connection to a database without the table yet
        return []
    return [{"id": r["id"], "name": r["name"], "match": json.loads(r["match"]), "cadence": r["cadence"],
             "amount_min": None if r["amount_min_cents"] is None else r["amount_min_cents"] / 100,
             "amount_max": None if r["amount_max_cents"] is None else r["amount_max_cents"] / 100}
            for r in rows]


def matches(bill: dict, payee: str, cents: int | None = None) -> bool:
    low = (payee or "").lower()
    if not any(m in low for m in bill["match"]):
        return False
    if cents is not None:
        amt = -cents / 100
        if bill["amount_min"] is not None and amt < bill["amount_min"]:
            return False
        if bill["amount_max"] is not None and amt > bill["amount_max"]:
            return False
    return True


def evaluate(conn: sqlite3.Connection, bill: dict, today: date, not_transfer: str) -> dict | None:
    """The bill in the same shape as a detected subscription, from its matching payments.
    Pending card charges count: a new price often shows up as one before it posts."""
    clauses = " OR ".join("lower(t.payee) LIKE ?" for _ in bill["match"])
    rows = [r for r in conn.execute(
        f"""SELECT t.posted_date, t.amount_cents, t.payee, t.category FROM transactions t
            WHERE t.amount_cents < 0 AND {not_transfer} AND t.posted_date <= ?
              AND ({clauses}) ORDER BY t.posted_date, t.id""",
        (today.isoformat(), *[f"%{m}%" for m in bill["match"]]))
        if matches(bill, r["payee"], r["amount_cents"])]
    if not rows:
        return None
    period = PERIODS[bill["cadence"]]
    last = rows[-1]
    last_date = date.fromisoformat(last["posted_date"])
    amount = -last["amount_cents"] / 100
    return {
        "payee": bill["name"],
        "category": last["category"],
        "merged": sorted({r["payee"] for r in rows}),
        "search": bill["match"][0],
        "cadence": bill["cadence"],
        "typical_amount": round(amount, 2),
        "last_amount": round(amount, 2),
        "count": len(rows),
        "first_date": rows[0]["posted_date"],
        "last_date": last["posted_date"],
        "next_expected": (last_date + timedelta(days=round(period))).isoformat(),
        "active": (today - last_date).days <= period * 2 + 8,  # same rule as detected ones
        "confidence": "named",
        "varying_days": True,
        "named": True,
        "monthly_cost": round(amount * 30.4 / period, 2),
    }


# ----------------------------------------------------------------------------- your choices
# Names and cancellations apply to any recurring payment, detected or named. Detected ones are
# keyed by the text kete matches them on (their `search`), so a rename survives new payments.


def pref_key(s: dict) -> str:
    return (s.get("search") or s.get("original_payee") or s["payee"]).lower()


def apply_prefs(conn: sqlite3.Connection, subs: list[dict]) -> list[dict]:
    """Display names and cancellations, applied to queries.subscriptions() results."""
    try:
        prefs = {r["key"]: r for r in conn.execute("SELECT * FROM recurring_prefs")}
    except sqlite3.OperationalError:
        return subs
    for s in subs:
        p = prefs.get(pref_key(s))
        if p is None:
            continue
        if p["name"] and not s.get("named"):
            s["original_payee"], s["payee"] = s["payee"], p["name"]
        if p["cancelled_on"]:
            s["cancelled"], s["cancelled_on"] = True, p["cancelled_on"]
            if p["cancelled_last"] and s["last_date"] > p["cancelled_last"]:
                s["charged_after_cancel"] = True  # stays active: you're still paying
            else:
                s["active"] = False
    return subs


def find(subs: list[dict], name: str) -> dict | None:
    """A recurring payment by its shown name, or its original payee text."""
    low = " ".join(name.split()).lower()
    for s in subs:
        if low in (s["payee"].lower(), (s.get("original_payee") or "").lower(), pref_key(s)):
            return s
    return None


def rename(conn: sqlite3.Connection, s: dict, new: str) -> None:
    new = " ".join(new.split())
    if not new:
        raise BillError("the new name is empty")
    if s.get("named"):
        try:
            conn.execute("UPDATE bills SET name = ? WHERE lower(name) = lower(?)", (new, s["payee"]))
        except sqlite3.IntegrityError:
            raise BillError(f"there's already a bill called {new!r}") from None
        return
    conn.execute("""INSERT INTO recurring_prefs (key, name) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET name = excluded.name""", (pref_key(s), new))


def set_cancelled(conn: sqlite3.Connection, s: dict, cancelled: bool, today: date | None = None) -> None:
    """Mark as cancelled from today: it leaves the totals and Coming up now, and a later charge
    is flagged as charged after you cancelled. cancelled=False takes that back."""
    if cancelled:
        conn.execute("""INSERT INTO recurring_prefs (key, cancelled_on, cancelled_last) VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET cancelled_on = excluded.cancelled_on,
                        cancelled_last = excluded.cancelled_last""",
                     (pref_key(s), (today or date.today()).isoformat(), s["last_date"]))
    else:
        conn.execute("UPDATE recurring_prefs SET cancelled_on = NULL, cancelled_last = NULL WHERE key = ?",
                     (pref_key(s),))
