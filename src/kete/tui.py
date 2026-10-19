"""Textual dashboard: kete tui."""

from __future__ import annotations

import json
import re
from datetime import datetime

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, Sparkline, Static,
                             TabbedContent, TabPane)

from rich.markup import escape as cell_escape
from textual.markup import escape

from . import db as dbmod
from . import history, paycycle, queries, rules as rules_mod, shares as shares_mod
from .paths import get_paths
from .tui_charts import ColumnChart, CompareBars, compact, spark

HIDDEN = "•••"


def money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def short_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %-d %b")


class AskCategory(ModalScreen[str | None]):
    """One line of input. With choices, Tab completes from them."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    AskCategory { align: center middle; }
    AskCategory > Vertical { width: 72; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    AskCategory .hint { color: $text-muted; }
    """

    def __init__(self, prompt: str, choices: list[str], placeholder: str = "Category (Tab completes)",
                 hint: str = "", value: str = "") -> None:
        super().__init__()
        self.prompt, self.choices, self.placeholder, self.hint, self.value = prompt, choices, placeholder, hint, value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            yield Input(value=self.value, placeholder=self.placeholder,
                        suggester=SuggestFromList(self.choices, case_sensitive=False) if self.choices else None)
            if self.hint:
                yield Label(self.hint, classes="hint")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen[str | None]):
    """A question with a few one-key answers: [(key, label)]. Returns the key."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 72; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    Confirm Horizontal { height: auto; margin-top: 1; }
    Confirm Button { margin-right: 1; }
    """

    def __init__(self, question: str, answers: list[tuple[str, str]]) -> None:
        super().__init__()
        self.question, self.answers = question, answers

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.question)
            with Horizontal():
                for i, (key, label) in enumerate(self.answers):
                    yield Button(f"{label} ({key})", id=f"answer-{key}", variant="primary" if i == 0 else "default")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
        elif any(event.key == k for k, _ in self.answers):
            self.dismiss(event.key)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id.removeprefix("answer-"))


