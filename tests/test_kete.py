import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from kete import db as dbmod
from kete import importer, queries, rules, transfers
from kete.parsers.common import ParseError, parse_amount, parse_date
from kete.paths import Paths

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(tmp_path):
    paths = Paths(tmp_path / "kete")
    paths.ensure()
    rules.ensure_rules_file(paths.rules)
    conn = dbmod.connect(paths.db)
    return paths, conn


def copy(paths, name, dest_name=None):
    dest = paths.inbox / (dest_name or name)
    shutil.copy(FIX / name, dest)
    return dest


def test_parse_date_is_day_first():
    assert parse_date("03/04/2026") == date(2026, 4, 3)
    assert parse_date("2026-04-03") == date(2026, 4, 3)
    with pytest.raises(ParseError):
        parse_date("04/31/2026")  # month-first input is rejected, not guessed


@pytest.mark.parametrize("text,cents", [
    ("-4.50", -450), ("$1,234.56", 123456), ("(4.50)", -450), ("12.00 DR", -1200), ("0.01", 1),
])
def test_parse_amount(text, cents):
    assert parse_amount(text) == cents


def test_csv_import_keeps_identical_same_day_rows(env):
    paths, conn = env
    res = importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    assert res.status == "imported" and res.account == "everyday"
    assert res.rows_new == 9 and res.rows_dup == 0
    coffees = conn.execute("SELECT COUNT(*) FROM transactions WHERE payee LIKE 'Cafe Express%'").fetchone()[0]
    assert coffees == 2
    # moved from inbox into archive/YYYY with a canonical name
    assert not (paths.inbox / "everyday_2026-09.csv").exists()
    assert Path(res.archived_to).name == "everyday_2026-09-01_2026-09-12.csv"


