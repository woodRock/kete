"""Reports: a dated, local HTML page (or JSON) of findings for a month, year or NZ tax year.

build_report() assembles the model from the detectors (detectors.py) plus summary tables;
render() inlines it with the dashboard's design tokens into report.html. Same guarantees as the
dashboard: computed locally, no network access, owner-only file permissions.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
import tomllib
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path

from .detectors import DETECTORS, Context, Finding, is_spend, run_all

DEFAULT_CONFIG = {
    "spike_ratio": 1.4,  # a category this many times its normal is flagged
    "spike_min": 60.0,  # ...and only if it's at least this many dollars over
    "unusual_multiple": 2.5,  # a charge this many times a merchant's usual amount
    "unusual_min": 30.0,  # ...and at least this many dollars more
    "large_new_merchant": 150.0,  # first payments to a new merchant above this are noted
    "duplicate_min": 15.0,  # identical charges below this aren't flagged (two coffees)
    "uncategorised_share": 10.0,  # percent of spending
    "good_savings_rate": 20.0,  # percent
    # Recurring but never a "subscription" (same default as the dashboard's list).
    "not_subscriptions": ["Rent", "Mortgage", "Rates", "Loan", "Groceries", "Childcare"],
}

__all__ = ["DETECTORS", "DEFAULT_CONFIG", "Finding", "build_report", "generate", "load_config",
           "parse_period", "render"]


def load_config(rules_path: Path | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if rules_path and rules_path.exists():
        try:
            data = tomllib.loads(rules_path.read_text())
        except tomllib.TOMLDecodeError:
            data = {}
        cfg.update({k: v for k, v in (data.get("report") or {}).items() if k in cfg})
    return cfg


# ----------------------------------------------------------------------------- periods


def _month_bounds(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def parse_period(month: str | None = None, year: int | None = None, tax_year: int | None = None,
                 today: date | None = None) -> tuple[str, date, date, str]:
    """-> (kind, start, end, label). Default: the last complete month.

    NZ tax year N runs 1 April N-1 to 31 March N (tax year 2026 = Apr 2025 – Mar 2026)."""
    today = today or date.today()
    if tax_year:
        return "tax-year", date(tax_year - 1, 4, 1), date(tax_year, 3, 31), f"Tax year {tax_year - 1}–{str(tax_year)[2:]}"
    if year:
        return "year", date(year, 1, 1), date(year, 12, 31), str(year)
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must look like 2026-09")
        y, m = map(int, month.split("-"))
    else:
        last = today.replace(day=1) - timedelta(days=1)
        y, m = last.year, last.month
    start, end = _month_bounds(y, m)
    return "month", start, end, start.strftime("%B %Y")


def _previous(kind: str, start: date, end: date) -> tuple[date, date, str]:
    if kind == "month":
        p_end = start - timedelta(days=1)
        p_start, p_end = _month_bounds(p_end.year, p_end.month)
        return p_start, p_end, p_start.strftime("%B %Y")
    p_start, p_end = start.replace(year=start.year - 1), end.replace(year=end.year - 1)
    label = str(p_start.year) if kind == "year" else f"Tax year {p_start.year}–{str(p_end.year)[2:]}"
    return p_start, p_end, label


# ----------------------------------------------------------------------------- model

_DOLLARS = re.compile(r"\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)")


def _dollars_in(text: str) -> float | None:
    m = _DOLLARS.fullmatch((text or "").strip())
    return float(m.group(1).replace(",", "")) if m else None


def _tax_interest(conn: sqlite3.Connection, start: date, end: date) -> dict:
    """Westpac interest rows: Particulars = gross interest, Reference = tax withheld (RWT) when the
    Analysis Code says "Less W/Tax"; the amount is what was paid in."""
    rows = conn.execute(
        """SELECT posted_date, amount_cents, particulars, reference, code FROM transactions
           WHERE amount_cents > 0 AND (description LIKE '%Interest%' OR category = 'Interest')
           AND posted_date BETWEEN ? AND ? ORDER BY posted_date""", (start.isoformat(), end.isoformat())).fetchall()
    gross = rwt = net = 0.0
    for r in rows:
        paid = r["amount_cents"] / 100
        g = _dollars_in(r["particulars"]) or paid
        tax = _dollars_in(r["reference"]) if "w/tax" in (r["code"] or "").lower() else 0.0
        gross, rwt, net = gross + g, rwt + (tax or 0), net + paid
    return {"payments": len(rows), "gross": round(gross, 2), "rwt": round(rwt, 2), "net": round(net, 2)}


def _load_txns(conn: sqlite3.Connection, end: date) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT t.id, t.posted_date AS d, t.amount_cents AS c, t.payee,
                  COALESCE(t.category, 'Uncategorised') AS cat, a.key AS account
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.is_transfer = 0 AND t.transfer_pair_id IS NULL AND t.posted_date <= ?
           ORDER BY t.posted_date, t.id""", (end.isoformat(),))]


