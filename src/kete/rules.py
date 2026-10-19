"""Categorisation rules from rules.toml. Ordered; first match wins.

Rules only ever write category / is_transfer / rule_index. They never change amounts or dates,
and never overwrite a category set by hand (category_source = 'manual') or by transfer pairing.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

TEXT_FIELDS = ("payee", "description", "particulars", "code", "reference")
CONDITION_KEYS = set(TEXT_FIELDS) | {"any", "account", "amount_min", "amount_max"}


class RulesError(Exception):
    pass


@dataclass
class Rule:
    index: int
    name: str
    conditions: dict
    category: str | None
    transfer: bool
    regex: bool = False
    _compiled: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for key in CONDITION_KEYS & set(self.conditions):
            if key in ("amount_min", "amount_max"):
                continue
            values = self.conditions[key]
            values = [values] if isinstance(values, str) else list(values)
            if self.regex:
                self._compiled[key] = [re.compile(v, re.I) for v in values]
            else:
                self._compiled[key] = [v.lower() for v in values]

    def _hit(self, key: str, text: str) -> bool:
        pats = self._compiled[key]
        if self.regex:
            return any(p.search(text) for p in pats)
        low = text.lower()
        return any(p in low for p in pats)

    def matches(self, tx: dict, check_amount: bool = True) -> bool:
        if not self.conditions:
            return False
        for key in self._compiled:
            if key == "any":
                if not any(self._hit("any", tx.get(f) or "") for f in TEXT_FIELDS):
                    return False
            elif key == "account":
                if (tx.get("account") or "").lower() not in self._compiled["account"]:
                    return False
            elif not self._hit(key, tx.get(key) or ""):
                return False
        if check_amount and tx.get("amount_cents") is not None:
            cents = tx["amount_cents"]
            if "amount_min" in self.conditions and cents < round(self.conditions["amount_min"] * 100):
                return False
            if "amount_max" in self.conditions and cents > round(self.conditions["amount_max"] * 100):
                return False
        return True

    def describe(self) -> str:
        what = "transfer" if self.transfer else f"category={self.category}"
        conds = ", ".join(f"{k}={v!r}" for k, v in self.conditions.items())
        return f"#{self.index} {self.name or ''} [{conds}] -> {what}".replace("  ", " ")


def starter_rules() -> str:
    return resources.files("kete").joinpath("rules_starter.toml").read_text()


def ensure_rules_file(path: Path) -> None:
    if not path.exists():
        path.write_text(starter_rules())


def load_rules(path: Path) -> list[Rule]:
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise RulesError(f"{path}: {e}") from e
    out = []
    for i, r in enumerate(data.get("rule", []), start=1):
        unknown = set(r) - CONDITION_KEYS - {"name", "category", "transfer", "regex"}
        if unknown:
            raise RulesError(f"rule #{i}: unknown keys {sorted(unknown)}")
        if not r.get("category") and not r.get("transfer"):
            raise RulesError(f"rule #{i}: needs category = \"...\" or transfer = true")
        conditions = {k: v for k, v in r.items() if k in CONDITION_KEYS}
        if not conditions:
            raise RulesError(f"rule #{i}: has no match conditions")
        out.append(
            Rule(
                index=i,
                name=r.get("name", ""),
                conditions=conditions,
                category=r.get("category") or ("Transfer" if r.get("transfer") else None),
                transfer=bool(r.get("transfer")),
                regex=bool(r.get("regex")),
            )
        )
    # "groceries" in one rule and "Groceries" in another are the same category: first spelling wins.
    spelling: dict[str, str] = {}
    for rule in out:
        if rule.category:
            rule.category = spelling.setdefault(rule.category.lower(), rule.category)
    return out


def known_categories(conn: sqlite3.Connection, rules: list[Rule]) -> list[str]:
    """Categories on transactions plus those only named in rules, one spelling each."""
    seen: dict[str, str] = {}
    names = [r[0] for r in conn.execute(
        "SELECT category FROM transactions WHERE category IS NOT NULL GROUP BY category "
        "ORDER BY COUNT(*) DESC")]
    for name in names + [r.category for r in rules if r.category]:
        seen.setdefault(name.lower(), name)
    return sorted(seen.values(), key=str.lower)


def canonical_category(name: str, known: list[str]) -> str:
    """Typed names reuse an existing category's spelling regardless of case."""
    name = " ".join(name.split())
    return next((k for k in known if k.lower() == name.lower()), name)


