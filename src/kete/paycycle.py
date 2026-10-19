"""Pay cycles: spending grouped payday to payday, and what's safe to spend until the next one.

The payday is found from your income: the payer whose regular, similar-sized payments add up to
the most. Override it in rules.toml:

    [paycycle]
    payee = "Acme Ltd"        # whose payments are your pay
    save_per_cycle = 300      # set aside each cycle before "safe to spend"
"""

from __future__ import annotations

import sqlite3
import tomllib
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from . import queries

DEFAULT_CONFIG = {"payee": "", "save_per_cycle": 0.0, "min_amount": 200.0}
CADENCES = (("weekly", 7, 2), ("fortnightly", 14, 3), ("four-weekly", 28, 3), ("monthly", 30.4, 4))


def load_config(rules_path: Path | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if rules_path and rules_path.exists():
        try:
            data = tomllib.loads(rules_path.read_text())
        except tomllib.TOMLDecodeError:
            data = {}
        cfg.update({k: v for k, v in (data.get("paycycle") or {}).items() if k in cfg})
    return cfg


def _income(conn: sqlite3.Connection, today: date) -> dict[str, list]:
    groups: dict[str, list] = defaultdict(list)
    for r in conn.execute(
        f"""SELECT t.posted_date, {queries.OWN} AS c, t.payee FROM transactions t
            WHERE {queries.OWN} > 0 AND t.pending = 0 AND {queries.NOT_TRANSFER} AND t.posted_date <= ?
            ORDER BY t.posted_date""", (today.isoformat(),)):
        groups[queries.payee_key(r["payee"])].append(r)
    return groups


def find_payday(conn: sqlite3.Connection, cfg: dict | None = None, today: date | None = None) -> dict | None:
    """-> {payee, cadence, period_days, paydays: [iso dates], typical} or None."""
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    today = today or date.today()
    best = None
    for key, rows in _income(conn, today).items():
        if cfg["payee"] and cfg["payee"].lower() not in key:
            continue
        by_day: dict[str, int] = defaultdict(int)
        for r in rows:
            by_day[r["posted_date"]] += r["c"]
        days = sorted(d for d, c in by_day.items() if c >= cfg["min_amount"] * 100)
        if len(days) < 3:
            continue
        recent = days[-12:]
        gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(recent, recent[1:])]
        g = median(gaps)
        cadence = next((c for c in CADENCES if abs(g - c[1]) <= c[2]), None)
        if not cadence or sum(abs(x - cadence[1]) <= cadence[2] * 2 for x in gaps) / len(gaps) < 0.6:
            continue
        amounts = [by_day[d] for d in recent]
        typical = median(amounts)
        if max(amounts) > typical * 1.6:  # bonuses happen; wildly varying credits aren't pay
            amounts = [a for a in amounts if a <= typical * 1.6]
            typical = median(amounts)
        score = typical * 30.4 / cadence[1]
        if best is None or score > best[0]:
            best = (score, {"payee": rows[-1]["payee"], "cadence": cadence[0], "period_days": cadence[1],
                            "paydays": days, "typical": round(typical / 100, 2)})
    return best[1] if best else None


def cycles(conn: sqlite3.Connection, cfg: dict | None = None, today: date | None = None,
           count: int = 6) -> dict | None:
    """The current cycle (so far) and the previous `count`, newest first."""
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    today = today or date.today()
    pay = find_payday(conn, cfg, today)
    if not pay:
        return None
    days = [date.fromisoformat(d) for d in pay["paydays"]]
    period = pay["period_days"]
    starts = days[-(count + 1):]
    next_pay = days[-1] + timedelta(days=round(period))
    pay_missing = next_pay + timedelta(days=3) < today  # a pay should have arrived; export may be stale
    while next_pay <= today:  # the payday after today, even if the last pay hasn't been exported yet
        next_pay += timedelta(days=round(period))
    out = []
    for i, start in enumerate(starts):
        end = (starts[i + 1] if i + 1 < len(starts) else next_pay) - timedelta(days=1)
        s = queries.spend(conn, date_from=start.isoformat(), date_to=min(end, today).isoformat())
        income = conn.execute(
            f"""SELECT COALESCE(SUM({queries.OWN}), 0) FROM transactions t
                WHERE {queries.OWN} > 0 AND {queries.NOT_TRANSFER} AND t.posted_date BETWEEN ? AND ?""",
            (start.isoformat(), end.isoformat())).fetchone()[0] / 100
        out.append({"start": start.isoformat(), "end": end.isoformat(), "spend": s["total"] or 0.0,
                    "income": income, "current": i == len(starts) - 1, "categories": s["rows"][:5]})
    out.reverse()
    current = out[0]
    past = [c["spend"] for c in out[1:]]
    start = date.fromisoformat(current["start"])
    elapsed = (today - start).days + 1
    days_left = (next_pay - today).days
    # Recurring charges still to come this cycle (subscriptions and bills, by their next date).
    upcoming = [s for s in queries.subscriptions(conn, today=today)
                if s["active"] and today < date.fromisoformat(s["next_expected"]) < next_pay]
    upcoming_total = sum(s["typical_amount"] for s in upcoming)
    pay_amount = pay["typical"]
    safe = pay_amount - current["spend"] - upcoming_total - float(cfg["save_per_cycle"])
    return {
        "payday": pay,
        "next_payday": next_pay.isoformat(),
        "pay_missing": pay_missing,
        "days_left": days_left,
        "elapsed": elapsed,
        "current": current,
        "previous": out[1:],
        "average_spend": round(sum(past) / len(past), 2) if past else None,
        "upcoming": [{"payee": s["payee"], "date": s["next_expected"], "amount": s["typical_amount"]}
                     for s in sorted(upcoming, key=lambda s: s["next_expected"])],
        "safe_to_spend": round(safe, 2),
        "safe_per_day": round(safe / days_left, 2) if days_left > 0 else None,
        "save_per_cycle": float(cfg["save_per_cycle"]),
        "basis": f"typical pay {pay_amount:,.2f} − spent so far − bills due before payday"
                 + (" − savings" if cfg["save_per_cycle"] else ""),
    }
