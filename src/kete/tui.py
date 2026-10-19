"""Textual dashboard: kete tui."""

from __future__ import annotations

import json

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import (DataTable, Footer, Header, Input, Label, Sparkline, Static,
                             TabbedContent, TabPane)

from . import db as dbmod
from . import queries, rules as rules_mod
from .paths import get_paths


def money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


class AskCategory(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    AskCategory { align: center middle; }
    AskCategory > Vertical { width: 64; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    """

    def __init__(self, prompt: str, choices: list[str]) -> None:
        super().__init__()
        self.prompt, self.choices = prompt, choices

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            yield Input(placeholder="Category (Tab completes)",
                        suggester=SuggestFromList(self.choices, case_sensitive=False))

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


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


TX_COLUMNS = ("Date", "Account", "Amount", "Payee", "Type", "Particulars", "Code", "Reference", "Category")


class KeteApp(App):
    TITLE = "kete"
    CSS = """
    #overview-body { padding: 0 1; }
    .section { margin-top: 1; text-style: bold; }
    #spark { height: 3; margin: 0 0 1 0; }
    #accounts-table, #cashflow-table { height: auto; max-height: 16; }
    #cat-header { height: 1; padding: 0 1; }
    #tx-filter { margin: 0 0 1 0; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("escape,backspace", "back", "Back to categories"),
        Binding("1", "tab('overview')", "Overview", show=False),
        Binding("2", "tab('transactions')", "Transactions", show=False),
        Binding("3", "tab('categories')", "Categories", show=False),
        Binding("4", "tab('uncategorised')", "Uncategorised", show=False),
        Binding("5", "tab('imports')", "Imports", show=False),
        Binding("c", "categorise", "Set category"),
        Binding("t", "toggle_transfer", "Transfer"),
        Binding("n", "new_rule", "Rule from payee"),
        Binding("o", "open_raw", "Raw row"),
        Binding("left_square_bracket", "month(-1)", "Prev month", show=False),
        Binding("right_square_bracket", "month(1)", "Next month", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.paths = get_paths()
        self.conn = dbmod.connect(self.paths.db)
        self.cat_month = queries.current_month()
        self.drilled = False  # Transactions tab is showing a Categories drill-down
        self.drilled_row = 0

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="overview"):
            with TabPane("1 Overview", id="overview"):
                with VerticalScroll(id="overview-body"):
                    yield Label("Accounts", classes="section")
                    yield DataTable(id="accounts-table", cursor_type="none", zebra_stripes=True)
                    yield Static(id="month-compare", classes="section")
                    yield Label("Monthly spend, last 12 months", classes="section")
                    yield Sparkline([], id="spark")
                    yield DataTable(id="cashflow-table", cursor_type="none", zebra_stripes=True)
            with TabPane("2 Transactions", id="transactions"):
                yield Input(id="tx-filter",
                            placeholder="Filter: text, account:uber, cat:Groceries, month:2026-10, "
                                        "from:2026-01-01, to:…, uncat")
                yield DataTable(id="tx-table", cursor_type="row", zebra_stripes=True)
            with TabPane("3 Categories", id="categories"):
                yield Static(id="cat-header")
                yield DataTable(id="cat-table", cursor_type="row", zebra_stripes=True)
            with TabPane("4 Uncategorised", id="uncategorised"):
                yield Static("c set category · n create a rule from this payee · t mark as transfer · o raw row",
                             id="uncat-help")
                yield DataTable(id="uncat-table", cursor_type="row", zebra_stripes=True)
            with TabPane("5 Import log", id="imports"):
                yield DataTable(id="imports-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#accounts-table", DataTable).add_columns(
            "Account", "Number", "Txns", "Newest", "Age", "Balance")
        self.query_one("#cashflow-table", DataTable).add_columns("Month", "Income", "Spend", "Net")
        self.query_one("#tx-table", DataTable).add_columns(*TX_COLUMNS)
        self.query_one("#uncat-table", DataTable).add_columns(*TX_COLUMNS[:-1])
        self.query_one("#cat-table", DataTable).add_columns("Category", "Spend", "Count", "Share")
        self.query_one("#imports-table", DataTable).add_columns(
            "Imported", "File", "Format", "Account", "From", "To", "New", "Dup", "Warnings")
        self.action_refresh()

    # ------------------------------------------------------------------ data

    def action_refresh(self) -> None:
        self.load_overview()
        self.load_transactions()
        self.load_categories()
        self.load_uncategorised()
        self.load_imports()

    def load_overview(self) -> None:
        t = self.query_one("#accounts-table", DataTable)
        t.clear()
        for a in queries.accounts(self.conn):
            age = a["days_since_last"]
            age_txt = "—" if age is None else (f"[red]{age}d stale[/]" if age > 30 else f"{age}d")
            t.add_row(a["label"], a["number_masked"] or "", str(a["transactions"]),
                      a["last_date"] or "—", age_txt, money(a["balance"]))
        this_m = queries.current_month()
        now = queries.spend(self.conn, this_m)["total"] or 0
        prev = queries.spend(self.conn, queries.prev_month(this_m))["total"] or 0
        delta = f" ({(now - prev) / prev:+.0%})" if prev else ""
        self.query_one("#month-compare", Static).update(
            f"Spend this month {money(now)} vs last month {money(prev)}{delta}")
        flow = queries.cashflow(self.conn, 12)
        self.query_one("#spark", Sparkline).data = [m["spend"] for m in flow]
        ct = self.query_one("#cashflow-table", DataTable)
        ct.clear()
        for m in reversed(flow):
            net = money(m["net"])
            ct.add_row(m["month"], money(m["income"]), money(m["spend"]),
                       f"[red]{net}[/]" if m["net"] < 0 else f"[green]{net}[/]")

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
            amt = money(r["amount"])
            cells = [r["date"], r["account"], f"[green]{amt}[/]" if r["amount"] > 0 else amt,
                     r["payee"] + (" [dim](pending)[/]" if r["pending"] else ""),
                     r["description"], r["particulars"], r["code"], r["reference"]]
            if with_category:
                cat = r["category"] or ""
                cells.append(f"[dim]↔ {cat}[/]" if r["is_transfer"] else cat)
            table.add_row(*cells, key=str(r["id"]))
        if rows:
            table.move_cursor(row=min(cursor, len(rows) - 1))

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

    def load_categories(self) -> None:
        data = queries.spend(self.conn, self.cat_month)
        self.query_one("#cat-header", Static).update(
            f"[b]{data['month']}[/]  total {money(data['total'])}   "
            "[dim]\\[ / ] change month · Enter shows transactions[/]")
        t = self.query_one("#cat-table", DataTable)
        t.clear()
        total = data["total"] or 1
        for r in data["rows"]:
            t.add_row(r["name"], money(r["total"]), str(r["count"]), f"{r['total'] / total:.0%}",
                      key=r["name"])

    def load_imports(self) -> None:
        t = self.query_one("#imports-table", DataTable)
        t.clear()
        for r in queries.import_log(self.conn):
            t.add_row(r["imported_at"].replace("T", " "), r["filename"], r["format"], r["account"] or "—",
                      r["date_from"] or "", r["date_to"] or "", str(r["rows_new"]), str(r["rows_dup"]),
                      f"[yellow]{'; '.join(r['warnings'])}[/]" if r["warnings"] else "")

    # ------------------------------------------------------------------ events

    @on(Input.Changed, "#tx-filter")
    def filter_changed(self) -> None:
        self.load_transactions()

    @on(DataTable.RowSelected, "#cat-table")
    def drill_down(self, event: DataTable.RowSelected) -> None:
        name = event.row_key.value
        f = f"month:{self.cat_month} " + ("uncat" if name == "Uncategorised" else f"cat:{name.replace(' ', '_')}")
        self.drilled_row = event.cursor_row
        self.query_one("#tx-filter", Input).value = f
        self.action_tab("transactions")
        self.drilled = True
        self.refresh_bindings()

    @on(TabbedContent.TabActivated)
    def tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if self.drilled and event.pane.id != "transactions":
            self.drilled = False
            self.query_one("#tx-filter", Input).value = ""
            self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "back":
            return self.drilled  # only shown in the footer after a drill-down
        return True

    def action_back(self) -> None:
        """Return from a category drill-down to the Categories tab, on the same row."""
        if not self.drilled:
            return
        self.action_tab("categories")  # tab_changed clears the filter
        table = self.query_one("#cat-table", DataTable)
        if table.row_count:
            table.move_cursor(row=min(self.drilled_row, table.row_count - 1))

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        target = {"transactions": "#tx-table", "categories": "#cat-table",
                  "uncategorised": "#uncat-table", "imports": "#imports-table"}.get(tab)
        if target:
            self.query_one(target).focus()

    def action_month(self, step: int) -> None:
        if self.query_one(TabbedContent).active != "categories":
            return
        m = self.cat_month
        if step < 0:
            m = queries.prev_month(m)
        else:
            y, mo = map(int, m.split("-"))
            m = f"{y + (mo == 12)}-{mo % 12 + 1:02d}"
        self.cat_month = m
        self.load_categories()

    # ------------------------------------------------------------------ edits

    def _selected_tx(self) -> dict | None:
        table = self.focused
        if not isinstance(table, DataTable) or table.id not in ("tx-table", "uncat-table"):
            self.notify("Select a transaction first (Transactions or Uncategorised tab)")
            return None
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        row = self.conn.execute(
            "SELECT id, payee, category, is_transfer, transfer_pair_id FROM transactions WHERE id = ?",
            (int(key),)).fetchone()
        return dict(row) if row else None

    def action_open_raw(self) -> None:
        tx = self._selected_tx()
        if tx:
            self.push_screen(RawRow(queries.raw_row(self.conn, tx["id"])))

    def _categories(self) -> list[str]:
        try:
            rules = rules_mod.load_rules(self.paths.rules)
        except rules_mod.RulesError:
            rules = []
        return rules_mod.known_categories(self.conn, rules)

    def action_categorise(self) -> None:
        tx = self._selected_tx()
        if not tx:
            return

        def done(cat: str | None) -> None:
            if cat:
                cat = rules_mod.canonical_category(cat, self._categories())
                self.conn.execute(
                    "UPDATE transactions SET category = ?, category_source = 'manual', is_transfer = 0 "
                    "WHERE id = ?", (cat, tx["id"]))
                self.conn.commit()
                self.action_refresh()

        self.push_screen(AskCategory(f"Category for {tx['payee']}", self._categories()), done)

    def action_toggle_transfer(self) -> None:
        tx = self._selected_tx()
        if not tx:
            return
        if tx["transfer_pair_id"]:
            self.notify("Paired transfer; undo with: kete transfers unpair " + str(tx["id"]))
            return
        if tx["is_transfer"]:
            self.conn.execute("UPDATE transactions SET is_transfer = 0, category = NULL, "
                              "category_source = NULL WHERE id = ?", (tx["id"],))
            rules_mod.apply_rules(self.conn, rules_mod.load_rules(self.paths.rules), only_ids=[tx["id"]])
        else:
            self.conn.execute("UPDATE transactions SET is_transfer = 1, category = 'Transfer', "
                              "category_source = 'manual' WHERE id = ?", (tx["id"],))
        self.conn.commit()
        self.action_refresh()

    def action_new_rule(self) -> None:
        tx = self._selected_tx()
        if not tx:
            return
        payee = queries._TRAILING_NUM.sub("", " ".join(tx["payee"].split()))

        def done(cat: str | None) -> None:
            if not cat:
                return
            cat = rules_mod.canonical_category(cat, self._categories())
            rules_mod.append_rule(self.paths.rules, payee=payee, category=cat)
            try:
                result = rules_mod.apply_rules(self.conn, rules_mod.load_rules(self.paths.rules))
            except rules_mod.RulesError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"Rule added for “{payee}”; {result['unmatched']} still uncategorised")
            self.action_refresh()

        self.push_screen(AskCategory(f"New rule: payee contains “{payee}” →", self._categories()), done)
