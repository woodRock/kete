"""kete command line. Every read command supports --json so scripts, agents and the TUI agree."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db as dbmod
from . import history, importer, queries, rules as rules_mod, shares as shares_mod, transfers
from .paths import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
rules_app = typer.Typer(no_args_is_help=True, help="Categorisation rules (rules.toml).")
transfers_app = typer.Typer(no_args_is_help=True, help="Transfers between your own accounts.")
balance_app = typer.Typer(no_args_is_help=True, help="Balance anchors (CSV exports carry none).")
accounts_app = typer.Typer(help="List, add and edit accounts.")
app.add_typer(accounts_app, name="accounts")
categories_app = typer.Typer(help="List and rename categories.")
report_app = typer.Typer(help="Reports: findings for a month, year or NZ tax year.")
app.add_typer(report_app, name="report")
app.add_typer(categories_app, name="categories")
app.add_typer(rules_app, name="rules")
app.add_typer(transfers_app, name="transfers")
app.add_typer(balance_app, name="balance")
bills_app = typer.Typer(help="Bills you name yourself, for ones detection misses (kete subs shows both).")
app.add_typer(bills_app, name="bills")
owed_app = typer.Typer(help="Money other people owe you (shared costs) and their repayments.")
app.add_typer(owed_app, name="owed")
formats_app = typer.Typer(help="Saved CSV layouts for other banks and cards (formats.toml).")
app.add_typer(formats_app, name="formats")
autoimport_app = typer.Typer(no_args_is_help=True, help="Import exports automatically when they're downloaded (macOS).")
app.add_typer(autoimport_app, name="autoimport")

console = Console()
err = Console(stderr=True)

JsonOpt = typer.Option(False, "--json", help="Machine-readable output.")
ACCOUNT_TYPES = ("everyday", "savings", "credit", "loan", "other")


def _emit_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _open(readonly: bool = True) -> sqlite3.Connection:
    paths = get_paths()
    if readonly and not paths.db.exists():
        err.print("No database yet. Drop exports into "
                  f"[bold]{paths.inbox}[/] and run [bold]kete import[/].")
        raise typer.Exit(1)
    if not readonly:
        paths.ensure()
        rules_mod.ensure_rules_file(paths.rules)
    return dbmod.connect(paths.db, readonly=readonly)


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{abs(v):,.2f}"
    return f"-${s}" if v < 0 else f"${s}"


def _table(columns: list[str], rows: list[list], right: set[int] | None = None, title: str | None = None):
    t = Table(title=title, show_edge=False, header_style="bold")
    for i, c in enumerate(columns):
        t.add_column(c, justify="right" if right and i in right else "left")
    for r in rows:
        t.add_row(*["" if v is None else str(v) for v in r])
    console.print(t)


def _ask_account(filename: str, choices: list[str], suggested: str | None) -> str | None:
    if not sys.stdin.isatty():
        return None
    err.print(f"[yellow]Which account is {filename}?[/] Existing: {', '.join(choices)}. "
              "A new name creates an account; blank skips the file.")
    while True:
        answer = typer.prompt("account", default=suggested or "", show_default=bool(suggested))
        answer = answer.strip().lower()
        if not answer:
            return None
        if not importer.KEY_RE.match(answer):
            err.print("Use lower-case letters, digits and dashes, e.g. joint or visa-card.")
            continue
        if answer in choices or typer.confirm(f"Create new account '{answer}'?", default=True):
            return answer


# --------------------------------------------------------------------------- import


@app.command("import")
def import_cmd(
    paths_in: list[Path] = typer.Argument(None, metavar="[PATHS]...", help="Files or folders; default: inbox."),
    account: Optional[str] = typer.Option(
        None, "--account", "-a", help="Import every file into this account (see kete accounts)."),
    map_: bool = typer.Option(False, "--map", help="Describe an unrecognised CSV's columns first, and save the layout."),
    as_json: bool = JsonOpt,
):
    """Import Westpac exports (OFX or CSV). Re-importing overlapping files is safe."""
    paths = get_paths()
    conn = _open(readonly=False)
    if map_:
        for p in paths_in or []:
            if p.is_file() and p.suffix.lower() == ".csv":
                _map_csv(p)
    if paths.cloud_synced():
        err.print(f"[yellow]Warning:[/] {paths.home} looks cloud-synced; your financial history "
                  "will leave this machine. Set KETE_HOME to a local folder.")
    try:
        files = importer.collect(paths, list(paths_in or []))
    except FileNotFoundError as e:
        err.print(f"[red]No such file:[/] {e}")
        raise typer.Exit(1)
    if account and not importer.account_exists(conn, account):
        err.print(f"[red]Unknown account {account!r}.[/] Create it first: kete accounts add {account}")
        raise typer.Exit(1)
    try:
        results = [importer.import_file(conn, paths, f, ask=_ask_account, account=account)
                   for f in files]
    except (rules_mod.RulesError, ValueError) as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    if as_json:
        _emit_json([r.to_dict() for r in results])
        return
    if not files:
        console.print(f"Nothing to import in {paths.inbox}")
        return
    _table(
        ["file", "status", "account", "from", "to", "new", "dup", "pending"],
        [[r.filename, r.status, r.account, r.date_from, r.date_to, r.rows_new, r.rows_dup, r.rows_pending]
         for r in results],
        right={5, 6, 7},
    )
    for r in results:
        for w in r.warnings:
            err.print(f"[yellow]{r.filename}:[/] {w}")
        if r.error:
            err.print(f"[red]{r.filename}:[/] {r.error}")
    if any(r.rows_new for r in results):
        pending = transfers.detect(conn)
        if pending:
            console.print(f"\n{len(pending)} possible transfers between your accounts; "
                          "review with [bold]kete transfers detect[/].")
    if any(r.status == "error" for r in results):
        raise typer.Exit(1)


def _check_type(type_: str | None) -> None:
    if type_ and type_ not in ACCOUNT_TYPES:
        raise typer.BadParameter(f"type must be one of {', '.join(ACCOUNT_TYPES)}")


@accounts_app.command("add")
def accounts_add(
    key: str = typer.Argument(..., help="Short name, also the filename prefix: KEY_....csv"),
    label: Optional[str] = typer.Option(None, "--label", help="Display name."),
    type_: str = typer.Option("everyday", "--type", help=", ".join(ACCOUNT_TYPES)),
    number: Optional[str] = typer.Option(None, "--number", help="Account number, e.g. 0100-0012345-05."),
):
    """Add an account. Exports named KEY_....csv then import into it automatically."""
    key = key.lower()
    if not importer.KEY_RE.match(key):
        raise typer.BadParameter("use lower-case letters, digits and dashes")
    _check_type(type_)
    conn = _open(readonly=False)
    if dbmod.find_account(conn, key):
        err.print(f"[red]Account {key!r} already exists.[/] Use kete accounts edit.")
        raise typer.Exit(1)
    try:
        conn.execute(
            "INSERT INTO accounts (key, label, type, number, number_masked) VALUES (?, ?, ?, ?, ?)",
            (key, label or key.replace("-", " ").title(), type_, number,
             dbmod.mask_number(number) if number else None),
        )
    except sqlite3.IntegrityError:
        err.print(f"[red]Another account already has number {number}.[/]")
        raise typer.Exit(1)
    conn.commit()
    console.print(f"Added {key}. Name its exports {key}_<anything>.csv")


@accounts_app.command("edit")
def accounts_edit(
    key: str,
    label: Optional[str] = typer.Option(None, "--label"),
    type_: Optional[str] = typer.Option(None, "--type", help=", ".join(ACCOUNT_TYPES)),
    number: Optional[str] = typer.Option(None, "--number"),
    rename: Optional[str] = typer.Option(None, "--rename", help="New short name."),
):
    """Change an account's label, type, number or short name."""
    _check_type(type_)
    conn = _open(readonly=False)
    acct = dbmod.find_account(conn, key)
    if not acct:
        err.print(f"[red]Unknown account {key!r}[/]")
        raise typer.Exit(1)
    if rename:
        rename = rename.lower()
        if not importer.KEY_RE.match(rename):
            raise typer.BadParameter("use lower-case letters, digits and dashes")
        if dbmod.find_account(conn, rename):
            err.print(f"[red]Account {rename!r} already exists.[/]")
            raise typer.Exit(1)
    try:
        conn.execute(
            """UPDATE accounts SET label = COALESCE(?, label), type = COALESCE(?, type),
               number = COALESCE(?, number), number_masked = COALESCE(?, number_masked),
               key = COALESCE(?, key) WHERE id = ?""",
            (label, type_, number, dbmod.mask_number(number) if number else None, rename, acct["id"]),
        )
    except sqlite3.IntegrityError:
        err.print(f"[red]Another account already has number {number}.[/]")
        raise typer.Exit(1)
    conn.commit()
    console.print(f"Updated {rename or acct['key']}.")


