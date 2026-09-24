"""The latest outcome for each file describarr has worked on.

A request for a description is a promise with no ending: whoever asked is told
the described track "will appear when it is ready", and when no source has
one it never appears and nothing says so. Defender asked for Heat's described
track on 2026-09-18 and again on 09-23 (audit 2026-09-23, feature 5). The
decision log cannot answer "what became of this one": it is a short ring for
the operator, keyed by a display label. This keeps one entry per file, so a
client can ask about a title and be told.

Keyed by the file's name, not its path. The same film arrives under different
mount points depending on who asked -- Radarr's hook names ``/movies/...``,
a request relayed from Jellyfin names ``/media/movies/...`` -- and a publish
rewrites the file in place, so its size and inode change while its name does
not.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

#: Enough for every film and episode in a large library; the oldest go first.
DEFAULT_MAX_ENTRIES = 20000

#: Beside the decision log in the cache directory.
OUTCOMES_FILENAME = "outcomes.json"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def key(path: str | Path) -> str:
    """The name a file is known by here, whatever mount it was reached through."""
    return Path(str(path)).name.casefold()


class OutcomeLog:
    """One entry per file: what happened the last time it was worked on."""

    def __init__(self, state_path: Path, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._path = state_path
        self._max = max(1, int(max_entries))

    @classmethod
    def in_cache(cls, cache_dir: Path) -> "OutcomeLog":
        return cls(Path(cache_dir) / OUTCOMES_FILENAME)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, ValueError, OSError):
            logger.warning("Corrupt outcome log at %s; starting afresh.", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def record(self, path: str | Path, outcome: str, detail: str = "",
               label: str = "") -> None:
        """Keep this outcome as the file's latest. Never raises: losing the
        record must not fail the work it describes."""
        try:
            with _LOCK:
                entries = self._load()
                entries[key(path)] = {
                    "outcome": outcome,
                    "detail": detail or "",
                    "label": label or "",
                    "path": str(path),
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                if len(entries) > self._max:
                    oldest = sorted(entries, key=lambda k: entries[k].get("at", ""))
                    for stale in oldest[: len(entries) - self._max]:
                        del entries[stale]
                _atomic_write_text(self._path, json.dumps(entries, indent=1))
        except Exception:
            logger.debug("Outcome-log write failed.", exc_info=True)

    def get(self, path: str | Path) -> Optional[dict]:
        with _LOCK:
            return self._load().get(key(path))
