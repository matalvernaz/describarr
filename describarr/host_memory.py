"""How much memory the machine has free, and whether an alignment was stopped for it.

describarr shares its machine with everything else on it. A feature film's
alignment holds a few gigabytes for most of an hour, and on a busy machine a
watchdog outside the container may kill it to keep the machine up (Forrest
Gump, 2026-11-01: stopped twice while a build loop on the same host swung
between 1.5 and 4 GB). That kill is not a verdict on the file or on the
recording. Once the memory is back the same alignment can run to the end, so
the workflow asks two questions of this module:

* :func:`stopped_from_outside`: was a run that died by a signal killed by the
  container's own memory limit (the kernel's OOM killer, which a second run
  would meet again), or by something outside the container?
* :func:`wait_for_room`: has the machine kept enough memory free, for long
  enough, to try again?

Inside a Docker container ``/proc/meminfo`` is the host's own, so its
``MemAvailable`` is the figure a host watchdog acts on. LXC's lxcfs rewrites
that file for an LXC container, but not for a Docker container nested in one:
measured 2026-11-01, describarr's figure matched the host's within 250 MB while
the LXC container's own overstated it by 6 GB.
"""

from __future__ import annotations

import logging
import math
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MEMINFO = Path("/proc/meminfo")
PROC_SELF_CGROUP = Path("/proc/self/cgroup")
CGROUP_ROOT = Path("/sys/fs/cgroup")

# Memory one alignment needs, estimated from the video's length: the engine
# holds the decoded sound of both sides, so it grows with the runtime.
# Measured: a 142-minute film (8526 s) held 2.4 GB when it was stopped
# (2026-11-01), 45-minute episodes 0.6-1.1 GB (2026-10-18).
ENGINE_BASE_MB = 300
ENGINE_MB_PER_SECOND = 0.3

# How long the machine must keep that much free before a stopped run is tried
# again. Longer than one cycle of the load that stopped it: the build loop of
# 2026-11-01 swung free memory between 2.5 and 6 GB every four to five
# minutes, so a shorter window could open in a trough and close on the next
# peak.
SETTLE_SECONDS = 600.0
# How long to wait for that before giving up. There is one worker, so the
# whole queue waits with this file; a machine that stays short for this long
# gets the file reported as failed, to be asked for again.
MAX_WAIT_SECONDS = 6 * 3600.0
# How often the figure is read while waiting, and how often the log says the
# wait goes on (the host monitor looks for that line).
POLL_SECONDS = 5.0
LOG_EVERY_SECONDS = 600.0

# The signals something outside the process stops it with: a watchdog's
# SIGKILL (Matt's host, systemd-oomd) or SIGTERM (earlyoom's first try, a
# person). A crash ends in a signal of its own (SIGSEGV, SIGABRT, SIGBUS),
# which a second run would meet again, so those are never taken for a kill.
OUTSIDE_SIGNALS = frozenset({signal.SIGKILL, signal.SIGTERM})

# The wait in progress, for /status: who is waiting, for how much, since when.
_current_wait: Optional[dict] = None
_current_wait_lock = threading.Lock()


def available_mb(meminfo: Path = MEMINFO) -> Optional[int]:
    """The machine's ``MemAvailable`` in whole megabytes, or None when it cannot be read."""
    try:
        text = meminfo.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("MemAvailable:"):
            continue
        fields = line.split()
        try:
            kilobytes = int(fields[1])
        except (IndexError, ValueError):
            return None
        return kilobytes // 1024
    return None


def oom_kills(
    proc_cgroup: Path = PROC_SELF_CGROUP,
    cgroup_root: Path = CGROUP_ROOT,
) -> Optional[int]:
    """How many processes the kernel's OOM killer has killed in describarr's
    cgroup, or None when that cannot be read.

    The engine runs in describarr's own cgroup (the container's), so a kill
    by the container's memory limit is counted here. A kill from outside, by a
    host watchdog or by hand, is not: the counter only counts the kernel's own
    OOM kills. Read from cgroup v2's ``memory.events``; on cgroup v1 this
    answers None, and the caller treats every signal kill as the limit's, as
    it always did.
    """
    try:
        lines = proc_cgroup.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        # cgroup v2 has one hierarchy, written "0::<path>".
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy != "0" or controllers != "":
            continue
        events = cgroup_root / path.lstrip("/") / "memory.events"
        return _read_counter(events, "oom_kill")
    return None


