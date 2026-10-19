"""Import bank exports as soon as they land in a folder (usually ~/Downloads), via launchd.

`kete autoimport install` writes a LaunchAgent that watches the folder. When it changes,
launchd runs `kete autoimport run`, which moves recognisable exports (OFX/QFX, or CSVs whose
header kete can read) into the inbox and imports them. Other files are never touched. Results
go to a macOS notification; nothing leaves the machine.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .parsers import detect_format
from .parsers.formats import find_format, load_formats
from .paths import Paths

LABEL = "com.github.kete.autoimport"
MAX_AGE_HOURS = 24  # only files downloaded recently; an old CSV in Downloads isn't a new export


def plist_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def kete_executable() -> str:
    found = shutil.which("kete")
    if found:
        return found
    argv0 = Path(sys.argv[0])
    return str(argv0.resolve()) if argv0.exists() else "kete"


def build_plist(folder: Path, paths: Paths, executable: str | None = None) -> dict:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if os.environ.get("KETE_HOME"):
        env["KETE_HOME"] = str(paths.home)
    return {
        "Label": LABEL,
        "ProgramArguments": [executable or kete_executable(), "autoimport", "run", "--folder", str(folder)],
        "WatchPaths": [str(folder)],
        "ThrottleInterval": 10,
        "RunAtLoad": False,
        "EnvironmentVariables": env,
        "StandardOutPath": str(paths.home / "autoimport.log"),
        "StandardErrorPath": str(paths.home / "autoimport.log"),
    }


def looks_like_export(path: Path, paths: Paths) -> bool:
    """OFX/QFX, or a CSV whose header names a date and amount (or matches a saved format)."""
    if path.suffix.lower() not in (".csv", ".ofx", ".qfx") or path.name.startswith("."):
        return False
    try:
        fmt = detect_format(path)
    except OSError:
        return False
    if fmt == "ofx":
        return True
    if fmt != "csv":
        return False
    from .parsers.westpac_csv import guess_header, read_rows

    try:
        rows = read_rows(path)
    except Exception:
        return False
    if find_format(load_formats(paths.formats), rows):
        return True
    guess = guess_header(rows)
    # A bank export: date + amount + who the money went to or came from.
    return bool(guess and {"payee", "particulars", "description"} & set(guess[1]))


def candidates(folder: Path, paths: Paths, now: float | None = None) -> list[Path]:
    now = now or time.time()
    out = []
    for p in sorted(folder.iterdir()) if folder.is_dir() else []:
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_file() or now - st.st_mtime > MAX_AGE_HOURS * 3600:
            continue
        if now - st.st_mtime < 2:  # still being written
            time.sleep(2)
        if looks_like_export(p, paths):
            out.append(p)
    return out


def move_to_inbox(files: list[Path], paths: Paths) -> list[Path]:
    paths.ensure()
    moved = []
    for f in files:
        dest = paths.inbox / f.name
        n = 2
        while dest.exists():
            dest = paths.inbox / f"{f.stem} ({n}){f.suffix}"
            n += 1
        shutil.move(str(f), dest)
        moved.append(dest)
    return moved


def notify(title: str, message: str) -> None:
    """A local macOS notification (osascript); silently skipped elsewhere."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return
    script = f"display notification {_as(message)} with title {_as(title)}"
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _as(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def install(folder: Path, paths: Paths, load: bool = True) -> Path:
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if load and target.exists():
        uninstall(remove=False)
    with target.open("wb") as fh:
        plistlib.dump(build_plist(folder, paths), fh)
    if load:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
    return target


def uninstall(remove: bool = True) -> bool:
    target = plist_path()
    if not target.exists():
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)], check=False,
                   capture_output=True)
    if remove:
        target.unlink()
    return True


def status() -> dict:
    target = plist_path()
    if not target.exists():
        return {"installed": False}
    with target.open("rb") as fh:
        data = plistlib.load(fh)
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False,
                            capture_output=True).returncode == 0
    return {"installed": True, "loaded": loaded, "folder": data.get("WatchPaths", [None])[0],
            "plist": str(target)}