@accounts_app.command("remove")
def accounts_remove(key: str):
    """Delete an account that has no transactions (e.g. one created by a typo)."""
    conn = _open(readonly=False)
    acct = dbmod.find_account(conn, key)
    if not acct:
        err.print(f"[red]Unknown account {key!r}[/]")
        raise typer.Exit(1)
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (acct["id"],)).fetchone()[0]
    if n:
        err.print(f"[red]{key} has {n} transactions; not removing it.[/]")
        raise typer.Exit(1)
    for table in ("file_mappings", "balances"):
        conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (acct["id"],))
    conn.execute("UPDATE imports SET account_id = NULL WHERE account_id = ?", (acct["id"],))
    conn.execute("DELETE FROM accounts WHERE id = ?", (acct["id"],))
    conn.commit()
    console.print(f"Removed {key}.")


# --------------------------------------------------------------------------- reads


@accounts_app.callback(invoke_without_command=True)
def accounts(ctx: typer.Context, as_json: bool = JsonOpt):
    """Accounts with last transaction, last import and latest known balance."""
    if ctx.invoked_subcommand:
        return
    data = queries.accounts(_open())
    if as_json:
        return _emit_json(data)
    _table(
        ["account", "label", "number", "txns", "first", "last", "age", "balance"],
        [[a["key"], a["label"], a["number_masked"], a["transactions"], a["first_date"], a["last_date"],
          f"{a['days_since_last']}d" if a["days_since_last"] is not None else None,
          _money(a["balance"]) + (f" ({a['balance_as_of']})" if a["balance"] is not None else "")]
         for a in data],
        right={3, 6, 7},
    )


@app.command()
def tx(
    account: Optional[str] = typer.Option(None, "--account", "-a"),
    date_from: Optional[str] = typer.Option(None, "--from", help="yyyy-mm-dd"),
    date_to: Optional[str] = typer.Option(None, "--to", help="yyyy-mm-dd"),
    cat: Optional[str] = typer.Option(None, "--cat", help="Category name."),
    search: Optional[str] = typer.Option(None, "--search", "-s"),
    uncategorised: bool = typer.Option(False, "--uncategorised", "-u"),
    limit: int = typer.Option(50, "--limit", "-n", help="0 for all."),
    as_json: bool = JsonOpt,
):
    """List transactions, newest first."""
    rows = queries.transactions(_open(), account, date_from, date_to, cat, search,
                                uncategorised, limit or None)
    if as_json:
        return _emit_json(rows)
    _table(
        ["id", "date", "account", "amount", "payee", "type", "category"],
        [[r["id"], r["date"], r["account"], _money(r["amount"]),
          r["payee"] + (" (pending)" if r["pending"] else ""), r["description"],
          ("↔ " if r["is_transfer"] else "") + (r["category"] or "")] for r in rows],
        right={0, 3},
    )


@app.command()
def spend(
    month: Optional[str] = typer.Option(None, "--month", help="yyyy-mm; default this month."),
    date_from: Optional[str] = typer.Option(None, "--from", help="yyyy-mm-dd (instead of --month)."),
    date_to: Optional[str] = typer.Option(None, "--to", help="yyyy-mm-dd, inclusive."),
    period: Optional[str] = typer.Option(None, "--period", help="2026-03, 2026, 2026-01..2026-03, since 2026-02-01, tax 2026"),
    compare: bool = typer.Option(False, "--compare", help="Add the same period a year earlier."),
    by: str = typer.Option("cat", "--by", help="cat or payee"),
    account: Optional[str] = typer.Option(None, "--account", "-a"),
    as_json: bool = JsonOpt,
):
    """Spending totals for a month or any date range. Transfers are excluded."""
    if by not in ("cat", "payee"):
        raise typer.BadParameter("--by must be cat or payee")
    if period:
        try:
            date_from, date_to, _ = queries.parse_period(period)
        except ValueError as e:
            raise typer.BadParameter(str(e))
    elif not (date_from or date_to):
        date_from, date_to = queries.month_range(month or queries.current_month())
    conn = _open()
    data = queries.spend(conn, by=by, account=account, date_from=date_from, date_to=date_to)
    prev = None
    if compare:
        p_from, p_to = queries.year_earlier(data["from"], data["to"] or date.today().isoformat())
        prev = queries.spend(conn, by=by, account=account, date_from=p_from, date_to=p_to)
        data["previous"] = prev
    if as_json:
        return _emit_json(data)
    title = f"Spend {data['from']} to {data['to']}: {_money(data['total'])}"
    if prev is None:
        _table(["category" if by == "cat" else "payee", "total", "count"],
               [[r["name"], _money(r["total"]), r["count"]] for r in data["rows"]], right={1, 2}, title=title)
        return
    before = {r["name"]: r["total"] for r in prev["rows"]}
    names = [r["name"] for r in data["rows"]] + [n for n in before if n not in {r["name"] for r in data["rows"]}]
    now = {r["name"]: r["total"] for r in data["rows"]}
    _table(["category" if by == "cat" else "payee", "total", "a year earlier", "change"],
           [[n, _money(now.get(n, 0)), _money(before.get(n, 0)), _money(now.get(n, 0) - before.get(n, 0))]
            for n in names], right={1, 2, 3},
           title=title + f" (a year earlier: {_money(prev['total'])})")