def _totals(txns: list[dict], start: date, end: date) -> dict:
    lo, hi = start.isoformat(), end.isoformat()
    period = [t for t in txns if lo <= t["d"] <= hi]
    income = sum(t["c"] for t in period if t["c"] > 0) / 100
    spend = -sum(t["c"] for t in period if is_spend(t)) / 100
    cats: dict[str, float] = defaultdict(float)
    for t in period:
        if is_spend(t):
            cats[t["cat"]] += -t["c"] / 100
    return {"income": round(income, 2), "spend": round(spend, 2), "net": round(income - spend, 2),
            "savings_rate": round((income - spend) / income * 100, 1) if income else None,
            "categories": {k: round(v, 2) for k, v in cats.items()}, "count": len(period)}


def _txn(t: dict) -> dict:
    return {"id": t["id"], "date": t["d"], "account": t["account"], "payee": t["payee"],
            "amount": round(t["c"] / 100, 2), "category": t["cat"]}


def build_report(conn: sqlite3.Connection, kind: str, start: date, end: date, label: str,
                 config: dict | None = None, today: date | None = None, demo: bool = False) -> dict:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    today = today or date.today()
    txns = _load_txns(conn, end)
    ctx = Context(conn=conn, start=start, end=end, today=today, kind=kind, cfg=cfg, txns=txns)
    try:
        dismissed = {r[0] for r in conn.execute("SELECT finding_id FROM dismissals")}
    except sqlite3.OperationalError:  # read-only connection to a database without the table yet
        dismissed = set()
    by_id = {t["id"]: t for t in txns}
    findings = []
    for f in run_all(ctx):
        d = asdict(f)
        d["evidence"] = [_txn(by_id[i]) for i in f.evidence if i in by_id]
        d["dismissed"] = f.id in dismissed
        findings.append(d)

    now = _totals(txns, start, end)
    p_start, p_end, p_label = _previous(kind, start, end)
    if end >= today:  # unfinished period: compare like for like, the same number of days
        p_end = min(p_end, p_start + timedelta(days=ctx.days - 1))
        p_label += " (same days)"
    prev = _totals(txns, p_start, p_end)
    prev_has_data = any(p_start.isoformat() <= t["d"] <= p_end.isoformat() for t in txns)
    first_txn = min((t["d"] for t in txns), default=None)
    prev_partial = bool(prev_has_data and first_txn and first_txn > p_start.isoformat())
    names = sorted(set(now["categories"]) | (set(prev["categories"]) if prev_has_data else set()),
                   key=lambda c: -max(now["categories"].get(c, 0), prev["categories"].get(c, 0)))
    categories = [{
        "name": c, "spend": now["categories"].get(c, 0.0),
        "previous": prev["categories"].get(c, 0.0) if prev_has_data else None,
        "change": round(now["categories"].get(c, 0.0) - prev["categories"].get(c, 0.0), 2) if prev_has_data else None,
    } for c in names]

    monthly = []
    if kind in ("year", "tax-year"):
        m = start
        while m <= end:
            ms, me = _month_bounds(m.year, m.month)
            tot = _totals(txns, ms, me)
            monthly.append({"month": ms.strftime("%Y-%m"), "label": ms.strftime("%b %y"),
                            "income": tot["income"], "spend": tot["spend"], "net": tot["net"],
                            "future": ms > today})
            m = me + timedelta(days=1)

    lo, hi = start.isoformat(), end.isoformat()
    largest = sorted((t for t in txns if lo <= t["d"] <= hi and is_spend(t)), key=lambda t: t["c"])[:10]
    return {
        "version": 1,
        "demo": demo,
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "period": {"kind": kind, "label": label, "start": lo, "end": hi,
                   "complete": end < today, "days": ctx.days},
        "previous": {"label": p_label, "start": p_start.isoformat(), "end": p_end.isoformat(),
                     "has_data": prev_has_data, "partial": prev_partial,
                     **{k: prev[k] for k in ("income", "spend", "net", "savings_rate")}},
        "summary": {k: now[k] for k in ("income", "spend", "net", "savings_rate", "count")},
        "categories": categories,
        "monthly": monthly,
        "largest": [_txn(t) for t in largest],
        "tax": _tax_interest(conn, start, end) if kind in ("year", "tax-year") else None,
        "findings": findings,
        "detectors": [fn.__name__ for fn in DETECTORS],
        "config": cfg,
    }


# ----------------------------------------------------------------------------- render


def _asset(package: str, name: str) -> str:
    return resources.files(package).joinpath("assets", name).read_text()


def render(model: dict) -> str:
    data = json.dumps(model, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = _asset("kete.report", "report.html")
    css = _asset("kete.dashboard", "style.css") + "\n" + _asset("kete.report", "report.css")
    for marker, value in (("/*__STYLE__*/", css), ("/*__SCRIPT__*/", _asset("kete.report", "report.js")),
                          ("__DATA__", data)):
        if marker not in html:
            raise ValueError(f"report.html is missing the {marker} marker")
        html = html.replace(marker, value, 1)
    return html


def default_filename(kind: str, start: date, end: date) -> str:
    if kind == "month":
        return f"{start:%Y-%m}.html"
    if kind == "year":
        return f"{start.year}.html"
    return f"tax-year-{end.year}.html"


def generate(conn: sqlite3.Connection, out: Path, kind: str, start: date, end: date, label: str,
             rules_path: Path | None = None, today: date | None = None, demo: bool = False) -> Path:
    model = build_report(conn, kind, start, end, label, load_config(rules_path), today=today, demo=demo)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(model))
    os.chmod(out, 0o600)
    return out
