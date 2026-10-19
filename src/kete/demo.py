"""Synthetic finances for `kete dashboard --demo` and tests.

Nothing here comes from a real account. Agents should build and preview dashboards against this
data, never against ~/kete.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import db as dbmod
from . import rules as rules_mod
from . import transfers

DEMO_RULES = """
[[rule]]
name = "Salary"
payee = "Acme Ltd"
amount_min = 0.0
category = "Income"

[[rule]]
name = "Rent"
payee = "Harbour Property"
category = "Rent"

[[rule]]
name = "Shopping"
payee = ["the warehouse", "kmart", "noel leeming", "hallensteins"]
category = "Shopping"

[[rule]]
name = "Fitness"
payee = "citygym"
category = "Fitness"
"""


def build_demo_db(today: date | None = None, months: int = 15, seed: int = 7) -> sqlite3.Connection:
    today = today or date.today()
    rng = random.Random(seed)
    conn = dbmod.connect(Path(":memory:"))
    everyday = dbmod.get_or_create_account(conn, "everyday")
    savings = dbmod.get_or_create_account(conn, "savings")
    conn.execute("UPDATE accounts SET number = '0100-0000000-01', number_masked = '0100-***0000-01' WHERE id = ?", (everyday,))
    conn.execute("UPDATE accounts SET number = '0100-0000000-02', number_masked = '0100-***0000-02' WHERE id = ?", (savings,))
    start = date(today.year, today.month, 1)
    for _ in range(months):
        start = (start - timedelta(days=1)).replace(day=1)

    rows: list[tuple] = []

    def add(acct: int, d: date, amount: float, payee: str, desc: str = "DEBIT", code: str = "") -> None:
        if d <= today:
            rows.append((acct, d.isoformat(), round(amount * 100), payee, desc, code))

    d = start
    week = 0
    while d <= today:
        months_ago = (today.year - d.year) * 12 + today.month - d.month
        recent = months_ago < 3  # the last few months drift up, so suggestions have something to find
        winter = d.month in (6, 7, 8)
        if d.weekday() == 3 and week % 2 == 0:  # fortnightly pay into savings
            add(savings, d, 2450.00, "Acme Ltd", "DIRECT CREDIT")
        if d.weekday() == 0:
            week += 1
            add(everyday, d, -520.00, "Harbour Property", "AUTOMATIC PAYMENT")
            amt = 900.0
            add(savings, d, -amt, "To 0100-0000000-01", "ONLINE BANKING", "Transfer")
            add(everyday, d, amt, "From 0100-0000000-02", "DIRECT CREDIT", "Transfer")
            add(everyday, d, -14.00, "CityGym Membership", "AUTOMATIC PAYMENT")
        if d.weekday() == 5:
            add(everyday, d, -rng.uniform(110, 165) * (1.22 if recent else 1), "NEW WORLD CITY")
        if d.weekday() == 2 and rng.random() < 0.5:
            add(everyday, d, -rng.uniform(25, 60), "COUNTDOWN METRO")
        if rng.random() < (0.55 if recent else 0.35):
            add(everyday, d, -rng.uniform(24, 48), f"Uber *eats Help.U {d.day:02d}")
        if rng.random() < 0.22:
            add(everyday, d, -rng.uniform(11, 28), f"Uber *trip Help.U {d.day:02d}")
        if d.weekday() < 5 and rng.random() < 0.55:
            add(everyday, d, -5.50, f"Cafe Express {d.day:02d}")
        if d.weekday() >= 4 and rng.random() < 0.35:
            add(everyday, d, -rng.uniform(30, 90), rng.choice(["Sushi Place", "Little Kitchen Cafe", "Burger Joint"]))
        if rng.random() < 0.06:
            add(everyday, d, -rng.uniform(25, 140), rng.choice(["The Warehouse", "Kmart", "Hallensteins"]))
        if rng.random() < 0.02:
            add(everyday, d, -rng.uniform(2, 6), "Foreign Currency Fee")
        if rng.random() < 0.025:
            add(everyday, d, -rng.uniform(15, 80), rng.choice(["Hardware Depot", "Market Stall"]))
        if d.day == 3:
            add(everyday, d, -18.99, f"Netflix.com {d.day:02d}")
        if d.day == 9:
            add(everyday, d, -16.99, f"Spotify {d.day:02d}")
        if d.day == 15:
            add(everyday, d, -4.99, f"Google One {d.day:02d}")
        if d.day == 21:
            add(everyday, d, -rng.uniform(190, 260) if winter else -rng.uniform(120, 170), "Mercury Energy", "BILL PAYMENT")
        if d.day == 25:
            add(everyday, d, -45.00, "One NZ Mobile", "BILL PAYMENT")
        if d.day == 1 and d > start:
            add(savings, d - timedelta(days=1), round(rng.uniform(8, 14), 2), "Credit Interest", "Credit Interest")
        d += timedelta(days=1)

    for i, (acct, posted, cents, payee, desc, code) in enumerate(rows):
        particulars = "WBC Internet" if code == "Transfer" else ""
        conn.execute(
            """INSERT INTO transactions (account_id, posted_date, amount_cents, payee, description,
               particulars, code, dedupe_key, source, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'demo', ?)""",
            (acct, posted, cents, payee, desc, particulars, code, f"demo:{i}",
             json.dumps({"Other Party": payee, "Description": desc})))
    rules = rules_mod.parse_rules(rules_mod.starter_rules() + DEMO_RULES, "demo rules")
    rules_mod.apply_rules(conn, rules)
    transfers.apply(conn, transfers.detect(conn))
    conn.execute("INSERT INTO balances VALUES (?, ?, ?, 'manual')", (everyday, today.isoformat(), 184250))
    conn.execute("INSERT INTO balances VALUES (?, ?, ?, 'manual')", (savings, today.isoformat(), 1265000))
    conn.commit()
    return conn
