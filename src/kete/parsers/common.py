from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


class ParseError(Exception):
    pass


@dataclass
class Txn:
    posted_date: date
    amount_cents: int
    payee: str = ""
    description: str = ""
    particulars: str = ""
    code: str = ""
    reference: str = ""
    fitid: str | None = None
    pending: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class Statement:
    txns: list[Txn]
    account_number: str | None = None  # OFX ACCTID or a CSV account column
    balance_cents: int | None = None  # closing (ledger) balance
    balance_date: date | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def date_from(self) -> date | None:
        return min((t.posted_date for t in self.txns), default=None)

    @property
    def date_to(self) -> date | None:
        return max((t.posted_date for t in self.txns), default=None)


_DAY_FIRST = ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d")


def parse_date(text: str) -> date:
    """Day-first only. A month-first guess would silently corrupt every date <= 12th."""
    text = text.strip()
    for fmt in _DAY_FIRST:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"unrecognised date {text!r} (expected dd/mm/yyyy)")


def parse_amount(text: str) -> int:
    """'$1,234.50' -> 123450, '(4.50)' / '4.50 DR' / '-4.50' -> -450."""
    s = text.strip().replace("$", "").replace(",", "").replace(" ", "").replace("NZD", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        s, neg = s[1:-1], True
    up = s.upper()
    if up.endswith("DR"):
        s, neg = s[:-2], True
    elif up.endswith("CR"):
        s = s[:-2]
    try:
        value = Decimal(s)
    except InvalidOperation as e:
        raise ParseError(f"unrecognised amount {text!r}") from e
    cents = int((value * 100).to_integral_value())
    return -abs(cents) if neg else cents


_WS = re.compile(r"\s+")


def normalise_text(*parts: str) -> str:
    return "|".join(_WS.sub(" ", p).strip().lower() for p in parts)