@app.command()
def cashflow(months: int = typer.Option(12, "--months"), as_json: bool = JsonOpt):
    """Income, spend and net per month. Transfers are excluded."""
    data = queries.cashflow(_open(), months)
    if as_json:
        return _emit_json(data)
    _table(["month", "income", "spend", "net"],
           [[r["month"], _money(r["income"]), _money(r["spend"]), _money(r["net"])] for r in data],
           right={1, 2, 3})


@app.command()
def subs(
    include_inactive: bool = typer.Option(False, "--all"),
    explain: bool = typer.Option(False, "--explain", help="Also list near-misses and why they were rejected."),
    as_json: bool = JsonOpt,
):
    """Recurring charges: same payee, similar amount, regular interval."""
    rows = queries.subscriptions(_open(), explain=explain)
    data = [s for s in rows if not s.get("rejected") and (include_inactive or s["active"])]
    rejected = [s for s in rows if s.get("rejected")]
    if as_json:
        return _emit_json(data + rejected)
    _table(["payee", "cadence", "typical", "≈/month", "count", "last", "next", "notes"],
           [[s["payee"], s["cadence"], _money(s["typical_amount"]), _money(s["monthly_cost"]),
             s["count"], s["last_date"], s["next_expected"], ", ".join(queries.recurring_notes(s))] for s in data],
           right={2, 3, 4})
    if data:
        total = sum(s["monthly_cost"] for s in data if s["active"])
        console.print(f"Active recurring ≈ {_money(total)}/month")
    if explain:
        _table(["payee", "charges", "last", "why not"],
               [[s["payee"], s["charges"], s["last_date"], s["reason"]] for s in rejected],
               right={1}, title="Not detected")
        console.print("Charges must be similar (within 15%) and regular; a new or changed price "
                      "needs 3 charges, or 2 identical monthly/yearly ones.")


@app.command()
def stale(days: int = typer.Option(30, "--days"), as_json: bool = JsonOpt):
    """Accounts whose newest transaction is older than --days: time to export."""
    data = queries.stale(_open(), days)
    if as_json:
        return _emit_json(data)
    if not data:
        console.print(f"All accounts have transactions in the last {days} days.")
        return
    for a in data:
        console.print(f"[yellow]{a['key']}[/]: newest transaction {a['last_date']} "
                      f"({a['days_since_last']} days ago) — export from Westpac One")
    raise typer.Exit(2)


@app.command()
def sql(query: str, as_json: bool = JsonOpt):
    """Read-only SQL escape hatch."""
    conn = _open()
    try:
        cur = conn.execute(query)
    except sqlite3.Error as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    cols = [d[0] for d in cur.description or []]
    rows = cur.fetchall()
    if as_json:
        return _emit_json([dict(zip(cols, r)) for r in rows])
    _table(cols, [list(r) for r in rows])


# --------------------------------------------------------------------------- rules


@rules_app.command("apply")
def rules_apply(as_json: bool = JsonOpt):
    """Re-run every rule over all transactions (manual categories are kept)."""
    paths = get_paths()
    conn = _open(readonly=False)
    try:
        with history.tracked(conn, "rules apply", "Re-ran rules"):
            result = rules_mod.apply_rules(conn, rules_mod.load_rules(paths.rules))
    except rules_mod.RulesError as e:
        err.print(f"[red]rules.toml error:[/] {e}")
        raise typer.Exit(1)
    if as_json:
        return _emit_json(result)
    console.print(f"{result['matched']} categorised, {result['unmatched']} uncategorised "
                  f"(rules: {paths.rules})")


@rules_app.command("test")
def rules_test(
    text: str,
    amount: Optional[float] = typer.Option(None, "--amount", help="Signed dollars, to test amount ranges."),
    as_json: bool = JsonOpt,
):
    """Show which rule would match TEXT (checked against every text field)."""
    rules = rules_mod.load_rules(get_paths().rules)
    tx_ = {f: text for f in rules_mod.TEXT_FIELDS}
    tx_["amount_cents"] = round(amount * 100) if amount is not None else None
    # 'account' conditions can't be satisfied by free text; ignore those rules unless they match.
    rule = rules_mod.first_match(rules, tx_, check_amount=amount is not None)
    if as_json:
        return _emit_json(None if rule is None else {
            "index": rule.index, "name": rule.name, "category": rule.category,
            "transfer": rule.transfer, "conditions": rule.conditions})
    console.print(rule.describe() if rule else "No rule matches.")


@rules_app.command("path")
def rules_path():
    """Print the rules file location."""
    print(get_paths().rules)


@categories_app.callback(invoke_without_command=True)
def categories(ctx: typer.Context, as_json: bool = JsonOpt):
    """Every category, with how many transactions and rules use it."""
    if ctx.invoked_subcommand:
        return
    conn = _open()
    try:
        rules = rules_mod.load_rules(get_paths().rules)
    except rules_mod.RulesError as e:
        err.print(f"[red]rules.toml error:[/] {e}")
        raise typer.Exit(1)
    counts = {r[0].lower(): (r[1], r[2]) for r in conn.execute(
        "SELECT category, COUNT(*), SUM(category_source = 'manual') FROM transactions "
        "WHERE category IS NOT NULL GROUP BY lower(category)")}
    data = []
    for name in rules_mod.known_categories(conn, rules):
        n, manual = counts.get(name.lower(), (0, 0))
        data.append({"category": name, "transactions": n, "manual": manual or 0,
                     "rules": sum(1 for r in rules if (r.category or "").lower() == name.lower())})
    if as_json:
        return _emit_json(data)
    _table(["category", "txns", "set by hand", "rules"],
           [[d["category"], d["transactions"], d["manual"], d["rules"]] for d in data], right={1, 2, 3})


