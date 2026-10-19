"""Filesystem layout. KETE_HOME overrides the default ~/kete (used by tests)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CLOUD_MARKERS = ("Library/Mobile Documents", "iCloud", "Dropbox", "OneDrive", "Google Drive")


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def inbox(self) -> Path:
        return self.home / "inbox"

    @property
    def archive(self) -> Path:
        return self.home / "archive"

    @property
    def db(self) -> Path:
        return self.home / "kete.db"

    @property
    def rules(self) -> Path:
        return self.home / "rules.toml"

    @property
    def formats(self) -> Path:
        return self.home / "formats.toml"

    def ensure(self) -> None:
        for d in (self.home, self.inbox, self.archive):
            d.mkdir(parents=True, exist_ok=True)
        # The database is the user's full financial history; keep it private.
        os.chmod(self.home, 0o700)

    def cloud_synced(self) -> bool:
        resolved = str(self.home.resolve())
        return any(m in resolved for m in CLOUD_MARKERS)


def get_paths() -> Paths:
    return Paths(Path(os.environ.get("KETE_HOME", "~/kete")).expanduser())
