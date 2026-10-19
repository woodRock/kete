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
    run("accounts", "add", "rent")
    r = run("accounts", "edit", "rent", "--number", "0100-0000000-09")
    assert r.exit_code == 1 and "on shared" in r.output and "--number none" in r.output
    assert run("accounts", "edit", "shared", "--number", "none").exit_code == 0
    assert run("accounts", "edit", "rent", "--number", "0100-0000000-09").exit_code == 0
    run("accounts", "edit", "rent", "--number", "none")
    run("accounts", "edit", "shared", "--number", "0100-0000000-09")
    run("accounts", "remove", "rent")
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


def _charges(conn, rows):
    acct = dbmod.get_or_create_account(conn, "everyday")
    for i, (d, cents, payee) in enumerate(rows):
        conn.execute(
            "INSERT INTO transactions (account_id, posted_date, amount_cents, payee, dedupe_key, source, raw_json)"
            " VALUES (?, ?, ?, ?, ?, 'test', '{}')", (acct, d, cents, payee, f"s{payee}{i}{d}"))


def _sub_names(conn, today=date(2026, 10, 19), **kw):
    return {s["payee"]: s for s in queries.subscriptions(conn, today=today, **kw) if not s.get("rejected")}


def test_subscription_survives_price_change(env):
    _, conn = env
    _charges(conn, [("2026-04-05", -4000, "Streamflix"), ("2026-05-05", -4000, "Streamflix"),
                    ("2026-06-05", -4000, "Streamflix"), ("2026-07-05", -17600, "Streamflix"),
                    ("2026-08-05", -17600, "Streamflix"), ("2026-09-05", -17600, "Streamflix"),
                    ("2026-10-05", -17600, "Streamflix")])
    s = _sub_names(conn)["Streamflix"]
    assert s["typical_amount"] == 176.0 and s["count"] == 4


def test_subscription_merges_payee_spellings(env):
    _, conn = env
    _charges(conn, [("2026-06-14", -4600, "Cloudco 13"), ("2026-07-14", -4600, "Cloudco* Pro"),
                    ("2026-08-14", -4600, "Cloudco 13"), ("2026-09-14", -4600, "Cloudco* Pro"),
                    ("2026-10-14", -4600, "Cloudco 14")])
    subs = _sub_names(conn)
    assert len(subs) == 1
    (s,) = subs.values()
    assert s["cadence"] == "monthly" and s["count"] == 5


def test_two_identical_monthly_charges_count_but_variable_shopping_does_not(env):
    _, conn = env
    _charges(conn, [("2026-08-20", -1299, "New App"), ("2026-09-20", -1299, "New App")])
    _charges(conn, [(f"2026-{m:02d}-{d:02d}", -c, "Big Supermarket") for m, d, c in
                    [(7, 4, 15200), (7, 11, 11800), (7, 18, 16900), (7, 25, 12100), (8, 1, 17500),
                     (8, 8, 10900), (8, 15, 14800), (8, 22, 12300)]])
    subs = _sub_names(conn)
    assert subs["New App"]["confidence"] == "low"
    assert "Big Supermarket" not in subs


def test_same_day_duplicate_charge_does_not_break_detection(env):
    _, conn = env
    _charges(conn, [("2026-07-16", -4600, "Seatco"), ("2026-08-16", -4600, "Seatco"),
                    ("2026-09-16", -4600, "Seatco"), ("2026-10-16", -4600, "Seatco"),
                    ("2026-10-16", -4600, "Seatco")])
    assert _sub_names(conn)["Seatco"]["typical_amount"] == 46.0


def test_subs_explain_lists_near_misses(env):
    _, conn = env
    _charges(conn, [("2026-08-01", -999, "Gamepass"), ("2026-09-02", -1499, "Gamepass")])
    rows = queries.subscriptions(conn, today=date(2026, 10, 19), explain=True)
    miss = [r for r in rows if r.get("rejected")]
    assert miss and miss[0]["payee"] == "Gamepass" and "amount" in miss[0]["reason"]


def test_dashboard_lists_bills_but_not_rent():
    from kete.dashboard import build_model
    from kete.demo import build_demo_db

    today = date(2026, 10, 19)
    m = build_model(build_demo_db(today=today), today=today, demo=True)
    subs = {s["payee"]: s for s in m["subscriptions"]}
    assert "Harbour Property" not in subs  # rent is recurring but never "cancellable"
    assert subs["One NZ Mobile"]["bill"] and not subs["Netflix.com"]["bill"]
    assert "Spotify" in subs and "CityGym Membership" in subs


def test_tui_change_month_keys_and_buttons(env, monkeypatch):
    import asyncio

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    monkeypatch.setenv("KETE_HOME", str(paths.home))
    from kete.tui import KeteApp

    async def run():
        app = KeteApp()
        app.cat_month = "2026-10"
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("2")
            app.query_one("#tx-filter").focus()  # focus left in another tab's filter box
            await pilot.click("#tabs-list Tab#--content-tab-categories")
            await pilot.pause()
            assert app.focused.id == "cat-table"
            await pilot.press("left_square_bracket")
            assert app.cat_month == "2026-09"
            assert app.query_one("#tx-filter").value == ""
            await pilot.click("#next-month")
            await pilot.pause()
            assert app.cat_month == "2026-10"
            await pilot.click("#prev-month")
            await pilot.pause()
            assert app.cat_month == "2026-09"
            assert app.query_one("#cat-table").row_count > 0
    asyncio.run(run())


# --------------------------------------------------------------------------- reports

TODAY = date(2026, 10, 19)


@pytest.fixture(scope="module")
def demo_report():
    from kete import report as rep
    from kete.demo import build_demo_db

    conn = build_demo_db(today=TODAY)
    kind, start, end, label = rep.parse_period(today=TODAY)
    return conn, rep.build_report(conn, kind, start, end, label, today=TODAY, demo=True)


def _by_detector(model):
    out = {}
    for f in model["findings"]:
        out.setdefault(f["detector"], []).append(f)
    return out


def test_parse_period():
    from kete.report import parse_period

    assert parse_period(today=TODAY)[1:3] == (date(2026, 9, 1), date(2026, 9, 30))
    assert parse_period(month="2026-02", today=TODAY)[1:3] == (date(2026, 2, 1), date(2026, 2, 28))
    assert parse_period(tax_year=2026)[1:] == (date(2025, 4, 1), date(2026, 3, 31), "Tax year 2025–26")
    with pytest.raises(ValueError):
        parse_period(month="Sept")