@categories_app.command("rename")
def categories_rename(old: str, new: str, as_json: bool = JsonOpt):
    """Rename a category in transactions and rules.toml. Renaming onto an existing one merges them."""
    conn = _open(readonly=False)
    try:
        with history.tracked(conn, "rename", f"Renamed {old} → {new}", get_paths().rules):
            result = rules_mod.rename_category(conn, get_paths().rules, old, new)
    except rules_mod.RulesError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    if as_json:
        return _emit_json(result)
    if not result["transactions"] and not result["rules"]:
        err.print(f"[yellow]No category named {old!r}.[/]")
        raise typer.Exit(1)
    console.print(f"{result['from']} → {result['to']}: {result['transactions']} transactions, "
                  f"{result['rules']} rules updated")


# --------------------------------------------------------------------------- transfers


@transfers_app.command("detect")
def transfers_detect(
    apply: bool = typer.Option(False, "--apply", help="Accept all proposed pairs."),
    days: int = typer.Option(transfers.MAX_DAYS, "--days", help="Max days between legs."),
    as_json: bool = JsonOpt,
):
    """Propose pairs: money out of one account and the same amount into another within --days."""
    conn = _open(readonly=not apply)
    pairs = transfers.detect(conn, days)
    if apply:
        with history.tracked(conn, "transfers", f"Paired {len(pairs)} transfers"):
            transfers.apply(conn, pairs)
    if as_json:
        return _emit_json({"applied": apply, "pairs": pairs})
    _table(["out date", "from", "to", "amount", "in date", "out payee"],
           [[p["out_date"], p["from_account"], p["to_account"], _money(p["amount"]), p["in_date"],
             p["out_payee"]] for p in pairs], right={3})
    if pairs and not apply:
        console.print(f"{len(pairs)} proposed. Accept with [bold]kete transfers detect --apply[/].")
    elif apply:
        console.print(f"Paired {len(pairs)} transfers.")


@transfers_app.command("unpair")
def transfers_unpair(tx_id: int):
    """Undo a pairing (either leg's id)."""
    conn = _open(readonly=False)
    with history.tracked(conn, "unpair", f"Unpaired transfer {tx_id}"):
        transfers.unpair(conn, tx_id)
        rules_mod.apply_rules(conn, rules_mod.load_rules(get_paths().rules))
    console.print("Unpaired.")


# --------------------------------------------------------------------------- balances


@balance_app.command("set")
def balance_set(
    account: str,
    amount: float,
    as_of: str = typer.Option(None, "--as-of", help="yyyy-mm-dd, end of day; default today."),
):
    """Record a known balance (from Westpac One) so CSV-only accounts can show balances."""
    conn = _open(readonly=False)
    acct = dbmod.find_account(conn, account)
    if not acct:
        err.print(f"[red]Unknown account {account!r}[/]")
        raise typer.Exit(1)
    as_of = as_of or date.today().isoformat()
    date.fromisoformat(as_of)
    conn.execute(
        "INSERT OR REPLACE INTO balances (account_id, as_of, balance_cents, source) VALUES (?, ?, ?, 'manual')",
        (acct["id"], as_of, round(amount * 100)),
    )
    conn.commit()
    console.print(f"{acct['key']}: {_money(amount)} at end of {as_of}")


# --------------------------------------------------------------------------- dashboard


@app.command()
def dashboard(
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Where to write the HTML file."),
    open_: bool = typer.Option(True, "--open/--no-open", help="Open it in your browser."),
    demo: bool = typer.Option(False, "--demo", help="Use synthetic data (for previews and agents)."),
    as_json: bool = typer.Option(False, "--json", help="Print the data model instead of writing HTML."),
):
    """Build the local HTML dashboard: trends, patterns, where to cut, and savings projections."""
    import tempfile
    import webbrowser

    from . import dashboard as dash

    paths = get_paths()
    if demo:
        from .demo import build_demo_db

        conn, rules_path = build_demo_db(), None
        out = out or Path(tempfile.gettempdir()) / "kete-demo" / "dashboard.html"
    else:
        conn, rules_path = _open(), paths.rules
        out = out or paths.home / "dashboard.html"
    if as_json:
        return _emit_json(dash.build_model(conn, dash.load_config(rules_path), demo=demo))
    path = dash.generate(conn, out, rules_path, demo=demo)
    console.print(f"Dashboard written to {path}")
    if open_:
        webbrowser.open(path.resolve().as_uri())


# --------------------------------------------------------------------------- report


