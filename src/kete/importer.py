"""The importer is the only writer of transactions."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import rules as rules_mod
from .db import DEFAULT_ACCOUNTS, get_or_create_account, mask_number
from .parsers import ParseError, Statement, Txn, parse_file
from .parsers.common import normalise_text
from .parsers.formats import load_formats
from .paths import Paths

# ask(filename, existing account keys, suggested key) -> account key, or None to skip the file.
AccountResolver = Callable[[str, list[str], "str | None"], "str | None"]

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

SUPPORTED = {".csv", ".ofx", ".qfx", ".qif"}


@dataclass
class ImportResult:
    filename: str
    status: str  # imported | duplicate-file | skipped | error
    account: str | None = None
    format: str | None = None
    rows_new: int = 0
    rows_dup: int = 0
    rows_pending: int = 0
    date_from: str | None = None
    date_to: str | None = None
    archived_to: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def filename_pattern(name: str) -> str:
    """Export_2026-10-19 (3).csv -> export_#-#-# (#).csv, so later exports map the same way."""
    return re.sub(r"\d+", "#", name.lower())


def account_exists(conn: sqlite3.Connection, key: str) -> bool:
    """Existing accounts, plus the default names that are created on first use."""
    key = key.lower()
    if key in DEFAULT_ACCOUNTS:
        return True
    return conn.execute("SELECT 1 FROM accounts WHERE key = ?", (key,)).fetchone() is not None


def account_keys(conn: sqlite3.Connection) -> list[str]:
    keys = [r["key"] for r in conn.execute("SELECT key FROM accounts ORDER BY id")]
    return keys + [k for k in DEFAULT_ACCOUNTS if k not in keys]


def _prefix_account(conn: sqlite3.Connection, name: str) -> str | None:
    """joint_2026-10.csv -> 'joint', if an account with that key exists."""
    prefix = name.lower().split("_", 1)[0] if "_" in name else None
    return prefix if prefix and account_exists(conn, prefix) else None


def _overlap_account(conn: sqlite3.Connection, st: Statement, fmt: str) -> int | None:
    """The account that already holds rows from this file. Exports overlap by design, so an
    unlabelled file usually shares a week of transactions with exactly one account."""
    sample = [t for t in st.txns if not t.pending]
    if not sample:
        return None
    hits = {}
    for (account_id,) in conn.execute("SELECT id FROM accounts"):
        keys = statement_keys(account_id, sample, fmt)
        found = sum(
            1 for k in keys
            if conn.execute("SELECT 1 FROM transactions WHERE dedupe_key = ?", (k,)).fetchone()
        )
        if found:
            hits[account_id] = found
    if len(hits) == 1:
        (account_id, found), = hits.items()
        if found >= min(3, len(sample)):
            return account_id
    return None


def resolve_account(
    conn: sqlite3.Connection,
    path: Path,
    st: Statement,
    fmt: str,
    ask: AccountResolver | None,
    forced: str | None = None,
) -> int | None:
    """Account number in the file > --account > filename prefix > overlap with existing rows >
    ask (offering the account last chosen for this filename pattern). Never a silent guess."""
    if st.account_number:
        row = conn.execute(
            "SELECT id FROM accounts WHERE number = ?", (st.account_number,)
        ).fetchone()
        if row:
            return row["id"]
    pattern = filename_pattern(path.name)
    key = forced or _prefix_account(conn, path.name)
    account_id = None
    if key is None:
        account_id = _overlap_account(conn, st, fmt)
    if key is None and account_id is None and ask is not None:
        row = conn.execute(
            """SELECT a.key FROM file_mappings m JOIN accounts a ON a.id = m.account_id
               WHERE m.pattern = ?""",
            (pattern,),
        ).fetchone()
        key = ask(path.name, account_keys(conn), row["key"] if row else None)
        if key and not KEY_RE.match(key.lower()):
            raise ValueError(f"account names are lower-case letters, digits and dashes: {key!r}")
    if key is None and account_id is None:
        return None
    if account_id is None:
        account_id = get_or_create_account(conn, key)
    conn.execute(
        "INSERT OR REPLACE INTO file_mappings (pattern, account_id) VALUES (?, ?)",
        (pattern, account_id),
    )
    if st.account_number:
        conn.execute(
            """UPDATE accounts SET number = ?, number_masked = ? WHERE id = ? AND number IS NULL
               AND NOT EXISTS (SELECT 1 FROM accounts b WHERE b.number = ?)""",
            (st.account_number, mask_number(st.account_number), account_id, st.account_number),
        )
    return account_id