def test_overlapping_reimport_adds_only_new_rows(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    res = importer.import_file(conn, paths, copy(paths, "everyday_2026-09b.csv"))
    assert (res.rows_new, res.rows_dup) == (2, 4)
    # and the identical file again is skipped outright
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv", "everyday_again.csv"))
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert total == 11


def test_identical_file_is_skipped(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    res = importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    assert res.status == "duplicate-file"


def test_explicit_path_outside_inbox_is_copied_not_moved(env, tmp_path):
    paths, conn = env
    src = tmp_path / "everyday_x.csv"
    shutil.copy(FIX / "everyday_2026-09.csv", src)
    importer.import_file(conn, paths, src)
    assert src.exists()


def test_unknown_filename_asks_once_and_remembers(env):
    paths, conn = env
    asked = []

    def ask(name, choices, suggested):
        asked.append(name)
        return "everyday"

    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv", "Export_20260912.csv"), ask=ask)
    res = importer.import_file(conn, paths, copy(paths, "everyday_2026-09b.csv", "Export_20261006.csv"), ask=ask)
    assert asked == ["Export_20260912.csv"]
    assert res.account == "everyday" and res.rows_new == 2


def test_unknown_filename_without_answer_is_skipped(env):
    paths, conn = env
    res = importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv", "Export.csv"))
    assert res.status == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert (paths.inbox / "Export.csv").exists()


def test_raw_row_and_nz_fields_stored(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    row = conn.execute("SELECT * FROM transactions WHERE payee = 'Mercury Energy'").fetchone()
    assert (row["particulars"], row["code"], row["reference"]) == ("Test User", "A-0000", "13:48-22222")
    assert json.loads(row["raw_json"])["Other Party"] == "Mercury Energy"


def test_rules_categorise_and_mark_transfers(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    cats = dict(conn.execute("SELECT payee, category FROM transactions").fetchall())
    assert cats["NEW WORLD CITY"] == "Groceries"
    assert cats["Uber *eats Help.U 11"] == "Takeaways"
    assert cats["Uber *trip Help.U 01"] == "Transport"
    assert cats["To 0100-0000000-00"] == "Transfer"
    # manual categories survive a full re-apply
    conn.execute("UPDATE transactions SET category='Bills', category_source='manual' WHERE payee='Mercury Energy'")
    rules.apply_rules(conn, rules.load_rules(paths.rules))
    assert conn.execute("SELECT category FROM transactions WHERE payee='Mercury Energy'").fetchone()[0] == "Bills"


def test_rules_first_match_wins_and_validate(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('[[rule]]\npayee = "uber"\ncategory = "A"\n[[rule]]\npayee = "uber *eats"\ncategory = "B"\n')
    rs = rules.load_rules(p)
    assert rules.first_match(rs, {"payee": "Uber *eats"}).category == "A"
    p.write_text('[[rule]]\npayee = "x"\n')
    with pytest.raises(rules.RulesError):
        rules.load_rules(p)


def test_transfer_pairing_and_spend_exclusion(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    importer.import_file(conn, paths, copy(paths, "savings_2026-09.csv"))
    pairs = transfers.detect(conn)
    assert len(pairs) == 1
    p = pairs[0]
    assert (p["from_account"], p["to_account"], p["amount"]) == ("savings", "everyday", 200.0)
    transfers.apply(conn, pairs)
    # account numbers learned from the payee text of each leg
    nums = dict(conn.execute("SELECT key, number FROM accounts").fetchall())
    assert nums == {"everyday": "0100-0000000-02", "savings": "0100-0000000-01"}

    cf = {r["month"]: r for r in queries.cashflow(conn, 120)}["2026-09"]
    assert cf["income"] == pytest.approx(1850.00 + 0.12)  # the $200 top-up is not income
    s = queries.spend(conn, "2026-09")
    assert "Transfer" not in [r["name"] for r in s["rows"]]
    assert s["total"] == pytest.approx(12.40 + 5.50 * 2 + 86.20 + 18.99 + 120.00 + 23.10)
    # rules apply does not undo pairing
    rules.apply_rules(conn, rules.load_rules(paths.rules))
    assert len(transfers.detect(conn)) == 0


def test_ofx_matches_account_by_acctid_and_checks_balance(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    importer.import_file(conn, paths, copy(paths, "savings_2026-09.csv"))
    transfers.apply(conn, transfers.detect(conn))  # learns everyday = 0100-0000000-02
    res = importer.import_file(conn, paths, copy(paths, "westpac.ofx"))
    assert res.account == "everyday"
    # F001 and F002 already exist from the CSV (cross-format match); F003 is new
    assert (res.rows_new, res.rows_dup) == (1, 2)
    assert conn.execute("SELECT fitid FROM transactions WHERE payee LIKE 'Uber *trip%'").fetchone()[0] == "F002"
    bal = queries.latest_balance(conn, dbmod.find_account(conn, "everyday")["id"])
    assert bal["balance"] == 1000.00


def test_ofx_reimport_dedupes_on_fitid(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "westpac.ofx", "everyday_a.ofx"))
    text = (FIX / "westpac.ofx").read_text().replace("<NAME>Some Cafe", "<NAME>Some Cafe Ltd")
    (paths.inbox / "everyday_b.ofx").write_text(text)
    res = importer.import_file(conn, paths, paths.inbox / "everyday_b.ofx")
    assert (res.rows_new, res.rows_dup) == (0, 3)


def test_manual_balance_rolls_forward(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    uid = dbmod.find_account(conn, "everyday")["id"]
    conn.execute("INSERT INTO balances VALUES (?, '2026-09-05', 50000, 'manual')", (uid,))
    bal = queries.latest_balance(conn, uid)
    assert bal["balance"] == pytest.approx(500 - 120 - 30 - 23.10)


def test_subscriptions_detect_monthly(env):
    paths, conn = env
    uid = dbmod.get_or_create_account(conn, "everyday")
    for i, d in enumerate(["2026-06-05", "2026-07-05", "2026-08-05", "2026-09-04"]):
        conn.execute(
            "INSERT INTO transactions (account_id, posted_date, amount_cents, payee, dedupe_key, source, raw_json)"
            " VALUES (?, ?, -4600, ?, ?, 'test', '{}')", (uid, d, f"Streamflix {i + 10}", f"k{i}"))
    subs = queries.subscriptions(conn)
    assert subs[0]["payee"] == "Streamflix" and subs[0]["cadence"] == "monthly"


def test_payee_key_strips_day_numbers():
    assert queries.payee_key("Some Merchant 21") == queries.payee_key("Some Merchant  15")


def test_tui_smoke(env, monkeypatch):
    import asyncio

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09b.csv"))
    monkeypatch.setenv("KETE_HOME", str(paths.home))
    from kete.tui import KeteApp

    async def run():
        app = KeteApp()
        async with app.run_test(size=(140, 40)) as pilot:
            for key in "2345":
                await pilot.press(key)
            uncat = app.query_one("#uncat-table")
            assert uncat.row_count == 1  # Mystery Shop
            await pilot.press("4", "n")
            await pilot.pause()
            await pilot.press(*"Bills", "enter")
            await pilot.pause()
            assert uncat.row_count == 0
    asyncio.run(run())
    assert "Bills" in paths.rules.read_text()


def test_transfer_via_untracked_account_is_not_paired(env):
    paths, conn = env
    s, u = dbmod.get_or_create_account(conn, "savings"), dbmod.get_or_create_account(conn, "everyday")
    rows = [(s, -6000, "To 0100-0000000-00"), (u, 6000, "From 0100-0000000-00")]
    for i, (acct, cents, payee) in enumerate(rows):
        conn.execute(
            "INSERT INTO transactions (account_id, posted_date, amount_cents, payee, dedupe_key, source, raw_json)"
            " VALUES (?, '2026-09-01', ?, ?, ?, 'test', '{}')", (acct, cents, payee, f"v{i}"))
    assert transfers.detect(conn) == []


def test_pending_rows_are_replaced_by_next_export(env):
    paths, conn = env
    a = importer.import_file(conn, paths, copy(paths, "everyday_pending_a.csv"))
    assert (a.rows_new, a.rows_pending) == (3, 2)
    b = importer.import_file(conn, paths, copy(paths, "everyday_pending_b.csv"))
    assert (b.rows_new, b.rows_dup, b.rows_pending) == (2, 1, 0)
    payees = [r[0] for r in conn.execute("SELECT payee FROM transactions ORDER BY posted_date")]
    assert payees == ["Sushi Place 16", "Uber *trip Help.U 17", "Cafe Express 18"]
    s = queries.spend(conn, "2026-10")
    assert s["total"] == pytest.approx(18.40 + 11.20 + 5.50)  # not double counted


def _rows(*rows):
    head = "Date,Amount,Other Party,Description,Reference,Particulars,Analysis Code\n"
    return head + "".join(f'{d},{a},"{p}","DEBIT",,"999999******","0000"\n' for d, a, p in rows)


def test_any_number_of_accounts_by_prefix(env):
    paths, conn = env
    for key in ("joint", "visa-card", "kids"):
        dbmod.get_or_create_account(conn, key)
        f = paths.inbox / f"{key}_export.csv"
        f.write_text(_rows(("01/10/2026", "-5.00", f"Shop {key}")))
        res = importer.import_file(conn, paths, f)
        assert res.account == key and res.rows_new == 1
    assert len(queries.accounts(conn)) == 3


def test_unknown_prefix_does_not_create_account(env):
    paths, conn = env
    f = paths.inbox / "Export_20261001.csv"
    f.write_text(_rows(("01/10/2026", "-5.00", "Shop")))
    assert importer.import_file(conn, paths, f).status == "skipped"
    assert queries.accounts(conn) == []


def test_unlabelled_file_matched_by_overlap(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    importer.import_file(conn, paths, copy(paths, "savings_2026-09.csv"))

    def ask(*_):
        raise AssertionError("should not need to ask")

    res = importer.import_file(conn, paths, copy(paths, "everyday_2026-09b.csv", "Export_1.csv"), ask=ask)
    assert res.account == "everyday" and res.rows_new == 2


def test_shared_filename_pattern_asks_instead_of_guessing(env):
    paths, conn = env
    for key in ("joint", "kids"):
        dbmod.get_or_create_account(conn, key)
    calls = []

    def ask(name, choices, suggested):
        calls.append((name, suggested))
        return "joint" if name.endswith("1.csv") else "kids"

    a = paths.inbox / "Export_1.csv"
    a.write_text(_rows(("01/10/2026", "-5.00", "Joint shop")))
    importer.import_file(conn, paths, a, ask=ask)
    b = paths.inbox / "Export_2.csv"
    b.write_text(_rows(("02/10/2026", "-7.00", "Kids shop")))
    res = importer.import_file(conn, paths, b, ask=ask)
    assert res.account == "kids"
    assert calls == [("Export_1.csv", None), ("Export_2.csv", "joint")]  # remembered = suggestion only


def test_forced_account(env):
    paths, conn = env
    dbmod.get_or_create_account(conn, "joint")
    f = paths.inbox / "everyday_misnamed.csv"
    f.write_text(_rows(("01/10/2026", "-5.00", "Shop")))
    assert importer.import_file(conn, paths, f, account="joint").account == "joint"


def test_transfers_across_three_accounts(env):
    paths, conn = env
    ids = {k: dbmod.get_or_create_account(conn, k) for k in ("savings", "everyday", "joint")}
    rows = [("savings", -5000, "To 0100-0000000-03"), ("joint", 5000, "From 0100-0000000-01"),
            ("everyday", -2000, "To 0100-0000000-01"), ("savings", 2000, "From 0100-0000000-02")]
    for n, (k, cents, payee) in enumerate(rows):
        conn.execute(
            "INSERT INTO transactions (account_id, posted_date, amount_cents, payee, dedupe_key, source, raw_json)"
            " VALUES (?, '2026-10-01', ?, ?, ?, 'test', '{}')", (ids[k], cents, payee, f"t{n}"))
    pairs = transfers.detect(conn)
    assert {(p["from_account"], p["to_account"]) for p in pairs} == {("savings", "joint"), ("everyday", "savings")}


def test_cli_account_commands(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kete.cli import app

    monkeypatch.setenv("KETE_HOME", str(tmp_path / "k"))
    run = lambda *a: CliRunner().invoke(app, list(a))
    assert run("accounts", "add", "joint", "--type", "savings").exit_code == 0
    assert run("accounts", "add", "joint").exit_code == 1
    assert run("import", "-a", "nope", str(FIX / "everyday_2026-09.csv")).exit_code == 1
    assert run("import", "-a", "joint", str(FIX / "everyday_2026-09.csv")).exit_code == 0
    assert run("accounts", "edit", "joint", "--rename", "shared", "--number", "0100-0000000-09").exit_code == 0
    assert run("accounts", "add", "other", "--number", "0100-0000000-09").exit_code == 1
    assert run("accounts", "remove", "shared").exit_code == 1  # has transactions
    data = json.loads(run("accounts", "--json").output)
    assert [(a["key"], a["type"], a["transactions"]) for a in data] == [("shared", "savings", 9)]


def test_tui_back_from_category_drilldown(env, monkeypatch):
    import asyncio

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    monkeypatch.setenv("KETE_HOME", str(paths.home))
    from kete.tui import KeteApp

    async def run():
        app = KeteApp()
        app.cat_month = "2026-09"
        async with app.run_test(size=(140, 40)) as pilot:
            app.load_categories()
            await pilot.press("3", "down", "enter")
            await pilot.pause()
            tabs = app.query_one("TabbedContent")
            assert tabs.active == "transactions"
            assert app.query_one("#tx-filter").value.startswith("month:2026-09 cat:")
            await pilot.press("escape")
            await pilot.pause()
            assert tabs.active == "categories"
            assert app.query_one("#cat-table").cursor_row == 1
            assert app.query_one("#tx-filter").value == ""
            await pilot.press("escape")  # no-op when not drilled down
            assert tabs.active == "categories"
    asyncio.run(run())


def test_canonical_category_and_rule_spellings(env, tmp_path):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    known = rules.known_categories(conn, rules.load_rules(paths.rules))
    assert "Fees" in known  # rule-only category (no matching transactions) is suggested
    assert rules.canonical_category("  groceries ", known) == "Groceries"
    assert rules.canonical_category("Pets", known) == "Pets"
    p = tmp_path / "r.toml"
    p.write_text('[[rule]]\npayee = "a"\ncategory = "Food"\n[[rule]]\npayee = "b"\ncategory = "food"\n')
    assert [r.category for r in rules.load_rules(p)] == ["Food", "Food"]


def test_rename_category_updates_transactions_and_rules_file(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    conn.execute("UPDATE transactions SET category='groceries', category_source='manual' "
                 "WHERE payee LIKE 'Cafe Express%'")
    conn.commit()
    before = paths.rules.read_text()
    res = rules.rename_category(conn, paths.rules, "Groceries", "Food")
    assert res == {"from": "Groceries", "to": "Food", "transactions": 3, "rules": 1}
    after = paths.rules.read_text()
    assert 'category = "Food"' in after
    assert not any(l.startswith("category") and "Groceries" in l for l in after.splitlines())
    assert after.count("\n") == before.count("\n") and "# kete categorisation rules." in after
    rules.apply_rules(conn, rules.load_rules(paths.rules))
    cats = {r[0] for r in conn.execute("SELECT category FROM transactions WHERE category IS NOT NULL")}
    assert "Food" in cats and not {"Groceries", "groceries"} & cats
    # renaming onto an existing category merges, using its spelling
    res = rules.rename_category(conn, paths.rules, "Takeaways", "food")
    assert res["to"] == "Food"
    with pytest.raises(rules.RulesError):
        rules.rename_category(conn, paths.rules, "Food", "Transfer")


def test_cli_categories(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kete.cli import app

    monkeypatch.setenv("KETE_HOME", str(tmp_path / "k"))
    run = lambda *a: CliRunner().invoke(app, list(a))
    run("import", str(FIX / "everyday_2026-09.csv"))
    data = {d["category"]: d for d in json.loads(run("categories", "--json").output)}
    assert data["Groceries"]["transactions"] == 1 and data["Groceries"]["rules"] == 1
    assert run("categories", "rename", "Groceries", "Supermarket").exit_code == 0
    assert run("categories", "rename", "Nope", "X").exit_code == 1
    assert "Supermarket" in run("categories").output


# --------------------------------------------------------------------------- dashboard


def test_dashboard_demo_model_shape():
    from kete.dashboard import build_model
    from kete.demo import build_demo_db

    m = build_model(build_demo_db(today=date(2026, 10, 19)), today=date(2026, 10, 19), demo=True)
    assert m["demo"] and len(m["monthly"]) == 12 and m["monthly"][-1]["status"] == "partial"
    assert len(m["series"]) <= 8 and len(m["daily"]) % 7 in range(7)
    assert m["kpis"]["avg_monthly_spend"] > 0 and m["kpis"]["savings_rate"] is not None
    kinds = {o["kind"] for o in m["opportunities"]}
    assert {"cut", "cancel"} <= kinds
    # transfers never count as spending, and fixed costs are never "subscriptions to cancel"
    assert "Transfer" not in m["series"]
    assert all(s["category"] not in ("Rent", "Groceries") for s in m["subscriptions"])
    # category totals reconcile with monthly spend
    for mo in m["monthly"]:
        assert sum(mo["by_category"].values()) == pytest.approx(mo["spend"], abs=0.05)
        assert sum(mo["by_series"].values()) == pytest.approx(mo["spend"], abs=0.05)


def test_dashboard_render_is_self_contained(tmp_path):
    from kete.dashboard import generate
    from kete.demo import build_demo_db

    out = generate(build_demo_db(), tmp_path / "d.html", demo=True)
    html = out.read_text()
    assert "default-src 'none'" in html  # CSP: no network
    assert "__DATA__" not in html and "/*__SCRIPT__*/" not in html
    stripped = html.replace("http://www.w3.org/2000/svg", "")  # a namespace name, never fetched
    assert not any(x in stripped for x in ("http://", "https://", "src=\"//")), "no external resources"
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_dashboard_escapes_script_breakout(env, tmp_path):
    from kete.dashboard import generate

    paths, conn = env
    f = paths.inbox / "everyday_x.csv"
    f.write_text('Date,Amount,Other Party,Description,Reference,Particulars,Analysis Code\n'
                 '01/10/2026,-5.00,"</script><script>alert(1)</script>","DEBIT",,,\n')
    importer.import_file(conn, paths, f)
    html = generate(conn, tmp_path / "d.html").read_text()
    assert "</script><script>alert(1)" not in html


def test_dashboard_config_from_rules_toml(tmp_path):
    from kete.dashboard import load_config

    p = tmp_path / "rules.toml"
    p.write_text('[dashboard]\ninterest_rate = 0.05\ndiscretionary = ["Fun"]\nunknown = 1\n')
    cfg = load_config(p)
    assert cfg["interest_rate"] == 0.05 and cfg["discretionary"] == ["Fun"] and "unknown" not in cfg


def test_cli_dashboard_demo(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kete.cli import app

    monkeypatch.setenv("KETE_HOME", str(tmp_path / "k"))
    out = tmp_path / "demo.html"
    r = CliRunner().invoke(app, ["dashboard", "--demo", "--no-open", "--out", str(out)])
    assert r.exit_code == 0 and out.exists()
    assert not (tmp_path / "k").exists()  # demo never touches the real data folder