@report_app.callback(invoke_without_command=True)
def report(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month", help="yyyy-mm; default: last complete month."),
    year: Optional[int] = typer.Option(None, "--year", help="Calendar year, e.g. 2026."),
    tax_year: Optional[int] = typer.Option(None, "--tax-year", help="NZ tax year ending 31 March, e.g. 2026."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Where to write the HTML file."),
    open_: bool = typer.Option(True, "--open/--no-open", help="Open it in your browser."),
    demo: bool = typer.Option(False, "--demo", help="Use synthetic data (for previews and agents)."),
    as_json: bool = typer.Option(False, "--json", help="Print the report model instead of writing HTML."),
):
    """Build a report of findings: unusual charges, price rises, double charges, fees and more."""
    if ctx.invoked_subcommand:
        return
    import tempfile
    import webbrowser

    from . import report as rep

    if sum(x is not None for x in (month, year, tax_year)) > 1:
        raise typer.BadParameter("use one of --month, --year or --tax-year")
    try:
        kind, start, end, label = rep.parse_period(month, year, tax_year)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    paths = get_paths()
    if demo:
        from .demo import build_demo_db

        conn, rules_path = build_demo_db(), None
        out = out or Path(tempfile.gettempdir()) / "kete-demo" / "reports" / rep.default_filename(kind, start, end)
    else:
        conn, rules_path = _open(readonly=False), paths.rules
        out = out or paths.home / "reports" / rep.default_filename(kind, start, end)
    if as_json:
        return _emit_json(rep.build_report(conn, kind, start, end, label, rep.load_config(rules_path), demo=demo))
    path = rep.generate(conn, out, kind, start, end, label, rules_path, demo=demo)
    console.print(f"Report written to {path}")
    if open_:
        webbrowser.open(path.resolve().as_uri())


@report_app.command("dismiss")
def report_dismiss(finding_ids: list[str] = typer.Argument(..., help="Ids shown on the report page.")):
    """Hide findings from future reports."""
    conn = _open(readonly=False)
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany("INSERT OR REPLACE INTO dismissals (finding_id, dismissed_at) VALUES (?, ?)",
                     [(f, now) for f in finding_ids])
    conn.commit()
    console.print(f"Dismissed {len(finding_ids)} finding{'s' if len(finding_ids) != 1 else ''}.")


@report_app.command("restore")
def report_restore(finding_ids: list[str] = typer.Argument(...)):
    """Show previously dismissed findings again."""
    conn = _open(readonly=False)
    n = sum(conn.execute("DELETE FROM dismissals WHERE finding_id = ?", (f,)).rowcount for f in finding_ids)
    conn.commit()
    console.print(f"Restored {n}.")


# --------------------------------------------------------------------------- history


@app.command("undo")
def undo_cmd(force: bool = typer.Option(False, "--force", help="Undo even if rules.toml was edited since."),
             as_json: bool = JsonOpt):
    """Undo the last change to categories, transfers, shared costs or rules (not imports)."""
    conn = _open(readonly=False)
    try:
        result = history.undo(conn, force=force)
    except history.UndoError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    if as_json:
        return _emit_json(result)
    if result is None:
        console.print("Nothing to undo.")
        return
    extra = " and rules.toml" if result["rules_file"] else ""
    console.print(f"Undid “{result['summary']}” ({result['transactions']} transactions{extra}).")


@app.command("history")
def history_cmd(limit: int = typer.Option(20, "--limit", "-n"), as_json: bool = JsonOpt):
    """Recent changes that kete undo can revert, newest first."""
    data = history.log(_open(), limit)
    if as_json:
        return _emit_json(data)
    _table(["#", "when", "change", "txns", "rules", ""],
           [[c["id"], c["at"].replace("T", " "), c["summary"], c["transactions"], "yes" if c["rules_file"] else "",
             "undone" if c["undone"] else ""] for c in data], right={0, 3})


# --------------------------------------------------------------------------- manual edits


@app.command("add", context_settings={"ignore_unknown_options": True})  # so -12.50 is an amount
def add_cmd(
    account: str = typer.Argument(..., help="Account key, e.g. cash (kete accounts add cash --type other)."),
    amount: float = typer.Argument(..., help="Signed dollars: -12.50 spent, 50 received."),
    payee: str = typer.Argument(..., help="Who it was paid to or received from."),
    on: Optional[str] = typer.Option(None, "--date", help="yyyy-mm-dd; default today."),
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Default: your rules decide."),
    note: str = typer.Option("", "--note", help="Stored as the description."),
    as_json: bool = JsonOpt,
):
    """Add a transaction by hand (cash, or anything missing from an export). kete undo removes it."""
    import uuid

    conn = _open(readonly=False)
    acct = dbmod.find_account(conn, account)
    if not acct:
        err.print(f"[red]Unknown account {account!r}.[/] Create it first: kete accounts add {account.lower()} --type other")
        raise typer.Exit(1)
    posted = on or date.today().isoformat()
    try:
        date.fromisoformat(posted)
    except ValueError:
        raise typer.BadParameter("--date must be yyyy-mm-dd")
    if not amount:
        raise typer.BadParameter("amount can't be zero")
    paths = get_paths()
    with history.tracked(conn, "add", f"Added {_money(amount)} {payee}"):
        cur = conn.execute(
            """INSERT INTO transactions (account_id, posted_date, amount_cents, payee, description,
               dedupe_key, source, raw_json) VALUES (?, ?, ?, ?, ?, ?, 'manual', ?)""",
            (acct["id"], posted, round(amount * 100), payee, note or "MANUAL", f"manual:{uuid.uuid4()}",
             json.dumps({"entered": datetime.now().isoformat(timespec="seconds")})))
        tx_id = cur.lastrowid
        if category:
            cat = rules_mod.canonical_category(category, rules_mod.known_categories(conn, rules_mod.load_rules(paths.rules)))
            conn.execute("UPDATE transactions SET category = ?, category_source = 'manual' WHERE id = ?", (cat, tx_id))
        else:
            rules_mod.apply_rules(conn, rules_mod.load_rules(paths.rules), only_ids=[tx_id])
    row = queries.transactions(conn, limit=None, account=acct["key"], date_from=posted, date_to=posted)
    added = next(r for r in row if r["id"] == tx_id)
    if as_json:
        return _emit_json(added)
    console.print(f"Added #{tx_id}: {posted} {acct['key']} {_money(amount)} {payee} → {added['category'] or 'Uncategorised'}")


@app.command("categorise")
def categorise_cmd(
    category: str = typer.Argument(..., help="Category name; an existing one is matched ignoring case."),
    ids: list[int] = typer.Argument(None, help="Transaction ids (from kete tx)."),
    search: Optional[str] = typer.Option(None, "--search", "-s", help="Every transaction matching this text instead."),
    uncategorised_only: bool = typer.Option(True, "--uncategorised-only/--all",
                                            help="With --search, only change uncategorised ones."),
    as_json: bool = JsonOpt,
):
    """Set the category of several transactions at once. kete undo reverts it."""
    conn = _open(readonly=False)
    paths = get_paths()
    if search:
        ids = [r["id"] for r in queries.transactions(conn, search=search, uncategorised=uncategorised_only)
               if not r["is_transfer"]]
    if not ids:
        err.print("[yellow]No transactions to change.[/]")
        raise typer.Exit(1)
    rules = rules_mod.load_rules(paths.rules)
    cat = rules_mod.canonical_category(category, rules_mod.known_categories(conn, rules))
    with history.tracked(conn, "categorise", f"{len(ids)} → {cat}"):
        n = rules_mod.set_category(conn, ids, cat)
    suggestions = rules_mod.suggest_rules(conn, rules, payee_keys=_payee_keys(conn, ids))
    if as_json:
        return _emit_json({"category": cat, "changed": n, "rule_suggestions": suggestions})
    console.print(f"{n} transactions → {cat}")
    for sug in suggestions:
        console.print(f"Tip: “{sug['payee']}” is always {sug['category']}; make it a rule with "
                      f"[bold]kete rules suggest --apply[/]")


def _payee_keys(conn: sqlite3.Connection, ids: list[int]) -> set[str]:
    marks = ",".join("?" * len(ids))
    return {rules_mod.rule_payee(r[0]).lower()
            for r in conn.execute(f"SELECT payee FROM transactions WHERE id IN ({marks})", ids)}


@rules_app.command("suggest")
def rules_suggest(
    apply: bool = typer.Option(False, "--apply", help="Add every suggestion to rules.toml."),
    min_count: int = typer.Option(2, "--min", help="Times categorised by hand, always the same way."),
    as_json: bool = JsonOpt,
):
    """Rules learnt from what you've categorised by hand."""
    paths = get_paths()
    conn = _open(readonly=not apply)
    rules = rules_mod.load_rules(paths.rules)
    data = rules_mod.suggest_rules(conn, rules, min_count=min_count)
    if apply and data:
        with history.tracked(conn, "rules suggest", f"Added {len(data)} learnt rules", paths.rules):
            for sug in data:
                rules_mod.append_rule(paths.rules, payee=sug["payee"], category=sug["category"], before=sug["before"])
            rules_mod.apply_rules(conn, rules_mod.load_rules(paths.rules))
    if as_json:
        return _emit_json({"applied": apply, "suggestions": data})
    _table(["payee contains", "category", "times by hand"],
           [[d["payee"], d["category"], d["count"]] for d in data], right={2})
    if data and not apply:
        console.print("Add them with [bold]kete rules suggest --apply[/], or skip one with "
                      "[bold]kete rules decline PAYEE[/].")
    elif apply and data:
        console.print(f"Added {len(data)} rules to {paths.rules}.")


@rules_app.command("decline")
def rules_decline(payee: str):
    """Stop suggesting a rule for this payee."""
    rules_mod.decline_suggestion(_open(readonly=False), payee)
    console.print(f"Won't suggest a rule for “{payee}” again.")


# --------------------------------------------------------------------------- named bills


@bills_app.callback(invoke_without_command=True)
def bills_list(ctx: typer.Context, as_json: bool = JsonOpt):
    """Your named bills, with their latest payment and next expected date."""
    if ctx.invoked_subcommand:
        return
    from . import bills as bills_mod

    conn = _open()
    named = bills_mod.declared(conn)
    found = {s["payee"]: s for s in queries.subscriptions(conn) if s.get("named")}
    data = [{**b, "latest": found.get(b["name"])} for b in named]
    if as_json:
        return _emit_json(data)
    if not data:
        console.print("No named bills. Add one with [bold]kete bills add NAME --match TEXT[/] "
                      "(or b on a transaction in kete tui).")
        return
    _table(["bill", "matches", "amounts", "cadence", "payments", "latest", "next"],
           [[d["name"], ", ".join(d["match"]),
             "any" if d["amount_min"] is None and d["amount_max"] is None
             else f"{_money(d['amount_min']) if d['amount_min'] is not None else ''}–{_money(d['amount_max']) if d['amount_max'] is not None else ''}",
             d["cadence"], d["latest"]["count"] if d["latest"] else 0,
             f"{_money(d['latest']['last_amount'])} {d['latest']['last_date']}" if d["latest"] else "none found",
             d["latest"]["next_expected"] if d["latest"] else "—"] for d in data], right={4})


@bills_app.command("add")
def bills_add(
    name: str = typer.Argument(..., help="What to call it, e.g. Internet."),
    match: list[str] = typer.Option(..., "--match", "-m", help="Text in the payee; repeat for other spellings."),
    cadence: str = typer.Option("monthly", "--cadence", help="weekly, fortnightly, monthly, quarterly or yearly"),
    amount_min: Optional[float] = typer.Option(None, "--min", help="Ignore smaller payments to the same payee."),
    amount_max: Optional[float] = typer.Option(None, "--max", help="Ignore bigger payments to the same payee."),
    as_json: bool = JsonOpt,
):
    """Name a bill or subscription so it always shows in Coming up, even if paid on varying days."""
    from . import bills as bills_mod

    conn = _open(readonly=False)
    try:
        bill = bills_mod.add(conn, name, match, cadence, amount_min, amount_max)
    except bills_mod.BillError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    conn.commit()
    latest = next((s for s in queries.subscriptions(conn) if s.get("named") and s["payee"] == bill["name"]), None)
    if as_json:
        return _emit_json({**bill, "latest": latest})
    if latest:
        console.print(f"{bill['name']}: {latest['count']} payments found, latest {_money(latest['last_amount'])} "
                      f"on {latest['last_date']}; next expected about {latest['next_expected']}.")
    else:
        console.print(f"Saved {bill['name']}, but no payments match yet. Check the --match text against kete tx.")


def _recurring(conn, name: str) -> dict:
    from . import bills as bills_mod

    s = bills_mod.find(queries.subscriptions(conn), name)
    if s is None:
        err.print(f"[red]No recurring payment called {name!r}.[/] See kete subs --all.")
        raise typer.Exit(1)
    return s


@bills_app.command("rename")
def bills_rename(name: str = typer.Argument(..., help="As shown in kete subs (or the bank's payee text)."),
                 new: str = typer.Argument(...)):
    """Rename any recurring payment (detected or named). Only the name shown changes."""
    from . import bills as bills_mod

    conn = _open(readonly=False)
    s = _recurring(conn, name)
    try:
        bills_mod.rename(conn, s, new)
    except bills_mod.BillError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    conn.commit()
    console.print(f"{s['payee']} is now shown as {new}.")


@bills_app.command("cancel")
def bills_cancel(name: str, undo_: bool = typer.Option(False, "--undo", help="Take back a cancellation.")):
    """Mark a subscription or bill as cancelled: it leaves totals and Coming up now, and kete
    warns you if it charges again."""
    from . import bills as bills_mod

    conn = _open(readonly=False)
    s = _recurring(conn, name)
    bills_mod.set_cancelled(conn, s, not undo_)
    conn.commit()
    console.print(f"{s['payee']}: " + ("no longer marked cancelled." if undo_ else
                  f"marked cancelled. A charge after {s['last_date']} will be flagged."))


@bills_app.command("remove")
def bills_remove(name: str):
    """Stop tracking a named bill."""
    from . import bills as bills_mod

    conn = _open(readonly=False)
    if not bills_mod.remove(conn, name):
        err.print(f"[red]No bill named {name!r}.[/]")
        raise typer.Exit(1)
    conn.commit()
    console.print(f"Removed {name}.")


# --------------------------------------------------------------------------- shared costs


@owed_app.callback(invoke_without_command=True)
def owed(ctx: typer.Context, include_settled: bool = typer.Option(False, "--all", help="Include people who are square."),
         as_json: bool = JsonOpt):
    """Who owes you what, from shared payments minus what they've paid back."""
    if ctx.invoked_subcommand:
        return
    data = shares_mod.balances(_open(), include_settled)
    if as_json:
        return _emit_json(data)
    if not data:
        console.print("Nobody owes you anything. Record a shared payment with "
                      "[bold]kete owed add TXID NAME [AMOUNT][/].")
        return
    _table(["person", "owes you", "shared", "paid back", "last"],
           [[d["person"], _money(d["balance"]), _money(d["owed"]), _money(d["repaid"]), d["last_date"]] for d in data],
           right={1, 2, 3})


@owed_app.command("add")
def owed_add(
    tx_id: int = typer.Argument(..., help="Transaction id (kete tx)."),
    person: str = typer.Argument(...),
    amount: Optional[str] = typer.Argument(None, help="Dollars, half, 1/3 or 25%; default: all of it."),
    note: str = typer.Option("", "--note"),
    as_json: bool = JsonOpt,
):
    """On a payment: PERSON owes you AMOUNT of it. On money in: PERSON paid you back AMOUNT."""
    conn = _open(readonly=False)
    try:
        with history.tracked(conn, "owed", f"{person} on #{tx_id}"):
            result = shares_mod.set_share(conn, tx_id, person, amount, note)
    except shares_mod.ShareError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    if as_json:
        return _emit_json(result)
    console.print(f"{result['person']} {result['kind']} {_money(abs(result['amount']))} (#{tx_id}). "
                  "Your totals now count only your share.")


@owed_app.command("list")
def owed_list(person: Optional[str] = typer.Argument(None), as_json: bool = JsonOpt):
    """Every shared payment and repayment."""
    data = shares_mod.entries(_open(), person)
    if as_json:
        return _emit_json(data)
    _table(["id", "date", "person", "payee", "amount", "their part", ""],
           [[d["tx_id"], d["date"], d["person"], d["payee"], _money(d["amount"]), _money(abs(d["share"])), d["kind"]]
            for d in data], right={0, 4, 5})


@owed_app.command("remove")
def owed_remove(tx_id: int, person: Optional[str] = typer.Argument(None)):
    """Remove a share (one person's, or all on that transaction)."""
    conn = _open(readonly=False)
    with history.tracked(conn, "owed", f"Removed share on #{tx_id}"):
        n = shares_mod.remove_share(conn, tx_id, person)
    console.print(f"Removed {n}.")


# --------------------------------------------------------------------------- pay cycle


@app.command()
def cycle(as_json: bool = JsonOpt):
    """This pay cycle: spent so far, bills before payday, and what's safe to spend."""
    from . import paycycle

    data = paycycle.cycles(_open(), paycycle.load_config(get_paths().rules))
    if as_json:
        return _emit_json(data)
    if data is None:
        err.print("[yellow]Couldn't find a regular pay.[/] Set it in rules.toml:\n  [paycycle]\n  payee = \"Your Employer\"")
        raise typer.Exit(1)
    cur, pay = data["current"], data["payday"]
    console.print(f"Pay: {pay['payee']} ({pay['cadence']}, about {_money(pay['typical'])})")
    console.print(f"This cycle {cur['start']} → {cur['end']}: spent {_money(cur['spend'])}"
                  + (f" (typical cycle {_money(data['average_spend'])})" if data["average_spend"] else ""))
    for u in data["upcoming"]:
        console.print(f"  due {u['date']}  {u['payee']}  {_money(u['amount'])}")
    colour = "green" if data["safe_to_spend"] >= 0 else "red"
    per_day = f", {_money(data['safe_per_day'])}/day" if data["safe_per_day"] is not None else ""
    console.print(f"[{colour}]Safe to spend until {data['next_payday']}: {_money(data['safe_to_spend'])}{per_day}[/]"
                  f"  [dim]({data['basis']})[/]")
    if data["pay_missing"]:
        console.print("[yellow]A pay looks overdue; the latest export may be missing it.[/]")
    _table(["cycle", "spend", "income"],
           [[f"{c['start']} → {c['end']}", _money(c["spend"]), _money(c["income"])] for c in data["previous"]],
           right={1, 2}, title="Previous cycles")


# --------------------------------------------------------------------------- export


@app.command("export")
def export_cmd(
    fmt: str = typer.Argument(..., metavar="FORMAT", help="csv, hledger or beancount"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="File to write; default: print."),
    date_from: Optional[str] = typer.Option(None, "--from"),
    date_to: Optional[str] = typer.Option(None, "--to"),
    period: Optional[str] = typer.Option(None, "--period", help="As in kete spend, e.g. 'tax 2026'."),
    account: Optional[str] = typer.Option(None, "--account", "-a"),
):
    """Export transactions for an accountant or plain-text accounting tools."""
    import os

    from . import export as export_mod

    if period:
        try:
            date_from, date_to, _ = queries.parse_period(period)
        except ValueError as e:
            raise typer.BadParameter(str(e))
    try:
        text = export_mod.export(_open(), fmt.lower(), date_from, date_to, account)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    if out is None:
        sys.stdout.write(text)
        return
    out.write_text(text)
    os.chmod(out, 0o600)
    err.print(f"Wrote {out} (readable only by you). It holds your transactions: keep it off cloud folders.")


# --------------------------------------------------------------------------- csv formats


def _map_csv(path: Path) -> None:
    """Interactive: show an unrecognised CSV's columns and ask which is which."""
    from .parsers import formats as fm
    from .parsers.westpac_csv import ALIASES, _map_header, read_rows

    paths = get_paths()
    rows = read_rows(path)
    if fm.find_format(fm.load_formats(paths.formats), rows):
        err.print(f"{path.name}: already has a saved format.")
        return
    header_at = next((i for i, r in enumerate(rows[:15]) if sum(bool(c.strip()) for c in r) >= 2
                      and any(ch.isalpha() for ch in "".join(r))), 0)
    header = [c.strip() for c in rows[header_at]]
    sample = next((r for r in rows[header_at + 1:] if any(c.strip() for c in r)), [])
    err.print(f"[bold]{path.name}[/] columns:")
    for i, col in enumerate(header):
        err.print(f"  {i + 1:>2}. {col:<28} e.g. {sample[i] if i < len(sample) else ''}")
    guess = _map_header(header)

    def pick(field: str, required: bool = False) -> str | None:
        default = str(guess[field] + 1) if field in guess else ("" if not required else "1")
        while True:
            ans = typer.prompt(f"Column number for {field}" + ("" if required else " (blank: none)"),
                               default=default, show_default=bool(default)).strip()
            if not ans and not required:
                return None
            if ans.isdigit() and 1 <= int(ans) <= len(header):
                return header[int(ans) - 1]
            err.print("Enter one of the numbers above.")

    columns = {"date": pick("date", True)}
    if typer.confirm("Is there a single amount column (rather than separate debit and credit)?",
                     default="amount" in guess or "debit" not in guess):
        columns["amount"] = pick("amount", True)
    else:
        columns["debit"], columns["credit"] = pick("debit", True), pick("credit", True)
    for f in ("payee", "description", "particulars", "code", "reference", "balance"):
        if f in ALIASES and (col := pick(f)):
            columns[f] = col
    negate = False
    if "amount" in columns:
        negate = not typer.confirm("Are payments (money out) negative numbers in this file?", default=True)
    dates = [r[header.index(columns["date"])] for r in rows[header_at + 1:header_at + 30]
             if len(r) > header.index(columns["date"]) and r[header.index(columns["date"])].strip()]
    date_format = None
    from .parsers.common import ParseError, parse_date
    try:
        for d in dates:
            parse_date(d)
    except ParseError:
        fits = [f for f in fm.DATE_FORMATS if all(_fits(d, f) for d in dates)]
        if not fits:
            err.print("[red]Couldn't read the dates in that column.[/]")
            raise typer.Exit(1)
        err.print("Dates: " + ", ".join(f"{i + 1}. {f}" for i, f in enumerate(fits)))
        date_format = fits[int(typer.prompt("Which date format", default="1")) - 1]
    name = typer.prompt("Name for this layout (e.g. visa, anz)").strip().lower() or "custom"
    fm.save_format(paths.formats, fm.CsvFormat(name=name, header=header, columns=columns, negate=negate,
                                               date_format=date_format))
    err.print(f"Saved to {paths.formats}. Files with this header now import automatically.")


def _fits(text: str, fmt: str) -> bool:
    try:
        datetime.strptime(text.strip(), fmt)
        return True
    except ValueError:
        return False


@formats_app.callback(invoke_without_command=True)
def formats_list(ctx: typer.Context, as_json: bool = JsonOpt):
    """Saved CSV layouts."""
    if ctx.invoked_subcommand:
        return
    from .parsers import formats as fm

    data = fm.load_formats(get_paths().formats)
    if as_json:
        return _emit_json([{"name": f.name, "header": f.header, "columns": f.columns, "negate": f.negate,
                            "date_format": f.date_format} for f in data])
    if not data:
        console.print("No saved layouts. Add one with [bold]kete import --map FILE[/] or [bold]kete formats add[/].")
        return
    _table(["name", "columns", "negate", "dates"],
           [[f.name, ", ".join(f"{k}={v}" for k, v in f.columns.items()), "yes" if f.negate else "",
             f.date_format or "day-first"] for f in data])


@formats_app.command("add")
def formats_add(
    name: str,
    sample: Path = typer.Option(..., "--from", help="An export with this layout (only its header is read)."),
    date_col: str = typer.Option(..., "--date"),
    amount: Optional[str] = typer.Option(None, "--amount"),
    debit: Optional[str] = typer.Option(None, "--debit"),
    credit: Optional[str] = typer.Option(None, "--credit"),
    payee: Optional[str] = typer.Option(None, "--payee"),
    description: Optional[str] = typer.Option(None, "--description"),
    particulars: Optional[str] = typer.Option(None, "--particulars"),
    code: Optional[str] = typer.Option(None, "--code"),
    reference: Optional[str] = typer.Option(None, "--reference"),
    negate: bool = typer.Option(False, "--negate", help="Payments are positive numbers in this file."),
    date_format: Optional[str] = typer.Option(None, "--date-format", help="e.g. %m/%d/%Y; default day-first."),
):
    """Save a layout without prompts (scripts and agents; columns are header names)."""
    from .parsers import formats as fm
    from .parsers.westpac_csv import read_rows

    columns = {k: v for k, v in {"date": date_col, "amount": amount, "debit": debit, "credit": credit,
                                 "payee": payee, "description": description, "particulars": particulars,
                                 "code": code, "reference": reference}.items() if v}
    wanted = {c.strip().lower() for c in columns.values()}
    header = next((r for r in read_rows(sample)[:15] if wanted <= {c.strip().lower() for c in r}), None)
    if header is None:
        err.print(f"[red]No row in {sample.name} has all of those columns.[/]")
        raise typer.Exit(1)
    try:
        fm.save_format(get_paths().formats, fm.CsvFormat(name=name.lower(), header=[c.strip() for c in header],
                                                         columns=columns, negate=negate, date_format=date_format))
    except fm.ParseError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"Saved layout {name.lower()}.")


