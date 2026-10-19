"""Local HTML dashboard.

build_model() (model.py) computes every number from the database; render() inlines that JSON plus
assets/style.css and assets/app.js into assets/template.html, producing a single file that works
offline. A Content-Security-Policy forbids all network access, so the page cannot send data anywhere.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from importlib import resources
from pathlib import Path

from .model import DEFAULT_CONFIG, build_model, load_config

__all__ = ["DEFAULT_CONFIG", "build_model", "load_config", "render", "generate"]


def _asset(name: str) -> str:
    return resources.files("kete.dashboard").joinpath("assets", name).read_text()


def render(model: dict) -> str:
    # "</" would end the <script> block early; JSON allows the escaped form.
    data = json.dumps(model, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = _asset("template.html")
    for marker, value in (("/*__STYLE__*/", _asset("style.css")),
                          ("/*__SCRIPT__*/", _asset("app.js")),
                          ("__DATA__", data)):
        if marker not in html:
            raise ValueError(f"template.html is missing the {marker} marker")
        html = html.replace(marker, value, 1)
    return html


def generate(conn: sqlite3.Connection, out: Path, rules_path: Path | None = None,
             today: date | None = None, demo: bool = False) -> Path:
    model = build_model(conn, load_config(rules_path), today=today, demo=demo)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(model))
    os.chmod(out, 0o600)  # it is a full summary of your finances
    return out
