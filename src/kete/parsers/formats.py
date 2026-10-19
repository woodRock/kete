"""Saved CSV layouts for exports kete doesn't recognise on its own (other banks, credit cards).

Stored in ~/kete/formats.toml, one [[format]] per layout, recognised by its exact header row:

    [[format]]
    name = "visa"
    header = ["Transaction Date", "Details", "Amount"]
    negate = true                # charges are positive numbers in this export
    date_format = "%d/%m/%Y"     # optional; default tries day-first formats
    [format.columns]
    date = "Transaction Date"
    amount = "Amount"            # or debit = "..." and credit = "..."
    payee = "Details"
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .common import ParseError

FIELDS = ("date", "amount", "debit", "credit", "payee", "description", "particulars", "code",
          "reference", "balance")
# Offered when a file's dates aren't day-first: only used when you choose them explicitly.
DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%m/%d/%Y", "%Y/%m/%d")


def norm_header(row: list[str]) -> tuple[str, ...]:
    return tuple(c.strip().lower() for c in row)


@dataclass
class CsvFormat:
    name: str
    header: list[str]
    columns: dict[str, str]
    negate: bool = False
    date_format: str | None = None
    extra: dict = field(default_factory=dict)

    def mapping(self, header_row: list[str]) -> dict[str, int]:
        cols = norm_header(header_row)
        out = {}
        for f, col in self.columns.items():
            if col.strip().lower() not in cols:
                raise ParseError(f"format {self.name!r}: column {col!r} is missing")
            out[f] = cols.index(col.strip().lower())
        return out

    def parse_date(self, text: str):
        return datetime.strptime(text.strip(), self.date_format).date()

    def to_toml(self) -> str:
        q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731 - JSON strings are TOML strings
        lines = ["[[format]]", f"name = {q(self.name)}",
                 "header = [" + ", ".join(q(h) for h in self.header) + "]",
                 f"negate = {'true' if self.negate else 'false'}"]
        if self.date_format:
            lines.append(f"date_format = {q(self.date_format)}")
        lines.append("[format.columns]")
        lines += [f"{k} = {q(v)}" for k, v in self.columns.items()]
        return "\n".join(lines) + "\n"


def validate(fmt: CsvFormat) -> None:
    unknown = set(fmt.columns) - set(FIELDS)
    if unknown:
        raise ParseError(f"format {fmt.name!r}: unknown fields {sorted(unknown)}")
    if "date" not in fmt.columns or not ({"amount"} & set(fmt.columns) or {"debit", "credit"} & set(fmt.columns)):
        raise ParseError(f"format {fmt.name!r}: needs a date column and amount (or debit/credit) columns")
    lowered = norm_header(fmt.header)
    for col in fmt.columns.values():
        if col.strip().lower() not in lowered:
            raise ParseError(f"format {fmt.name!r}: column {col!r} isn't in its header")


def load_formats(path: Path | None) -> list[CsvFormat]:
    if not path or not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ParseError(f"{path}: {e}") from e
    out = []
    for i, f in enumerate(data.get("format", []), start=1):
        try:
            fmt = CsvFormat(name=f["name"], header=list(f["header"]), columns=dict(f["columns"]),
                            negate=bool(f.get("negate", False)), date_format=f.get("date_format"))
        except KeyError as e:
            raise ParseError(f"{path}: format #{i} is missing {e}") from e
        validate(fmt)
        out.append(fmt)
    return out


def find_format(formats: list[CsvFormat], rows: list[list[str]]) -> tuple[int, CsvFormat] | None:
    """(header row index, format) for the first saved format whose header appears in the file."""
    by_header = {norm_header(f.header): f for f in formats}
    for i, row in enumerate(rows[:15]):
        f = by_header.get(norm_header(row))
        if f:
            return i, f
    return None


def save_format(path: Path, fmt: CsvFormat) -> None:
    """Add a format, replacing any with the same name or the same header."""
    validate(fmt)
    kept = [f for f in load_formats(path)
            if f.name != fmt.name and norm_header(f.header) != norm_header(fmt.header)]
    text = "# CSV layouts saved by `kete import --map` or `kete formats add`. Edit freely.\n\n"
    text += "\n".join(f.to_toml() for f in kept + [fmt])
    tomllib.loads(text)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def remove_format(path: Path, name: str) -> bool:
    formats = load_formats(path)
    kept = [f for f in formats if f.name != name]
    if len(kept) == len(formats):
        return False
    text = "# CSV layouts saved by `kete import --map` or `kete formats add`. Edit freely.\n\n"
    path.write_text(text + "\n".join(f.to_toml() for f in kept))
    return True