@formats_app.command("remove")
def formats_remove(name: str):
    """Delete a saved layout."""
    from .parsers import formats as fm

    if not fm.remove_format(get_paths().formats, name):
        err.print(f"[red]No layout named {name!r}.[/]")
        raise typer.Exit(1)
    console.print(f"Removed {name}.")


# --------------------------------------------------------------------------- autoimport


@autoimport_app.command("install")
def autoimport_install(folder: Path = typer.Option(Path("~/Downloads"), "--folder", help="Folder to watch.")):
    """Watch a folder and import bank exports as they arrive (a macOS LaunchAgent)."""
    from . import autoimport

    if sys.platform != "darwin":
        err.print("[red]Automatic import uses macOS launchd.[/]")
        raise typer.Exit(1)
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise typer.BadParameter(f"{folder} is not a folder")
    paths = get_paths()
    paths.ensure()
    target = autoimport.install(folder, paths)
    console.print(f"Watching {folder}. Exports downloaded there are imported within seconds "
                  f"and moved out of it.\nAgent: {target}\nLog: {paths.home / 'autoimport.log'}")
    console.print("[dim]If nothing happens, macOS may need to allow kete's Python to read that folder: "
                  "System Settings → Privacy & Security → Files and Folders.[/]")


@autoimport_app.command("uninstall")
def autoimport_uninstall():
    """Stop watching."""
    from . import autoimport

    console.print("Stopped." if autoimport.uninstall() else "Wasn't installed.")