def test_report_finds_each_planted_issue(demo_report):
    _, m = demo_report
    found = _by_detector(m)
    assert any("Bike Shop" in f["title"] for f in found["duplicate_charge"])
    assert any("COUNTDOWN" in f["title"] for f in found["unusual_charge"])
    assert any("Noel Leeming" in f["title"] for f in found["new_merchant"])
    price = next(f for f in found["price_rise"] if "Netflix" in f["title"])
    assert price["impact_yearly"] == pytest.approx(48.0, abs=1)
    assert any("Fitness Plus" in f["title"] for f in found["new_subscription"])
    assert found["fees"][0]["evidence"]
    assert any(f["title"].startswith("Shopping") for f in found["category_change"])
    # every finding is explainable and has a stable id
    for f in m["findings"]:
        assert f["id"] and f["title"] and f["severity"] in ("high", "medium", "low", "good")
    assert "price_rise" not in {f["detector"] for f in m["findings"] if "NEW WORLD" in f["title"]}


def test_report_summary_and_comparison(demo_report):
    _, m = demo_report
    assert m["period"]["complete"] and m["previous"]["label"] == "August 2026"
    assert m["summary"]["spend"] == pytest.approx(sum(c["spend"] for c in m["categories"]), abs=0.05)
    assert all(t["amount"] < 0 for t in m["largest"])


def test_report_ids_are_stable_and_dismissals_stick(env):
    from kete import report as rep
    from kete.demo import build_demo_db

    conn = build_demo_db(today=TODAY)
    kind, start, end, label = rep.parse_period(today=TODAY)
    first = rep.build_report(conn, kind, start, end, label, today=TODAY)
    again = rep.build_report(conn, kind, start, end, label, today=TODAY)
    assert [f["id"] for f in first["findings"]] == [f["id"] for f in again["findings"]]
    target = first["findings"][0]["id"]
    conn.execute("INSERT INTO dismissals VALUES (?, '2026-10-19')", (target,))
    after = rep.build_report(conn, kind, start, end, label, today=TODAY)
    assert next(f for f in after["findings"] if f["id"] == target)["dismissed"]


def test_tax_year_interest_from_westpac_fields(env):
    from kete import report as rep

    paths, conn = env
    f = paths.inbox / "savings_x.csv"
    f.write_text('Date,Amount,Other Party,Description,Reference,Particulars,Analysis Code\n'
                 '30/04/2025,0.39,"Bonus Interest","Credit Interest","$0.05","$0.44","Less W/Tax"\n'
                 '31/05/2025,0.01,"Credit Interest","Credit Interest","","$0.01","Exempt"\n')
    importer.import_file(conn, paths, f)
    kind, start, end, label = rep.parse_period(tax_year=2026)
    tax = rep.build_report(conn, kind, start, end, label, today=TODAY)["tax"]
    assert tax == {"payments": 2, "gross": 0.45, "rwt": 0.05, "net": 0.40}


def test_unfinished_period_compares_same_days():
    from kete import report as rep
    from kete.demo import build_demo_db

    conn = build_demo_db(today=TODAY)
    m = rep.build_report(conn, "year", date(2026, 1, 1), date(2026, 12, 31), "2026", today=TODAY)
    assert m["previous"]["end"] == "2025-10-19" and m["previous"]["partial"]
    assert not m["period"]["complete"]


def test_new_merchant_needs_history(env):
    from kete import report as rep

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))  # all history in one month
    m = rep.build_report(conn, "month", date(2026, 9, 1), date(2026, 9, 30), "Sep", today=TODAY)
    assert "new_merchant" not in {f["detector"] for f in m["findings"]}


def test_report_render_is_self_contained(tmp_path):
    from kete import report as rep
    from kete.demo import build_demo_db

    kind, start, end, label = rep.parse_period(today=TODAY)
    out = rep.generate(build_demo_db(today=TODAY), tmp_path / "r.html", kind, start, end, label, today=TODAY, demo=True)
    html = out.read_text()
    assert "default-src 'none'" in html and "__DATA__" not in html and "/*__SCRIPT__*/" not in html
    stripped = html.replace("http://www.w3.org/2000/svg", "")
    assert "http://" not in stripped and "https://" not in stripped
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_cli_report_and_dismiss(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kete.cli import app

    monkeypatch.setenv("KETE_HOME", str(tmp_path / "k"))
    run = lambda *a: CliRunner().invoke(app, list(a))
    out = tmp_path / "demo.html"
    assert run("report", "--demo", "--no-open", "--out", str(out)).exit_code == 0 and out.exists()
    assert not (tmp_path / "k").exists()  # demo never touches the real data folder
    assert run("report", "--month", "2026-09", "--year", "2026", "--demo").exit_code != 0
    run("import", str(FIX / "everyday_2026-09.csv"))
    assert run("report", "dismiss", "fees:2026-09-01:2026-09-30").exit_code == 0
    assert run("report", "restore", "fees:2026-09-01:2026-09-30").exit_code == 0
    r = run("report", "--month", "2026-09", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["period"]["label"] == "September 2026"


# ----------------------------------------------------------------------------- privacy guards

SRC = Path(__file__).parent.parent / "src" / "kete"
NETWORK_MODULES = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "ftplib",
                   "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "websocket", "websockets",
                   "asyncio.streams", "email.mime"}


def _imports(path):
    import ast

    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def test_kete_code_has_no_network_access():
    """kete promises your data never leaves the machine: none of its code may import networking."""
    offenders = []
    for path in SRC.rglob("*.py"):
        for name in _imports(path):
            if name in NETWORK_MODULES or name.split(".")[0] in NETWORK_MODULES:
                offenders.append(f"{path.relative_to(SRC)}: {name}")
            if name.split(".")[0] == "subprocess" and path.name != "autoimport.py":
                offenders.append(f"{path.relative_to(SRC)}: subprocess (only autoimport may run commands)")
    assert not offenders, offenders


def test_pages_make_no_network_requests():
    import re

    banned = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)\b|import\(|"
                        r"https?://(?!www\.w3\.org/2000/svg)")
    offenders = [f"{p.relative_to(SRC)}: {m.group(0)}"
                 for p in list(SRC.rglob("*.js")) + list(SRC.rglob("*.html")) + list(SRC.rglob("*.css"))
                 for m in banned.finditer(p.read_text())]
    assert not offenders, offenders


# ----------------------------------------------------------------------------- new features

def _cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kete.cli import app

    monkeypatch.setenv("KETE_HOME", str(tmp_path / "k"))
    return lambda *a: CliRunner().invoke(app, [str(x) for x in a])


def _ids(run, *args):
    return [r["id"] for r in json.loads(run("tx", "--json", "-n", "0", *args).output)]


