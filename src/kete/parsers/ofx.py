"""OFX/QFX via ofxparse. Carries FITID (stable bank txn id) and LEDGERBAL."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from .common import ParseError, Statement, Txn


def _cents(value: Decimal) -> int:
    return int((Decimal(value) * 100).to_integral_value())


def parse_ofx(path: Path) -> list[Statement]:
    import ofxparse

    try:
        with path.open("rb") as fh:
            ofx = ofxparse.OfxParser.parse(fh)
    except Exception as e:  # ofxparse raises a grab-bag of exception types
        raise ParseError(f"{path.name}: invalid OFX ({e})") from e

    statements = []
    for acct in ofx.accounts:
        stmt = acct.statement
        txns = []
        for t in stmt.transactions:
            raw = {
                "id": t.id,
                "type": t.type,
                "date": t.date.isoformat() if t.date else None,
                "amount": str(t.amount),
                "payee": t.payee,
                "memo": t.memo,
                "checknum": getattr(t, "checknum", ""),
            }
            txns.append(
                Txn(
                    posted_date=t.date.date(),
                    amount_cents=_cents(t.amount),
                    payee=(t.payee or "").strip(),
                    description=(t.memo or "").strip(),
                    reference=(getattr(t, "checknum", "") or "").strip(),
                    fitid=t.id,
                    raw=raw,
                )
            )
        st = Statement(txns=txns, account_number=acct.account_id)
        if getattr(stmt, "balance", None) is not None and getattr(stmt, "balance_date", None):
            st.balance_cents = _cents(stmt.balance)
            st.balance_date = stmt.balance_date.date()
        statements.append(st)
    if not statements:
        raise ParseError(f"{path.name}: OFX contains no statements")
    return statements
