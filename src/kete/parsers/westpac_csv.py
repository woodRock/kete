"""Westpac One CSV exports, header-sniffed so column renames or reordering don't break us.

Observed personal-account header (Oct 2026):
    Date,Amount,Other Party,Description,Reference,Particulars,Analysis Code
No account number, no running balance, dates dd/mm/yyyy, oldest first.

Pending card authorisations are included at the end, dated the export day:
    20/10/2026,-12.30,"UBER * PENDING","EFTPOS TRANSACTION","18/10 16:08","400000******","0000"
Once posted they reappear as DEBIT rows with different payee text (and possibly amount).
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from .common import ParseError, Statement, Txn, parse_amount, parse_date
from .formats import CsvFormat, find_format

# canonical field -> accepted header names (lower-cased, stripped)
ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "transaction date", "processed date", "posted date", "date processed"),
    "amount": ("amount", "amount (nzd)", "transaction amount"),
    "debit": ("debit", "debit amount", "withdrawals", "withdrawal"),
    "credit": ("credit", "credit amount", "deposits", "deposit"),
    "payee": ("other party", "payee", "merchant", "other party name", "details"),
    "description": ("description", "transaction type", "type", "tran type"),
    "particulars": ("particulars",),
    "code": ("analysis code", "code"),
    "reference": ("reference", "ref"),
    "balance": ("balance", "running balance"),
    "account": ("account", "account number", "account no", "account no."),
}

PENDING_REF = re.compile(r"^\d{2}/\d{2} \d{2}:\d{2}$")


def _map_header(row: list[str]) -> dict[str, int]:
    norm = [c.strip().lower() for c in row]
    mapping: dict[str, int] = {}
    for field, names in ALIASES.items():
        for i, col in enumerate(norm):
            if col in names and i not in mapping.values():
                mapping[field] = i
                break
    return mapping


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParseError(f"{path.name}: cannot decode file")


def read_rows(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(_read_text(path))))


def guess_header(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    """Skip any preamble: the header is the first row that names a date and an amount column."""
    for i, row in enumerate(rows[:15]):
        m = _map_header(row)
        if "date" in m and ("amount" in m or "debit" in m or "credit" in m):
            return i, m
    return None


_SLASHED = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}$")


def _reject_month_first(path: Path, dates: list[str]) -> None:
    """A US-style file (09/14/2026) would otherwise import its early-month rows on the wrong dates."""
    for d in dates:
        m = _SLASHED.match(d.strip())
        if m and int(m.group(2)) > 12 and int(m.group(1)) <= 12:
            raise ParseError(f"{path.name}: dates look month-first ({d}); describe the file once with: "
                             f"kete import --map {path.name}")


def parse_csv(path: Path, formats: list[CsvFormat] | None = None) -> Statement:
    rows = read_rows(path)
    saved = find_format(formats or [], rows)
    fmt: CsvFormat | None = None
    if saved:
        header_at, fmt = saved
        mapping = fmt.mapping(rows[header_at])
    else:
        guess = guess_header(rows)
        if guess is None:
            raise ParseError(f"{path.name}: no header row with Date and Amount columns found; "
                             "describe its columns once with: kete import --map " + path.name)
        header_at, mapping = guess

    header = [c.strip() for c in rows[header_at]]
    known = set(mapping.values())
    warnings = []
    extra = [h for i, h in enumerate(header) if i not in known and h]
    if extra:
        warnings.append(f"unrecognised columns kept in raw_json only: {', '.join(extra)}")

    def get(row: list[str], field: str) -> str:
        i = mapping.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    if fmt is None:
        _reject_month_first(path, [get(r, "date") for r in rows[header_at + 1:]])
    txns: list[Txn] = []
    account_numbers: set[str] = set()
    last_balance = None
    for lineno, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        if not any(c.strip() for c in row):
            continue
        raw = {header[i] if i < len(header) else f"col{i}": v for i, v in enumerate(row)}
        try:
            if fmt and fmt.date_format:
                try:
                    posted = fmt.parse_date(get(row, "date"))
                except ValueError:
                    raise ParseError(f"date {get(row, 'date')!r} doesn't match {fmt.date_format}") from None
            else:
                posted = parse_date(get(row, "date"))
            if "amount" in mapping and get(row, "amount"):
                cents = parse_amount(get(row, "amount"))
            else:
                debit, credit = get(row, "debit"), get(row, "credit")
                cents = (abs(parse_amount(credit)) if credit else 0) - (
                    abs(parse_amount(debit)) if debit else 0
                )
            if fmt and fmt.negate:
                cents = -cents
        except ParseError as e:
            warnings.append(f"line {lineno}: skipped ({e})")
            continue
        if get(row, "account"):
            account_numbers.add(get(row, "account"))
        if get(row, "balance"):
            try:
                last_balance = (parse_amount(get(row, "balance")), posted)
            except ParseError:
                pass
        txns.append(
            Txn(
                posted_date=posted,
                amount_cents=cents,
                payee=get(row, "payee"),
                description=get(row, "description"),
                particulars=get(row, "particulars"),
                code=get(row, "code"),
                reference=get(row, "reference"),
                pending=get(row, "description").upper() == "EFTPOS TRANSACTION"
                and bool(PENDING_REF.match(get(row, "reference"))),
                raw=raw,
            )
        )

    if len(account_numbers) > 1:
        raise ParseError(f"{path.name}: file contains several accounts; export one at a time")
    st = Statement(txns=txns, account_number=next(iter(account_numbers), None), warnings=warnings,
                   source=f"{fmt.name}-csv" if fmt else None)
    # A running balance is only trustworthy as "closing" if rows are oldest-first.
    if last_balance and txns and txns[-1].posted_date >= txns[0].posted_date:
        st.balance_cents, st.balance_date = last_balance
    return st