def test_add_categorise_undo_and_history(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    run("import", FIX / "everyday_2026-09.csv")
    assert run("add", "cash", "-5", "Market").exit_code == 1  # no such account
    assert run("accounts", "add", "cash", "--type", "other").exit_code == 0
    r = run("add", "cash", "-12.50", "Mystery Shop", "--date", "2026-09-20", "--json")
    added = json.loads(r.output)
    assert added["amount"] == -12.5 and added["category"] is None
    first = _ids(run, "-s", "Uber")
    assert run("categorise", "Transport", *first).exit_code == 0
    cats = {r["category"] for r in json.loads(run("tx", "--json", "-s", "Uber").output)}
    assert cats == {"Transport"}
    hist = json.loads(run("history", "--json").output)
    assert [h["action"] for h in hist] == ["categorise", "add"]
    assert run("undo").exit_code == 0  # categorise
    cats = {r["category"] for r in json.loads(run("tx", "--json", "-s", "Uber").output)}
    assert cats == {"Takeaways", "Transport"}  # back to what the rules said
    assert run("undo").exit_code == 0  # add
    assert not _ids(run, "-s", "Mystery")
    assert "Nothing to undo" in run("undo").output


def test_undo_restores_rules_file_and_refuses_after_hand_edits(env):
    from kete import history

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    before = paths.rules.read_text()
    with history.tracked(conn, "rename", "Takeaways → Food", paths.rules):
        rules.rename_category(conn, paths.rules, "Takeaways", "Food")
    assert "Food" in paths.rules.read_text()
    assert history.undo(conn)["rules_file"]
    assert paths.rules.read_text() == before
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category = 'Takeaways'").fetchone()[0] == 1

    with history.tracked(conn, "rename", "again", paths.rules):
        rules.rename_category(conn, paths.rules, "Takeaways", "Food")
    paths.rules.write_text(paths.rules.read_text() + "\n# my own note\n")
    with pytest.raises(history.UndoError):
        history.undo(conn)
    assert history.undo(conn, force=True)


def test_rule_suggestions_learn_from_manual_categories(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    run("accounts", "add", "cash", "--type", "other")
    for day in ("01", "08"):
        run("add", "cash", "-9.99", f"Mystery Shop {day}", "--date", f"2026-09-{day}")
    first = _ids(run, "-s", "Mystery")
    r = json.loads(run("categorise", "Hobbies", first[0], "--json").output)
    assert r["rule_suggestions"] == []  # once isn't a pattern
    r = json.loads(run("categorise", "hobbies", first[1], "--json").output)
    assert [(s["payee"], s["category"], s["count"]) for s in r["rule_suggestions"]] == [("Mystery Shop", "Hobbies", 2)]
    assert run("rules", "suggest", "--apply").exit_code == 0
    assert 'payee = "Mystery Shop"' in (tmp_path / "k" / "rules.toml").read_text()
    run("add", "cash", "-4", "Mystery Shop 15", "--date", "2026-09-15")
    assert {r["category"] for r in json.loads(run("tx", "--json", "-s", "Mystery").output)} == {"Hobbies"}
    assert json.loads(run("rules", "suggest", "--json").output)["suggestions"] == []


def test_declined_suggestion_is_not_offered_again(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09b.csv"))
    ids = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE payee LIKE 'Netflix%'")]
    rules.set_category(conn, ids, "Streaming")  # overrides the Entertainment rule, so suggest beating it
    loaded = rules.load_rules(paths.rules)
    ent = next(r.index for r in loaded if r.category == "Entertainment")
    assert [(s["payee"], s["before"]) for s in rules.suggest_rules(conn, loaded)] == [("Netflix.com", ent)]
    rules.decline_suggestion(conn, "Netflix.com 03")
    mystery = conn.execute("SELECT id FROM transactions WHERE payee = 'Mystery Shop'").fetchone()[0]
    rules.set_category(conn, [mystery], "Hobbies")
    loaded = rules.load_rules(paths.rules)
    assert [s["payee"] for s in rules.suggest_rules(conn, loaded, min_count=1)] == ["Mystery Shop"]
    rules.decline_suggestion(conn, "Mystery Shop")
    assert rules.suggest_rules(conn, loaded, min_count=1) == []


def test_shared_costs_count_only_your_part(env):
    from kete import shares

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    power = conn.execute("SELECT id FROM transactions WHERE payee = 'Mercury Energy'").fetchone()[0]
    before = queries.spend(conn, "2026-09")["total"]
    shares.set_share(conn, power, "Sam", "half")
    assert queries.spend(conn, "2026-09")["total"] == round(before - 60, 2)
    with pytest.raises(shares.ShareError):
        shares.set_share(conn, power, "Alex", "70")  # only $60 left to share
    shares.set_share(conn, power, "alex", "1/4")
    assert {b["person"]: b["balance"] for b in shares.balances(conn)} == {"Sam": 60.0, "alex": 30.0}
    # Sam pays back $60: not income, and Sam is square
    conn.execute("""INSERT INTO transactions (account_id, posted_date, amount_cents, payee, dedupe_key, source, raw_json)
                    VALUES (1, '2026-09-20', 6000, 'S SMITH', 'x1', 'manual', '{}')""")
    repay = conn.execute("SELECT id FROM transactions WHERE dedupe_key = 'x1'").fetchone()[0]
    income_before = queries.cashflow(conn, 120)
    shares.set_share(conn, repay, "SAM")
    assert {b["person"] for b in shares.balances(conn)} == {"alex"}
    sep = lambda flow: next(m for m in flow if m["month"] == "2026-09")["income"]  # noqa: E731
    assert sep(queries.cashflow(conn, 120)) == sep(income_before) - 60
    assert shares.parse_amount_spec("25%", -8000) == 2000 and shares.parse_amount_spec("$12.50", -8000) == 1250
    with pytest.raises(shares.ShareError):
        shares.set_share(conn, conn.execute("SELECT id FROM transactions WHERE is_transfer = 1").fetchone()[0], "Sam")


def test_pay_cycle_on_demo_data():
    from kete import paycycle
    from kete.demo import build_demo_db

    conn = build_demo_db(today=TODAY)
    pay = paycycle.find_payday(conn, today=TODAY)
    assert pay["payee"] == "Acme Ltd" and pay["cadence"] == "fortnightly" and pay["typical"] == 2450.0
    c = paycycle.cycles(conn, today=TODAY)
    assert c["current"]["current"] and c["current"]["start"] == pay["paydays"][-1]
    assert date.fromisoformat(c["next_payday"]) > TODAY and c["days_left"] > 0
    assert len(c["previous"]) == 6 and c["average_spend"] > 0
    expected = 2450.0 - c["current"]["spend"] - sum(u["amount"] for u in c["upcoming"])
    assert c["safe_to_spend"] == round(expected, 2)
    saving = paycycle.cycles(conn, {"save_per_cycle": 300}, today=TODAY)
    assert saving["safe_to_spend"] == round(expected - 300, 2)
    assert paycycle.find_payday(conn, {"payee": "nobody"}, today=TODAY) is None


def test_period_parsing_and_year_comparison(tmp_path, monkeypatch):
    assert queries.parse_period("tax 2026") == ("2025-04-01", "2026-03-31", "tax year 2025–26")
    assert queries.parse_period("2026-01..2026-03")[:2] == ("2026-01-01", "2026-03-31")
    assert queries.parse_period("2026-02")[:2] == ("2026-02-01", "2026-02-28")
    assert queries.parse_period("last 7 days", today=TODAY)[:2] == ("2026-10-13", "2026-10-19")
    with pytest.raises(ValueError):
        queries.parse_period("2026-03..2026-01")
    with pytest.raises(ValueError):
        queries.parse_period("soon")
    run = _cli(tmp_path, monkeypatch)
    run("import", FIX / "everyday_2026-09.csv")
    r = json.loads(run("spend", "--period", "2026-09", "--compare", "--json").output)
    assert r["from"] == "2026-09-01" and r["to"] == "2026-09-30" and r["previous"]["total"] is not None
    assert run("spend", "--from", "2026-09-01", "--to", "2026-09-05").exit_code == 0


def test_saved_csv_format_for_another_card(env, tmp_path, monkeypatch):
    from kete.parsers import formats

    paths, conn = env
    run = _cli(tmp_path, monkeypatch)
    monkeypatch.setenv("KETE_HOME", str(paths.home))
    run("accounts", "add", "visa", "--type", "credit")
    res = importer.import_file(conn, paths, copy(paths, "card_custom.csv", "visa_sept.csv"))
    assert res.status == "error" and "month-first" in res.error and "--map" in res.error
    r = run("formats", "add", "visa", "--from", FIX / "card_custom.csv", "--date", "Transaction Date",
            "--amount", "Amount", "--payee", "Details", "--negate", "--date-format", "%m/%d/%Y")
    assert r.exit_code == 0, r.output
    assert [f.name for f in formats.load_formats(paths.formats)] == ["visa"]
    res = importer.import_file(conn, paths, copy(paths, "card_custom.csv", "visa_sept.csv"))
    assert res.status == "imported" and res.rows_new == 3 and res.account == "visa"
    rows = {r["payee"]: (r["date"], r["amount"], r["source"]) for r in queries.transactions(conn, account="visa")}
    assert rows["COFFEE CART"] == ("2026-09-03", -4.5, "visa-csv")  # month-first, charges negated
    assert rows["REFUND SHOP"][1] == 20.0
    assert json.loads(run("formats", "--json").output)[0]["negate"] is True
    assert run("formats", "remove", "visa").exit_code == 0 and formats.load_formats(paths.formats) == []


def test_import_map_prompts_and_saves(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kete.cli import app

    monkeypatch.setenv("KETE_HOME", str(tmp_path / "k"))
    CliRunner().invoke(app, ["accounts", "add", "visa", "--type", "credit"])
    src = tmp_path / "visa_x.csv"
    shutil.copy(FIX / "card_custom.csv", src)
    # date col 1, single amount: yes, amount col 3, payee col 2, no description/particulars/code/reference/balance,
    # payments negative? no, date format choice 1 (%m/%d/%Y is the only fit), name
    answers = "1\ny\n3\n2\n\n\n\n\n\nn\n1\nvisa\n"
    r = CliRunner().invoke(app, ["import", "--map", str(src)], input=answers)
    assert r.exit_code == 0, r.output
    assert "visa" in (tmp_path / "k" / "formats.toml").read_text()
    data = json.loads(CliRunner().invoke(app, ["tx", "--json", "-a", "visa"]).output)
    assert {d["payee"]: d["amount"] for d in data}["BOOK BARN"] == -32.0


def test_export_formats_balance(env):
    from kete import export, shares

    paths, conn = env
    for name in ("everyday_2026-09.csv", "savings_2026-09.csv"):
        importer.import_file(conn, paths, copy(paths, name))
    transfers.apply(conn, transfers.detect(conn))
    power = conn.execute("SELECT id FROM transactions WHERE payee = 'Mercury Energy'").fetchone()[0]
    shares.set_share(conn, power, "Sam", "half")

    text = export.export(conn, "csv")
    assert text.splitlines()[0].startswith("id,date,account,amount") and len(text.splitlines()) == 1 + conn.execute(
        "SELECT COUNT(*) FROM transactions").fetchone()[0]
    ledger = export.export(conn, "hledger")
    entries = [e for e in ledger.split("\n\n") if e and e[0].isdigit()]
    for e in entries:  # every entry balances
        amounts = [float(line.split()[-2]) for line in e.splitlines()[1:] if line.strip() and not line.strip().startswith(";")]
        assert round(sum(amounts), 2) == 0, e
    assert "Assets:Receivable:Sam" in ledger and "Expenses:Utilities" in ledger
    paired = conn.execute("SELECT COUNT(*) FROM transactions WHERE transfer_pair_id IS NOT NULL").fetchone()[0]
    assert ledger.count("Assets:Savings") >= paired // 2
    assert len(entries) == conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] - paired // 2
    bean = export.export(conn, "beancount")
    assert 'option "operating_currency" "NZD"' in bean and " open Assets:Everyday" in bean
    with pytest.raises(ValueError):
        export.export(conn, "qif")


def test_autoimport_picks_only_bank_exports(tmp_path, monkeypatch):
    import os
    import time

    from kete import autoimport

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    shutil.copy(FIX / "everyday_2026-09.csv", downloads / "everyday_new.csv")
    (downloads / "contacts.csv").write_text("Name,Email\nA,a@example.com\n")
    (downloads / "notes.txt").write_text("hi")
    old = downloads / "everyday_old.csv"
    shutil.copy(FIX / "everyday_2026-09b.csv", old)
    week_ago = time.time() - 7 * 86400
    os.utime(old, (week_ago, week_ago))
    for f in downloads.iterdir():
        if f != old:
            os.utime(f, (time.time() - 60, time.time() - 60))
    paths = Paths(tmp_path / "k")
    assert [p.name for p in autoimport.candidates(downloads, paths)] == ["everyday_new.csv"]

    plist = autoimport.build_plist(downloads, paths, executable="/usr/local/bin/kete")
    assert plist["WatchPaths"] == [str(downloads)]
    assert plist["ProgramArguments"][:3] == ["/usr/local/bin/kete", "autoimport", "run"]

    run = _cli(tmp_path, monkeypatch)
    assert run("autoimport", "run", "--folder", downloads, "--quiet").exit_code == 0
    assert not (downloads / "everyday_new.csv").exists() and (downloads / "contacts.csv").exists()
    assert len(_ids(run)) == 9


def test_tax_year_report_totals_donations():
    from kete import report as rep
    from kete.demo import build_demo_db

    conn = build_demo_db(today=TODAY)
    kind, start, end, label = rep.parse_period(tax_year=2026, today=TODAY)
    d = rep.build_report(conn, kind, start, end, label, today=TODAY)["donations"]
    assert d["count"] > 0 and d["total"] > 0
    assert d["eligible"] == round(sum(-i["amount"] for i in d["items"] if i["eligible"]), 2)
    assert d["credit"] == round(d["eligible"] / 3, 2)
    small = [i for i in d["items"] if i["payee"] == "Street Appeal"]
    assert small and not small[0]["eligible"]
    month = rep.build_report(conn, *rep.parse_period(today=TODAY), today=TODAY)
    assert month["donations"] is None


def test_tui_bulk_categorise_learns_rule_and_undoes(env, monkeypatch):
    import asyncio

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    for i, day in enumerate(("2026-09-20", "2026-09-27")):
        conn.execute("""INSERT INTO transactions (account_id, posted_date, amount_cents, payee, dedupe_key, source, raw_json)
                        VALUES (1, ?, -999, 'Mystery Shop', ?, 'manual', '{}')""", (day, f"m{i}"))
    conn.commit()
    monkeypatch.setenv("KETE_HOME", str(paths.home))
    from kete.tui import KeteApp

    async def run():
        app = KeteApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("4")
            uncat = app.query_one("#uncat-table")
            assert uncat.row_count == 2
            await pilot.press("a")
            assert len(app.marked) == 2
            await pilot.press("c")
            await pilot.pause()
            await pilot.press(*"Hobbies", "enter")
            await pilot.pause()
            assert uncat.row_count == 0 and not app.marked
            await pilot.press("y")  # the "make it a rule?" question
            await pilot.pause()
            assert 'payee = "Mystery Shop"' in paths.rules.read_text()
            await pilot.press("u")  # undo the rule
            await pilot.pause()
            assert "Mystery Shop" not in paths.rules.read_text()
            await pilot.press("u")  # undo the categorising
            await pilot.pause()
            assert uncat.row_count == 2
            await pilot.press("space")
            assert len(app.marked) == 1
            await pilot.press("x")
            assert not app.marked
    asyncio.run(run())


def test_tui_private_mode_period_and_share(env, monkeypatch):
    import asyncio

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    monkeypatch.setenv("KETE_HOME", str(paths.home))
    from kete.tui import KeteApp

    def cells(table):
        return [str(c) for row in range(table.row_count) for c in table.get_row_at(row)]

    async def run():
        app = KeteApp(private=True)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("2")
            tx = app.query_one("#tx-table")
            assert not any("$" in c or "Mercury" in c for c in cells(tx))
            await pilot.press("h")
            assert any("Mercury" in c for c in cells(tx))
            await pilot.press("3", "g")
            await pilot.pause()
            await pilot.press(*"2026-09-01..2026-09-07", "enter")
            await pilot.pause()
            assert app.cat_range[:2] == ("2026-09-01", "2026-09-07")
            await pilot.press("enter")  # drill down uses the custom range
            await pilot.pause()
            assert app.query_one("#tx-filter").value.startswith("from:2026-09-01 to:2026-09-07")
            await pilot.press("escape", "2")
            app.query_one("#tx-filter").value = "Mercury"
            await pilot.pause()
            tx.focus()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press(*"Sam half", "enter")
            await pilot.pause()
            assert conn.execute("SELECT shared_cents FROM transactions WHERE payee = 'Mercury Energy'").fetchone()[0] == 6000
    asyncio.run(run())


def _online_payments(conn, days, account_no="0100-0000099-00"):
    """Fortnightly online-banking payments to someone else's account: Westpac marks these
    'WBC Internet' / 'Transfer', exactly like moves between your own accounts."""
    ids = []
    for i, d in enumerate(days):
        cur = conn.execute(
            """INSERT INTO transactions (account_id, posted_date, amount_cents, payee, description, particulars,
               code, dedupe_key, source, raw_json) VALUES (1, ?, -6500, ?, 'ONLINE BANKING', 'WBC Internet',
               'Transfer', ?, 'manual', '{}')""", (d, f"To {account_no}", f"op{d}{i}"))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def test_payment_to_someone_elses_account_can_beat_the_transfer_rule(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    days = ["2026-07-03", "2026-07-17", "2026-07-31", "2026-08-14", "2026-08-28", "2026-09-11"]
    ids = _online_payments(conn, days)
    loaded = rules.load_rules(paths.rules)
    rules.apply_rules(conn, loaded)
    assert all(r[0] for r in conn.execute(f"SELECT is_transfer FROM transactions WHERE id IN ({','.join('?' * len(ids))})", ids))
    assert not [s for s in queries.subscriptions(conn, today=date(2026, 9, 15)) if "0099" in s["payee"]]

    rules.set_category(conn, ids[:2], "Internet")  # what c (or kete categorise) does
    sug = [s for s in rules.suggest_rules(conn, loaded) if "0099" in s["payee"]]
    assert sug and sug[0]["category"] == "Internet" and sug[0]["before"] == 1  # must beat rule #1
    rules.append_rule(paths.rules, payee=sug[0]["payee"], category="Internet", before=sug[0]["before"])
    reloaded = rules.load_rules(paths.rules)
    assert reloaded[0].category == "Internet" and reloaded[1].name == "Own-account transfer"
    rules.apply_rules(conn, reloaded)
    cats = {r[0] for r in conn.execute(f"SELECT category FROM transactions WHERE id IN ({','.join('?' * len(ids))})", ids)}
    assert cats == {"Internet"}
    subs = [s for s in queries.subscriptions(conn, today=date(2026, 9, 15)) if "0099" in s["payee"]]
    assert subs and subs[0]["cadence"] == "fortnightly"
    assert [u for u in queries.upcoming(conn, 14, today=date(2026, 9, 15)) if "0099" in u["payee"]]
    # real own-account transfers are still transfers
    assert conn.execute("SELECT is_transfer FROM transactions WHERE payee = 'To 0100-0000000-00'").fetchone()[0] == 1


def test_append_rule_before_keeps_comments_with_their_rule(tmp_path):
    p = tmp_path / "rules.toml"
    p.write_text('# header\n\n# about A\n[[rule]]\nname = "A"\npayee = "a"\ncategory = "X"\n\n[[rule]]\nname = "B"\npayee = "b"\ncategory = "Y"\n')
    rules.append_rule(p, payee="Shop", category="Z", before=1)
    names = [r.name for r in rules.load_rules(p)]
    assert names == ["Shop", "A", "B"]
    assert p.read_text().index('name = "Shop"') < p.read_text().index("# about A")
    with pytest.raises(rules.RulesError):
        rules.append_rule(p, payee="x", category="y", before=9)


def test_tui_unmarking_a_rule_transfer_sticks(env, monkeypatch):
    import asyncio

    paths, conn = env
    dbmod.get_or_create_account(conn, "everyday")
    _online_payments(conn, ["2026-09-04", "2026-09-18"])
    rules.apply_rules(conn, rules.load_rules(paths.rules))
    monkeypatch.setenv("KETE_HOME", str(paths.home))
    from kete.tui import KeteApp

    async def run():
        app = KeteApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("2")
            app.query_one("#tx-table").focus()
            await pilot.press("a", "t")  # both selected, unmark
            await pilot.pause()
            assert app.query_one("#uncat-table").row_count == 2
            await pilot.press("r")  # a refresh (or kete rules apply) must not re-mark them
            rules.apply_rules(app.conn, rules.load_rules(paths.rules))
            assert not any(r[0] for r in app.conn.execute("SELECT is_transfer FROM transactions"))
    asyncio.run(run())


def test_monthly_bill_on_varying_days_beside_a_smaller_product(env):
    """Like an internet plan (~$114, paid by hand on different days) from a company that also
    takes a ~$20 mobile top-up now and then."""
    _, conn = env
    _charges(conn, [("2026-04-02", -11400, "Telco"), ("2026-04-14", -2000, "Telco"),
                    ("2026-04-22", -11400, "Telco"), ("2026-05-25", -11450, "Telco"),
                    ("2026-07-03", -11400, "Telco"), ("2026-07-30", -2000, "Telco"),
                    ("2026-08-28", -11400, "Telco"), ("2026-09-01", -200, "Telco"),
                    ("2026-09-06", -2000, "Telco")])
    today = date(2026, 10, 2)
    s = _sub_names(conn, today=today)["Telco"]
    assert s["typical_amount"] == 114.0 and s["cadence"] == "monthly" and s["varying_days"]
    up = queries.upcoming(conn, 14, today=today)  # due ~27 Sep, not paid yet: still listed
    assert [(u["payee"], u["amount"], u["approx"], u["late"]) for u in up] == [("Telco", 114.0, True, True)]
    # weekly shopping with a spread of amounts still isn't a bill
    _charges(conn, [(f"2026-{m:02d}-{d:02d}", -c, "Big Supermarket") for m, d, c in
                    [(7, 4, 15200), (7, 11, 11800), (7, 18, 16900), (7, 25, 12100), (8, 1, 17500),
                     (8, 8, 10900), (8, 15, 14800), (8, 22, 12300)]])
    assert "Big Supermarket" not in _sub_names(conn, today=today)


def test_named_bill_merges_spellings_and_follows_a_new_price(env):
    from kete import bills

    _, conn = env
    _charges(conn, [("2026-04-05", -3400, "Aico.ai Subscr"), ("2026-05-08", -3400, "AICORP* 1234"),
                    ("2026-06-04", -3400, "Aico.ai Subscr"), ("2026-07-09", -3400, "AICORP* 5678"),
                    ("2026-08-06", -3400, "Aico.ai Subscr"), ("2026-09-07", -20000, "AICORP* MAX"),
                    ("2026-09-20", -500, "Aicorp API credits")])
    today = date(2026, 9, 30)
    assert "Aico" not in _sub_names(conn, today=today)
    bills.add(conn, "Aico", ["aico.ai", "aicorp*"], amount_min=20)
    subs = _sub_names(conn, today=today)
    a = subs["Aico"]
    assert a["named"] and a["typical_amount"] == 200.0 and a["count"] == 6  # the $5 API top-up isn't it
    assert a["last_date"] == "2026-09-07" and a["next_expected"] == "2026-10-07"
    assert not any("AICORP" in p or "Aico.ai" in p for p in subs)  # no detected duplicates
    assert [u["payee"] for u in queries.upcoming(conn, 14, today=today)] == ["Aico"]
    with pytest.raises(bills.BillError):
        bills.add(conn, "x", [])
    assert bills.remove(conn, "aico") and "Aico" not in _sub_names(conn, today=today)


def test_cli_bills(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    run("import", FIX / "everyday_2026-09.csv")
    r = run("bills", "add", "Power", "--match", "mercury", "--min", "50")
    assert r.exit_code == 0 and "1 payments found" in r.output
    data = json.loads(run("bills", "--json").output)
    assert data[0]["name"] == "Power" and data[0]["latest"]["last_amount"] == 120.0
    assert any(s["payee"] == "Power" for s in json.loads(run("subs", "--json").output))
    assert run("bills", "add", "Bad", "--match", "x", "--cadence", "daily").exit_code == 1
    assert run("bills", "remove", "Power").exit_code == 0 and run("bills", "remove", "Power").exit_code == 1


def test_tui_b_tracks_a_bill(env, monkeypatch):
    import asyncio

    from kete import bills

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    monkeypatch.setenv("KETE_HOME", str(paths.home))
    from kete.tui import KeteApp

    async def run():
        app = KeteApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("2")
            app.query_one("#tx-filter").value = "Mercury"
            await pilot.pause()
            app.query_one("#tx-table").focus()
            await pilot.press("b")
            await pilot.pause()
            app.screen.query_one("Input").value = ""
            await pilot.press(*"Power", "enter")
            await pilot.pause()
    asyncio.run(run())
    (b,) = bills.declared(conn)
    assert b["name"] == "Power" and b["match"] == ["mercury energy"] and b["amount_min"] == 72.0


def test_variable_power_bill_with_two_spellings_is_monthly(env):
    """Like a power bill: amount follows usage, paid as a card debit some months and a bill
    payment (with a reference glued to the name) in others."""
    _, conn = env
    _charges(conn, [("2026-04-03", -24100, "Powerco 03"), ("2026-05-08", -22400, "8510Powerco"),
                    ("2026-05-31", -26300, "Powerco 31"), ("2026-07-02", -18800, "8510Powerco"),
                    ("2026-08-02", -29900, "Powerco 02"), ("2026-08-29", -21100, "8510Powerco")])
    s = _sub_names(conn, today=date(2026, 9, 10))["Powerco"]
    assert s["cadence"] == "monthly" and s["varying_amount"] and len(s["merged"]) == 2
    assert s["amount_range"] == [188.0, 299.0] and s["next_expected"] == "2026-09-28"
    assert "varies $188–$299" in queries.recurring_notes(s, today=date(2026, 9, 10))
    assert queries.brand_key("8510Powerco") == "powerco" and queries.brand_key("2cheap") == "2cheap"


def test_tui_recurring_tab(monkeypatch, tmp_path):
    import asyncio
    import sqlite3

    from kete.demo import build_demo_db

    home = tmp_path / "k"
    home.mkdir()
    disk = sqlite3.connect(home / "kete.db")
    build_demo_db().backup(disk)
    disk.close()
    monkeypatch.setenv("KETE_HOME", str(home))
    from kete.tui import KeteApp

    async def run():
        app = KeteApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.press("6")
            t = app.query_one("#recurring-table")
            assert t.row_count >= 5 and app.focused is t
            first = [str(c) for c in t.get_row_at(0)]
            assert "Harbour Property" in first[0] and first[1] == "bill"
            kinds = {str(t.get_row_at(r)[1]) for r in range(t.row_count)}
            assert {"bill", "subscription", "regular spend"} <= kinds
            assert "a month" in str(app.query_one("#recurring-header").render())
            await pilot.press("enter")
            await pilot.pause()
            assert app.query_one("TabbedContent").active == "transactions"
            assert app.query_one("#tx-filter").value == "Harbour Property"
            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("TabbedContent").active == "recurring"
            await pilot.press("h")
            await pilot.pause()
            assert not any("$" in str(c) for r in range(t.row_count) for c in t.get_row_at(r))
    asyncio.run(run())


def test_rename_and_cancel_recurring_payments(env):
    from kete import bills

    _, conn = env
    _charges(conn, [(f"2026-{m:02d}-09", -1699, f"Streamco {m:02d}") for m in range(3, 10)])
    bills.add(conn, "Power", ["powerco"])
    _charges(conn, [(f"2026-{m:02d}-20", -20000, "Powerco") for m in range(5, 10)])
    today = date(2026, 9, 25)
    subs = queries.subscriptions(conn, today=today)
    streamco = bills.find(subs, "streamco")
    bills.rename(conn, streamco, "Movies")
    bills.rename(conn, bills.find(subs, "Power"), "Electricity")
    names = {s["payee"]: s for s in queries.subscriptions(conn, today=today)}
    assert {"Movies", "Electricity"} <= set(names) and names["Movies"]["original_payee"] == "Streamco"
    assert "shown as Streamco by the bank" in queries.recurring_notes(names["Movies"], today)
    assert [b["name"] for b in bills.declared(conn)] == ["Electricity"]
    assert bills.find(queries.subscriptions(conn, today=today), "Streamco")["payee"] == "Movies"

    bills.set_cancelled(conn, names["Movies"], True, today=today)
    movies = bills.find(queries.subscriptions(conn, today=today), "Movies")
    assert movies["cancelled"] and not movies["active"]
    assert "Movies" not in [u["payee"] for u in queries.upcoming(conn, 31, today=today)]
    _charges(conn, [("2026-10-09", -1699, "Streamco 10")])  # it charged anyway
    movies = bills.find(queries.subscriptions(conn, today=date(2026, 10, 12)), "Movies")
    assert movies["charged_after_cancel"] and movies["active"]
    assert "charged after you cancelled!" in queries.recurring_notes(movies)
    bills.set_cancelled(conn, movies, False)
    assert not bills.find(queries.subscriptions(conn, today=date(2026, 10, 12)), "Movies").get("cancelled")


def test_report_flags_stopped_and_charged_after_cancel(env):
    from kete import bills
    from kete import report as rep

    _, conn = env
    _charges(conn, [(f"2026-{m:02d}-05", -1299, "Gymapp") for m in range(2, 8)])   # last paid 5 Jul
    _charges(conn, [(f"2026-{m:02d}-15", -999, "Musicco") for m in range(2, 9)])   # last paid 15 Aug
    _charges(conn, [(f"2026-{m:02d}-12", -100000, "Big Supermarket") for m in range(2, 7)])
    conn.execute("UPDATE transactions SET category = 'Groceries' WHERE payee = 'Big Supermarket'")  # a habit

    def findings(month, today):
        kind, start, end, label = rep.parse_period(month=month, today=today)
        return [f for f in rep.build_report(conn, kind, start, end, label, today=today)["findings"]
                if f["detector"] == "stopped_subscription"]

    # Due ~4 Aug and never came; after two missed cycles (early Sep) it counts as stopped.
    assert findings("2026-08", date(2026, 9, 2)) == []
    music = bills.find(queries.subscriptions(conn, today=date(2026, 8, 20)), "Musicco")
    bills.set_cancelled(conn, music, True, today=date(2026, 8, 20))
    _charges(conn, [("2026-09-15", -999, "Musicco")])
    sep = findings("2026-09", date(2026, 10, 2))
    assert sorted((f["severity"], f["title"]) for f in sep) == [
        ("good", "Gymapp has stopped"), ("high", "Musicco charged you after you cancelled")]
    gym = next(f for f in sep if f["severity"] == "good")
    assert gym["impact_yearly"] == round(12.99 * 12, 2) and "since 2026-07-05" in gym["body"]
    assert len(next(f for f in sep if f["severity"] == "high")["evidence"]) == 1


def test_latest_price_wins_once_two_charges_agree(env):
    _, conn = env
    _charges(conn, [("2026-03-09", -1699, "Streamco"), ("2026-04-09", -1699, "Streamco"),
                    ("2026-05-09", -1699, "Streamco"), ("2026-06-09", -1699, "Streamco"),
                    ("2026-07-09", -1899, "Streamco"), ("2026-08-09", -1899, "Streamco")])
    s = _sub_names(conn, today=date(2026, 8, 20))["Streamco"]
    assert s["typical_amount"] == 18.99 and s["monthly_cost"] == 18.99


def test_named_bill_stops_after_two_missed_cycles(env):
    from kete import bills

    _, conn = env
    _charges(conn, [("2026-05-01", -5000, "Telco"), ("2026-06-01", -5000, "Telco")])
    bills.add(conn, "Phone", ["telco"])
    assert bills.find(queries.subscriptions(conn, today=date(2026, 7, 20)), "Phone")["active"]
    assert not bills.find(queries.subscriptions(conn, today=date(2026, 8, 15)), "Phone")["active"]


def test_cli_bills_rename_and_cancel(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    run("import", FIX / "everyday_2026-09.csv")
    run("bills", "add", "Power", "--match", "mercury")
    assert run("bills", "rename", "Power", "Electricity").exit_code == 0
    assert run("bills", "rename", "Nope", "X").exit_code == 1
    assert run("bills", "cancel", "electricity").exit_code == 0
    subs = json.loads(run("subs", "--all", "--json").output)
    e = next(s for s in subs if s["payee"] == "Electricity")
    assert e["cancelled"] and not e["active"]
    assert run("bills", "cancel", "Electricity", "--undo").exit_code == 0


def test_tui_rename_and_cancel_on_recurring_tab(monkeypatch, tmp_path):
    import asyncio
    import sqlite3

    from kete.demo import build_demo_db

    home = tmp_path / "k"
    home.mkdir()
    disk = sqlite3.connect(home / "kete.db")
    build_demo_db().backup(disk)
    disk.close()
    monkeypatch.setenv("KETE_HOME", str(home))
    from kete.tui import KeteApp

    def names(t):
        return [str(t.get_row_at(r)[0]) for r in range(t.row_count)]

    async def run():
        app = KeteApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.press("6")
            t = app.query_one("#recurring-table")
            assert "Harbour Property" in names(t)[0]
            await pilot.press("e")
            await pilot.pause()
            app.screen.query_one("Input").value = ""
            await pilot.press(*"Rent", "enter")
            await pilot.pause()
            assert names(t)[0] == "Rent"
            await pilot.press("k")
            await pilot.pause()
            assert "Rent" in names(t)[-1] and "dim" in names(t)[-1]  # cancelled: dimmed, at the bottom
            assert "Rent" not in str(app.query_one("#upcoming").render())
    asyncio.run(run())


def _span(key, first, covered, opened=None, closed=None):
    return {"key": key, "first": first, "last": covered, "covered_until": covered,
            "opened_on": opened, "closed_on": closed}


def test_new_and_closed_accounts_keep_months_complete():
    old = _span("old", "2026-01-01", "2026-10-15")
    new = _span("new", "2026-06-10", "2026-10-15")
    assert not queries.month_complete([old, new], "2026-03")  # maybe new had history you didn't export
    new["opened_on"] = "2026-06-10"
    assert queries.month_complete([old, new], "2026-03")      # it didn't exist yet
    assert queries.month_complete([old, new], "2026-06")      # needed only from the day it opened
    quiet = _span("old", "2026-01-01", "2026-07-20")          # stopped getting exports in July
    assert not queries.month_complete([quiet, new], "2026-09")
    quiet["closed_on"] = "2026-07-25"
    assert queries.month_complete([quiet, new], "2026-09")
    assert queries.month_complete([quiet, new], "2026-07")


def test_quiet_account_with_fresh_exports_is_not_stale(env):
    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))  # newest row 12 Sep 2026
    today = date.today()
    acct = conn.execute("SELECT id FROM accounts WHERE key = 'everyday'").fetchone()[0]

    def set_import(day):
        conn.execute("UPDATE imports SET imported_at = ? WHERE account_id = ?", (f"{day}T09:00:00", acct))
        conn.commit()

    set_import("2026-10-20")  # exported a month after its newest row: nothing happened since
    (span,) = queries.account_spans(conn, today=date(2026, 10, 25))
    assert span["covered_until"] == "2026-10-20"
    set_import("2027-03-01")  # an old file imported much later says nothing about recent months
    (span,) = queries.account_spans(conn, today=date(2027, 3, 5))
    assert span["covered_until"] == "2026-09-12"
    assert [a["key"] for a in queries.stale(conn)] == (["everyday"] if (today - date(2026, 9, 12)).days > 30 else [])
    conn.execute("UPDATE accounts SET closed_on = '2026-09-30'")
    assert queries.stale(conn) == []


def test_hand_entered_account_is_never_stale(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    run("accounts", "add", "cash", "--type", "other")
    run("add", "cash", "-5", "Market", "--date", "2025-01-01")
    data = json.loads(run("accounts", "--json").output)
    assert data[0]["manual_only"] and run("stale").exit_code == 0


def test_cli_opened_and_closed(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    run("import", FIX / "everyday_2026-09.csv")
    assert run("accounts", "edit", "everyday", "--closed", "30/09/2026").exit_code != 0
    r = run("accounts", "edit", "everyday", "--opened", "2026-09-01", "--closed", "2026-09-30")
    assert r.exit_code == 0 and "closed" in r.output
    a = json.loads(run("accounts", "--json").output)[0]
    assert (a["opened_on"], a["closed_on"]) == ("2026-09-01", "2026-09-30")
    assert "closed" in run("accounts").output and run("stale").exit_code == 0
    run("accounts", "edit", "everyday", "--closed", "none")
    assert json.loads(run("accounts", "--json").output)[0]["closed_on"] is None


def test_report_ignores_accounts_outside_their_open_dates(env):
    from kete import report as rep

    paths, conn = env
    importer.import_file(conn, paths, copy(paths, "everyday_2026-09.csv"))
    importer.import_file(conn, paths, copy(paths, "savings_2026-09.csv"))
    kind, start, end, label = rep.parse_period(month="2026-09", today=date(2026, 10, 19))

    def coverage():
        model = rep.build_report(conn, kind, start, end, label, today=date(2026, 10, 19))
        return sorted(f["title"] for f in model["findings"] if f["id"].startswith("coverage:"))

    conn.execute("UPDATE imports SET imported_at = '2026-09-13T09:00:00'")  # last exported mid-September
    assert coverage() == ["everyday doesn't cover the whole period"]  # savings has a row on the 30th
    conn.execute("UPDATE accounts SET opened_on = '2026-09-01', closed_on = '2026-09-12'")
    assert coverage() == []


def test_add_account_before_its_first_export(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    assert run("accounts", "add", "everyday", "--opened", "2026-10-20").exit_code == 0
    assert run("accounts", "add", "other", "--opened", "soon").exit_code != 0
    assert "no transactions yet" in run("stale").output
    run("import", FIX / "everyday_2026-09.csv")
    a = json.loads(run("accounts", "--json").output)[0]
    assert (a["key"], a["opened_on"], a["transactions"]) == ("everyday", "2026-10-20", 9)


def test_negative_amounts_on_the_command_line(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    run("import", FIX / "everyday_2026-09.csv")
    r = run("balance", "set", "everyday", "-350.20", "--as-of", "2026-09-12")
    assert r.exit_code == 0, r.output
    assert json.loads(run("accounts", "--json").output)[0]["balance"] == -350.2
    run("accounts", "add", "cash", "--type", "other")
    assert run("add", "cash", "-4.50", "Coffee").exit_code == 0


@pytest.mark.parametrize("text,value", [("-9.74", -9.74), ("\u20139.74", -9.74), ("\u22129.74", -9.74),
                                        ("$1,234.50", 1234.5), ("-$12", -12.0), ("0", 0.0)])
def test_parse_dollars_accepts_typographic_minus(text, value):
    from kete.cli import parse_dollars

    assert parse_dollars(text) == value


def test_balance_set_with_en_dash(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch)
    run("import", FIX / "everyday_2026-09.csv")
    r = run("balance", "set", "everyday", "\u20139.74", "--as-of", "2026-09-12")
    assert r.exit_code == 0, r.output
    assert json.loads(run("accounts", "--json").output)[0]["balance"] == -9.74
    assert run("balance", "set", "everyday", "nine").exit_code != 0
