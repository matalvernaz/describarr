"""Append-only, size-capped log of describarr's per-item accept / reject /
skip decisions.

Surfaced at ``/status`` as a screen-reader-friendly audit trail so a blind
operator can review what happened overnight — which files were described,
which were rejected and why, which found no audio description — without
grepping multi-megabyte container logs. It replaces the human who, using
describealign manually, would eyeball each alignment plot and score.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# The single worker thread writes these and the HTTP status handler reads them;
# a module-level lock keeps the read-modify-write append atomic across threads
# regardless of how many DecisionLog instances get constructed.
_LOCK = threading.Lock()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write via sibling .tmp + fsync + os.replace so a crash mid-write can't
    leave a truncated log and the rename is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class DecisionLog:
    """Bounded ring of recent decision entries persisted as a JSON list,
    newest last. ``append`` trims to *max_entries* so the file can't grow
    without bound."""

    def __init__(self, state_path: Path, max_entries: int = 50) -> None:
        self._path = state_path
        self._max = max(0, int(max_entries))

    def load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, ValueError, OSError):
            logger.warning("Corrupt decision log at %s — ignoring.", self._path)
            return []
        return data if isinstance(data, list) else []

    def append(self, entry: dict) -> None:
        if self._max == 0:
            return
        record = dict(entry)
        record.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        with _LOCK:
            items = self.load()
            items.append(record)
            if len(items) > self._max:
                items = items[-self._max:]
            _atomic_write_text(self._path, json.dumps(items, indent=2))

    def recent(self, limit: int = 0) -> list[dict]:
        """Return entries newest-first, optionally capped to *limit*."""
        items = list(reversed(self.load()))
        return items[:limit] if limit > 0 else items
