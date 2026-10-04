"""Notifications held back while a show is still arriving.

Adding a show sends its episodes through describarr one after another, and each
outcome used to be its own Pushover, and each description its own message to
everyone on the notifications hub: Friends on 2026-10-04 was 177 outcomes in
eight hours, up to 39 an hour, and the Family Guy and Law & Order: SVU retries
on 10-01 were 957 in a day.

The first outcome for a show is still sent at once, so a single new episode is
heard as soon as it is ready. What follows is held: the operator gets one
summary when the show has gone quiet, or after a longer wait while a long run
is still going; everyone else hears once the show goes quiet, since they asked
what is new, not how a backfill is getting on. Films share one group.

Kept in the cache directory, so a restart does not lose what is held. A summary
is taken out of the file before it is sent, never after, so it cannot be sent
twice.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

#: Beside the outcome log in the cache directory.
HELD_FILENAME = "held_notifications.json"

#: The group every film is held in.
FILMS = "films"

#: Everyone hears about a show when it goes quiet; a run that never does is
#: told about after this long all the same.
EVERYONE_MAX_WAIT_SECONDS = 12 * 3600


def show_group(title: str) -> str:
    """The group a show's outcomes are held in."""
    return f"show:{title}"


def group_name(group: str) -> str:
    """The show's title, or "films"."""
    return group.removeprefix("show:")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class Due:
    """What one group has to say now."""

    group: str
    #: For the operator: ``{"label", "outcome", "message"}``, in arrival order.
    operator: list[dict] = field(default_factory=list)
    #: For everyone: the labels of what was described, in arrival order.
    everyone: list[str] = field(default_factory=list)


class HeldNotifications:
    """Each group's held outcomes, and when it last had one."""

    def __init__(self, path: Path, quiet_seconds: float, max_wait_seconds: float,
                 everyone_max_wait_seconds: float = EVERYONE_MAX_WAIT_SECONDS) -> None:
        self._path = path
        self._quiet = quiet_seconds
        self._max_wait = max_wait_seconds
        self._everyone_max_wait = everyone_max_wait_seconds

    @classmethod
    def in_cache(cls, cache_dir: Path, quiet_minutes: float,
                 max_wait_minutes: float) -> "HeldNotifications":
        return cls(Path(cache_dir) / HELD_FILENAME, quiet_minutes * 60, max_wait_minutes * 60)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, ValueError, OSError):
            logger.warning("Corrupt held-notification file at %s; starting afresh.", self._path)
            return {}
        if not isinstance(data, dict):
            return {}
        return {group: held for group, held in data.items()
                if isinstance(held, dict) and isinstance(held.get("last"), (int, float))}

    def _save(self, state: dict) -> bool:
        try:
            _atomic_write_text(self._path, json.dumps(state, indent=1))
        except Exception:
            logger.warning("Could not keep held notifications at %s.", self._path, exc_info=True)
            return False
        return True

    def offer(self, group: str, label: str, outcome: str, message: str, now: float) -> bool:
        """Whether to send this outcome now. False means it is held for the
        group's summary. Never holds what it could not write down."""
        if self._quiet <= 0:
            return True
        with _LOCK:
            state = self._load()
            held = state.get(group)
            if held is None or (not held.get("operator") and not held.get("everyone")
                                and now - held["last"] >= self._quiet):
                # Nothing heard from this show lately: this one goes now and
                # opens a window for whatever follows it.
                state[group] = {"last": now, "operator": [], "operator_since": None,
                                "everyone": [], "everyone_since": None}
                send_now = True
            else:
                held.setdefault("operator", []).append(
                    {"label": label, "outcome": outcome, "message": message})
                if held.get("operator_since") is None:
                    held["operator_since"] = now
                if outcome == "described":
                    held.setdefault("everyone", []).append(label)
                    if held.get("everyone_since") is None:
                        held["everyone_since"] = now
                held["last"] = now
                send_now = False
            if not self._save(state):
                return True
            return send_now

    def due(self, now: float) -> list[Due]:
        """Take out every summary that is ready to send. Returns nothing when
        the taking could not be written down, so nothing is sent twice."""
        with _LOCK:
            state = self._load()
            ready: list[Due] = []
            changed = False
            for group in list(state):
                held = state[group]
                quiet = now - held["last"] >= self._quiet
                due = Due(group)
                if held.get("operator") and (
                        quiet or _waited(held.get("operator_since"), self._max_wait, now)):
                    due.operator = held["operator"]
                    held["operator"], held["operator_since"] = [], None
                if held.get("everyone") and (
                        quiet or _waited(held.get("everyone_since"), self._everyone_max_wait, now)):
                    due.everyone = held["everyone"]
                    held["everyone"], held["everyone_since"] = [], None
                if due.operator or due.everyone:
                    ready.append(due)
                    changed = True
                if quiet and not held.get("operator") and not held.get("everyone"):
                    del state[group]
                    changed = True
            if changed and not self._save(state):
                return []
            return ready


def _waited(since: float | None, limit: float, now: float) -> bool:
    return limit > 0 and since is not None and now - since >= limit
