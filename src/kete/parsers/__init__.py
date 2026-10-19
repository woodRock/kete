"""Export-file parsers. Each returns a list of Statements (one per account in the file)."""

from __future__ import annotations

from pathlib import Path

from .common import ParseError, Statement, Txn


def detect_format(path: Path) -> str:
    head = path.read_bytes()[:2048].lstrip().upper()
    if head.startswith(b"OFXHEADER") or b"<OFX>" in head or head.startswith(b"<?XML"):
        return "ofx"
    if head.startswith(b"!TYPE"):
        return "qif"
    if path.suffix.lower() in (".ofx", ".qfx"):
        return "ofx"
    return "csv"


def parse_file(path: Path) -> tuple[str, list[Statement]]:
    fmt = detect_format(path)
    if fmt == "ofx":
        from .ofx import parse_ofx

        return fmt, parse_ofx(path)
    if fmt == "csv":
        from .westpac_csv import parse_csv

        return fmt, [parse_csv(path)]
    raise ParseError(f"{path.name}: {fmt.upper()} is not supported; export OFX or CSV instead")


__all__ = ["ParseError", "Statement", "Txn", "detect_format", "parse_file"]