class RawRow(ModalScreen[None]):
    BINDINGS = [("escape,o,q", "close", "Close")]
    DEFAULT_CSS = """
    RawRow { align: center middle; }
    RawRow > VerticalScroll { width: 80; height: auto; max-height: 80%; padding: 1 2; border: thick $accent; background: $surface; }
    """

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(json.dumps(self.data, indent=2, ensure_ascii=False), markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


TAB_TABLES = {"transactions": "#tx-table", "categories": "#cat-table",
              "uncategorised": "#uncat-table", "imports": "#imports-table", "recurring": "#recurring-table"}

TX_COLUMNS = ("Date", "Account", "Amount", "Payee", "Type", "Particulars", "Code", "Reference", "Category")
TX_TABS = ("transactions", "uncategorised")
SHOPPING_CATEGORIES = queries.HABIT_CATEGORIES
# Footer keys that only make sense on some tabs (keeps the footer short).
TAB_ACTIONS = {
    "categorise": TX_TABS, "toggle_transfer": TX_TABS, "new_rule": TX_TABS, "open_raw": TX_TABS,
    "share": TX_TABS, "bill": TX_TABS, "mark": TX_TABS, "mark_all": TX_TABS, "clear_marks": TX_TABS,
    "month": ("categories",), "period": ("categories",), "toggle_cashflow": ("overview",),
    "rename_recurring": ("recurring",), "cancel_recurring": ("recurring",),
}


class KeteApp(App):
    TITLE = "kete"
    CSS = """
    #overview-body { padding: 0 1; }
    .section { margin-top: 1; text-style: bold; }
    .row { height: auto; }
    .panel { width: 1fr; height: auto; margin-right: 2; }
    .panel:last-of-type { margin-right: 0; }
    .wide { width: 2fr; }
    .muted { color: $text-muted; }
    #accounts-table { height: auto; max-height: 12; }
    #cashflow-table { height: auto; max-height: 16; }
    #cat-bar { height: 1; margin: 0 0 1 0; }
    #cat-bar Button { min-width: 5; }
    #cat-header { width: 1fr; padding: 0 1; }
    #tx-filter { margin: 0 0 1 0; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("escape,backspace", "back", "Back"),
        Binding("1", "tab('overview')", "Overview", show=False),
        Binding("2", "tab('transactions')", "Transactions", show=False),
        Binding("3", "tab('categories')", "Categories", show=False),
        Binding("4", "tab('uncategorised')", "Uncategorised", show=False),
        Binding("5", "tab('imports')", "Imports", show=False),
        Binding("6", "tab('recurring')", "Recurring", show=False),
        Binding("c", "categorise", "Set category"),
        Binding("space", "mark", "Select", show=False),
        Binding("a", "mark_all", "Select all", show=False),
        Binding("x", "clear_marks", "Clear selection", show=False),
        Binding("t", "toggle_transfer", "Transfer"),
        Binding("n", "new_rule", "Rule from payee"),
        Binding("s", "share", "Shared"),
        Binding("b", "bill", "Track as bill"),
        Binding("e", "rename_recurring", "Rename"),
        Binding("k", "cancel_recurring", "Cancelled"),
        Binding("o", "open_raw", "Raw row", show=False),
        Binding("u", "undo", "Undo"),
        Binding("h", "toggle_private", "Hide amounts"),
        Binding("v", "toggle_cashflow", "Table view"),
        Binding("d", "dashboard", "Dashboard"),
        Binding("p", "report", "Report"),
        Binding("left_square_bracket", "month(-1)", "Prev month"),
        Binding("right_square_bracket", "month(1)", "Next month"),
        Binding("g", "period", "Period"),
    ]

    def __init__(self, private: bool = False) -> None:
        super().__init__()
        self.paths = get_paths()
        self.conn = dbmod.connect(self.paths.db)
        self.cat_month = queries.current_month()
        self.cat_range: tuple[str, str, str] | None = None  # (from, to, label) from the g key
        self.drilled = False  # Transactions tab is showing a drill-down from Categories or Recurring
        self.drilled_row = 0
        self.drilled_from = "categories"
        self.private = private
        self.marked: set[int] = set()
        self.tx_date_col: dict[str, object] = {}

    # ------------------------------------------------------------------ privacy-aware formatting

    def m(self, v: float | None) -> str:
        return HIDDEN if self.private and v is not None else money(v)

    def who(self, text: str) -> str:
        """Payee-like text for Static/Label markup: hidden in private mode, always escaped."""
        return HIDDEN if self.private and text else escape(text)

    def _hide_money(self, text: str) -> str:
        return re.sub(r"\$[\d,.]+", HIDDEN, text) if self.private else text

    def cell(self, text: str) -> str:
        """The same, for DataTable cells (rich markup)."""
        return HIDDEN if self.private and text else cell_escape(text)

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="overview"):
            with TabPane("1 Overview", id="overview"):
                with VerticalScroll(id="overview-body"):
                    with Horizontal(classes="row"):
                        with Vertical(classes="panel wide"):
                            yield Label("Accounts", classes="section")
                            yield DataTable(id="accounts-table", cursor_type="none", zebra_stripes=True)
                        with Vertical(classes="panel", id="cycle-panel"):
                            yield Label("Pay cycle", classes="section")
                            yield Static(id="cycle")
                    with Horizontal(classes="row"):
                        with Vertical(classes="panel"):
                            yield Static(id="month-compare", classes="section")
                            yield ColumnChart(id="spend-chart")
                        with Vertical(classes="panel"):
                            yield Label("This month by category, against a typical month", classes="section")
                            yield CompareBars(id="cat-bars")
                    with Horizontal(classes="row"):
                        with Vertical(classes="panel"):
                            yield Label("Coming up, next 14 days", classes="section")
                            yield Static(id="upcoming")
                        with Vertical(classes="panel", id="owed-panel"):
                            yield Label("Owed to you", classes="section")
                            yield Static(id="owed")
                    yield DataTable(id="cashflow-table", cursor_type="none", zebra_stripes=True)
            with TabPane("2 Transactions", id="transactions"):
                yield Input(id="tx-filter",
                            placeholder="Filter: text, account:everyday, cat:Groceries, month:2026-10, "
                                        "from:2026-01-01, to:…, uncat")
                yield DataTable(id="tx-table", cursor_type="row", zebra_stripes=True)
            with TabPane("3 Categories", id="categories"):
                with Horizontal(id="cat-bar"):
                    yield Button("‹ Prev", id="prev-month", compact=True)
                    yield Static(id="cat-header")
                    yield Button("Next ›", id="next-month", compact=True)
                yield DataTable(id="cat-table", cursor_type="row", zebra_stripes=True)
            with TabPane("4 Uncategorised", id="uncategorised"):
                yield Static("c set category · space select (a all, x clear) · n rule from payee · "
                             "s shared · t transfer · o raw row", id="uncat-help")
                yield DataTable(id="uncat-table", cursor_type="row", zebra_stripes=True)
            with TabPane("5 Import log", id="imports"):
                yield DataTable(id="imports-table", cursor_type="row", zebra_stripes=True)
            with TabPane("6 Recurring", id="recurring"):
                yield Static(id="recurring-header")
                yield DataTable(id="recurring-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#accounts-table", DataTable).add_columns(
            "Account", "Number", "Newest", "Age", "Balance", "Last 90 days")
        ct = self.query_one("#cashflow-table", DataTable)
        ct.add_columns("Month", "Income", "Spend", "Net")
        ct.display = False
        self.tx_date_col["tx-table"] = self.query_one("#tx-table", DataTable).add_columns(*TX_COLUMNS)[0]
        self.tx_date_col["uncat-table"] = self.query_one("#uncat-table", DataTable).add_columns(*TX_COLUMNS[:-1])[0]
        self.query_one("#cat-table", DataTable).add_columns(
            "Category", "Spend", "Count", "Share", "12 months", "A year earlier", "Change")
        self.query_one("#imports-table", DataTable).add_columns(
            "Imported", "File", "Format", "Account", "From", "To", "New", "Dup", "Warnings")
        self.query_one("#recurring-table", DataTable).add_columns(
            "Name", "Kind", "How often", "Amount", "≈ a month", "Share", "Last paid", "Next", "Notes")
        self._update_title()
        self.action_refresh()

    def _update_title(self) -> None:
        parts = []
        if self.private:
            parts.append("amounts hidden (h)")
        if self.marked:
            parts.append(f"{len(self.marked)} selected · c categorise · x clear")
        self.sub_title = " · ".join(parts)

    # ------------------------------------------------------------------ data

    def action_refresh(self) -> None:
        self.load_overview()
        self.load_transactions()
        self.load_categories()
        self.load_uncategorised()
        self.load_imports()
        self.load_recurring()

    def load_overview(self) -> None:
        t = self.query_one("#accounts-table", DataTable)
        t.clear()
        ids = {r["key"]: r["id"] for r in self.conn.execute("SELECT id, key FROM accounts")}
        for a in queries.accounts(self.conn):
            age = a["days_since_last"]
            if a["closed_on"]:
                age_txt = "[dim]closed[/]"
            elif a["manual_only"]:
                age_txt = "[dim]by hand[/]"
            else:
                age_txt = "—" if age is None else (f"[red]{age}d stale[/]" if age > 30 else f"{age}d")
            hist = queries.balance_history(self.conn, ids[a["key"]], 90)
            trend = f"[dodger_blue2]{spark(hist, 24)}[/]" if hist else "[dim]set a balance to see this[/]"
            t.add_row(cell_escape(a["label"]), a["number_masked"] or "", a["last_date"] or "—", age_txt,
                      self.m(a["balance"]), trend)

        this_m = queries.current_month()
        now = queries.spend(self.conn, this_m)["total"] or 0
        prev = queries.spend(self.conn, queries.prev_month(this_m))["total"] or 0
        delta = f" ({(now - prev) / prev:+.0%})" if prev else ""
        self.query_one("#month-compare", Static).update(
            f"Spending by month · this month {self.m(now)} vs {self.m(prev)} last month{delta if not self.private else ''}")
        flow = queries.cashflow(self.conn, 12)
        complete = [m["spend"] for m in flow[:-1] if m["spend"]]
        avg = sum(complete) / len(complete) if complete else 0
        items = []
        for m in flow:
            label = datetime.strptime(m["month"], "%Y-%m").strftime("%b")
            over = avg and m["spend"] > avg * 1.15
            style = "$text-muted" if m["month"] == this_m else ("$warning" if over else "$primary")
            items.append((label, m["spend"], style))
        note = "" if self.private else f"avg {compact(avg)}/month · orange 15%+ above avg · grey so far"
        self.query_one("#spend-chart", ColumnChart).set_data(items, note, self.private)

        ct = self.query_one("#cashflow-table", DataTable)
        ct.clear()
        for m in reversed(flow):
            net = self.m(m["net"])
            ct.add_row(m["month"], self.m(m["income"]), self.m(m["spend"]),
                       f"[red]{net}[/]" if m["net"] < 0 else f"[green]{net}[/]")

        months = queries.months_back(this_m, 7)
        by_cat = queries.category_months(self.conn, months)
        base = months[:-1]
        rows = []
        for cat, per in by_cat.items():
            cur = per.get(this_m, 0.0)
            seen = [per.get(mo, 0.0) for mo in base]
            typical = sum(seen) / len(base) if any(seen) else None
            if cur or (typical and typical > 20):
                rows.append((cat, cur, typical))
        rows.sort(key=lambda r: -max(r[1], r[2] or 0))
        self.query_one("#cat-bars", CompareBars).set_data(
            rows[:7], "" if self.private else "bar: this month so far · ┃ typical month (6-month average)", self.private)

        self._load_cycle()
        up = queries.upcoming(self.conn, 14)
        lines = [f"{'≈' if u['approx'] else ' '}{short_date(u['date'])}  {self.who(u['payee'])}  {self.m(-u['amount'])}"
                 + ("  [dim](due, not seen yet)[/]" if u["late"] else "") for u in up[:6]]
        if any(u["approx"] for u in up[:6]):
            lines.append("[dim]≈ paid on varying days · b on a payment tracks a bill[/]")
        if len(up) > 6:
            lines.append(f"[dim]+{len(up) - 6} more · kete subs[/]")
        self.query_one("#upcoming", Static).update("\n".join(lines) or "[dim]No regular charges due[/]")
        owed = shares_mod.balances(self.conn)
        self.query_one("#owed-panel").display = bool(owed)
        self.query_one("#owed", Static).update("\n".join(
            f"{self.who(o['person'])}  {self.m(o['balance'])}" for o in owed[:5]))

    def _load_cycle(self) -> None:
        panel = self.query_one("#cycle", Static)
        try:
            c = paycycle.cycles(self.conn, paycycle.load_config(self.paths.rules))
        except Exception:  # never let a forecast break the overview
            c = None
        if c is None:
            panel.update("[dim]No regular pay found yet.\nSet [paycycle] payee in rules.toml.[/]")
            return
        cur = c["current"]
        days_total = c["elapsed"] + c["days_left"] - 1
        width = 24
        filled = round(min(c["elapsed"] / max(days_total, 1), 1) * width)
        safe = c["safe_to_spend"]
        colour = "$success" if safe >= 0 else "$error"
        lines = [f"Day {c['elapsed']} of {days_total} · payday {short_date(c['next_payday'])}",
                 f"[$primary]{'█' * filled}[/][$panel]{'█' * (width - filled)}[/]"]
        if c["average_spend"]:
            used = cur["spend"] / c["average_spend"] if c["average_spend"] else 0
            spent_w = round(min(used, 1) * width)
            lines += [f"Spent {self.m(cur['spend'])} of a typical {self.m(c['average_spend'])}",
                      f"[{'$warning' if used > c['elapsed'] / max(days_total, 1) + 0.1 else '$primary'}]"
                      f"{'█' * spent_w}[/][$panel]{'█' * (width - spent_w)}[/]"]
        else:
            lines.append(f"Spent {self.m(cur['spend'])} so far")
        per_day = f" · {self.m(c['safe_per_day'])}/day" if c["safe_per_day"] is not None else ""
        lines.append(f"[b {colour}]Safe to spend {self.m(safe)}[/]{per_day}")
        if c["upcoming"]:
            lines.append(f"[dim]after {len(c['upcoming'])} bills due before payday[/]")
        if c["pay_missing"]:
            lines.append("[$warning]A pay looks overdue: export may be stale[/]")
        panel.update("\n".join(lines))

    def _parse_filter(self, text: str) -> dict:
        args: dict = {"limit": 1000}
        words = []
        for tok in text.split():
            key, _, val = tok.partition(":")
            if key == "account" and val:
                args["account"] = val
            elif key in ("cat", "category") and val:
                args["category"] = val.replace("_", " ")
            elif key == "from" and val:
                args["date_from"] = val
            elif key == "to" and val:
                args["date_to"] = val
            elif key == "month" and val:
                start, end = queries._month_bounds(val)
                args["date_from"], args["date_to"] = start, end[:8] + "00"
            elif tok in ("uncat", "uncategorised"):
                args["uncategorised"] = True
            else:
                words.append(tok)
        if words:
            args["search"] = " ".join(words)
        return args

    def _fill_tx(self, table: DataTable, rows: list[dict], with_category: bool = True) -> None:
        cursor = table.cursor_row
        table.clear()
        for r in rows:
            amt = self.m(r["amount"])
            payee = self.cell(r["payee"]) + (" [dim](pending)[/]" if r["pending"] else "")
            if r["shared"]:
                payee += f" [dim](shared {self.m(abs(r['shared']))})[/]"
            cells = [self._date_cell(r["id"], r["date"]), r["account"],
                     f"[green]{amt}[/]" if r["amount"] > 0 else amt, payee,
                     cell_escape(r["description"]), self.cell(r["particulars"]), self.cell(r["code"]),
                     self.cell(r["reference"])]
            if with_category:
                cat = cell_escape(r["category"] or "")
                cells.append(f"[dim]↔ {cat}[/]" if r["is_transfer"] else cat)
            table.add_row(*cells, key=str(r["id"]))
        if rows:
            table.move_cursor(row=min(cursor, len(rows) - 1))

    def _date_cell(self, tx_id: int, iso: str) -> str:
        return f"[b dodger_blue2]● {iso}[/]" if tx_id in self.marked else f"  {iso}"

    def load_transactions(self) -> None:
        text = self.query_one("#tx-filter", Input).value
        try:
            rows = queries.transactions(self.conn, **self._parse_filter(text))
        except ValueError:
            self.notify("Bad filter (dates are yyyy-mm-dd, months yyyy-mm)", severity="error")
            return
        self._fill_tx(self.query_one("#tx-table", DataTable), rows)

    def load_uncategorised(self) -> None:
        rows = queries.transactions(self.conn, uncategorised=True)
        self._fill_tx(self.query_one("#uncat-table", DataTable), rows, with_category=False)
        pane = self.query_one(TabbedContent).get_tab("uncategorised")
        pane.label = f"4 Uncategorised ({len(rows)})"

    def _period(self) -> tuple[str, str, str]:
        if self.cat_range:
            return self.cat_range
        start, end = queries.month_range(self.cat_month)
        return start, end, datetime.strptime(self.cat_month, "%Y-%m").strftime("%B %Y")

    def load_categories(self) -> None:
        start, end, label = self._period()
        data = queries.spend(self.conn, date_from=start, date_to=end)
        p_from, p_to = queries.year_earlier(start, end)
        prev = queries.spend(self.conn, date_from=p_from, date_to=p_to)
        has_prev = bool(self.conn.execute("SELECT 1 FROM transactions WHERE posted_date <= ? LIMIT 1",
                                          (p_from,)).fetchone())
        prev_txt = f" · a year earlier {self.m(prev['total'])}" if has_prev else ""
        hint = "\\[ ] month · g any period · Enter shows transactions"
        self.query_one("#cat-header", Static).update(
            f"[b]{escape(label)}[/]  total {self.m(data['total'])}{prev_txt}   [dim]{hint}[/]")
        trend_months = queries.months_back(end[:7], 12)
        by_cat = queries.category_months(self.conn, trend_months)
        before = {r["name"]: r["total"] for r in prev["rows"]}
        t = self.query_one("#cat-table", DataTable)
        t.clear()
        total = data["total"] or 1
        for r in data["rows"]:
            trend = spark([by_cat.get(r["name"], {}).get(mo, 0.0) for mo in trend_months])
            was = before.get(r["name"], 0.0)
            change = ""
            if has_prev and not self.private:
                diff = r["total"] - was
                change = (f"[yellow]+{money(diff)}[/]" if diff > 0 else f"[green]{money(diff)}[/]") if diff else "—"
            t.add_row(cell_escape(r["name"]), self.m(r["total"]), str(r["count"]), f"{r['total'] / total:.0%}",
                      f"[dodger_blue2]{trend}[/]", self.m(was) if has_prev else "—", change, key=r["name"])

    def load_imports(self) -> None:
        t = self.query_one("#imports-table", DataTable)
        t.clear()
        for r in queries.import_log(self.conn):
            t.add_row(r["imported_at"].replace("T", " "), cell_escape(r["filename"]), r["format"], r["account"] or "—",
                      r["date_from"] or "", r["date_to"] or "", str(r["rows_new"]), str(r["rows_dup"]),
                      f"[yellow]{cell_escape('; '.join(r['warnings']))}[/]" if r["warnings"] else "")

    def _bill_categories(self) -> set[str]:
        from .dashboard.model import load_config

        try:
            cfg = load_config(self.paths.rules)
        except Exception:
            from .dashboard.model import DEFAULT_CONFIG as cfg
        return {c.lower() for c in cfg["fixed_costs"]} | {c.lower() for c in cfg["not_subscriptions"]}

    def load_recurring(self) -> None:
        """Bills and subscriptions: what leaves your accounts every month, biggest first."""
        subs = queries.subscriptions(self.conn)
        self.recurring = {str(i): s for i, s in enumerate(subs)}
        bills_cats = self._bill_categories()
        active = [s for s in subs if s["active"]]
        total = sum(s["monthly_cost"] for s in active) or 0.0
        def kind(s: dict) -> str:
            cat = (s["category"] or "").lower()
            if cat in SHOPPING_CATEGORIES:
                return "regular spend"  # a weekly shop is recurring, but not a bill
            return "bill" if cat in bills_cats else "subscription"

        bills_total = sum(s["monthly_cost"] for s in active if kind(s) == "bill")
        subs_total = sum(s["monthly_cost"] for s in active if kind(s) == "subscription")
        self.query_one("#recurring-header", Static).update(
            f"[b]≈ {self.m(total)} a month[/] on {len(active)} recurring payments ({self.m(total * 12)} a year) · "
            f"bills {self.m(bills_total)} · subscriptions {self.m(subs_total)}"
            + (f" · regular spending {self.m(total - bills_total - subs_total)}" if total - bills_total - subs_total > 0.005 else "")
            + "\n"
            "[dim]Enter shows the payments · e rename · k mark cancelled · missing one? b on a payment "
            "in Transactions · kete subs --explain lists near misses[/]")
        t = self.query_one("#recurring-table", DataTable)
        cursor = t.cursor_row
        t.clear()
        top = max((s["monthly_cost"] for s in active), default=0) or 1
        for i, s in sorted(self.recurring.items(), key=lambda kv: (not kv[1]["active"], -kv[1]["monthly_cost"])):
            nxt = s["next_expected"] if s["active"] else "—"
            share = "" if not s["active"] else f"[dodger_blue2]{'█' * max(1, round(s['monthly_cost'] / top * 10))}[/]"
            cells = [self.cell(s["payee"]), kind(s), s["cadence"], self.m(-s["last_amount"]),
                     self.m(s["monthly_cost"]), share, s["last_date"],
                     ("≈ " if s.get("varying_days") else "") + nxt,
                     cell_escape(self._hide_money(", ".join(queries.recurring_notes(s))))]
            if s.get("charged_after_cancel"):
                cells = [f"[b red]{c}[/]" for c in cells]
            elif not s["active"]:
                cells = [f"[dim]{c}[/]" for c in cells]
            t.add_row(*cells, key=i)
        if t.row_count:
            t.move_cursor(row=min(cursor, t.row_count - 1))

    # ------------------------------------------------------------------ events

    @on(DataTable.RowSelected, "#recurring-table")
    def recurring_drill(self, event: DataTable.RowSelected) -> None:
        s = self.recurring.get(event.row_key.value)
        if not s:
            return
        self.drilled_row, self.drilled_from = event.cursor_row, "recurring"
        self.query_one("#tx-filter", Input).value = s.get("search") or s["payee"]
        self.action_tab("transactions")
        self.drilled = True
        self.refresh_bindings()

    @on(Input.Changed, "#tx-filter")
    def filter_changed(self) -> None:
        self.load_transactions()

    @on(DataTable.RowSelected, "#cat-table")
    def drill_down(self, event: DataTable.RowSelected) -> None:
        name = event.row_key.value
        what = "uncat" if name == "Uncategorised" else f"cat:{name.replace(' ', '_')}"
        if self.cat_range:
            f = f"from:{self.cat_range[0]} to:{self.cat_range[1]} {what}"
        else:
            f = f"month:{self.cat_month} {what}"
        self.drilled_row, self.drilled_from = event.cursor_row, "categories"
        self.query_one("#tx-filter", Input).value = f
        self.action_tab("transactions")
        self.drilled = True
        self.refresh_bindings()

    @on(TabbedContent.TabActivated)
    def tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if self.drilled and event.pane.id != "transactions":
            self.drilled = False
            self.query_one("#tx-filter", Input).value = ""
        # Clicking a tab leaves focus on the tab strip (or a hidden filter box, which would
        # swallow keys like [ and ]); put it on the tab's table instead.
        target = TAB_TABLES.get(event.pane.id)
        if target:
            self.query_one(target).focus()
        self.refresh_bindings()

    @on(Button.Pressed, "#prev-month")
    def prev_month(self) -> None:
        self.action_month(-1)

    @on(Button.Pressed, "#next-month")
    def next_month(self) -> None:
        self.action_month(1)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "back":
            return self.drilled  # only shown in the footer after a drill-down
        tabs = TAB_ACTIONS.get(action)
        if tabs is not None:  # only on the tabs where it does something, so the footer stays short
            return self.query_one(TabbedContent).active in tabs
        return True

    def action_back(self) -> None:
        """Return from a drill-down to the tab it came from, on the same row."""
        if not self.drilled:
            return
        self.action_tab(self.drilled_from)  # tab_changed clears the filter
        table = self.query_one(TAB_TABLES[self.drilled_from], DataTable)
        if table.row_count:
            table.move_cursor(row=min(self.drilled_row, table.row_count - 1))

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        target = TAB_TABLES.get(tab)
        if target:
            self.query_one(target).focus()

    def action_month(self, step: int) -> None:
        if self.query_one(TabbedContent).active != "categories":
            return
        if self.cat_range:  # leave a custom period from its last month
            self.cat_month = self.cat_range[1][:7]
            self.cat_range = None
            step = 0
        m = self.cat_month
        if step < 0:
            m = queries.prev_month(m)
        elif step > 0:
            y, mo = map(int, m.split("-"))
            m = f"{y + (mo == 12)}-{mo % 12 + 1:02d}"
        self.cat_month = m
        self.load_categories()

    def action_period(self) -> None:
        def done(text: str | None) -> None:
            if not text:
                return
            try:
                start, end, label = queries.parse_period(text)
            except ValueError as e:
                self.notify(str(e), severity="error")
                return
            self.cat_range = (start, end, label)
            self.load_categories()

        self.push_screen(AskCategory(
            "Show categories for which period?", [], placeholder="e.g. 2026 · 2026-01..2026-03 · since 2026-02-01",
            hint="Also: last 90 days · tax 2026 · last month. Compared with the same dates a year earlier."), done)

    def action_toggle_cashflow(self) -> None:
        ct = self.query_one("#cashflow-table", DataTable)
        ct.display = not ct.display
        self.query_one("#spend-chart").display = not ct.display

    def action_toggle_private(self) -> None:
        self.private = not self.private
        self._update_title()
        self.action_refresh()

    # ------------------------------------------------------------------ selection

    def _tx_table(self) -> DataTable | None:
        table = self.focused
        if isinstance(table, DataTable) and table.id in ("tx-table", "uncat-table"):
            return table
        return None

    def _current_id(self, table: DataTable) -> int | None:
        if table.row_count == 0:
            return None
        return int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def _redraw_marks(self, table: DataTable, ids: list[int]) -> None:
        dates = dict(self.conn.execute(
            f"SELECT id, posted_date FROM transactions WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()) if ids else {}
        for other in ("tx-table", "uncat-table"):
            t = self.query_one(f"#{other}", DataTable)
            for i in ids:
                if str(i) in t.rows:
                    t.update_cell(str(i), self.tx_date_col[other], self._date_cell(i, dates[i]))
        self._update_title()

    def action_mark(self) -> None:
        table = self._tx_table()
        if table is None:
            return
        tx_id = self._current_id(table)
        if tx_id is None:
            return
        self.marked.symmetric_difference_update({tx_id})
        self._redraw_marks(table, [tx_id])
        if table.cursor_row < table.row_count - 1:
            table.move_cursor(row=table.cursor_row + 1)

    def action_mark_all(self) -> None:
        table = self._tx_table()
        if table is None:
            return
        ids = [int(k.value) for k in table.rows]
        self.marked.update(ids)
        self._redraw_marks(table, ids)
        self.notify(f"{len(self.marked)} selected · c categorises them all · x clears")

    def action_clear_marks(self) -> None:
        ids = list(self.marked)
        self.marked.clear()
        table = self._tx_table()
        if table is not None:
            self._redraw_marks(table, ids)
        self._update_title()

    # ------------------------------------------------------------------ edits

    def _selected_tx(self) -> dict | None:
        table = self._tx_table()
        if table is None:
            self.notify("Select a transaction first (Transactions or Uncategorised tab)")
            return None
        key = self._current_id(table)
        if key is None:
            return None
        row = self.conn.execute(
            "SELECT id, payee, amount_cents, category, is_transfer, transfer_pair_id FROM transactions WHERE id = ?",
            (key,)).fetchone()
        return dict(row) if row else None

    def action_dashboard(self) -> None:
        import webbrowser

        from . import dashboard as dash

        path = dash.generate(self.conn, self.paths.home / "dashboard.html", self.paths.rules)
        webbrowser.open(path.resolve().as_uri())
        self.notify(f"Dashboard opened: {path}")

    def action_report(self) -> None:
        """Last complete month's report (`kete report --year` etc. for other periods)."""
        import webbrowser

        from . import report as rep

        kind, start, end, label = rep.parse_period()
        path = rep.generate(self.conn, self.paths.home / "reports" / rep.default_filename(kind, start, end),
                            kind, start, end, label, self.paths.rules)
        webbrowser.open(path.resolve().as_uri())
        self.notify(f"Report opened: {path}")

    def action_open_raw(self) -> None:
        tx = self._selected_tx()
        if tx:
            self.push_screen(RawRow(queries.raw_row(self.conn, tx["id"])))

    def _rules(self) -> list:
        try:
            return rules_mod.load_rules(self.paths.rules)
        except rules_mod.RulesError:
            return []

    def _categories(self) -> list[str]:
        return rules_mod.known_categories(self.conn, self._rules())

    def action_categorise(self) -> None:
        if self.query_one(TabbedContent).active not in TX_TABS:
            return
        if self.marked:
            ids = sorted(self.marked)
            prompt = f"Category for {len(ids)} selected transactions"
        else:
            tx = self._selected_tx()
            if not tx:
                return
            ids, prompt = [tx["id"]], f"Category for {self.who(tx['payee'])}"

        def done(cat: str | None) -> None:
            if not cat:
                return
            cat = rules_mod.canonical_category(cat, self._categories())
            with history.tracked(self.conn, "categorise", f"{len(ids)} → {cat}"):
                rules_mod.set_category(self.conn, ids, cat)
            self.marked.clear()
            self._update_title()
            self.action_refresh()
            self._offer_rule(ids)

        self.push_screen(AskCategory(prompt, self._categories()), done)

    def _offer_rule(self, ids: list[int]) -> None:
        """After categorising by hand: if a payee is always put in the same category, offer a rule."""
        marks = ",".join("?" * len(ids))
        keys = {rules_mod.rule_payee(r[0]).lower()
                for r in self.conn.execute(f"SELECT payee FROM transactions WHERE id IN ({marks})", ids)}
        suggestions = rules_mod.suggest_rules(self.conn, self._rules(), payee_keys=keys)
        if not suggestions:
            return
        sug = suggestions[0]

        def answered(key: str | None) -> None:
            if key == "y":
                with history.tracked(self.conn, "rule", f"Rule: {sug['payee']} → {sug['category']}", self.paths.rules):
                    rules_mod.append_rule(self.paths.rules, payee=sug["payee"], category=sug["category"],
                                          before=sug["before"])
                    result = rules_mod.apply_rules(self.conn, rules_mod.load_rules(self.paths.rules))
                self.notify(f"Rule added; {result['unmatched']} still uncategorised")
                self.action_refresh()
            elif key == "v":
                rules_mod.decline_suggestion(self.conn, sug["payee"])

        self.push_screen(Confirm(
            f"You've put “{self.who(sug['payee'])}” in {escape(sug['category'])} {sug['count']} times. "
            "Make it a rule, so future ones are categorised automatically?",
            [("y", "Add rule"), ("n", "Not now"), ("v", "Never for this payee")]), answered)

    def action_toggle_transfer(self) -> None:
        """Mark as a transfer between your own accounts, or unmark (e.g. a payment to someone
        else's account that the own-account rule caught). Applies to all selected rows, if any."""
        tx = self._selected_tx()
        if not tx:
            return
        if tx["transfer_pair_id"]:
            self.notify("Paired transfer; undo with: kete transfers unpair " + str(tx["id"]))
            return
        ids = sorted(self.marked) or [tx["id"]]
        unmark = bool(tx["is_transfer"])
        marks = ",".join("?" * len(ids))
        with history.tracked(self.conn, "transfer", f"{'Unmarked' if unmark else 'Marked'} {len(ids)} as transfer"):
            if unmark:
                # Manual and uncategorised, so the transfer rule doesn't put it straight back.
                self.conn.execute(f"""UPDATE transactions SET is_transfer = 0, category = NULL, category_source = 'manual',
                                      rule_index = NULL WHERE id IN ({marks}) AND transfer_pair_id IS NULL""", ids)
            else:
                self.conn.execute(f"""UPDATE transactions SET is_transfer = 1, category = 'Transfer', category_source = 'manual'
                                      WHERE id IN ({marks}) AND transfer_pair_id IS NULL""", ids)
        self.marked.clear()
        self._update_title()
        self.action_refresh()
        if unmark:
            self.notify("Not a transfer now: it counts as spending. Set its category with c "
                        "(do it twice and kete offers a rule that beats the transfer rule).")

    def action_new_rule(self) -> None:
        tx = self._selected_tx()
        if not tx:
            return
        payee = rules_mod.rule_payee(tx["payee"])
        full = dict(self.conn.execute(
            """SELECT t.payee, t.description, t.particulars, t.code, t.reference, t.amount_cents, a.key AS account
               FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE t.id = ?""", (tx["id"],)).fetchone())

        def done(cat: str | None) -> None:
            if not cat:
                return
            cat = rules_mod.canonical_category(cat, self._categories())
            rules = self._rules()
            beat = rules_mod.overridden_rule(rules, full, cat)
            try:
                with history.tracked(self.conn, "rule", f"Rule: {payee} → {cat}", self.paths.rules):
                    rules_mod.append_rule(self.paths.rules, payee=payee, category=cat,
                                          before=beat.index if beat else None)
                    result = rules_mod.apply_rules(self.conn, rules_mod.load_rules(self.paths.rules))
            except rules_mod.RulesError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"Rule added for “{self.who(payee)}”; {result['unmatched']} still uncategorised")
            self.action_refresh()

        self.push_screen(AskCategory(f"New rule: payee contains “{self.who(payee)}” →", self._categories()), done)

    def action_share(self) -> None:
        tx = self._selected_tx()
        if not tx:
            return
        out = tx["amount_cents"] < 0
        people = [p["person"] for p in shares_mod.balances(self.conn, include_settled=True)]
        prompt = (f"Who owes you part of {self.who(tx['payee'])} ({self.m(tx['amount_cents'] / 100)})?" if out
                  else f"Who paid you back with this {self.m(tx['amount_cents'] / 100)}?")

        def done(text: str | None) -> None:
            if not text:
                return
            words = text.split()
            try:
                with history.tracked(self.conn, "owed", f"Shared #{tx['id']}"):
                    if words[0].lower() == "clear":
                        shares_mod.remove_share(self.conn, tx["id"], " ".join(words[1:]) or None)
                        msg = "Share removed"
                    else:
                        spec = None
                        if len(words) > 1:
                            try:
                                shares_mod.parse_amount_spec(words[-1], tx["amount_cents"])
                                spec, words = words[-1], words[:-1]
                            except shares_mod.ShareError:
                                pass
                        r = shares_mod.set_share(self.conn, tx["id"], " ".join(words), spec)
                        msg = f"{self.who(r['person'])} {r['kind']} {self.m(abs(r['amount']))}"
            except shares_mod.ShareError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(msg + " · totals now count only your share")
            self.action_refresh()

        self.push_screen(AskCategory(
            prompt, people, placeholder="Name and amount: Sam half · Sam 40 · Sam 1/3 · Sam (all of it)",
            hint="“clear” removes shares on this transaction. u undoes."), done)

    def action_bill(self) -> None:
        """Track this payment's payee as a bill, at about this amount, so it shows in Coming up."""
        from . import bills as bills_mod

        tx = self._selected_tx()
        if not tx:
            return
        if tx["amount_cents"] >= 0:
            self.notify("Pick a payment (money out)")
            return
        payee = rules_mod.rule_payee(tx["payee"])
        amount = -tx["amount_cents"] / 100

        def done(name: str | None) -> None:
            if not name:
                return
            try:
                bills_mod.add(self.conn, name, [payee], "monthly", round(amount * 0.6, 2), round(amount * 1.4, 2))
            except bills_mod.BillError as e:
                self.notify(str(e), severity="error")
                return
            self.conn.commit()
            self.notify(f"Tracking {escape(name)}: monthly payments to “{self.who(payee)}” of about {self.m(amount)}. "
                        "kete bills add can add other spellings or change it.", timeout=8)
            self.action_refresh()

        self.push_screen(AskCategory(
            f"Track payments to “{self.who(payee)}” of about {self.m(amount)} as a monthly bill. Name:", [],
            placeholder="e.g. Internet", value=payee,
            hint="It will show in Coming up from its latest payment, even when paid on different days."), done)

    def _selected_recurring(self) -> dict | None:
        t = self.query_one("#recurring-table", DataTable)
        if self.query_one(TabbedContent).active != "recurring" or not t.row_count:
            return None
        return self.recurring.get(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)

    def action_rename_recurring(self) -> None:
        from . import bills as bills_mod

        s = self._selected_recurring()
        if not s:
            return

        def done(new: str | None) -> None:
            if not new:
                return
            try:
                bills_mod.rename(self.conn, s, new)
            except bills_mod.BillError as e:
                self.notify(str(e), severity="error")
                return
            self.conn.commit()
            self.action_refresh()

        self.push_screen(AskCategory(f"New name for {self.who(s['payee'])}:", [], placeholder="e.g. Internet",
                                     value=s["payee"], hint="Only the name shown changes; matching stays the same."), done)

    def action_cancel_recurring(self) -> None:
        """Mark cancelled (or take it back): out of totals and Coming up now; a later charge is flagged."""
        from . import bills as bills_mod

        s = self._selected_recurring()
        if not s:
            return
        undo = bool(s.get("cancelled"))
        bills_mod.set_cancelled(self.conn, s, not undo)
        self.conn.commit()
        self.action_refresh()
        self.notify(f"{self.who(s['payee'])}: " + ("no longer marked cancelled" if undo else
                    "marked cancelled. kete will warn you if it charges again (k takes it back)"))

    def action_undo(self) -> None:
        try:
            result = history.undo(self.conn)
        except history.UndoError as e:
            self.notify(f"{e}", severity="error", timeout=8)
            return
        if result is None:
            self.notify("Nothing to undo")
            return
        self.marked.clear()
        self._update_title()
        self.notify(f"Undid “{result['summary']}”")
        self.action_refresh()
