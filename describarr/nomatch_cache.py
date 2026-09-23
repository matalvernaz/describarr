"""Remembers episodes no audio-description source could cover.

Without this, ``/retry?dir=`` is pure repetition: an episode that no source
carries is never recorded anywhere the scan consults (a ``no_match`` reaches
only ``decisions.json``, a bounded display ring), so every rescan re-runs the
full AudioVault + extra-source search for it. Observed 2026-09-22: a 60-episode
show with no AD anywhere cost 60 lookups and ~3.5 minutes, and the next scan
would have cost exactly the same again, forever.

A miss is remembered against the *file it was looked up for*, not just the
episode number, so the two things that genuinely warrant another search both
invalidate it automatically:

* the file changed (a Sonarr re-grab or quality upgrade replaced it), or
* the entry aged out — catalogues do add titles, so a miss is a fact with a
  shelf life, not a verdict.

Only the directory scan consults the cache. Webhooks always search (a fresh
import is new content by definition) and an explicit single-file
``/retry?path=`` always searches, because that is an operator saying "try this
one again" — see ``force`` on the directory form for the same override.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Serialises the load→mutate→save cycle: the worker thread records misses while
# a scan (same thread today, but the HTTP handlers are threaded) may read.
_LOCK = threading.RLock()

_SECONDS_PER_DAY = 86400


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write via sibling .tmp + fsync + os.replace so a crash mid-write cannot
    leave a truncated cache that then reads as "no misses recorded"."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _identity(video_path: Path) -> Optional[dict]:
    """Size + mtime of *video_path*, or None if it can't be stat'ed.

    Deliberately not the inode: the library lives on NFS from the storage box
    and an inode is not a stable identity across a remount. Size and mtime both
    change when an arr replaces a file, which is exactly the event that should
    make describarr look again.
    """
    try:
        st = video_path.stat()
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


class NoMatchCache:
    """Per-show record of episodes for which no source had audio description.

    Stored as one JSON object at *state_path*, keyed ``"s01e02"``. Entries are
    ``{"size", "mtime_ns", "ts"}``. A zero or negative *ttl_days* disables the
    cache entirely — every lookup misses and nothing is recorded — so the
    previous always-research behaviour stays one env var away.
    """

    def __init__(self, state_path: Path, ttl_days: int) -> None:
        self._path = state_path
        self._ttl = max(0, int(ttl_days)) * _SECONDS_PER_DAY

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    @staticmethod
    def key(season: int, episode: int) -> str:
        return f"s{season:02d}e{episode:02d}"

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, ValueError, OSError):
            logger.warning("Corrupt no-match cache at %s — ignoring.", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def is_fresh_miss(self, key: str, video_path: Path) -> bool:
        """True if *key* was recorded as a miss, the file is byte-for-byte the
        one that was searched for, and the record hasn't aged out."""
        if not self.enabled:
            return False
        entry = self._load().get(key)
        if not isinstance(entry, dict):
            return False
        ident = _identity(video_path)
        if ident is None:
            return False
        if entry.get("size") != ident["size"] or entry.get("mtime_ns") != ident["mtime_ns"]:
            return False
        try:
            age = time.time() - float(entry["ts"])
        except (KeyError, TypeError, ValueError):
            return False
        return 0 <= age < self._ttl

    def record_miss(self, key: str, video_path: Path) -> None:
        """Remember that nothing was found for *key* against this exact file."""
        if not self.enabled:
            return
        ident = _identity(video_path)
        if ident is None:
            return
        try:
            with _LOCK:
                data = self._load()
                data[key] = {**ident, "ts": time.time()}
                _atomic_write_json(self._path, data)
        except OSError:
            # A cache is an optimisation; failing to persist one must never
            # take down the item being processed.
            logger.warning("Could not persist no-match cache %s", self._path, exc_info=True)

    def forget(self, key: str) -> None:
        """Drop *key*, so the next scan searches for it again."""
        if not self._path.exists():
            return
        try:
            with _LOCK:
                data = self._load()
                if data.pop(key, None) is None:
                    return
                _atomic_write_json(self._path, data)
        except OSError:
            logger.warning("Could not update no-match cache %s", self._path, exc_info=True)
