"""Turns the database into the dashboard's data model: one JSON-able dict.

Everything here runs locally. The model is the contract between analysis (this file) and
presentation (assets/app.js); see AGENTS.md for its shape. Money values are in dollars.
"""

from __future__ import annotations

import calendar
import sqlite3
import tomllib
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

from .. import queries

# Overridable from a [dashboard] table in rules.toml.
DEFAULT_CONFIG = {
    # Categories treated as "wants": candidates for a percentage cut.
    "discretionary": ["Takeaways", "Eating out", "Entertainment", "Shopping", "Transport",
                      "Alcohol", "Hobbies", "Gifts"],
    # Necessities: never suggested as subscriptions to cancel, and left out of the day-to-day
    # patterns (calendar, weekday, merchant share) so rent day doesn't drown everything out.
    "fixed_costs": ["Rent", "Mortgage", "Utilities", "Insurance", "Loan", "Groceries", "Health",
                    "Childcare", "Rates"],
    # Recurring but never something you'd "cancel": kept out of the subscription list. Other
    # fixed costs (phone, power, insurance) are listed as bills, unticked.
    "not_subscriptions": ["Rent", "Mortgage", "Rates", "Loan", "Groceries", "Childcare", "Donations"],
    "default_cut": 0.25,  # suggested cut for discretionary categories
    "interest_rate": 0.03,  # annual rate used in the savings projection
    "savings_goal": 0,  # dollars; 0 = no goal line
    "recent_months": 3,  # "recent" window for trends and suggestions
    "baseline_months": 6,  # the window "recent" is compared against
    "display_months": 12,  # months shown in the charts
    "small_purchase_max": 20.0,  # a "small" purchase, for the frequent-small pattern
    "small_purchase_per_month": 6,  # how often counts as "frequent"
}

MAX_SERIES = 7  # categorical palette slots; the rest fold into "Other"
NOT_SPEND = {"income", "interest", "transfer"}


