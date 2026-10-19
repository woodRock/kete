"""kete command line. Every read command supports --json so scripts, agents and the TUI agree."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db as dbmod
from . import importer, queries, rules as rules_mod, transfers
from .paths import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
rules_app = typer.Typer(no_args_is_help=True, help="Categorisation rules (rules.toml).")
transfers_app = typer.Typer(no_args_is_help=True, help="Transfers between your own accounts.")
balance_app = typer.Typer(no_args_is_help=True, help="Balance anchors (CSV exports carry none).")
accounts_app = typer.Typer(help="List, add and edit accounts.")
app.add_typer(accounts_app, name="accounts")
categories_app = typer.Typer(help="List and rename categories.")
app.add_typer(categories_app, name="categories")
app.add_typer(rules_app, name="rules")
app.add_typer(transfers_app, name="transfers")
app.add_typer(balance_app, name="balance")

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
    as_json: bool = JsonOpt,
):
    """Import Westpac exports (OFX or CSV). Re-importing overlapping files is safe."""
    paths = get_paths()
    conn = _open(readonly=False)
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
    by: str = typer.Option("cat", "--by", help="cat or payee"),
    account: Optional[str] = typer.Option(None, "--account", "-a"),
    as_json: bool = JsonOpt,
):
    """Spending totals for a month. Transfers are excluded."""
    if by not in ("cat", "payee"):
        raise typer.BadParameter("--by must be cat or payee")
    data = queries.spend(_open(), month, by, account)
    if as_json:
        return _emit_json(data)
    _table(["category" if by == "cat" else "payee", "total", "count"],
           [[r["name"], _money(r["total"]), r["count"]] for r in data["rows"]],
           right={1, 2}, title=f"Spend {data['month']}: {_money(data['total'])}")


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
    _table(["payee", "cadence", "typical", "≈/month", "count", "last", "next"],
           [[s["payee"], s["cadence"], _money(s["typical_amount"]), _money(s["monthly_cost"]),
             s["count"], s["last_date"], s["next_expected"]] for s in data],
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


# --------------------------------------------------------------------------- tui


@app.command()
def tui():
    """Interactive dashboard."""
    _open().close()  # fail early with a helpful message if there's no database
    from .tui import KeteApp

    KeteApp().run()


if __name__ == "__main__":
    app()
