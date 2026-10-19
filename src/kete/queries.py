"""Read-only queries shared by the CLI (--json) and the TUI. Money is returned in dollars."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

NOT_TRANSFER = "t.is_transfer = 0 AND t.transfer_pair_id IS NULL"
# Your own part of a transaction: money others owe you on a payment, or paid you back, is excluded.
OWN = "(t.amount_cents + t.shared_cents)"


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


def month_range(month: str) -> tuple[str, str]:
    """'2026-02' -> ('2026-02-01', '2026-02-28'), inclusive."""
    start, end = _month_bounds(month)
    return start, (date.fromisoformat(end) - timedelta(days=1)).isoformat()


def _endpoint(text: str, end: bool) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        date.fromisoformat(text)
        return text
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return month_range(text)[1 if end else 0]
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-12-31" if end else f"{text}-01-01"
    raise ValueError(f"not a date, month or year: {text!r}")


def parse_period(text: str, today: date | None = None) -> tuple[str, str, str]:
    """A period typed by a person -> (from, to, label), inclusive ISO dates.

    2026-03 · 2026 · 2026-01..2026-03 · 2026-01-15..2026-02-14 · since 2026-02-01 ·
    last 90 days · tax 2026 (NZ tax year: 1 Apr 2025 – 31 Mar 2026) · this month · last month"""
    today = today or date.today()
    t = " ".join(text.lower().split())
    if not t:
        raise ValueError("empty period")
    if t in ("this month", "month"):
        start, _ = month_range(current_month())
        return start, today.isoformat(), "this month"
    if t == "last month":
        m = prev_month(current_month())
        return (*month_range(m), m)
    if m := re.fullmatch(r"(?:last )?(\d+) ?(?:d|days?)", t):
        start = today - timedelta(days=int(m.group(1)) - 1)
        return start.isoformat(), today.isoformat(), f"last {m.group(1)} days"
    if m := re.fullmatch(r"(?:tax|tax year|ty) ?(\d{4})", t):
        y = int(m.group(1))
        return f"{y - 1}-04-01", f"{y}-03-31", f"tax year {y - 1}–{str(y)[2:]}"
    if t.startswith("since "):
        return _endpoint(t[6:].strip(), False), today.isoformat(), t
    if ".." in t:
        a, b = (x.strip() for x in t.split("..", 1))
        start, end = _endpoint(a, False), (_endpoint(b, True) if b else today.isoformat())
        if start > end:
            raise ValueError("the period ends before it starts")
        return start, end, t
    return _endpoint(t, False), _endpoint(t, True), t


def year_earlier(date_from: str, date_to: str) -> tuple[str, str]:
    def back(d: str) -> str:
        x = date.fromisoformat(d)
        try:
            return x.replace(year=x.year - 1).isoformat()
        except ValueError:  # 29 Feb
            return x.replace(year=x.year - 1, day=28).isoformat()
    return back(date_from), back(date_to)


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


RECENT_EXPORT_DAYS = 45  # an import whose newest row is this recent was a fresh export


def account_spans(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    """Per account with imported transactions: the dates its data covers.

    covered_until is the newest transaction, or the day a fresh export was imported if later: a
    quiet account you still export isn't stale. Accounts with only hand-entered transactions
    (kete add) are left out, since there's no export to keep up to date."""
    today = today or date.today()
    out = []
    for a in conn.execute(
        """SELECT a.id, a.key, a.opened_on, a.closed_on, MIN(t.posted_date) first, MAX(t.posted_date) last,
                  SUM(t.source != 'manual') exported
           FROM accounts a JOIN transactions t ON t.account_id = a.id GROUP BY a.id ORDER BY a.id"""):
        if not a["exported"]:
            continue
        imports = conn.execute(
            "SELECT substr(imported_at, 1, 10) day, date_to FROM imports WHERE account_id = ? AND date_to IS NOT NULL",
            (a["id"],)).fetchall()
        covered = a["last"]
        for imp in imports:
            if (date.fromisoformat(imp["day"]) - date.fromisoformat(imp["date_to"])).days <= RECENT_EXPORT_DAYS:
                covered = max(covered, min(imp["day"], today.isoformat()))
        out.append({"key": a["key"], "first": a["first"], "last": a["last"], "covered_until": covered,
                    "opened_on": a["opened_on"], "closed_on": a["closed_on"]})
    return out