def load_config(rules_path: Path | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if rules_path and rules_path.exists():
        try:
            data = tomllib.loads(rules_path.read_text())
        except tomllib.TOMLDecodeError:
            data = {}
        for k, v in (data.get("dashboard") or {}).items():
            if k in cfg:
                cfg[k] = v
    return cfg


# ----------------------------------------------------------------------------- helpers


def _month(d: str | date) -> str:
    return (d if isinstance(d, str) else d.isoformat())[:7]


def _month_add(m: str, n: int) -> str:
    y, mo = map(int, m.split("-"))
    idx = y * 12 + (mo - 1) + n
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def _month_range(start: str, end: str) -> list[str]:
    out = []
    m = start
    while m <= end:
        out.append(m)
        m = _month_add(m, 1)
    return out


def _last_day(m: str) -> date:
    y, mo = map(int, m.split("-"))
    return date(y, mo, calendar.monthrange(y, mo)[1])


def _r(x: float) -> float:
    return round(x, 2)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _money(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"


def _clean_payee(payee: str) -> str:
    return queries._TRAILING_NUM.sub("", " ".join(payee.split()))


# ----------------------------------------------------------------------------- model


def build_model(conn: sqlite3.Connection, config: dict | None = None,
                today: date | None = None, demo: bool = False) -> dict:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    today = today or date.today()
    discretionary = {c.lower() for c in cfg["discretionary"]}

    txns = [dict(r) for r in conn.execute(
        """SELECT t.posted_date AS d, t.amount_cents + t.shared_cents AS c, COALESCE(t.category, 'Uncategorised') AS cat,
                  t.payee, t.pending, a.key AS account
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.is_transfer = 0 AND t.transfer_pair_id IS NULL AND t.posted_date <= ?
             AND t.amount_cents + t.shared_cents != 0
           ORDER BY t.posted_date""", (today.isoformat(),))]
    spend = [t for t in txns if t["c"] < 0 and t["cat"].lower() not in NOT_SPEND]
    fixed = {c.lower() for c in cfg["fixed_costs"]}
    day_to_day = [t for t in spend if t["cat"].lower() not in fixed]

    # --- coverage: months where every account has data (an account added later would
    # otherwise make earlier months look cheap).
    spans = conn.execute(
        """SELECT a.key, MIN(posted_date) first, MAX(posted_date) last FROM transactions t
           JOIN accounts a ON a.id = t.account_id GROUP BY a.id""").fetchall()
    this_month = _month(today)
    if spans:
        first = max(date.fromisoformat(s["first"]) for s in spans)
        last = min(date.fromisoformat(s["last"]) for s in spans)
        cov_start = _month(first) if first.day <= 3 else _month_add(_month(first), 1)
        cov_end = _month(last) if last >= _last_day(_month(last)) - timedelta(days=2) else _month_add(_month(last), -1)
        cov_end = min(cov_end, _month_add(this_month, -1))
    else:
        cov_start, cov_end = this_month, _month_add(this_month, -1)
    complete = _month_range(cov_start, cov_end) if cov_start <= cov_end else []
    recent = complete[-cfg["recent_months"]:]
    baseline = complete[-(cfg["recent_months"] + cfg["baseline_months"]):-cfg["recent_months"]] \
        if len(complete) > cfg["recent_months"] else []
    avg_window = recent + baseline

    display = _month_range(_month_add(this_month, -(cfg["display_months"] - 1)), this_month)

    # --- per month and category
    month_cat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    month_income: dict[str, float] = defaultdict(float)
    month_spend: dict[str, float] = defaultdict(float)
    for t in txns:
        m = _month(t["d"])
        if t["c"] > 0:
            month_income[m] += t["c"] / 100
        elif t["cat"].lower() not in NOT_SPEND:
            month_spend[m] += -t["c"] / 100
            month_cat[m][t["cat"]] += -t["c"] / 100

    # Series: the biggest categories over the display window keep a fixed colour slot.
    cat_totals = Counter()
    for m in display:
        for c, v in month_cat[m].items():
            cat_totals[c] += v
    ranked = [c for c, _ in cat_totals.most_common()]
    series = ranked[:MAX_SERIES]
    if len(ranked) > MAX_SERIES:
        series.append("Other")

    def folded(m: str) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for c, v in month_cat[m].items():
            out[c if c in series else "Other"] += v
        return {k: _r(v) for k, v in out.items()}

    monthly = []
    for m in display:
        status = "partial" if m == this_month else ("complete" if m in complete else "incomplete")
        monthly.append({
            "month": m,
            "label": datetime.strptime(m, "%Y-%m").strftime("%b %y"),
            "status": status,
            "income": _r(month_income[m]),
            "spend": _r(month_spend[m]),
            "net": _r(month_income[m] - month_spend[m]),
            "by_series": folded(m),
            "by_category": {c: _r(v) for c, v in sorted(month_cat[m].items(), key=lambda kv: -kv[1])},
        })

    # Every month with data (up to 3 years), for any-period breakdowns and year-on-year comparisons.
    first_month = min(month_cat) if month_cat else this_month
    history_months = _month_range(max(first_month, _month_add(this_month, -35)), this_month)
    months_all = [{
        "month": m,
        "status": "partial" if m == this_month else ("complete" if m in complete else "incomplete"),
        "spend": _r(month_spend[m]),
        "by_category": {c: _r(v) for c, v in month_cat[m].items()},
    } for m in history_months]

    # --- subscriptions (active), used by categories and opportunities
    never = {c.lower() for c in cfg["not_subscriptions"]}
    subs = [s for s in queries.subscriptions(conn, today=today)
            if s["active"] and (s["category"] or "").lower() not in never]
    for s in subs:
        s["bill"] = (s["category"] or "").lower() in fixed
    sub_keys = {queries.payee_key(s["payee"]) for s in subs}
    subs_by_cat: dict[str, float] = defaultdict(float)
    for s in subs:
        subs_by_cat[s["category"] or "Uncategorised"] += s["monthly_cost"]

    # --- categories
    recent_set, baseline_set = set(recent), set(baseline)
    cat_recent_txns: dict[str, list] = defaultdict(list)
    for t in spend:
        if _month(t["d"]) in recent_set:
            cat_recent_txns[t["cat"]].append(t)

    categories = []
    for c in ranked:
        r_avg = _avg([month_cat[m].get(c, 0) for m in recent]) if recent else 0
        b_avg = _avg([month_cat[m].get(c, 0) for m in baseline]) if baseline else None
        change = (r_avg - b_avg) / b_avg if b_avg else None
        merchants = Counter()
        for t in cat_recent_txns[c]:
            merchants[_clean_payee(t["payee"])] += -t["c"] / 100
        categories.append({
            "name": c,
            "series": c if c in series else "Other",
            "total": _r(cat_totals[c]),
            "monthly": [_r(month_cat[m].get(c, 0)) for m in display],
            "recent_avg": _r(r_avg),
            "baseline_avg": None if b_avg is None else _r(b_avg),
            "change_pct": None if change is None else round(change * 100, 1),
            "discretionary": c.lower() in discretionary,
            "recent_count": len(cat_recent_txns[c]),
            "top_merchants": [{"name": n, "total": _r(v)} for n, v in merchants.most_common(3)],
        })

    # --- merchants (recent window)
    merch: dict[str, dict] = {}
    for t in spend:
        if _month(t["d"]) not in recent_set:
            continue
        k = queries.payee_key(t["payee"])
        m = merch.setdefault(k, {"name": _clean_payee(t["payee"]), "total": 0.0, "count": 0,
                                 "category": t["cat"], "amounts": []})
        m["total"] += -t["c"] / 100
        m["count"] += 1
        m["amounts"].append(-t["c"] / 100)
    n_recent = max(len(recent), 1)
    merchants = []
    for k, m in sorted(merch.items(), key=lambda kv: -kv[1]["total"]):
        merchants.append({
            "name": m["name"], "category": m["category"], "total": _r(m["total"]),
            "count": m["count"], "per_month": round(m["count"] / n_recent, 1),
            "monthly_avg": _r(m["total"] / n_recent), "typical": _r(median(m["amounts"])),
            "subscription": k in sub_keys,
        })
    flexible = [m for m in merchants if m["category"].lower() not in fixed]
    frequent_small = [m for m in merchants if not m["subscription"]
                      and m["per_month"] >= cfg["small_purchase_per_month"]
                      and m["typical"] <= cfg["small_purchase_max"]]

    # --- weekday pattern over complete covered months
    window_days = [d for m in avg_window for d in _days_in(m)] or _days_in(this_month, until=today)
    day_set = {d.isoformat() for d in window_days}
    wd_total = [0.0] * 7
    for t in day_to_day:
        if t["d"] in day_set:
            wd_total[date.fromisoformat(t["d"]).weekday()] += -t["c"] / 100
    wd_count = Counter(d.weekday() for d in window_days)
    weekday = [_r(wd_total[i] / wd_count[i]) if wd_count[i] else 0 for i in range(7)]

    # --- daily spend for the heatmap (26 weeks ending today, starting on a Monday)
    start = today - timedelta(days=7 * 26 - 1)
    start -= timedelta(days=start.weekday())
    daily_map: dict[str, float] = defaultdict(float)
    for t in day_to_day:
        if t["d"] >= start.isoformat():
            daily_map[t["d"]] += -t["c"] / 100
    daily = []
    d = start
    while d <= today:
        daily.append({"date": d.isoformat(), "spend": _r(daily_map.get(d.isoformat(), 0))})
        d += timedelta(days=1)

    # --- headline numbers
    avg_spend = _avg([month_spend[m] for m in avg_window])
    avg_income = _avg([month_income[m] for m in avg_window])
    avg_net = avg_income - avg_spend
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    so_far = month_spend[this_month]
    pace = so_far / today.day * days_in_month if today.day else 0
    last_complete = complete[-1] if complete else None

    balances = [queries.latest_balance(conn, r["id"]) for r in conn.execute("SELECT id FROM accounts")]
    start_balance = _r(sum(b["balance"] for b in balances)) if balances and all(balances) else None

    kpis = {
        "avg_monthly_spend": _r(avg_spend),
        "avg_monthly_income": _r(avg_income),
        "avg_monthly_net": _r(avg_net),
        "savings_rate": round(avg_net / avg_income * 100, 1) if avg_income else None,
        "this_month_spend": _r(so_far),
        "this_month_pace": _r(pace),
        "this_month_day": today.day,
        "this_month_days": days_in_month,
        "last_month": last_complete,
        "last_month_spend": _r(month_spend[last_complete]) if last_complete else None,
        "spend_trend": [_r(month_spend[m]) for m in display],
        "subscriptions_monthly": _r(sum(s["monthly_cost"] for s in subs if not s["bill"])),
    }

    opportunities = _opportunities(categories, subs, subs_by_cat, cfg, len(recent))
    insights = _insights(categories, flexible, frequent_small, subs, weekday, kpis, len(recent), today)

    stale = [s["key"] for s in spans if (today - date.fromisoformat(s["last"])).days > 30]
    uncat = next((c for c in categories if c["name"] == "Uncategorised"), None)
    recent_total = sum(c["recent_avg"] for c in categories) or 1

    return {
        "version": 1,
        "demo": demo,
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "currency": "NZD",
        "config": {k: cfg[k] for k in ("default_cut", "interest_rate", "savings_goal",
                                       "recent_months", "baseline_months", "fixed_costs")},
        "windows": {"recent": recent, "baseline": baseline, "complete": complete,
                    "coverage_start": cov_start},
        "series": series,
        "kpis": kpis,
        "monthly": monthly,
        "months_all": months_all,
        "categories": categories,
        "merchants": merchants[:15],
        "frequent_small": frequent_small[:5],
        "weekday": weekday,
        "daily": daily,
        "subscriptions": [{
            "payee": s["payee"], "cadence": s["cadence"], "typical": s["typical_amount"],
            "monthly": s["monthly_cost"], "annual": _r(s["monthly_cost"] * 12),
            "category": s["category"], "last_date": s["last_date"], "bill": s["bill"],
            "confidence": s["confidence"], "merged": s["merged"],
        } for s in subs],
        "opportunities": opportunities,
        "insights": insights,
        "projection": {"monthly_net": _r(avg_net), "start_balance": start_balance},
        "data_quality": {
            "months_complete": len(complete),
            "uncategorised_share": round((uncat["recent_avg"] if uncat else 0) / recent_total * 100, 1),
            "stale_accounts": stale,
            "accounts": [{"key": s["key"], "first": s["first"], "last": s["last"]} for s in spans],
        },
    }


def _days_in(m: str, until: date | None = None) -> list[date]:
    y, mo = map(int, m.split("-"))
    last = calendar.monthrange(y, mo)[1]
    days = [date(y, mo, d) for d in range(1, last + 1)]
    return [d for d in days if until is None or d <= until]


def _opportunities(categories, subs, subs_by_cat, cfg, n_recent) -> list[dict]:
    """Explainable, rule-based suggestions. JS multiplies base_monthly by the chosen cut."""
    out = []
    if not n_recent:
        return out
    months = f"{n_recent} month{'s' if n_recent != 1 else ''}"
    for c in categories:
        name = c["name"]
        if c["discretionary"]:
            base = max(c["recent_avg"] - subs_by_cat.get(name, 0), 0)
            if base < 20:
                continue
            per_month = c["recent_count"] / n_recent
            tops = ", ".join(f"{m['name']} ({_money(m['total'] / n_recent)}/mo)" for m in c["top_merchants"])
            trend = ""
            if c["change_pct"] is not None and abs(c["change_pct"]) >= 15:
                word = "up" if c["change_pct"] > 0 else "down"
                trend = f" That's {word} {abs(c['change_pct']):.0f}% on the {cfg['baseline_months']} months before."
            out.append({
                "id": f"cut:{name}", "kind": "cut", "group": "categories", "category": name,
                "title": f"Spend less on {name.lower()}",
                "detail": (f"You averaged {_money(c['recent_avg'])}/month on {name.lower()} over the last "
                           f"{months}, about {per_month:.0f} purchases a month.{trend}"),
                "evidence": f"Most of it: {tops}." if tops else "",
                "base_monthly": _r(base), "cut": cfg["default_cut"], "adjustable": True, "enabled": True,
            })
        elif (name.lower() not in NOT_SPEND and name != "Uncategorised"
              and c["baseline_avg"] and c["recent_avg"] > c["baseline_avg"] * 1.2
              and c["recent_avg"] - c["baseline_avg"] >= 25):
            diff = c["recent_avg"] - c["baseline_avg"]
            out.append({
                "id": f"restore:{name}", "kind": "restore", "group": "categories", "category": name,
                "title": f"Bring {name.lower()} back to normal",
                "detail": (f"{name} rose from {_money(c['baseline_avg'])} to {_money(c['recent_avg'])} a month "
                           f"(+{c['change_pct']:.0f}%). Returning to your earlier level saves the difference."),
                "evidence": ", ".join(f"{m['name']} ({_money(m['total'] / n_recent)}/mo)" for m in c["top_merchants"]),
                "base_monthly": _r(diff), "cut": 1.0, "adjustable": True, "enabled": False,
            })
    fees = next((c for c in categories if c["name"].lower() == "fees" and c["recent_avg"] > 0), None)
    if fees:
        out.append({
            "id": "avoid:fees", "kind": "avoid", "group": "categories", "category": fees["name"],
            "title": "Avoid bank fees",
            "detail": f"Fees cost {_money(fees['recent_avg'])}/month recently. Most are avoidable "
                      "(foreign-currency card fees, overdrawn or account fees).",
            "evidence": "", "base_monthly": fees["recent_avg"], "cut": 1.0, "adjustable": False, "enabled": True,
        })
    for s in subs:
        extra = []
        if s["merged"]:
            extra.append("Billed as " + ", ".join(s["merged"]) + ".")
        if s["confidence"] == "low":
            extra.append("Only two charges so far.")
        out.append({
            "id": f"cancel:{queries.payee_key(s['payee'])}", "kind": "cancel", "group": "subscriptions",
            "category": s["category"], "title": _clean_payee(s["payee"]), "bill": s["bill"],
            "detail": f"{s['cadence'].capitalize()}, about {_money(s['typical_amount'])} each time"
                      + (". A bill: worth checking for a cheaper plan." if s["bill"] else "."),
            "evidence": " ".join([f"Last charged {s['last_date']}."] + extra),
            "base_monthly": s["monthly_cost"], "cut": 1.0, "adjustable": False, "enabled": False,
        })
    out.sort(key=lambda o: (o["group"] != "categories", -o["base_monthly"] * o["cut"]))
    return out


def _insights(categories, flexible, frequent_small, subs, weekday, kpis, n_recent, today):
    """Short plain-language observations. tone: good | warn | info."""
    out = []
    rate = kpis["savings_rate"]
    if rate is not None:
        if rate < 0:
            out.append({"tone": "warn", "title": "Spending more than you earn",
                        "body": f"Over your recent months you spent {_money(-kpis['avg_monthly_net'])}/month "
                                "more than came in."})
        else:
            tone = "good" if rate >= 20 else "info"
            out.append({"tone": tone, "title": f"You keep {rate:.0f}% of your income",
                        "body": f"On average {_money(kpis['avg_monthly_income'])} comes in and "
                                f"{_money(kpis['avg_monthly_spend'])} goes out each month. "
                                + ("20% or more is a strong savings rate." if rate >= 20
                                   else "A common target is 20%.")})
    if kpis["avg_monthly_spend"] and today.day >= 5:
        diff = kpis["this_month_pace"] / kpis["avg_monthly_spend"] - 1
        if abs(diff) >= 0.1:
            out.append({"tone": "warn" if diff > 0 else "good",
                        "title": f"On pace for {_money(kpis['this_month_pace'])} this month",
                        "body": f"That's {abs(diff) * 100:.0f}% {'above' if diff > 0 else 'below'} your "
                                f"usual {_money(kpis['avg_monthly_spend'])}, based on "
                                f"{_money(kpis['this_month_spend'])} spent in the first {today.day} days."})
    movers = [c for c in categories if c["change_pct"] is not None and c["name"].lower() not in NOT_SPEND
              and abs(c["recent_avg"] - (c["baseline_avg"] or 0)) >= 20]
    if movers:
        up = max(movers, key=lambda c: c["recent_avg"] - c["baseline_avg"])
        if up["recent_avg"] > up["baseline_avg"]:
            out.append({"tone": "warn", "title": f"{up['name']} is up {up['change_pct']:.0f}%",
                        "body": f"{_money(up['baseline_avg'])} → {_money(up['recent_avg'])} a month, "
                                f"comparing your last {n_recent} months with the months before."})
        down = min(movers, key=lambda c: c["recent_avg"] - c["baseline_avg"])
        if down["recent_avg"] < down["baseline_avg"]:
            out.append({"tone": "good", "title": f"{down['name']} is down {abs(down['change_pct']):.0f}%",
                        "body": f"{_money(down['baseline_avg'])} → {_money(down['recent_avg'])} a month."})
    if frequent_small:
        f = frequent_small[0]
        out.append({"tone": "info", "title": "Small purchases add up",
                    "body": f"{f['name']}: about {f['per_month']:.0f} purchases a month at "
                            f"{_money(f['typical'])}, {_money(f['monthly_avg'])}/month or "
                            f"{_money(f['monthly_avg'] * 12)} a year."})
    subs = [s for s in subs if not s["bill"]]
    if subs:
        total = sum(s["monthly_cost"] for s in subs)
        out.append({"tone": "info", "title": f"{len(subs)} active subscription{'s' if len(subs) != 1 else ''}",
                    "body": f"About {_money(total)}/month, {_money(total * 12)} a year. "
                            "Review them under Where to cut."})
    if any(weekday):
        names = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]
        hi = max(range(7), key=lambda i: weekday[i])
        avg = sum(weekday) / 7
        if avg and weekday[hi] > avg * 1.3:
            out.append({"tone": "info", "title": f"{names[hi]} cost the most",
                        "body": f"{_money(weekday[hi])} on an average {names[hi][:-1]} versus "
                                f"{_money(avg)} across the week. Dates are when the bank posted the "
                                "transaction, often a day after the purchase."})
    if len(flexible) >= 8:
        share = sum(m["total"] for m in flexible[:5]) / (sum(m["total"] for m in flexible) or 1) * 100
        out.append({"tone": "info", "title": f"Five places take {share:.0f}% of day-to-day spending",
                    "body": ", ".join(m["name"] for m in flexible[:5])
                            + ". Day-to-day means everything except rent, bills and groceries."})
    return out
