"""
Persistent FIFO queue for webhook payloads and retry requests.

Distinct from ``retry_queue.py`` which is specifically for items deferred
because of AudioVault's daily download cap. This queue holds everything
the server has *received* but not yet processed — Sonarr/Radarr webhook
payloads, manual /retry requests, scheduled drains. Persisting them on
arrival means a container restart in the middle of an overnight batch
no longer silently loses work.

State is two JSON files written atomically (sibling ``.tmp`` + ``os.replace``)
so a SIGKILL mid-write can't leave a corrupt queue file:

  - ``<state>.json`` — the FIFO of pending items.
  - ``<state>.inflight.json`` — items the worker has *claimed* but not yet
    acknowledged. A worker crash between claim and ack used to silently
    drop the popped item (Sonarr saw 202, nothing happened to the file);
    now restart will see the in-flight file and push it back to the front
    of the pending queue before the worker resumes.
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
    """FIFO queue persisted as a JSON list. Thread-safe within one process.

    Use :meth:`claim_first` + :meth:`ack` / :meth:`requeue` instead of
    :meth:`pop_first` when the consumer can crash between claim and
    completion: claim takes the head item but parks it in a sibling
    ``<state>.inflight.json`` file. If the process dies before ack, the
    next ``PendingQueue`` instance pushes the unacked item back to the
    front of the queue on construction, restoring at-least-once semantics.
    """

    def __init__(self, state_path: Path) -> None:
        self._path = state_path
        self._inflight_path = state_path.with_suffix(state_path.suffix + ".inflight")
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        # Best-effort recovery: any item that was claimed before a crash
        # gets pushed back to the front of the pending queue. We do this
        # under the same atomic-write discipline as the rest of the class.
        self._recover_inflight_locked()

    # ── persistence helpers ────────────────────────────────────────────

    @staticmethod
    def _read_json_list(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError):
            logger.warning("Corrupt queue file at %s — discarding.", path)
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def _write_json_list(path: Path, items: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(items, indent=2))
        os.replace(tmp, path)

    def _load(self) -> list[dict]:
        return self._read_json_list(self._path)

    def _save(self, items: list[dict]) -> None:
        self._write_json_list(self._path, items)

    def _load_inflight(self) -> list[dict]:
        return self._read_json_list(self._inflight_path)

    def _save_inflight(self, items: list[dict]) -> None:
        if items:
            self._write_json_list(self._inflight_path, items)
        else:
            self._inflight_path.unlink(missing_ok=True)

    def _recover_inflight_locked(self) -> None:
        """Push any leftover in-flight item(s) back to the front of pending.

        Called once from __init__. Holding the lock isn't strictly necessary
        at construction (no other thread has the instance yet) but keeps
        the invariant explicit for future readers.
        """
        leftover = self._load_inflight()
        if not leftover:
            return
        with self._lock:
            items = self._load()
            # Preserve original order (oldest claim first) by prepending in
            # reverse so the first leftover ends up at index 0.
            for item in reversed(leftover):
                items.insert(0, item)
            self._save(items)
            self._save_inflight([])
        logger.warning(
            "Recovered %d in-flight pending item(s) on startup — likely a previous crash mid-process.",
            len(leftover),
        )

    # ── public read-only ───────────────────────────────────────────────

    def load(self) -> list[dict]:
        """Return a snapshot of the pending list (excludes in-flight items)."""
        with self._lock:
            return self._load()

    def size(self) -> int:
        with self._lock:
            return len(self._load())

    # ── enqueue ────────────────────────────────────────────────────────

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

    # ── claim/ack lifecycle ────────────────────────────────────────────

    def claim_first(self) -> Optional[dict]:
        """Atomically move the head item to the in-flight file and return it.

        If the worker dies before calling :meth:`ack` or :meth:`requeue`,
        the next process start will pull the item back into pending. Use
        this instead of :meth:`pop_first` whenever the consumer wants
        crash-resilience guarantees.
        """
        with self._lock:
            items = self._load()
            if not items:
                return None
            item = items.pop(0)
            # Write inflight FIRST so that a crash between the two writes
            # results in "item is in both files" → recovery prepends it back
            # → at-least-once delivery preserved. If we wrote pending first,
            # a crash would lose the item.
            inflight = self._load_inflight()
            inflight.append(item)
            self._save_inflight(inflight)
            self._save(items)
            return item

    def ack(self, item: dict) -> None:
        """Remove *item* from in-flight after the consumer finished it.

        Matched by value (the same dict identity isn't required; this
        survives a deepcopy round-trip through JSON if a caller chose to
        re-serialise the item).
        """
        with self._lock:
            inflight = self._load_inflight()
            try:
                inflight.remove(item)
            except ValueError:
                # Either already acked or the inflight file was cleared
                # by a restart-recovery cycle. Either way, nothing to do.
                return
            self._save_inflight(inflight)

    def requeue(self, item: dict) -> None:
        """Put *item* back at the front of pending and clear it from in-flight.

        Used by ``_process_item`` when a transient error makes the worker
        want to retry without losing its place in line.
        """
        with self._cv:
            inflight = self._load_inflight()
            try:
                inflight.remove(item)
            except ValueError:
                pass
            items = self._load()
            items.insert(0, item)
            self._save(items)
            self._save_inflight(inflight)
            self._cv.notify_all()

    # ── legacy non-durable API (retained for callers that don't need ACK) ──

    def pop_first(self) -> Optional[dict]:
        """Pop the head item without an in-flight marker.

        At-most-once: a crash between pop and the consumer's completion
        silently loses the item. New code should prefer :meth:`claim_first`
        + :meth:`ack`.
        """
        with self._lock:
            items = self._load()
            if not items:
                return None
            item = items.pop(0)
            self._save(items)
            return item

    # ── consumer notification ──────────────────────────────────────────

    def wait_for_item(self, timeout: float = 10.0) -> None:
        """Block up to *timeout* seconds for an item to appear."""
        with self._cv:
            if not self._load():
                self._cv.wait(timeout=timeout)