def _read_counter(path: Path, name: str) -> Optional[int]:
    """The value of *name* in a cgroup file of "<name> <value>" lines, or None."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] != name:
            continue
        try:
            return int(fields[1])
        except ValueError:
            return None
    return None


def stopped_from_outside(
    returncode: Optional[int],
    oom_kills_before: Optional[int],
    oom_kills_after: Optional[int],
) -> bool:
    """Whether a run that ended with *returncode* was killed from outside the container.

    True only for a run killed by SIGKILL or SIGTERM (``OUTSIDE_SIGNALS``)
    while describarr's OOM-kill count stayed where it was, both counts known.
    Any other signal is the engine crashing, and it would crash again. A count
    that went up means the container's own limit killed it, and a second run
    would meet the same limit. A count that cannot be read means nobody can
    tell, and the caller keeps the old answer: the limit.
    """
    if returncode is None or returncode >= 0:
        return False
    if -returncode not in OUTSIDE_SIGNALS:
        return False
    if oom_kills_before is None or oom_kills_after is None:
        return False
    return oom_kills_after == oom_kills_before


def engine_need_mb(runtime_seconds: float) -> int:
    """Estimated memory one alignment of a video *runtime_seconds* long holds at its peak."""
    runtime = max(0.0, runtime_seconds)
    return ENGINE_BASE_MB + math.ceil(ENGINE_MB_PER_SECOND * runtime)


def current_wait() -> Optional[dict]:
    """The wait in progress (label, need_mb, since), or None. For /status."""
    with _current_wait_lock:
        return dict(_current_wait) if _current_wait else None


def _set_current_wait(value: Optional[dict]) -> None:
    global _current_wait
    with _current_wait_lock:
        _current_wait = value


def wait_for_room(
    need_mb: int,
    *,
    label: str = "",
    settle_seconds: float = SETTLE_SECONDS,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    log_every_seconds: float = LOG_EVERY_SECONDS,
    read: Callable[[], Optional[int]] = available_mb,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until the machine has kept *need_mb* free for *settle_seconds* in a row.

    Returns True once it has, False when *max_wait_seconds* have passed first
    (a window already under way when the limit comes is let finish). A
    machine whose free memory cannot be read counts as having room, at once:
    this wait is for a figure describarr can see.
    """
    start = clock()
    started_wall = time.time()
    good_since: Optional[float] = None
    last_logged: Optional[float] = None
    _set_current_wait({"label": label, "need_mb": need_mb, "since": started_wall})
    try:
        while True:
            now = clock()
            free = read()
            if free is None:
                return True

            # Any reading under the need starts the window over.
            if free >= need_mb:
                if good_since is None:
                    good_since = now
            else:
                good_since = None
            if good_since is not None and now - good_since >= settle_seconds:
                logger.info(
                    "Memory is back for %s: %d MB free, kept above %d MB for %d minutes.",
                    label, free, need_mb, round(settle_seconds / 60),
                )
                return True

            # Give up at the limit, unless a good window is already under way.
            if now - start >= max_wait_seconds and good_since is None:
                logger.warning(
                    "Gave up waiting for memory for %s: %d MB free after %d hours, "
                    "it needs %d MB for %d minutes.",
                    label, free, round((now - start) / 3600), need_mb,
                    round(settle_seconds / 60),
                )
                return False

            if last_logged is None or now - last_logged >= log_every_seconds:
                logger.info(
                    "Waiting for memory: %s needs %d MB free for %d minutes; the machine has %d MB.",
                    label, need_mb, round(settle_seconds / 60), free,
                )
                last_logged = now
            sleep(poll_seconds)
    finally:
        _set_current_wait(None)
