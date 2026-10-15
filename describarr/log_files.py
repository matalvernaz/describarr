"""The history describarr keeps of its own work, where it outlives the container.

Two kinds of file, in ``<cache>/logs`` (the cache directory everything else
describarr keeps lives in, ``DESCRIBARR_CACHE_DIR``):

* ``describarr.log``: the log the container prints, one file a day, rotated
  at local midnight. A container's own log goes with the container, and every
  redeploy, compose edit and image update makes a new one. On 2026-10-13 a
  storage move did, and took the only account of three days' work on 387
  episodes, two out-of-memory kills among it.
* ``decisions-YYYY-MM.jsonl``: one JSON line for every decision, each
  recording tried on each file, whether it was published or refused and why:
  the video, the recording and its source, and what the gate read.
  ``decisions.json`` keeps only the last few, for /status; this keeps them all,
  a month to a file.

Both are kept for ``DESCRIBARR_LOG_RETENTION_DAYS`` (default 365; a month's
history counts from the month's last day). 0 keeps neither, and deletes none
already kept. Both hold library paths.

Keeping them is failure-tolerant, not non-blocking: a file that cannot be
opened or written never fails the work being recorded, but a share that hangs
holds the write as it holds every other file describarr keeps there. A history
line is one ``write()`` under a lock, so threads never interleave inside one;
a crash or a full disk can still leave a torn last line, and a reader should
skip a line that does not parse.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from .config import cache_dir_from_env

logger = logging.getLogger(__name__)

LOG_SUBDIR = "logs"
LOG_FILENAME = "describarr.log"
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s: %(message)s"
DEFAULT_RETENTION_DAYS = 365

_RETENTION_ENV = "DESCRIBARR_LOG_RETENTION_DAYS"
_HISTORY_NAME = re.compile(r"^decisions-(\d{4})-(\d{2})\.jsonl$")

# Each bad setting is reported once, not on every decision that reads it.
_warned: set[str] = set()


@dataclass(frozen=True)
class LogSettings:
    log_dir: Path
    retention_days: int


def retention_days() -> int:
    """Days to keep the log and the decision history; 0 keeps none.

    Never raises: it is read before any configuration loads, and a typo must
    not stop the server. Unset or blank is the default, anything unreadable is
    the default with a warning, a negative number is 0 with a warning.
    """
    raw = os.environ.get(_RETENTION_ENV, "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        _warn_once(raw, "%s=%r is not a whole number of days; keeping %d.",
                   _RETENTION_ENV, raw, DEFAULT_RETENTION_DAYS)
        return DEFAULT_RETENTION_DAYS
    if days < 0:
        _warn_once(raw, "%s=%r is negative; keeping no log files.", _RETENTION_ENV, raw)
        return 0
    return days


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def log_settings() -> LogSettings:
    """The log directory and retention, from the environment alone."""
    return LogSettings(cache_dir_from_env() / LOG_SUBDIR, retention_days())


def history_dir(cache_dir: Path) -> Optional[Path]:
    """Where decisions made with *cache_dir* are kept for good; None when off."""
    return Path(cache_dir) / LOG_SUBDIR if retention_days() > 0 else None


def attach_file_log(settings: Optional[LogSettings] = None) -> Optional[logging.Handler]:
    """Copy everything logged to ``describarr.log``; the handler, or None.

    Opened at once rather than on the first record, so a directory that cannot
    be written costs one warning here and no handler, instead of a traceback
    from every record after it. Attaching twice returns the first handler.
    """
    settings = settings or log_settings()
    if settings.retention_days <= 0:
        return None
    path = settings.log_dir / LOG_FILENAME
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, TimedRotatingFileHandler) \
                and handler.baseFilename == os.path.abspath(path):
            return handler
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            path, when="midnight", backupCount=settings.retention_days, encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Cannot keep a log file at %s (%s); logging to the console only.",
                       path, exc)
        return None
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    return handler


def append_history(directory: Path, record: dict, now: Optional[datetime] = None) -> None:
    """Add *record* to this month's decision history as one line. Never raises:
    losing the line must not fail the work it describes."""
    try:
        when = now or datetime.now()
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / f"decisions-{when:%Y-%m}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        logger.warning("Could not add a decision to the history in %s: %s", directory, exc)


def prune_decision_history(settings: Optional[LogSettings] = None,
                           today: Optional[date] = None) -> int:
    """Delete months of decision history older than the retention; how many.

    Only files named as this module names them, judged by the month in the
    name, never by modification time; anything else in the directory stays.
    """
    settings = settings or log_settings()
    if settings.retention_days <= 0 or not settings.log_dir.is_dir():
        return 0
    today = today or date.today()
    removed = 0
    for path in settings.log_dir.iterdir():
        match = _HISTORY_NAME.match(path.name)
        if not match or not path.is_file():
            continue
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            continue
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        if (today - last_day).days > settings.retention_days:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("Pruned %d month(s) of decision history older than %d days.",
                    removed, settings.retention_days)
    return removed