def month_complete(spans: list[dict], month: str) -> bool:
    """Every account open during the month has data for all of it (a few days' slack at each end,
    since exports and statement dates rarely line up exactly)."""
    ms, me = _month_bounds(month)
    ms_d, me_d = date.fromisoformat(ms), date.fromisoformat(me) - timedelta(days=1)
    for s in spans:
        opened = date.fromisoformat(s["opened_on"]) if s["opened_on"] else None
        closed = date.fromisoformat(s["closed_on"]) if s["closed_on"] else None
        if (opened and opened > me_d) or (closed and closed < ms_d):
            continue  # not open this month: nothing needed from it
        need_from = max(ms_d, opened) if opened else ms_d
        need_to = min(me_d, closed) if closed else me_d
        if not opened and date.fromisoformat(s["first"]) > need_from + timedelta(days=3):
            return False
        if date.fromisoformat(s["covered_until"]) < need_to - timedelta(days=2) and not (closed and closed <= me_d):
            return False
    return True


def accounts(conn: sqlite3.Connection) -> list[dict]:
    spans = {s["key"]: s for s in account_spans(conn)}
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
        covered = spans[a["key"]]["covered_until"] if a["key"] in spans else stats["last"]
        days = (date.today() - date.fromisoformat(covered)).days if covered else None
        out.append({
            "key": a["key"],
            "label": a["label"],
            "number_masked": a["number_masked"],
            "type": a["type"],
            "currency": a["currency"],
            "transactions": stats["n"],
            "first_date": stats["first"],
            "last_date": stats["last"],
            "covered_until": covered,
            "days_since_last": days,
            "opened_on": a["opened_on"],
            "closed_on": a["closed_on"],
            "manual_only": stats["n"] > 0 and a["key"] not in spans,
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
                    t.category_source, t.is_transfer, t.transfer_pair_id, t.source, t.pending,
                    t.shared_cents
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
        d["shared"] = dollars(d.pop("shared_cents"))
        d["is_transfer"] = bool(d["is_transfer"] or d["transfer_pair_id"])
        d["pending"] = bool(d["pending"])
        rows.append(d)
    return rows


def raw_row(conn: sqlite3.Connection, tx_id: int) -> dict:
    r = conn.execute("SELECT raw_json FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return json.loads(r["raw_json"]) if r else {}


def spend(conn: sqlite3.Connection, month: str | None = None, by: str = "cat",
          account: str | None = None, date_from: str | None = None, date_to: str | None = None) -> dict:
    """Spending for a month, or for date_from..date_to inclusive when given."""
    if date_from or date_to:
        month = None
        start = date_from or "0001-01-01"
        end = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat() if date_to else "9999-12-31"
    else:
        month = month or current_month()
        start, end = _month_bounds(month)
    sql = f"""SELECT {OWN} AS amount_cents, t.category, t.payee FROM transactions t
              JOIN accounts a ON a.id = t.account_id
              WHERE t.posted_date >= ? AND t.posted_date < ? AND {OWN} < 0
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
    return {"month": month, "from": start, "to": (date.fromisoformat(end) - timedelta(days=1)).isoformat()
            if end != "9999-12-31" else None, "by": by, "total": dollars(sum(totals.values())), "rows": rows}


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
                       SUM(CASE WHEN {OWN} > 0 THEN {OWN} ELSE 0 END) income,
                       -SUM(CASE WHEN {OWN} < 0 THEN {OWN} ELSE 0 END) spend
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
    """Accounts due a fresh export. Closed accounts and hand-entered ones (kete add) never are."""
    return [a for a in accounts(conn) if not a["closed_on"] and not a["manual_only"]
            and (a["days_since_last"] is None or a["days_since_last"] > days)]


CADENCES = (("weekly", 7, 1), ("fortnightly", 14, 2), ("monthly", 30.4, 4),
            ("quarterly", 91, 7), ("yearly", 365, 15))
# Recurring, but habits rather than bills: a weekly shop or regular takeaways.
HABIT_CATEGORIES = {"groceries", "eating out", "takeaways", "shopping", "transport"}
AMOUNT_TOLERANCE = 0.15  # FX-billed subscriptions wobble a few percent; price changes don't
SEPARATE_RATIO = 1.6  # amount groups this far apart are different products from one merchant
# First words that name a payment platform rather than the merchant ("Google One", "Google Spotify").
_PLATFORM_WORDS = {"google", "apple", "paypal", "sq", "sp", "dd", "pp", "amazon", "microsoft", "the",
                   "uber", "new", "to", "from", "frm", "bill", "direct", "online", "foreign"}
_WORD = re.compile(r"[a-z0-9]+")


def brand_key(payee: str) -> str | None:
    """'Cloudco* Pro', 'Cloudco 14', '8510Cloudco' -> 'cloudco'. None when the first word is too generic."""
    words = _WORD.findall(payee.lower())
    if words and not words[0].isdigit():
        words[0] = re.sub(r"^\d{3,}(?=[a-z]{4})", "", words[0])  # '8510Powerco' (a reference glued on)
    if not words or words[0] in _PLATFORM_WORDS or len(words[0]) < 4 or words[0].isdigit():
        return None
    return words[0]


def _clusters(by_day: dict[str, int]) -> list[list[str]]:
    """Group a merchant's charge days by amount, so two products from one company (a $114
    internet plan and a $20 mobile top-up) are judged separately. Each cluster is in date order."""
    clusters: list[list[str]] = []
    for d in sorted(by_day):
        amt = by_day[d]
        for c in clusters:
            ref = by_day[c[-1]]
            if abs(amt - ref) <= max(AMOUNT_TOLERANCE * ref, 100):
                c.append(d)
                break
        else:
            clusters.append([d])
    return clusters


def _evaluate_run(run: list[str], by_day: dict[str, int], today: date, min_count: int) -> tuple[dict | None, str]:
    gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(run, run[1:])]
    med_gap = median(gaps)
    loose = False
    cadence = next((c for c in CADENCES if abs(med_gap - c[1]) <= c[2]), None)
    if not cadence and len(run) >= 3 and abs(med_gap - 30.4) <= 8:
        # Monthly bills paid by hand land on different days; accept about one charge a month.
        span_months = (date.fromisoformat(run[-1]) - date.fromisoformat(run[0])).days / 30.4
        if abs(span_months - (len(run) - 1)) <= 1:
            cadence, loose = ("monthly", 30.4, 8), True
    if not cadence:
        return None, f"no regular interval (typically {med_gap:.0f} days apart)"
    name, period, tol = cadence
    regular = sum(1 for g in gaps if abs(g - period) <= tol * 2) / len(gaps)
    if regular < 0.6:
        return None, f"{name} at best, but only {regular:.0%} of intervals fit"
    amounts = [by_day[d] for d in run]
    exact = len(set(amounts)) == 1
    if len(run) < min_count and not (len(run) == 2 and exact and name in ("monthly", "quarterly", "yearly")):
        return None, f"only {len(run)} similar charges so far"
    last = date.fromisoformat(run[-1])
    status = "inactive" if (today - last).days > period * 2 + tol else "active"
    typical = median(amounts)
    if len(amounts) >= 2 and abs(amounts[-1] - amounts[-2]) <= amounts[-1] * 0.02 \
            and abs(amounts[-1] - typical) > typical * 0.05:
        typical = amounts[-1]  # the last two charges agree on a new price: that's the price now
    return {
        "cadence": name,
        "typical_amount": dollars(int(typical)),
        "last_amount": dollars(amounts[-1]),
        "count": len(run),
        "first_date": run[0],
        "last_date": run[-1],
        "next_expected": (last + timedelta(days=round(period))).isoformat(),
        "active": status == "active",
        "confidence": "low" if loose or len(run) < 3 else "high",
        "varying_days": loose,
        "monthly_cost": dollars(int(typical * 30.4 / period)),
    }, status


VARIABLE_MIN_CHARGES = 4
VARIABLE_MAX_RATIO = 2.5  # biggest / smallest charge: a power bill in winter vs summer


def _variable_monthly(by_day: dict[str, int], today: date) -> dict | None:
    """A bill whose amount changes with use (power, water): charged about once a month, regularly,
    at least four times, amounts within 2.5x of each other. Everyday shopping is too frequent to pass."""
    days = sorted(by_day)
    if len(days) < VARIABLE_MIN_CHARGES:
        return None
    amounts = [by_day[d] for d in days]
    if min(amounts) <= 0 or max(amounts) / min(amounts) > VARIABLE_MAX_RATIO:
        return None
    gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(days, days[1:])]
    if abs(median(gaps) - 30.4) > 8 or sum(1 for g in gaps if abs(g - 30.4) <= 10) / len(gaps) < 0.8:
        return None
    last = date.fromisoformat(days[-1])
    typical = median(amounts)
    return {
        "cadence": "monthly",
        "typical_amount": dollars(int(typical)),
        "last_amount": dollars(amounts[-1]),
        "count": len(days),
        "first_date": days[0],
        "last_date": days[-1],
        "next_expected": (last + timedelta(days=30)).isoformat(),
        "active": (today - last).days <= 30.4 * 2 + 8,
        "confidence": "low",
        "varying_days": True,
        "varying_amount": True,
        "amount_range": [dollars(min(amounts)), dollars(max(amounts))],
        "monthly_cost": dollars(int(typical)),
    }


def _evaluate(rows: list, today: date, min_count: int) -> tuple[dict | None, str]:
    """Decide whether one merchant's charges are a subscription. Returns (result, reason).

    When a merchant's charges fall into clearly different sizes (a $114 plan and a $20 top-up),
    each size is judged on its own and the best regular one wins (active first, then the biggest
    monthly cost). Otherwise the latest run of similar amounts is judged."""
    by_day: dict[str, int] = {}
    for r in rows:  # one charge per day: a same-day duplicate is a seat or a retry, not a cycle
        by_day[r["posted_date"]] = max(by_day.get(r["posted_date"], 0), -r["amount_cents"])
    if len(by_day) < 2:
        return None, "only one charge"
    clusters = _clusters(by_day)
    meds = sorted(median(by_day[d] for d in c) for c in clusters)
    separated = len(clusters) > 1 and all(b >= a * SEPARATE_RATIO for a, b in zip(meds, meds[1:]))
    if not separated:
        # One product (or amounts that blur together, like weekly shopping): judge the most
        # recent run of similar amounts, so a price rise starts a new run.
        days = sorted(by_day)
        last_amt = by_day[days[-1]]
        run = [days[-1]]
        for d in reversed(days[:-1]):
            if abs(by_day[d] - last_amt) <= max(AMOUNT_TOLERANCE * last_amt, 100):
                run.insert(0, d)
            else:
                break
        clusters = [run]
    clusters = [c for c in clusters if len(c) >= 2]
    if not clusters:
        variable = _variable_monthly(by_day, today)
        if variable:
            return variable, "active" if variable["active"] else "inactive"
        return None, f"amount changes every time (last {dollars(-by_day[max(by_day)])})"
    found, reasons = [], []
    for c in sorted(clusters, key=lambda c: c[-1], reverse=True):
        result, reason = _evaluate_run(c, by_day, today, min_count)
        if result:
            found.append(result)
        else:
            reasons.append(reason)
    if not found:
        variable = _variable_monthly(by_day, today)
        if variable:
            return variable, "active" if variable["active"] else "inactive"
        return None, reasons[0]
    best = max(found, key=lambda r: (r["active"], r["monthly_cost"]))
    return best, "active" if best["active"] else "inactive"


def subscriptions(conn: sqlite3.Connection, min_count: int = 3, today: date | None = None,
                  explain: bool = False) -> list[dict]:
    """Recurring charges: same merchant, similar amount, regular interval.

    `today` is the as-of date: later transactions are ignored, so past reports see what was
    known then. With explain=True, returns near-misses too, each with a `reason`."""
    today = today or date.today()
    groups: dict[str, list] = defaultdict(list)
    names: dict[str, str] = {}
    for r in conn.execute(
        f"""SELECT t.posted_date, t.amount_cents, t.payee, t.category FROM transactions t
            WHERE t.amount_cents < 0 AND t.pending = 0 AND {NOT_TRANSFER} AND t.posted_date <= ?
            ORDER BY t.posted_date""", (today.isoformat(),)
    ):
        k = payee_key(r["payee"])
        groups[k].append(r)
        names[k] = _TRAILING_NUM.sub("", " ".join(r["payee"].split()))  # latest spelling

    out, misses, leftover = [], [], defaultdict(list)
    for k, rows in groups.items():
        result, reason = _evaluate(rows, today, min_count)
        if result:
            out.append({"payee": names[k], "category": rows[-1]["category"], "merged": [], "search": names[k], **result})
            continue
        b = brand_key(rows[-1]["payee"])
        if b:
            leftover[b].append((k, rows))
        misses.append((k, rows, reason))

    # Second pass: the same service billed under several payee spellings.
    merged_keys: set[str] = set()
    for b, parts in leftover.items():
        if len(parts) < 2:
            continue
        rows = sorted((r for _, rs in parts for r in rs), key=lambda r: r["posted_date"])
        result, _ = _evaluate(rows, today, min_count)
        if result:
            keys = [k for k, _ in parts]
            merged_keys.update(keys)
            # Name it by the latest spelling that starts with the brand ("Powerco", not "8510Powerco").
            clean = [k for k in keys if k.startswith(b)] or keys
            out.append({"payee": names[max(clean, key=lambda k: groups[k][-1]["posted_date"])],
                        "category": rows[-1]["category"], "merged": [names[k] for k in keys], "search": b, **result})

    # Bills you named win over detection: drop detected entries they cover, add theirs.
    from . import bills as bills_mod

    named = bills_mod.declared(conn)
    if named:
        out = [s for s in out if not any(bills_mod.matches(b, s["payee"]) or any(bills_mod.matches(b, m) for m in s["merged"])
                                         for b in named)]
        out += [r for b in named if (r := bills_mod.evaluate(conn, b, today, NOT_TRANSFER))]
    bills_mod.apply_prefs(conn, out)
    out.sort(key=lambda s: (not s["active"], -s["monthly_cost"]))
    if not explain:
        return out
    near = []
    for k, rows, reason in misses:
        if k in merged_keys or any(bills_mod.matches(b, names[k]) for b in named):
            continue
        days = {r["posted_date"] for r in rows}
        span = (date.fromisoformat(max(days)) - date.fromisoformat(min(days))).days or 1
        if len(days) < 2 or len(days) / span * 30.4 > 8:  # one-offs and everyday shopping aren't near-misses
            continue
        near.append({"payee": names[k], "category": rows[-1]["category"], "charges": len(rows),
                     "last_date": max(days), "reason": reason})
    near.sort(key=lambda n: n["last_date"], reverse=True)
    return out + [{"rejected": True, **n} for n in near]


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


def months_back(end_month: str, n: int) -> list[str]:
    """The n months ending at end_month, oldest first."""
    keys = [end_month]
    for _ in range(n - 1):
        keys.append(prev_month(keys[-1]))
    return keys[::-1]


def category_months(conn: sqlite3.Connection, months: list[str]) -> dict[str, dict[str, float]]:
    """{category: {yyyy-mm: spend}} for the given months (your own share, transfers excluded)."""
    if not months:
        return {}
    start, _ = _month_bounds(months[0])
    _, end = _month_bounds(months[-1])
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for r in conn.execute(
        f"""SELECT COALESCE(t.category, 'Uncategorised') cat, substr(t.posted_date, 1, 7) m, -SUM({OWN}) c
            FROM transactions t WHERE {OWN} < 0 AND {NOT_TRANSFER} AND t.posted_date >= ? AND t.posted_date < ?
            GROUP BY cat, m""", (start, end)):
        out[r["cat"]][r["m"]] = r["c"] / 100
    return dict(out)


def balance_history(conn: sqlite3.Connection, account_id: int, days: int = 90,
                    today: date | None = None) -> list[float] | None:
    """End-of-day balances for the last `days` days, worked back from the latest known balance."""
    latest = latest_balance(conn, account_id)
    if latest is None:
        return None
    today = today or date.today()
    end = max(today, date.fromisoformat(latest["as_of"]))
    start = end - timedelta(days=days - 1)
    moves = dict(conn.execute(
        "SELECT posted_date, SUM(amount_cents) FROM transactions WHERE account_id = ? AND posted_date > ? "
        "GROUP BY posted_date", (account_id, start.isoformat())).fetchall())
    bal = round(latest["balance"] * 100)
    out = []
    d = end
    while d >= start:
        out.append(bal / 100)
        bal -= moves.get(d.isoformat(), 0)
        d -= timedelta(days=1)
    return out[::-1]


def upcoming(conn: sqlite3.Connection, days: int = 14, today: date | None = None) -> list[dict]:
    """Subscriptions and bills expected in the next `days` days (from their usual cycle)."""
    today = today or date.today()
    horizon = today + timedelta(days=days)
    out = []
    for s in subscriptions(conn, today=today):
        nxt = date.fromisoformat(s["next_expected"])
        grace = 10 if s.get("varying_days") else 3  # bills paid by hand can run a little late
        if s["active"] and today - timedelta(days=grace) <= nxt <= horizon:
            out.append({"payee": s["payee"], "date": s["next_expected"], "amount": s["typical_amount"],
                        "cadence": s["cadence"], "late": nxt < today, "named": bool(s.get("named")),
                        "approx": bool(s.get("varying_days"))})
    return sorted(out, key=lambda u: u["date"])


def recurring_notes(s: dict, today: date | None = None) -> list[str]:
    """Short plain-English notes about a recurring payment, for tables."""
    today = today or date.today()
    notes = []
    if s.get("charged_after_cancel"):
        notes.append("charged after you cancelled!")
    elif s.get("cancelled"):
        notes.append(f"cancelled {s['cancelled_on']}")
    elif not s["active"]:
        notes.append("stopped?")
    elif date.fromisoformat(s["next_expected"]) < today:
        notes.append("due, not seen yet")
    if s.get("named"):
        notes.append("named bill")
    if s.get("varying_amount") and s.get("amount_range"):
        lo, hi = s["amount_range"]
        notes.append(f"varies ${lo:,.0f}–${hi:,.0f}")
    elif s["last_amount"] and s["typical_amount"] and s["last_amount"] > s["typical_amount"] * 1.05:
        notes.append(f"up from ${s['typical_amount']:,.2f}")
    if s.get("original_payee"):
        notes.append(f"shown as {s['original_payee']} by the bank")
    if s.get("varying_days"):
        notes.append("varying days")
    if s.get("confidence") == "low" and not s.get("varying_amount") and not s.get("varying_days"):
        notes.append("new, or a new price")
    return notes
