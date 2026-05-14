"""
Persistent FIFO queue for webhook payloads and retry requests.

Distinct from ``retry_queue.py`` which is specifically for items deferred
because of AudioVault's daily download cap. This queue holds everything
the server has *received* but not yet processed — Sonarr/Radarr webhook
payloads, manual /retry requests, scheduled drains. Persisting them on
arrival means a container restart in the middle of an overnight batch
no longer silently loses work.

State is a JSON list of dicts on disk; writes are atomic (sibling .tmp +
os.replace) so a SIGKILL mid-write can't leave a corrupt queue file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PendingQueue:
    """FIFO queue persisted as a JSON list. Thread-safe within one process."""

    def __init__(self, state_path: Path) -> None:
        self._path = state_path
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, ValueError):
            logger.warning("Corrupt pending queue at %s — discarding.", self._path)
            return []

    def _save(self, items: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(items, indent=2))
        os.replace(tmp, self._path)

    def load(self) -> list[dict]:
        """Return a snapshot of the queue (callers must not mutate the on-disk state)."""
        with self._lock:
            return self._load()

    def push(self, item: dict) -> int:
        with self._cv:
            items = self._load()
            items.append(item)
            self._save(items)
            self._cv.notify_all()
            return len(items)

    def push_front(self, item: dict) -> None:
        with self._cv:
            items = self._load()
            items.insert(0, item)
            self._save(items)
            self._cv.notify_all()

    def pop_first(self) -> Optional[dict]:
        with self._lock:
            items = self._load()
            if not items:
                return None
            item = items.pop(0)
            self._save(items)
            return item

    def size(self) -> int:
        with self._lock:
            return len(self._load())

    def wait_for_item(self, timeout: float = 10.0) -> None:
        """Block up to *timeout* seconds for an item to appear. Returns early
        on push() via the Condition's notify."""
        with self._cv:
            if not self._load():
                self._cv.wait(timeout=timeout)