@autoimport_app.command("status")
def autoimport_status(as_json: bool = JsonOpt):
    """Is automatic import set up?"""
    from . import autoimport

    data = autoimport.status()
    if as_json:
        return _emit_json(data)
    if not data["installed"]:
        console.print("Not installed. Set up with [bold]kete autoimport install[/].")
    else:
        console.print(f"Watching {data['folder']} ({'running' if data['loaded'] else 'not loaded'}).")


@autoimport_app.command("run")
def autoimport_run(folder: Path = typer.Option(Path("~/Downloads"), "--folder"),
                   quiet: bool = typer.Option(False, "--quiet", help="No notification.")):
    """What launchd runs: move recognisable exports into the inbox and import them."""
    from . import autoimport

    paths = get_paths()
    files = autoimport.candidates(folder.expanduser(), paths)
    if not files:
        return
    moved = autoimport.move_to_inbox(files, paths)
    conn = _open(readonly=False)
    results = [importer.import_file(conn, paths, f) for f in moved]
    new = sum(r.rows_new for r in results)
    stuck = [r.filename for r in results if r.status in ("skipped", "error")]
    for r in results:
        print(f"{datetime.now().isoformat(timespec='seconds')} {r.filename}: {r.status} "
              f"new={r.rows_new} dup={r.rows_dup}" + (f" error={r.error}" if r.error else ""))
    msg = f"{new} new transaction{'s' if new != 1 else ''} from {len(results) - len(stuck)} file(s)."
    if stuck:
        msg += f" {len(stuck)} need an account: run kete import."
    if not quiet:
        autoimport.notify("kete", msg)


# --------------------------------------------------------------------------- tui


@app.command()
def tui(private: bool = typer.Option(False, "--private", help="Start with amounts and payees hidden (h toggles).")):
    """Interactive dashboard."""
    _open().close()  # fail early with a helpful message if there's no database
    from .tui import KeteApp

    KeteApp(private=private).run()


if __name__ == "__main__":
    app()