def csv_dedupe_key(account_id: int, t: Txn, occurrence: int) -> str:
    norm = normalise_text(t.payee, t.description, t.particulars, t.code, t.reference)
    material = f"{account_id}|{t.posted_date.isoformat()}|{t.amount_cents}|{norm}|{occurrence}"
    return "csv:" + hashlib.sha1(material.encode()).hexdigest()


def statement_keys(account_id: int, txns: list[Txn], fmt: str) -> list[str]:
    """Dedupe key per transaction, in file order. The occurrence index keeps identical
    same-day rows (two $4.50 coffees) distinct."""
    seen: Counter = Counter()
    keys = []
    for t in txns:
        if fmt == "ofx" and t.fitid:
            keys.append(f"ofx:{account_id}:{t.fitid}")
            continue
        ident = (t.posted_date, t.amount_cents,
                 normalise_text(t.payee, t.description, t.particulars, t.code, t.reference))
        keys.append(csv_dedupe_key(account_id, t, seen[ident]) + (":pending" if t.pending else ""))
        seen[ident] += 1
    return keys


def _payee_stub(payee: str) -> str:
    return re.sub(r"[^a-z0-9]", "", payee.lower())[:10]


def _cross_format_match(
    conn: sqlite3.Connection, account_id: int, t: Txn, source: str, claimed: set[int]
) -> int | None:
    """When an account switches between CSV and OFX, the keys differ; fall back to
    date + amount + payee within the other format's rows."""
    rows = conn.execute(
        """SELECT id, payee FROM transactions
           WHERE account_id = ? AND posted_date = ? AND amount_cents = ? AND source != ?
             AND pending = 0""",
        (account_id, t.posted_date.isoformat(), t.amount_cents, source),
    ).fetchall()
    stub = _payee_stub(t.payee)
    for r in rows:
        if r["id"] in claimed:
            continue
        other = _payee_stub(r["payee"])
        if not stub or not other or stub.startswith(other) or other.startswith(stub):
            return r["id"]
    return None


def _insert_statement(
    conn: sqlite3.Connection, account_id: int, st: Statement, fmt: str, import_id: int
) -> tuple[list[int], int]:
    source = st.source or f"westpac-{fmt}"
    # Pending rows are provisional: this export is the latest word on every day it covers.
    if st.date_from is not None:
        conn.execute(
            "DELETE FROM transactions WHERE account_id = ? AND pending = 1 AND posted_date >= ?",
            (account_id, st.date_from.isoformat()),
        )
    new_ids: list[int] = []
    dups = 0
    claimed: set[int] = set()
    for t, key in zip(st.txns, statement_keys(account_id, st.txns, fmt)):
        exists = conn.execute(
            "SELECT 1 FROM transactions WHERE dedupe_key = ? OR (account_id = ? AND fitid = ?)",
            (key, account_id, t.fitid),
        ).fetchone()
        if exists:
            dups += 1
            continue
        match = _cross_format_match(conn, account_id, t, source, claimed)
        if match is not None:
            claimed.add(match)
            if t.fitid:
                conn.execute("UPDATE transactions SET fitid = ? WHERE id = ?", (t.fitid, match))
            dups += 1
            continue
        cur = conn.execute(
            """INSERT INTO transactions
               (account_id, posted_date, amount_cents, payee, description, particulars, code,
                reference, fitid, dedupe_key, source, import_id, raw_json, pending)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_id, t.posted_date.isoformat(), t.amount_cents, t.payee, t.description,
                t.particulars, t.code, t.reference, t.fitid, key, source, import_id,
                json.dumps(t.raw, ensure_ascii=False), int(t.pending),
            ),
        )
        new_ids.append(cur.lastrowid)
    return new_ids, dups


def _record_balance(conn: sqlite3.Connection, account_id: int, st: Statement, source: str) -> str | None:
    """Store the closing balance and check it against the previous one; returns a warning."""
    if st.balance_cents is None or st.balance_date is None:
        return None
    as_of = st.balance_date.isoformat()
    prev = conn.execute(
        """SELECT as_of, balance_cents FROM balances
           WHERE account_id = ? AND as_of < ? ORDER BY as_of DESC LIMIT 1""",
        (account_id, as_of),
    ).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO balances (account_id, as_of, balance_cents, source) VALUES (?, ?, ?, ?)",
        (account_id, as_of, st.balance_cents, source),
    )
    if prev is None:
        return None
    moved = conn.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) FROM transactions
           WHERE account_id = ? AND posted_date > ? AND posted_date <= ?""",
        (account_id, prev["as_of"], as_of),
    ).fetchone()[0]
    expected = prev["balance_cents"] + moved
    if expected != st.balance_cents:
        diff = (st.balance_cents - expected) / 100
        return (
            f"balance check failed: {prev['as_of']} balance + transactions = {expected / 100:.2f}, "
            f"bank reports {st.balance_cents / 100:.2f} on {as_of} (off by {diff:+.2f}); "
            "an export may be missing a date range"
        )
    return None


