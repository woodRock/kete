"""Undo history for changes to categories, transfers, shares and rules.toml.

Wrap any edit in `tracked()`: it snapshots every transaction's categorisation state before and
after, and stores only what changed. `undo()` puts the latest change back. Imports are not
tracked (re-importing is how you change those).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

STATE_COLS = ("category", "category_source", "rule_index", "is_transfer", "transfer_pair_id", "shared_cents")


class UndoError(Exception):
    pass


def _state(conn: sqlite3.Connection) -> dict[int, tuple]:
    cols = ", ".join(STATE_COLS)
    return {r[0]: tuple(r[1:]) for r in conn.execute(f"SELECT id, {cols} FROM transactions")}


def _shares(conn: sqlite3.Connection) -> set[tuple]:
    return {tuple(r) for r in conn.execute("SELECT tx_id, person, cents, note, created_at FROM shares")}


def _read(path: Path | None) -> str | None:
    return path.read_text() if path and path.exists() else None


@contextmanager
def tracked(conn: sqlite3.Connection, action: str, summary: str, rules_path: Path | None = None):
    """Record what the body changes as one undoable step. Commits on success."""
    before, shares_before, rules_before = _state(conn), _shares(conn), _read(rules_path)
    ids_before = set(before)
    yield
    after, shares_after, rules_after = _state(conn), _shares(conn), _read(rules_path)
    rows = [{"id": i, "before": list(before[i]), "after": list(after[i])}
            for i in before if i in after and after[i] != before[i]]
    inserted = sorted(set(after) - ids_before)
    payload = {
        "rows": rows,
        "inserted": inserted,
        "shares_removed": sorted(map(list, shares_before - shares_after)),
        "shares_added": sorted(map(list, shares_after - shares_before)),
    }
    if rules_path is not None and rules_after != rules_before:
        payload["rules_path"], payload["rules_before"], payload["rules_after"] = (
            str(rules_path), rules_before, rules_after)
    if rows or inserted or payload["shares_removed"] or payload["shares_added"] or "rules_path" in payload:
        conn.execute("INSERT INTO changes (at, action, summary, payload) VALUES (?, ?, ?, ?)",
                     (datetime.now().isoformat(timespec="seconds"), action, summary, json.dumps(payload)))
    conn.commit()


def log(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,)):
        p = json.loads(r["payload"])
        out.append({"id": r["id"], "at": r["at"], "action": r["action"], "summary": r["summary"],
                    "transactions": len(p["rows"]) + len(p["inserted"]),
                    "rules_file": "rules_path" in p, "undone": r["undone_at"] is not None})
    return out


def undo(conn: sqlite3.Connection, force: bool = False) -> dict | None:
    """Revert the most recent change that hasn't been undone. Refuses if rules.toml was edited
    by hand since (unless force), rather than throwing those edits away."""
    row = conn.execute("SELECT * FROM changes WHERE undone_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    p = json.loads(row["payload"])
    rules_path = Path(p["rules_path"]) if "rules_path" in p else None
    if rules_path and _read(rules_path) != p["rules_after"] and not force:
        raise UndoError(f"{rules_path} has changed since “{row['summary']}”; "
                        "undo with --force to overwrite it anyway")
    sets = ", ".join(f"{c} = ?" for c in STATE_COLS)
    conn.executemany(f"UPDATE transactions SET {sets} WHERE id = ?",
                     [(*r["before"], r["id"]) for r in p["rows"]])
    for tx_id, person, *_ in p["shares_added"]:
        conn.execute("DELETE FROM shares WHERE tx_id = ? AND person = ?", (tx_id, person))
    conn.executemany("INSERT OR REPLACE INTO shares (tx_id, person, cents, note, created_at) VALUES (?, ?, ?, ?, ?)",
                     [tuple(s) for s in p["shares_removed"]])
    if p["inserted"]:
        marks = ",".join("?" * len(p["inserted"]))
        conn.execute(f"DELETE FROM shares WHERE tx_id IN ({marks})", p["inserted"])
        conn.execute(f"DELETE FROM transactions WHERE id IN ({marks})", p["inserted"])
    if rules_path:
        if p["rules_before"] is None:
            rules_path.unlink(missing_ok=True)
        else:
            tmp = rules_path.with_suffix(".toml.tmp")
            tmp.write_text(p["rules_before"])
            tmp.replace(rules_path)
    conn.execute("UPDATE changes SET undone_at = ? WHERE id = ?",
                 (datetime.now().isoformat(timespec="seconds"), row["id"]))
    conn.commit()
    return {"id": row["id"], "action": row["action"], "summary": row["summary"],
            "transactions": len(p["rows"]) + len(p["inserted"]), "rules_file": rules_path is not None}
