"""Detectors: small functions that look at the database and report findings.

Each detector receives a Context and returns Findings. A finding must be explainable: it carries
the transactions that justify it (evidence) and a stable id, so a dismissal sticks across runs.
To add one, write a function, decorate it with @detector, and add a test using kete.demo data.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from .. import queries

NOT_SPEND = {"income", "interest", "transfer"}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "good": 3}


@dataclass
class Finding:
    id: str  # stable across runs: detector + what it is about
    detector: str
    severity: str  # high | medium | low | good
    title: str
    body: str
    suggestion: str = ""
    impact_yearly: float | None = None  # dollars a year at stake, when meaningful
    evidence: list[int] = field(default_factory=list)  # transaction ids


@dataclass
class Context:
    conn: sqlite3.Connection
    start: date
    end: date  # inclusive
    today: date
    kind: str  # month | year | tax-year | range
    cfg: dict
    txns: list[dict] = field(default_factory=list)  # every non-transfer transaction up to `end`

    @property
    def complete(self) -> bool:
        return self.end < self.today

    @property
    def days(self) -> int:
        return (min(self.end, self.today) - self.start).days + 1

    def in_period(self, t: dict) -> bool:
        return self.start.isoformat() <= t["d"] <= self.end.isoformat()

    def period_spend(self) -> list[dict]:
        return [t for t in self.txns if self.in_period(t) and is_spend(t)]

    def before(self, d: date, days: int | None = None) -> list[dict]:
        lo = (d - timedelta(days=days)).isoformat() if days else ""
        return [t for t in self.txns if lo <= t["d"] < d.isoformat()]


def is_spend(t: dict) -> bool:
    return t["c"] < 0 and t["cat"].lower() not in NOT_SPEND


def money(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"


def clean(payee: str) -> str:
    return queries._TRAILING_NUM.sub("", " ".join(payee.split()))


DETECTORS: list = []


def detector(fn):
    DETECTORS.append(fn)
    return fn


def run_all(ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    for fn in DETECTORS:
        out.extend(fn(ctx))
    out.sort(key=lambda f: (SEVERITY_ORDER[f.severity], -(f.impact_yearly or 0)))
    return out


# ----------------------------------------------------------------------------- detectors


@detector
def overspend(ctx: Context) -> list[Finding]:
    """Spending more than came in, or a strong savings rate."""
    if not ctx.complete:
        return []
    period = [t for t in ctx.txns if ctx.in_period(t)]
    income = sum(t["c"] for t in period if t["c"] > 0) / 100
    spend = -sum(t["c"] for t in period if is_spend(t)) / 100
    if income <= 0 and spend <= 0:
        return []
    key = f"overspend:{ctx.start}:{ctx.end}"
    if spend > income:
        top = sorted((t for t in period if is_spend(t)), key=lambda t: t["c"])[:5]
        return [Finding(key, "overspend", "high", f"You spent {money(spend - income)} more than came in",
                        f"{money(spend)} went out and {money(income)} came in (transfers between your "
                        "accounts aren't counted).",
                        "Check the biggest items below, and the category changes in this report.",
                        evidence=[t["id"] for t in top])]
    rate = (income - spend) / income * 100
    if rate >= ctx.cfg["good_savings_rate"]:
        return [Finding(f"saved:{ctx.start}:{ctx.end}", "overspend", "good",
                        f"You kept {rate:.0f}% of your income",
                        f"{money(income - spend)} left over from {money(income)}.")]
    return []


@detector
def category_change(ctx: Context) -> list[Finding]:
    """A category well above (or below) your own normal for a period this long."""
    if not ctx.complete or ctx.kind not in ("month", "range"):
        return []
    history_start = ctx.start - timedelta(days=183)
    first = min((t["d"] for t in ctx.txns), default=ctx.start.isoformat())
    if first > (ctx.start - timedelta(days=90)).isoformat():
        return []  # not enough history to know what "normal" is
    hist_days = (ctx.start - max(history_start, date.fromisoformat(first))).days
    normal: dict[str, float] = defaultdict(float)
    for t in ctx.before(ctx.start, 183):
        if is_spend(t):
            normal[t["cat"]] += -t["c"] / 100
    now: dict[str, list] = defaultdict(list)
    for t in ctx.period_spend():
        now[t["cat"]].append(t)
    out = []
    for cat in set(normal) | set(now):
        if cat == "Uncategorised":
            continue
        typical = normal[cat] / hist_days * ctx.days
        spent = -sum(t["c"] for t in now[cat]) / 100
        diff = spent - typical
        if spent > typical * ctx.cfg["spike_ratio"] and diff >= ctx.cfg["spike_min"]:
            top = sorted(now[cat], key=lambda t: t["c"])[:5]
            pct = f" (+{diff / typical * 100:.0f}%)" if typical else ""
            out.append(Finding(f"spike:{cat}:{ctx.start}", "category_change", "medium",
                               f"{cat} was {money(diff)} above normal{pct}",
                               f"{money(spent)} this period against a typical {money(typical)}, "
                               "based on the six months before.",
                               "The biggest items are below. One-offs are fine; a new habit is worth a look.",
                               impact_yearly=None, evidence=[t["id"] for t in top]))
        elif typical >= ctx.cfg["spike_min"] and spent < typical * 0.6 and -diff >= ctx.cfg["spike_min"]:
            out.append(Finding(f"drop:{cat}:{ctx.start}", "category_change", "good",
                               f"{cat} was {money(-diff)} below normal",
                               f"{money(spent)} against a typical {money(typical)}."))
    return out


@detector
def unusual_charge(ctx: Context) -> list[Finding]:
    """A charge far above what this merchant normally costs."""
    out = []
    for t in ctx.period_spend():
        k = queries.payee_key(t["payee"])
        hist = [-h["c"] / 100 for h in ctx.before(date.fromisoformat(t["d"]), 365)
                if h["c"] < 0 and queries.payee_key(h["payee"]) == k]
        if len(hist) < 4:
            continue
        usual = median(hist)
        amount = -t["c"] / 100
        if amount > max(usual * ctx.cfg["unusual_multiple"], usual + ctx.cfg["unusual_min"]):
            out.append(Finding(f"unusual:{t['id']}", "unusual_charge", "medium",
                               f"Unusually large charge at {clean(t['payee'])}",
                               f"{money(amount)} on {t['d']}, against a usual {money(usual)} "
                               f"({len(hist)} earlier charges).",
                               "If you don't recognise it, check it in Westpac One.",
                               evidence=[t["id"]]))
    return out


@detector
def new_merchant(ctx: Context) -> list[Finding]:
    """A large first-ever payment to somewhere new."""
    first = min((t["d"] for t in ctx.txns), default=None)
    if not first or first > (ctx.start - timedelta(days=60)).isoformat():
        return []  # with little history, everything looks new
    seen = {queries.payee_key(t["payee"]) for t in ctx.before(ctx.start)}
    out = []
    for t in ctx.period_spend():
        k = queries.payee_key(t["payee"])
        if k in seen:
            continue
        seen.add(k)
        if -t["c"] / 100 >= ctx.cfg["large_new_merchant"]:
            out.append(Finding(f"new:{t['id']}", "new_merchant", "low",
                               f"First payment to {clean(t['payee'])}",
                               f"{money(-t['c'] / 100)} on {t['d']}; no earlier payments to this merchant.",
                               "Worth a glance if you don't recognise it.", evidence=[t["id"]]))
    return out


@detector
def duplicate_charge(ctx: Context) -> list[Finding]:
    """Same merchant, same amount, within two days."""
    groups: dict[tuple, list] = defaultdict(list)
    for t in ctx.period_spend():
        groups[(queries.payee_key(t["payee"]), t["c"])].append(t)
    out = []
    for (k, cents), rows in groups.items():
        if -cents / 100 < ctx.cfg["duplicate_min"] or len(rows) < 2:
            continue
        rows.sort(key=lambda t: t["d"])
        for a, b in zip(rows, rows[1:]):
            gap = (date.fromisoformat(b["d"]) - date.fromisoformat(a["d"])).days
            if gap <= 2:
                when = "on the same day" if gap == 0 else f"{gap} day{'s' if gap > 1 else ''} apart"
                out.append(Finding(f"dup:{a['id']}:{b['id']}", "duplicate_charge", "medium",
                                   f"Possible double charge at {clean(a['payee'])}",
                                   f"Two charges of {money(-cents / 100)} {when}.",
                                   "If you only bought once, ask the merchant or Westpac to reverse one.",
                                   evidence=[a["id"], b["id"]]))
    return out


@detector
def price_rise(ctx: Context) -> list[Finding]:
    """A regular charge that went up."""
    by_key: dict[str, list] = defaultdict(list)
    for t in ctx.txns:
        if t["c"] < 0 and t["d"] <= ctx.end.isoformat():
            by_key[queries.payee_key(t["payee"])].append(t)
    out = []
    for k, rows in by_key.items():
        rows.sort(key=lambda t: t["d"])
        for i in range(3, len(rows)):
            t = rows[i]
            if not ctx.in_period(t):
                continue
            prev = [-r["c"] for r in rows[max(0, i - 6):i]]
            mid = median(prev)
            if any(abs(c - mid) > mid * 0.02 for c in prev):
                continue  # earlier charges weren't a steady price (a shop, not a subscription)
            gaps = [(date.fromisoformat(b["d"]) - date.fromisoformat(a["d"])).days
                    for a, b in zip(rows[i - 3:i], rows[i - 2:i + 1])]
            cadence = next((c for c in queries.CADENCES if all(abs(g - c[1]) <= c[2] * 2 for g in gaps)), None)
            if not cadence:
                continue
            old, new = mid / 100, -t["c"] / 100
            if new > old * 1.03 and new - old >= 0.5 and new <= old * 2.5:
                per_year = (new - old) * 365 / cadence[1]
                out.append(Finding(f"price:{k}:{t['d']}", "price_rise", "medium",
                                   f"{clean(t['payee'])} went up {(new / old - 1) * 100:.0f}%",
                                   f"{money(old)} → {money(new)} a charge ({cadence[0]}), "
                                   f"about {money(per_year)} more a year.",
                                   "Check whether you still use it, or whether a cheaper plan exists.",
                                   impact_yearly=round(per_year, 2), evidence=[r["id"] for r in rows[i - 1:i + 1]]))
    return out


@detector
def new_subscription(ctx: Context) -> list[Finding]:
    """A recurring charge that started recently."""
    out = []
    # Two charges are needed to see a subscription, so look back ~3 months from the period end.
    recent_from = min(ctx.start, ctx.end - timedelta(days=92)).isoformat()
    first_ever = min((t["d"] for t in ctx.txns), default=ctx.end.isoformat())
    never = {c.lower() for c in ctx.cfg["not_subscriptions"]}
    for s in queries.subscriptions(ctx.conn, today=min(ctx.end, ctx.today)):
        if not s["active"] or s["first_date"] < recent_from or (s["category"] or "").lower() in never:
            continue
        if (date.fromisoformat(s["first_date"]) - date.fromisoformat(first_ever)).days < 60:
            continue  # it only looks new because your data starts around then
        k = queries.payee_key(s.get("original_payee") or s["payee"])
        if s.get("named") or s.get("merged"):
            continue  # several spellings: the first-seen check below can't tell, so stay quiet
        if any(queries.payee_key(t["payee"]) == k for t in ctx.before(date.fromisoformat(s["first_date"]))):
            continue  # an older subscription whose price changed: price_rise covers it
        rows = [t for t in ctx.txns if queries.payee_key(t["payee"]) == k and t["d"] >= s["first_date"]]
        out.append(Finding(f"newsub:{k}:{s['first_date']}", "new_subscription", "low",
                           f"New subscription: {clean(s['payee'])}",
                           f"{s['cadence'].capitalize()} at {money(s['typical_amount'])}, since {s['first_date']}: "
                           f"about {money(s['monthly_cost'] * 12)} a year.",
                           "Free trials often turn into these. Keep it if you meant to.",
                           impact_yearly=round(s["monthly_cost"] * 12, 2), evidence=[t["id"] for t in rows[-3:]]))
    return out


def _charges_of(ctx: Context, s: dict) -> list[dict]:
    """The transactions behind a recurring payment, matched on its payee text."""
    needles = {(s.get("search") or "").lower()} | {m.lower() for m in s.get("merged", [])} \
        | {(s.get("original_payee") or s["payee"]).lower()}
    needles.discard("")
    return [t for t in ctx.txns if t["c"] < 0 and any(n in t["payee"].lower() for n in needles)]


@detector
def stopped_subscription(ctx: Context) -> list[Finding]:
    """Recurring payments that stopped (or that you cancelled) in the period, and any that kept
    charging after you marked them cancelled."""
    out = []
    lo, hi = ctx.start.isoformat(), ctx.end.isoformat()
    for s in queries.subscriptions(ctx.conn, today=min(ctx.end, ctx.today)):
        name = clean(s["payee"])
        rows = _charges_of(ctx, s)
        key = (s.get("search") or s["payee"]).lower()
        if s.get("charged_after_cancel"):
            after = [t for t in rows if t["d"] > (s.get("cancelled_on") or "") and t["d"] >= lo]
            if after:
                total = -sum(t["c"] for t in after) / 100
                out.append(Finding(f"cancelcharged:{key}:{after[0]['d']}", "stopped_subscription", "high",
                                   f"{name} charged you after you cancelled",
                                   f"You marked it cancelled on {s['cancelled_on']}, but it has charged "
                                   f"{money(total)} since.",
                                   "Check the cancellation went through, and ask for a refund.",
                                   impact_yearly=round(s["monthly_cost"] * 12, 2), evidence=[t["id"] for t in after]))
            continue
        if s["active"] or (not s.get("cancelled") and (s["category"] or "").lower() in queries.HABIT_CATEGORIES):
            continue  # a weekly shop that changed supermarket isn't a cancelled subscription
        if s.get("cancelled"):
            when = s["cancelled_on"]
        else:
            # The day it stopped counting as active (about two cycles without a payment, the same
            # rule as kete subs and the TUI); the report for that period mentions it.
            cadence = next((c for c in queries.CADENCES if c[0] == s["cadence"]), ("", 30.4, 8))
            grace = 8 if s.get("varying_days") else cadence[2]
            when = (date.fromisoformat(s["last_date"]) + timedelta(days=int(cadence[1] * 2 + grace) + 1)).isoformat()
        if not (lo <= when <= hi):
            continue
        how = "You cancelled it" if s.get("cancelled") else "No payment since"
        out.append(Finding(f"stopped:{key}:{s['last_date']}", "stopped_subscription", "good",
                           f"{name} has stopped",
                           f"{how} {s['last_date'] if not s.get('cancelled') else 'on ' + s['cancelled_on']}: "
                           f"about {money(s['monthly_cost'])} a month ({money(s['monthly_cost'] * 12)} a year) "
                           "you're no longer paying." + ("" if s.get("cancelled") else " If you didn't cancel it, "
                                                          "check a card hasn't expired."),
                           impact_yearly=round(s["monthly_cost"] * 12, 2),
                           evidence=[t["id"] for t in rows if t["d"] <= s["last_date"]][-2:]))
    return out


FEE_WORDS = re.compile(r"\bfee\b|\bfees\b|overdrawn|dishonou?r|penalty|late payment", re.I)


@detector
def fees(ctx: Context) -> list[Finding]:
    """Bank and card fees, most of which can be avoided."""
    rows = [t for t in ctx.txns if ctx.in_period(t) and t["c"] < 0
            and (t["cat"].lower() == "fees" or FEE_WORDS.search(t["payee"]))]
    if not rows:
        return []
    total = -sum(t["c"] for t in rows) / 100
    fx = [t for t in rows if "foreign" in t["payee"].lower() or "currency" in t["payee"].lower()]
    tip = ("Most are foreign-currency fees: a card without FX fees for overseas and online purchases "
           "would avoid them." if len(fx) * 2 >= len(rows)
           else "Ask Westpac whether a different account type would waive these.")
    per_year = total / ctx.days * 365
    return [Finding(f"fees:{ctx.start}:{ctx.end}", "fees", "medium" if total >= 5 else "low",
                    f"{money(total)} in fees", f"{len(rows)} fee charge{'s' if len(rows) != 1 else ''} this period.",
                    tip, impact_yearly=round(per_year, 2), evidence=[t["id"] for t in rows[:10]])]


@detector
def data_quality(ctx: Context) -> list[Finding]:
    """Things that make the report less trustworthy: stale or partial data, uncategorised spending."""
    out = []
    lo, hi = ctx.start.isoformat(), ctx.end.isoformat()
    for s in queries.account_spans(ctx.conn, ctx.today):
        if (s["opened_on"] and s["opened_on"] > hi) or (s["closed_on"] and s["closed_on"] < lo):
            continue  # not open in this period
        gaps = []
        if not s["opened_on"] and s["first"] > lo:
            gaps.append(f"starts {s['first']}")
        if ctx.complete and not s["closed_on"] and s["covered_until"] < hi:
            gaps.append(f"ends {s['covered_until']}")
        if gaps:
            out.append(Finding(f"coverage:{s['key']}:{ctx.start}:{ctx.end}", "data_quality", "low",
                               f"{s['key']} doesn't cover the whole period",
                               f"Its data {' and '.join(gaps)}, so totals for this period are understated.",
                               "Export the missing dates from Westpac One and run kete import."))
    spend = ctx.period_spend()
    total = -sum(t["c"] for t in spend) or 1
    uncat = [t for t in spend if t["cat"] == "Uncategorised"]
    share = -sum(t["c"] for t in uncat) / total * 100
    if share >= ctx.cfg["uncategorised_share"]:
        top = sorted(uncat, key=lambda t: t["c"])[:8]
        out.append(Finding(f"uncat:{ctx.start}:{ctx.end}", "data_quality", "low",
                           f"{share:.0f}% of spending is uncategorised",
                           f"{len(uncat)} transactions without a category make the category numbers less useful.",
                           "Categorise them in kete tui (tab 4); a rule from one payee fixes all of its charges.",
                           evidence=[t["id"] for t in top]))
    return out