def _archive(paths: Paths, src: Path, account: str, res: ImportResult, move: bool) -> Path:
    year = (res.date_to or datetime.now().date().isoformat())[:4]
    folder = paths.archive / year
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{account}_{res.date_from or 'empty'}_{res.date_to or 'empty'}"
    ext = src.suffix.lower() or ".csv"
    dest = folder / f"{base}{ext}"
    n = 2
    while dest.exists():
        dest = folder / f"{base}_{n}{ext}"
        n += 1
    if move:
        shutil.move(str(src), dest)
    else:
        shutil.copy2(src, dest)
    return dest


def import_file(
    conn: sqlite3.Connection,
    paths: Paths,
    path: Path,
    ask: AccountResolver | None = None,
    account: str | None = None,
) -> ImportResult:
    """Import one file. `account` forces the account (it must already exist)."""
    res = ImportResult(filename=path.name, status="imported")
    digest = sha256_file(path)
    prior = conn.execute("SELECT imported_at FROM imports WHERE sha256 = ?", (digest,)).fetchone()
    if prior:
        res.status = "duplicate-file"
        res.warnings.append(f"identical file already imported at {prior['imported_at']}")
        return res
    try:
        fmt, statements = parse_file(path, load_formats(paths.formats))
    except ParseError as e:
        res.status, res.error = "error", str(e)
        return res
    res.format = fmt

    rules = rules_mod.load_rules(paths.rules)
    with conn:  # one transaction per file: a failure leaves nothing half-imported
        cur = conn.execute(
            "INSERT INTO imports (filename, sha256, format, imported_at) VALUES (?, ?, ?, ?)",
            (path.name, digest, fmt, datetime.now().isoformat(timespec="seconds")),
        )
        import_id = cur.lastrowid
        used_keys = []
        first_account_id = None
        all_new: list[int] = []
        for st in statements:
            res.warnings.extend(st.warnings)
            account_id = resolve_account(conn, path, st, fmt, ask, forced=account)
            if account_id is None:
                res.warnings.append(
                    "could not tell which account this file is; name it <account>_....csv "
                    "or pass --account (accounts: " + ", ".join(account_keys(conn)) + ")"
                )
                continue
            first_account_id = first_account_id or account_id
            used_keys.append(
                conn.execute("SELECT key FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
            )
            new_ids, dups = _insert_statement(conn, account_id, st, fmt, import_id)
            all_new.extend(new_ids)
            res.rows_new += len(new_ids)
            res.rows_dup += dups
            res.rows_pending += sum(t.pending for t in st.txns)
            warning = _record_balance(conn, account_id, st, st.source or f"westpac-{fmt}")
            if warning:
                res.warnings.append(warning)
            for d, attr in ((st.date_from, "date_from"), (st.date_to, "date_to")):
                if d is None:
                    continue
                cur_val = getattr(res, attr)
                iso = d.isoformat()
                if cur_val is None or (iso < cur_val if attr == "date_from" else iso > cur_val):
                    setattr(res, attr, iso)

        if first_account_id is None:
            conn.rollback()
            res.status = "skipped"
            return res
        res.account = ",".join(used_keys)
        rules_mod.apply_rules(conn, rules, only_ids=all_new)
        conn.execute(
            """UPDATE imports SET account_id = ?, rows_new = ?, rows_dup = ?, date_from = ?,
               date_to = ?, warnings = ? WHERE id = ?""",
            (first_account_id, res.rows_new, res.rows_dup, res.date_from, res.date_to,
             json.dumps(res.warnings), import_id),
        )

    in_inbox = path.resolve().parent == paths.inbox.resolve()
    dest = _archive(paths, path, used_keys[0] if len(used_keys) == 1 else "multi", res, move=in_inbox)
    res.archived_to = str(dest)
    conn.execute("UPDATE imports SET archived_to = ? WHERE id = ?", (str(dest), import_id))
    conn.commit()
    return res


def collect(paths: Paths, inputs: list[Path]) -> list[Path]:
    targets = inputs or [paths.inbox]
    files: list[Path] = []
    for p in targets:
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir() if f.suffix.lower() in SUPPORTED))
        elif p.exists():
            files.append(p)
        else:
            raise FileNotFoundError(p)
    return files