_CATEGORY_LINE = re.compile(r"""^(\s*category\s*=\s*)("(?:[^"\\]|\\.)*"|'[^']*')(.*)$""")


def rename_in_file(path: Path, old: str, new: str) -> int:
    """Rewrite `category = "Old"` lines in place, keeping comments and layout."""
    if not path.exists():
        return 0
    lines = path.read_text().splitlines(keepends=True)
    changed = 0
    for i, line in enumerate(lines):
        m = _CATEGORY_LINE.match(line.rstrip("\n"))
        if not m:
            continue
        literal = m.group(2)
        value = json.loads(literal) if literal.startswith('"') else literal[1:-1]
        if value.lower() == old.lower():
            ending = "\n" if line.endswith("\n") else ""
            lines[i] = f"{m.group(1)}{toml_string(new)}{m.group(3)}{ending}"
            changed += 1
    if changed:
        text = "".join(lines)
        tomllib.loads(text)  # never write a file that no longer parses
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(text)
        tmp.replace(path)
    return changed


def rename_category(conn: sqlite3.Connection, path: Path, old: str, new: str) -> dict:
    """Rename (or merge into an existing category) everywhere: transactions and rules.toml."""
    if old.lower() == "transfer" or new.lower() == "transfer":
        raise RulesError("Transfer is reserved; use transfer = true in a rule or the t key")
    new = canonical_category(new, [k for k in known_categories(conn, load_rules(path))
                                   if k.lower() != old.lower()])
    if not new:
        raise RulesError("new category name is empty")
    rules_changed = rename_in_file(path, old, new)
    cur = conn.execute("UPDATE transactions SET category = ? WHERE lower(category) = lower(?)",
                       (new, old))
    conn.commit()
    return {"from": old, "to": new, "transactions": cur.rowcount, "rules": rules_changed}


def first_match(rules: list[Rule], tx: dict, check_amount: bool = True) -> Rule | None:
    for rule in rules:
        if rule.matches(tx, check_amount):
            return rule
    return None


def apply_rules(conn: sqlite3.Connection, rules: list[Rule], only_ids: list[int] | None = None) -> dict:
    """Re-run rules over every rule-owned (or uncategorised) transaction."""
    sql = """SELECT t.id, t.payee, t.description, t.particulars, t.code, t.reference,
                    t.amount_cents, a.key AS account
             FROM transactions t JOIN accounts a ON a.id = t.account_id
             WHERE (t.category_source IS NULL OR t.category_source = 'rule')"""
    params: list = []
    if only_ids is not None:
        if not only_ids:
            return {"matched": 0, "unmatched": 0}
        sql += f" AND t.id IN ({','.join('?' * len(only_ids))})"
        params = only_ids
    matched = unmatched = 0
    updates = []
    for row in conn.execute(sql, params).fetchall():
        rule = first_match(rules, dict(row))
        if rule:
            matched += 1
            updates.append((rule.category, "rule", rule.index, int(rule.transfer), row["id"]))
        else:
            unmatched += 1
            updates.append((None, None, None, 0, row["id"]))
    conn.executemany(
        """UPDATE transactions SET category = ?, category_source = ?, rule_index = ?,
           is_transfer = ? WHERE id = ?""",
        updates,
    )
    conn.commit()
    return {"matched": matched, "unmatched": unmatched}


def toml_string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON strings are valid TOML basic strings


def append_rule(path: Path, *, payee: str, category: str) -> None:
    """Used by the TUI's 'create rule from payee'. Appends so existing order is preserved."""
    block = (
        f"\n[[rule]]\nname = {toml_string(payee)}\npayee = {toml_string(payee)}\n"
        f"category = {toml_string(category)}\n"
    )
    ensure_rules_file(path)
    with path.open("a") as fh:
        fh.write(block)
