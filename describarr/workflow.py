"""
High-level processing workflows for episodes and movies.

Each function:
  1. Searches AudioVault for the matching audio description.
  2. Downloads and caches the file.
  3. Runs describealaign.
  4. Keeps or discards the combined output based on the alignment score.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

import requests

from .aligner import (
    run as align,
    parse_score,
    content_score,
    slope_stability,
    sync_quality,
    source_has_ad_track,
)
from .audiovault import AudioVaultClient, DailyLimitReached, DownloadLimiter
from .config import Config
from .decision_log import DecisionLog
from .matcher import extract_episode, find_movie, find_season
from .retry_queue import RetryQueue
from .sources import load_extra_sources

# Errors we treat as transient (re-queue and retry on next drain).
# AudioVault occasional 5xx, a flaky Cloudflare edge, or a stalled CDN
# read should not silently lose the queued item.
_TRANSIENT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    ConnectionError,
    TimeoutError,
)

# After this many failed drain attempts the item is dropped — protects against
# truly bad URLs that would otherwise loop forever.
_MAX_DRAIN_ATTEMPTS = 5

# A queued item that keeps deferring (its season never gets a download slot,
# or it has no obtainable AD) is abandoned after this many drain passes —
# roughly this many days at one drain/day — so the retry queue can't
# accumulate permanent residents that will never succeed.
_MAX_DRAIN_PASSES = 30

# One movie must never walk enough of its candidate list to burn the whole
# AudioVault daily download budget (25 = roughly one drain's worth of items).
# After ranking, the genuine variants of the right film sit at the top, so
# anything past the first few is a wrong film that alignment would reject at
# the cost of a download slot each.
_MAX_MOVIE_CANDIDATES = 3


class AlignmentResourceKill(Exception):
    """The alignment subprocess was killed by a signal — under the container
    memory cap, the memcg OOM SIGKILL on an oversized decode. The kill is a
    property of the video file's decode footprint, not of the AD candidate:
    every further candidate for the same file dies the same way, each attempt
    costing a download slot, so candidate loops abort instead of iterating."""


class SourceVanished(Exception):
    """The source video disappeared while the alignment was running — an arr
    upgrade replacing the file mid-run, which takes minutes.

    Like AlignmentResourceKill this is a property of the file rather than of
    the AD candidate, so the candidate walk aborts rather than spending a
    download slot and another alignment on a file that is no longer there.
    The replacement import fires its own webhook, which is what actually
    describes the new file."""


def _should_abandon_stale(item: dict) -> bool:
    """Increment *item*'s deferral counter; return True once it has been
    deferred too many times and should be dropped instead of re-queued."""
    passes = int(item.get("drain_passes", 0)) + 1
    item["drain_passes"] = passes
    return passes > _MAX_DRAIN_PASSES

# Trailing "(YYYY)" tokens in the title break AudioVault's search index. The
# year stripping is applied defensively here even though callers usually pass
# the title and year separately, because the /retry endpoint and some Sonarr/
# Radarr setups can pass a year-suffixed title through verbatim.
_TITLE_YEAR_SUFFIX_RE = re.compile(r"\s*\((\d{4})\)\s*$")


def _strip_year_suffix(title: str) -> str:
  """Return *title* with a trailing ``(YYYY)`` token removed."""
  return _TITLE_YEAR_SUFFIX_RE.sub("", title).strip()


def _year_suffix(title: str) -> str:
  """The trailing ``(YYYY)`` year in *title*, or "" if it has none.

  Sonarr names some series with the disambiguating year already attached, so
  this recovers a series year even where the webhook carries no explicit one.
  """
  match = _TITLE_YEAR_SUFFIX_RE.search(title)
  return match.group(1) if match and match.groups() else ""

logger = logging.getLogger(__name__)


def process_episode(
    client: AudioVaultClient,
    config: Config,
    video_path: Path,
    series_title: str,
    season: int,
    episode: int,
    extra_episodes: Optional[list[int]] = None,
    series_year: str = "",
) -> tuple[bool, Optional[str]]:
    """
    Find and align the audio description for a single TV episode.

    *series_year* is the year the series began (Sonarr's ``sonarr_series_year``).
    It disambiguates a reboot that shares its parent's title and season
    numbering; absent, matching behaves exactly as it did before.

    *extra_episodes* covers Sonarr's multi-episode files (S01E01E02 etc.):
    one alignment runs against the primary *episode*'s AD audio, but every
    episode in ``[episode] + extra_episodes`` is recorded in the season's
    ``.done_sNN.json`` so the AudioVault zip-cleanup logic doesn't wait
    forever for episodes that share a file.

    Returns ``(described, reason)``: *described* is True if a combined file was
    produced with an acceptable score; *reason* is a human-readable failure
    cause when it wasn't (for the operator notification), else None.
    """
    all_episodes = [episode] + list(extra_episodes or [])
    label = f"{series_title} S{season:02d}" + "".join(f"E{e:02d}" for e in all_episodes)
    # Idempotency guard: never re-align a file that already has an AD track,
    # or we stack a second one (duplicate webhook / mid-drain restart).
    if source_has_ad_track(video_path):
        logger.info("%s already has an audio-description track — skipping.", video_path.name)
        return True, None
    if len(all_episodes) == 1:
        logger.info("Looking up: %s S%02dE%02d", series_title, season, episode)
    else:
        ep_label = "".join(f"E{e:02d}" for e in all_episodes)
        logger.info("Looking up: %s S%02d%s (multi-episode)", series_title, season, ep_label)

    search_title = _strip_year_suffix(series_title)
    stripped_note = " (year stripped)" if search_title != series_title else ""
    results = client.search_shows(search_title)
    if not results:
        logger.warning(
            "AudioVault has no results for show: %r%s", series_title, stripped_note
        )
        return False, None

    candidates = find_season(
        results, series_title, season,
        series_year or _year_suffix(series_title),
    )
    if not candidates:
        logger.warning("No season %d entry found for %r.", season, series_title)
        return False, None

    # Season zips are cached by download URL so we only fetch each season once.
    # Each candidate gets its own extract subdirectory so different zips don't
    # overwrite each other's extracted contents.
    zip_cache_dir = config.cache_dir / "shows" / _safe_dirname(series_title)
    limiter = DownloadLimiter(config.cache_dir / "daily_limit.json")

    last_reason: Optional[str] = None
    try:
        for candidate in candidates:
            try:
                zip_path = _get_cached(client, candidate["url"], zip_cache_dir, limiter)
            except DailyLimitReached:
                raise
            extract_dir = zip_cache_dir / f"season_{season:02d}" / _safe_dirname(candidate["name"])
            audio_path = extract_episode(zip_path, extract_dir, episode)
            if not audio_path:
                logger.warning(
                    "E%02d not found in %r — trying next candidate.", episode, candidate["name"]
                )
                continue
            published, reason = _align_and_keep(config, video_path, audio_path, label=label)
            if published:
                for ep in all_episodes:
                    _mark_episode_done(zip_cache_dir, season, ep, extract_dir, zip_path)
                return True, None
            last_reason = reason
            logger.info("Candidate %r below threshold — trying next.", candidate["name"])

        # Extra (privately-supplied) sources, tried after AudioVault. Each yields
        # candidate AD audio files; align in order, first acceptable wins.
        for source in load_extra_sources():
            try:
                for audio_path in source.episode_candidates(
                    config.cache_dir, series_title, season, episode
                ):
                    published, reason = _align_and_keep(config, video_path, audio_path, label=label)
                    if published:
                        # An extra source may deliver a bare per-episode file with
                        # no season zip; still record every covered episode in the
                        # ledger so zip-cleanup accounting and the /retry skip logic
                        # stay correct. No zip/extract_dir ⇒ cleanup is a no-op.
                        for ep in all_episodes:
                            _mark_episode_done(zip_cache_dir, season, ep)
                        return True, None
                    last_reason = reason
            finally:
                source.close()
    except (AlignmentResourceKill, SourceVanished) as exc:
        logger.error("Aborting candidate walk for %s: %s", video_path.name, exc)
        return False, str(exc)

    return False, last_reason


def process_movie(
    client: AudioVaultClient,
    config: Config,
    video_path: Path,
    movie_title: str,
    movie_year: str,
) -> tuple[bool, Optional[str]]:
    """
    Find and align the audio description for a movie.

    Returns ``(described, reason)`` — see :func:`process_episode`.
    """
    label = f"{movie_title} ({movie_year})" if movie_year else movie_title
    if source_has_ad_track(video_path):
        logger.info("%s already has an audio-description track — skipping.", video_path.name)
        return True, None
    logger.info("Looking up movie: %s (%s)", movie_title, movie_year)

    search_title = _strip_year_suffix(movie_title)
    stripped_note = " (year stripped)" if search_title != movie_title else ""
    results = client.search_movies(search_title)
    if not results:
        logger.warning(
            "AudioVault has no results for movie: %r%s", movie_title, stripped_note
        )
        return False, None

    candidates = find_movie(results, movie_title, movie_year)
    if not candidates:
        logger.warning("No suitable movie match found for %r.", movie_title)
        return False, None

    movie_cache_dir = config.cache_dir / "movies"
    limiter = DownloadLimiter(config.cache_dir / "daily_limit.json")

    if len(candidates) > _MAX_MOVIE_CANDIDATES:
        logger.info(
            "Trying the top %d of %d ranked candidates.",
            _MAX_MOVIE_CANDIDATES, len(candidates),
        )

    last_reason: Optional[str] = None
    try:
        for candidate in candidates[:_MAX_MOVIE_CANDIDATES]:
            try:
                audio_path = _get_cached(client, candidate["url"], movie_cache_dir, limiter)
            except DailyLimitReached:
                raise
            published, reason = _align_and_keep(config, video_path, audio_path, label=label)
            if published:
                return True, None
            last_reason = reason
            logger.info("Candidate %r below threshold — trying next.", candidate["name"])

        # Extra (privately-supplied) sources, tried after AudioVault.
        for source in load_extra_sources():
            try:
                for audio_path in source.movie_candidates(
                    config.cache_dir, movie_title, movie_year
                ):
                    published, reason = _align_and_keep(config, video_path, audio_path, label=label)
                    if published:
                        return True, None
                    last_reason = reason
            finally:
                source.close()
    except (AlignmentResourceKill, SourceVanished) as exc:
        logger.error("Aborting candidate walk for %s: %s", video_path.name, exc)
        return False, str(exc)

    return False, last_reason


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

# Acceptance gate tuning. similarity (describealaign's match-confidence
# metric) is the sync signal and is the only thing trusted on its own. The
# rescue path exists for sources whose similarity is depressed by a *known*
# cause while the time-mapping stays clean, and is bounded so a "stably wrong"
# alignment can't slip through:
_RESCUE_MIN_SCORE = 30.0          # describealaign warns < 20% = mismatched; stay clear of it
_RESCUE_MIN_STABLE_FRACTION = 90.0
_RESCUE_MIN_RUNTIME_SEC = 300.0
_NATIVE_RATE_TOLERANCE = 0.5      # |median_rate| ≤ this ⇒ native-rate (commercial-seam) rescue
_DRIFT_RATE_MIN = 2.0             # PAL/NTSC rate conversion is ~4.27%
_DRIFT_RATE_MAX = 6.0             # anything past this is not a standard rate shift → reject


def _acceptance_decision(
    *,
    score: float,
    content_coverage: float,
    stable_fraction: float,
    median_rate: float,
    total_runtime: float,
    sync_ok: bool,
    min_score: float,
) -> tuple[bool, str, str]:
    """Decide whether an alignment is good enough to overwrite the original.

    Pure function (no I/O) so the acceptance matrix can be unit-tested
    directly. Returns ``(accepted, path, detail)`` where *path* is
    ``"similarity"`` / ``"drift-rescue"`` / ``"reject"`` and *detail* is a
    human-readable reason for the log.

    similarity is the sync gate: it measures how much of the AD release's
    embedded program audio matched the video, i.e. how confidently the
    description was placed in time. The rescue path NEVER accepts on
    structure alone — it requires a minimum of real matched anchors
    (*score* ≥ ``_RESCUE_MIN_SCORE``), a tight low-variance time-mapping
    (``stable_fraction`` high AND ``sync_ok``), and a rate that is either
    native (commercial-break-seam case) or a *bounded* known drift
    (PAL/NTSC). content_coverage is informational only — "few seam
    artifacts" says nothing about whether the narration lands on time.
    """
    if score >= min_score:
        return True, "similarity", f"similarity {score:.1f}% ≥ {min_score:.0f}% (sync confident)"

    rate_ok = (
        abs(median_rate) <= _NATIVE_RATE_TOLERANCE
        or _DRIFT_RATE_MIN <= abs(median_rate) <= _DRIFT_RATE_MAX
    )
    rescue_ok = (
        sync_ok
        and stable_fraction >= _RESCUE_MIN_STABLE_FRACTION
        and score >= _RESCUE_MIN_SCORE
        and total_runtime >= _RESCUE_MIN_RUNTIME_SEC
        and rate_ok
    )
    if rescue_ok:
        return True, "drift-rescue", (
            f"similarity {score:.1f}% below {min_score:.0f}% but stable trunk "
            f"{stable_fraction:.1f}% at median rate {median_rate:.2f}% with a passing "
            f"sync-quality check — accepting consistent-drift alignment"
        )
    return False, "reject", (
        f"similarity {score:.1f}% (coverage {content_coverage:.1f}%, stable trunk "
        f"{stable_fraction:.1f}%, median rate {median_rate:.2f}%, sync_ok={sync_ok}) "
        f"— no trusted sync signal"
    )


_BACKUP_SUBDIR = ".describarr_backup"


def _prune_old_backups(backup_dir: Path, retention_days: int) -> None:
    """Delete ``*.bak`` files in *backup_dir* older than *retention_days*.

    Age is read from the ``<name>.<epoch>.bak`` timestamp embedded in the
    filename, NOT from ``st_mtime``. A backup is a hardlink, so it shares the
    *original* file's mtime — which for a library file is usually far older
    than the retention window. Pruning by mtime would therefore delete every
    backup the instant it was created. Names without a parseable timestamp are
    left untouched. retention_days ≤ 0 keeps backups indefinitely.
    """
    if retention_days <= 0 or not backup_dir.is_dir():
        return
    cutoff = time.time() - retention_days * 86400
    for p in backup_dir.glob("*.bak"):
        try:
            ts = int(p.name.rsplit(".", 2)[-2])
        except (ValueError, IndexError):
            continue  # unparseable name — don't touch it
        if ts < cutoff:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                continue


def _backup_original(
    video_path: Path, backup_dir: Optional[Path], retention_days: int,
    registry_path: Optional[Path] = None,
) -> Optional[Path]:
    """Hardlink *video_path* aside before it's overwritten, returning the
    backup path (or None if no backup could be made).

    A hardlink is instant and consumes no extra space until the original
    content has no other links, so this is cheap insurance against a
    mis-accepted alignment clobbering a good file. The backup is best-effort:
    a failure logs and returns None rather than blocking the core
    align-and-publish flow.

    backup_dir=None ⇒ a sibling ``.describarr_backup`` dir, always on the
    same filesystem as the video so the hardlink can't fail EXDEV. A
    configured backup_dir collects every backup in one place but must be on
    the same filesystem as the media; if it isn't, we fall back to the
    sibling dir.
    """
    ts = int(time.time())
    if backup_dir is not None:
        # Short hash of the full source path so two identically-named files
        # from different folders can't collide in the shared backup dir.
        digest = hashlib.md5(str(video_path).encode()).hexdigest()[:8]
        target_dir = backup_dir
        backup_name = f"{digest}.{video_path.name}.{ts}.bak"
    else:
        target_dir = video_path.parent / _BACKUP_SUBDIR
        backup_name = f"{video_path.name}.{ts}.bak"

    def _link_into(d: Path, name: str) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        dst = d / name
        os.link(video_path, dst)
        return dst

    try:
        backup_path = _link_into(target_dir, backup_name)
    except OSError as exc:
        # Most likely EXDEV: configured backup_dir is on a different
        # filesystem than the media. Fall back to the sibling dir, which is
        # guaranteed same-fs.
        if backup_dir is None:
            logger.error("Could not back up %s before overwrite: %s", video_path, exc)
            return None
        logger.warning(
            "Backup hardlink into %s failed (%s); falling back to a sibling dir.",
            target_dir, exc,
        )
        target_dir = video_path.parent / _BACKUP_SUBDIR
        backup_name = f"{video_path.name}.{ts}.bak"
        try:
            backup_path = _link_into(target_dir, backup_name)
        except OSError as exc2:
            logger.error("Could not back up %s before overwrite: %s", video_path, exc2)
            return None

    if registry_path is not None:
        _register_backup_dir(registry_path, target_dir)
    _prune_old_backups(target_dir, retention_days)
    return backup_path


def _register_backup_dir(registry_path: Path, backup_dir: Path) -> None:
    """Record *backup_dir* in a JSON registry so the nightly sweep can prune
    backups in folders no future publish will revisit (sibling
    ``.describarr_backup`` dirs are scattered across the library). Best-effort:
    never blocks a publish."""
    try:
        dirs: list = []
        if registry_path.exists():
            loaded = json.loads(registry_path.read_text())
            if isinstance(loaded, list):
                dirs = loaded
        s = str(backup_dir)
        if s not in dirs:
            dirs.append(s)
            _atomic_write_json(registry_path, dirs)
    except (OSError, json.JSONDecodeError):
        pass


def prune_registered_backups(cache_dir: Path, retention_days: int) -> None:
    """Prune aged backups in every registered backup dir, then drop dead
    entries from the registry.

    Run from the midnight loop so backups in a finished show's folder expire
    on schedule even though no later publish revisits that folder. Without
    this, sibling ``.describarr_backup`` dirs only get pruned when the same
    folder gets another publish — so a wrapped-up season's backups would
    linger past the retention window forever.
    """
    registry_path = cache_dir / "backup_dirs.json"
    if not registry_path.exists():
        return
    try:
        dirs = json.loads(registry_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(dirs, list):
        return
    live: list[str] = []
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue  # folder gone — drop the registry entry
        _prune_old_backups(p, retention_days)
        if any(p.glob("*.bak")):
            live.append(d)  # keep only entries that still hold backups
    if live != dirs:
        _atomic_write_json(registry_path, live)


# Orphaned alignment scratch older than this has no live run behind it: the
# worker aligns one file at a time and the subprocess hard-times-out at 1h, so
# 2h is a safe floor that can never race an in-progress run.
_SCRATCH_ORPHAN_AGE_SEC = 7200


def prune_output_scratch(cache_dir: Path) -> None:
    """Delete orphaned alignment scratch under ``<cache>/output``: ``run-*``
    dirs and stray ``ad_*`` / ``.tmp.*`` files left behind when an alignment
    died mid-flight (e.g. a container restart at Watchtower's 04:00 pull).

    The happy path cleans its own run dir on completion; this sweeps what
    crashes leave. Called from the midnight loop.
    """
    output_dir = cache_dir / "output"
    if not output_dir.is_dir():
        return
    cutoff = time.time() - _SCRATCH_ORPHAN_AGE_SEC
    removed = 0
    for p in output_dir.iterdir():
        try:
            if p.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if p.is_dir() and p.name.startswith("run-"):
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        elif p.is_file() and (p.name.startswith("ad_") or p.name.startswith(".tmp.")):
            p.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("Pruned %d orphaned output-scratch item(s).", removed)


def _log_decision(
    config: Config,
    title: str,
    outcome: str,
    detail: str,
    *,
    score: Optional[float] = None,
    coverage: Optional[float] = None,
    stable_fraction: Optional[float] = None,
    median_rate: Optional[float] = None,
    runtime: Optional[float] = None,
    path: Optional[str] = None,
) -> None:
    """Record one decision in the audit log. Best-effort: a logging failure
    must never break the alignment path."""
    try:
        DecisionLog(config.cache_dir / "decisions.json", config.history_size).append({
            "title": title,
            "outcome": outcome,
            "detail": detail,
            "score": score,
            "coverage": coverage,
            "stable_fraction": stable_fraction,
            "median_rate": median_rate,
            "runtime": runtime,
            "path": path,
        })
    except Exception:
        logger.debug("Decision-log write failed.", exc_info=True)


def _align_and_keep(
    config: Config,
    video_path: Path,
    audio_path: Path,
    label: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Run alignment and either keep or discard the combined output.

    Returns ``(published, reason)``. On success *reason* is None; on failure it
    is a human-readable cause (the engine's mismatch diagnosis or the rescue-
    gate rejection detail) for the operator notification. Every attempt is
    recorded in the decision log so the accept/reject judgment is auditable.
    """
    alignment_dir = config.cache_dir / "alignments"
    tmp_output_dir = config.cache_dir / "output"
    entry_title = label or video_path.name

    # Refuse the stretch-video path (config.stretch_audio is False) for an
    # in-place library replacement unless Matt has explicitly opted into the
    # "I know this can break hardware playback" mode. The setts bitstream
    # filter rewrites PTS/DTS on a stream-copied video, which is structurally
    # valid but breaks HRD assumptions in some hardware decoders (Apple TV,
    # webOS, etc.). Stretching the audio is the safe path.
    if not config.stretch_audio and not config.allow_video_retime:
        logger.error(
            "Refusing to use the stretch-video setts path for in-place "
            "replacement of %s. Set DESCRIBARR_STRETCH_AUDIO=true (recommended) "
            "or DESCRIBARR_ALLOW_VIDEO_RETIME=true to override.",
            video_path,
        )
        reason = ("stretch-video path disabled; set DESCRIBARR_STRETCH_AUDIO=true "
                  "or DESCRIBARR_ALLOW_VIDEO_RETIME=true")
        _log_decision(config, entry_title, "error", reason)
        return False, reason

    # Snapshot the source fingerprint BEFORE alignment, not after. Alignment
    # commonly runs 1–30 minutes; if Sonarr/Radarr upgrades the file during
    # that window, the previous check (captured inside _publish_in_place,
    # which is reached after align() returns) saw the already-upgraded file
    # and silently overwrote it with our stale-source alignment.
    try:
        pre_fp = _file_fingerprint(video_path)
    except FileNotFoundError:
        logger.error("Source video vanished before alignment could start: %s", video_path)
        return False, "source file vanished before alignment"

    result = align(video_path, audio_path, tmp_output_dir, alignment_dir, config.stretch_audio)
    if result is None or result.output is None:
        reason = (result.failure_reason if result is not None else None) \
            or "alignment produced no validated output"
        logger.error("Alignment produced no validated output file: %s", reason)
        _log_decision(config, entry_title, "failed", reason)
        if result is not None and result.returncode is not None and result.returncode < 0:
            raise AlignmentResourceKill(
                f"{reason} — killed by signal, likely the container memory cap; "
                "further candidates for this file would die the same way"
            )
        # Checked after the failure rather than before, because an alignment
        # takes minutes and an arr upgrade can replace the file inside that
        # window — the pre-flight fingerprint above cannot see it.
        if not video_path.exists():
            raise SourceVanished(
                "source video was replaced while the alignment was running "
                "(likely a Sonarr/Radarr upgrade); the replacement's own "
                "import will trigger a fresh run"
            )
        return False, reason

    combined = result.output
    report = result.report
    score = parse_score(report)
    cscore = content_score(report)
    median_rate, stable_fraction, total_runtime = slope_stability(report)

    # Always log every metric so acceptance decisions are auditable.
    logger.info(
        "Metrics for %s: similarity=%.1f%% coverage=%.1f%% "
        "slope_stability=%.1f%% median_rate=%.2f%% runtime=%.0fs",
        video_path.name, score, cscore, stable_fraction, median_rate, total_runtime,
    )

    # similarity is describealaign's match-confidence metric: the fraction of
    # the AD release's embedded program audio that aligned against the video.
    # High similarity means the description lands at the right time, so it is
    # the sync gate and is the only signal trusted on its own. The rescue path
    # (see _acceptance_decision) only covers similarity depressed by a *known*
    # cause — commercial-break seams on a native-rate source, or the pitch
    # shift on a PAL/NTSC rate-converted source — and demands a tight,
    # low-variance, bounded-rate time-mapping. Bare content-coverage acceptance
    # was removed: "few seam artifacts" says nothing about whether the
    # narration is on time, which is the entire point of the alignment.
    sync_ok, sync_reason = sync_quality(report)
    accepted, accept_path, decision_detail = _acceptance_decision(
        score=score,
        content_coverage=cscore,
        stable_fraction=stable_fraction,
        median_rate=median_rate,
        total_runtime=total_runtime,
        sync_ok=sync_ok,
        min_score=config.min_score,
    )
    if not accepted:
        logger.warning("Discarding %s — %s", video_path.name, decision_detail)
        _cleanup_combined(combined)
        _log_decision(
            config, entry_title, "rejected", decision_detail,
            score=score, coverage=cscore, stable_fraction=stable_fraction,
            median_rate=median_rate, runtime=total_runtime, path=accept_path,
        )
        return False, decision_detail
    logger.info("Accepting %s via %s path — %s", video_path.name, accept_path, decision_detail)

    try:
        _publish_in_place(
            combined, video_path, expected_fp=pre_fp,
            backup_originals=config.backup_originals,
            backup_dir=config.backup_dir,
            backup_retention_days=config.backup_retention_days,
            backup_registry=config.cache_dir / "backup_dirs.json",
        )
    finally:
        # Whether publish succeeded or failed, the run dir is no longer needed.
        # Cleaning it here keeps the per-run isolation tidy and prevents a
        # ~1-2 GB orphan sitting in the cache forever.
        _cleanup_combined(combined)

    # The rescue path already required sync_ok, so this only fires on the
    # high-similarity path — where a wobbly rate is unusual but worth flagging.
    if accept_path == "similarity" and not sync_ok:
        logger.warning(
            "SYNC QUALITY WARNING for %s — high similarity but %s",
            video_path, sync_reason,
        )

    logger.info(
        "Success: replaced %s (similarity=%.1f%%, stable trunk=%.1f%%)",
        video_path, score, stable_fraction,
    )
    _log_decision(
        config, entry_title, "described", decision_detail,
        score=score, coverage=cscore, stable_fraction=stable_fraction,
        median_rate=median_rate, runtime=total_runtime, path=accept_path,
    )
    return True, None


def _cleanup_combined(combined: Path) -> None:
    """Delete the combined output AND its per-run scratch dir (if any).

    `aligner.run` writes into ``<cache>/output/run-<uuid>/ad_<stem><ext>``.
    Removing just the file leaves an empty ``run-<uuid>`` dir behind on
    every run — over time that's noise the prune job doesn't touch.
    """
    if not combined.exists():
        return
    parent = combined.parent
    combined.unlink(missing_ok=True)
    if parent.name.startswith("run-"):
        shutil.rmtree(parent, ignore_errors=True)


def _file_fingerprint(path: Path) -> tuple[int, int, int]:
    """Stable identity tuple for *path* before/after an alignment.

    ``(st_ino, st_size, st_mtime_ns)`` rejects "Sonarr replaced the file
    while we were aligning" without depending on hashing megabytes of data.
    A normal in-place upgrade by Sonarr/Radarr breaks at least one of the
    three; our own atomic replace breaks all three deliberately.
    """
    st = path.stat()
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _publish_in_place(
    combined: Path,
    video_path: Path,
    expected_fp: Optional[tuple[int, int, int]] = None,
    *,
    backup_originals: bool = False,
    backup_dir: Optional[Path] = None,
    backup_retention_days: int = 14,
    backup_registry: Optional[Path] = None,
) -> None:
    """
    Atomically replace *video_path* with the contents of *combined*.

    *expected_fp* is the fingerprint captured BEFORE alignment started
    (in `_align_and_keep`). It's compared under the lock against the
    current on-disk fingerprint; mismatch means Sonarr/Radarr replaced
    the file during the alignment window and we must refuse to publish.
    If the caller didn't provide one (legacy/test code), we fall back to
    a fingerprint taken at entry — but that only catches changes within
    the publish lock window, not changes during the (much longer)
    alignment subprocess.

    Hardening:
      * fcntl.LOCK_EX on a sibling ``.<name>.admerge.lock`` serialises
        replacements of the same target across processes.
      * Unique tmp filename (``.<name>.admerge.<uuid>.tmp``).
      * Source-fingerprint check under lock aborts if another tool
        (Sonarr file upgrade, manual rsync, etc.) replaced the original
        while alignment ran. We never overwrite a *newer* version of the
        file with our stale alignment.
      * Size-equality check after ``shutil.copy2`` catches disk-full /
        truncated copies; combined with the ffprobe validation that
        ``aligner._validate_media_output`` already performed, that's
        end-to-end coverage without re-probing megabytes.
      * fsync of the tmp file before the rename, then fsync of the parent
        directory after the rename, so a host crash after we return cannot
        leave a torn rename behind.
    """
    pre_fp = expected_fp if expected_fp is not None else _file_fingerprint(video_path)
    expected_size = combined.stat().st_size

    parent = video_path.parent
    lock_path = parent / f".{video_path.name}.admerge.lock"
    tmp_dest = parent / f".{video_path.name}.admerge.{uuid.uuid4().hex}.tmp"

    parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            # Re-check the source under lock: another publisher may have run
            # in the gap between our pre-fingerprint and acquiring the lock.
            current_fp = _file_fingerprint(video_path)
            if current_fp != pre_fp:
                raise RuntimeError(
                    f"Source video changed during alignment (fp {pre_fp!r} → {current_fp!r}); "
                    f"refusing to overwrite {video_path}."
                )

            shutil.copy2(combined, tmp_dest)
            actual_size = tmp_dest.stat().st_size
            if actual_size != expected_size:
                raise RuntimeError(
                    f"Copied output size mismatch (expected {expected_size}, got {actual_size}); "
                    f"refusing to publish."
                )

            # fsync the data before the rename so a crash here doesn't
            # leave a metadata-renamed-but-data-empty file.
            with tmp_dest.open("rb") as fh:
                os.fsync(fh.fileno())

            # shutil.copy2 above can run for a minute on a multi-GB output.
            # The check at the top of the lock only covers the alignment
            # window; re-check here so a Sonarr/Radarr upgrade that landed
            # during the copy isn't silently clobbered.
            final_fp = _file_fingerprint(video_path)
            if final_fp != pre_fp:
                raise RuntimeError(
                    f"Source video changed during publish copy window "
                    f"(fp {pre_fp!r} → {final_fp!r}); refusing to overwrite {video_path}."
                )

            # Insurance: hardlink the original aside before replacing it so a
            # mis-accepted alignment can always be rolled back. Best-effort —
            # a backup failure logs but does not block the replacement.
            if backup_originals:
                backup_path = _backup_original(
                    video_path, backup_dir, backup_retention_days,
                    registry_path=backup_registry,
                )
                if backup_path is not None:
                    logger.info("Backed up original → %s", backup_path)

            os.replace(tmp_dest, video_path)

            # fsync the directory so the rename itself is durable.
            try:
                dir_fd = os.open(parent, os.O_DIRECTORY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception:
            tmp_dest.unlink(missing_ok=True)
            raise
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_file.close()
        # Lock file itself stays — it's harmless, and removing it racy.


def _get_cached(
    client: AudioVaultClient,
    url: str,
    cache_dir: Path,
    limiter: Optional[DownloadLimiter] = None,
) -> Path:
    """
    Return a locally cached copy of *url*, downloading if necessary.

    A JSON manifest (manifest.json) in *cache_dir* maps URL → local path so
    that subsequent calls skip the network entirely.

    If *limiter* is provided it is checked (and incremented) before any actual
    HTTP download so we never exceed AudioVault's 25-downloads-per-day cap.
    Cache hits bypass the limiter entirely.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "manifest.json"

    manifest: dict[str, str] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            logger.warning("Corrupt cache manifest at %s — ignoring.", manifest_path)

    if url in manifest:
        cached = Path(manifest[url])
        if cached.exists():
            logger.info("Cache hit: %s", cached.name)
            return cached
        # Stale entry — file was deleted; fall through to re-download.
        logger.warning("Cached file missing, re-downloading: %s", url)

    if limiter is not None:
        # Pre-flight check (without incrementing) — if we're already at the
        # cap, fail fast before opening a connection.
        if limiter.would_exceed():
            logger.error(
                "Skipping download of %s — AudioVault daily limit reached.", url
            )
            raise DailyLimitReached(
                f"AudioVault daily download limit ({DownloadLimiter.DAILY_LIMIT}) reached."
            )

    file_path = client.download(url, cache_dir)

    # Only spend a daily-quota slot once we know the download actually
    # produced a file. A transient 5xx / Cloudflare blip used to waste a
    # slot here.
    if limiter is not None:
        try:
            limiter.check_and_increment()
        except DailyLimitReached:
            # Genuinely shouldn't happen since we pre-checked, but if a
            # different process raced us across midnight we don't want to
            # silently double-spend. Surface it like before.
            logger.error("AudioVault daily limit reached between pre-check and increment.")
            raise

    manifest[url] = str(file_path)
    _atomic_write_json(manifest_path, manifest)

    return file_path


def _atomic_write_json(path: Path, obj) -> None:
    """Write JSON via .tmp + os.replace so a crash mid-write can't corrupt
    the destination. Used for manifests, done-lists, and similar state."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def drain_retry_queue(queue: RetryQueue, client: AudioVaultClient, config: Config) -> None:
    """
    Process items that were previously skipped due to the daily download limit.

    Once AudioVault's daily download cap is hit, only items that would need a
    *new* download are deferred — items whose season zip is already cached
    keep processing, because a cache hit never charges the limiter. (One
    AudioVault download is a whole season zip, so a single download clears
    every queued episode of that season.) A season that hits the cap is
    remembered for the rest of this drain so its other episodes skip straight
    to the deferred pile instead of re-searching AudioVault for a download
    that can't happen yet.

    Transient network errors re-queue the item with an attempt counter so a
    flaky AudioVault response can't silently drop a Sonarr-queued episode;
    items that hit `_MAX_DRAIN_ATTEMPTS` consecutive failures are dropped.

    Returns a summary dict (described / described_labels / no_match /
    abandoned / deferred / remaining) for the caller to notify on, or None
    if the queue was empty.
    """
    items = queue.load()
    if not items:
        return None
    logger.info("Draining %d queued item(s).", len(items))
    remaining: list[dict] = []
    # (type, title, season/year) keys that needed a download we couldn't make
    # under the cap; their siblings are deferred without a wasted search.
    capped_keys: set[tuple] = set()
    deferred = 0
    # Outcome tallies returned to the caller so it can fire a single
    # drain-summary notification (the per-item live-webhook path notifies
    # elsewhere; the bulk drain stays notify-agnostic itself).
    described_labels: list[str] = []
    no_match = 0
    abandoned = 0
    for item in items:
        video_path = Path(item["video_path"])
        if not video_path.is_file():
            logger.warning("Queued file no longer exists, dropping: %s", video_path)
            continue

        if item.get("type") == "episode":
            cap_key = ("episode", item.get("series_title"), item.get("season"))
        elif item.get("type") == "movie":
            cap_key = ("movie", item.get("movie_title"), item.get("movie_year", ""))
        else:
            cap_key = None
        if cap_key is not None and cap_key in capped_keys:
            if _should_abandon_stale(item):
                abandoned += 1
                logger.error(
                    "Abandoning %s after %d drain passes — never serviceable.",
                    item.get("video_path"), item.get("drain_passes"),
                )
            else:
                remaining.append(item)
                deferred += 1
            continue

        try:
            if item["type"] == "episode":
                # ``extra_episodes`` carries the rest of a Sonarr multi-episode
                # file (S01E01E02 → primary=1, extras=[2]). One alignment runs
                # against the primary episode's audio; the helper marks every
                # covered episode done in the same call.
                extra_episodes = list(item.get("extra_episodes") or [])
                described, _reason = process_episode(
                    client, config, video_path,
                    item["series_title"], item["season"], item["episode"],
                    series_year=item.get("series_year", ""),
                    extra_episodes=extra_episodes,
                )
            elif item["type"] == "movie":
                described, _reason = process_movie(
                    client, config, video_path,
                    item["movie_title"], item.get("movie_year", ""),
                )
            else:
                # Unknown type — keep the item rather than silently dropping it
                # in case a future schema bump lands in a stored queue.
                logger.error(
                    "Unknown retry item type %r — keeping queued: %r",
                    item.get("type"), item,
                )
                remaining.append(item)
                continue
            # Reached only on a non-exception episode/movie pass. The item
            # leaves the queue either way; we only tally which it was.
            if described:
                described_labels.append(_queue_item_label(item))
            else:
                no_match += 1
        except DailyLimitReached:
            if cap_key is not None:
                capped_keys.add(cap_key)
            if _should_abandon_stale(item):
                abandoned += 1
                logger.error(
                    "Abandoning %s after %d drain passes — never serviceable.",
                    item["video_path"], item.get("drain_passes"),
                )
            else:
                remaining.append(item)
                deferred += 1
                logger.info(
                    "Download cap reached for %s — deferring it (and its season's "
                    "other episodes) to the next drain; cache-served items continue.",
                    item["video_path"],
                )
        except _TRANSIENT_ERRORS as exc:
            attempts = int(item.get("drain_attempts", 0)) + 1
            if attempts >= _MAX_DRAIN_ATTEMPTS:
                logger.error(
                    "Queued item %s failed %d consecutive drain attempts — dropping. Last error: %s",
                    item["video_path"], attempts, exc,
                )
            else:
                item["drain_attempts"] = attempts
                remaining.append(item)
                logger.warning(
                    "Transient error draining %s (attempt %d/%d), re-queueing: %s",
                    item["video_path"], attempts, _MAX_DRAIN_ATTEMPTS, exc,
                )
        except requests.HTTPError as exc:
            # 5xx is transient (AudioVault overloaded / Cloudflare 502); 4xx is
            # terminal (auth/permission/not-found) and not worth retrying.
            status = getattr(exc.response, "status_code", 0) or 0
            if 500 <= status < 600:
                attempts = int(item.get("drain_attempts", 0)) + 1
                if attempts >= _MAX_DRAIN_ATTEMPTS:
                    logger.error(
                        "Queued item %s got HTTP %d for %d attempts — dropping.",
                        item["video_path"], status, attempts,
                    )
                else:
                    item["drain_attempts"] = attempts
                    remaining.append(item)
                    logger.warning(
                        "HTTP %d on drain of %s (attempt %d/%d), re-queueing.",
                        status, item["video_path"], attempts, _MAX_DRAIN_ATTEMPTS,
                    )
            else:
                logger.error(
                    "Terminal HTTP %d for queued item %s — dropping.",
                    status, item["video_path"],
                )
        except (KeyError, ValueError, TypeError) as exc:
            # Malformed item in the persisted retry queue — log and drop. A
            # KeyError specifically means an old schema we don't understand
            # anymore. Better to lose one entry than to loop on it forever.
            logger.error(
                "Malformed queued item dropped (%s): %r", exc, item,
            )
        except Exception:
            # Unknown error — keep the item so the next drain can retry, and
            # surface enough context for debugging. Previously this was a
            # silent drop; subtle bugs (e.g. an AudioVault layout change)
            # would erase the entire queue on the next midnight tick.
            logger.error(
                "Unexpected error processing queued item %s — re-queueing.",
                item["video_path"], exc_info=True,
            )
            remaining.append(item)
    if deferred:
        logger.info("%d item(s) deferred to the next drain (download cap).", deferred)
    if remaining:
        queue.save(remaining)
    else:
        queue.clear()
        logger.info("Retry queue drained successfully.")

    return {
        "described": len(described_labels),
        "described_labels": described_labels,
        "no_match": no_match,
        "abandoned": abandoned,
        "deferred": deferred,
        "remaining": len(remaining),
    }


def _queue_item_label(item: dict) -> str:
    """Human label for a retry-queue item, for logs and notifications."""
    if item.get("type") == "movie":
        year = item.get("movie_year", "")
        title = item.get("movie_title", "?")
        return f"{title} ({year})" if year else title
    try:
        return f"{item.get('series_title', '?')} S{int(item['season']):02d}E{int(item['episode']):02d}"
    except (KeyError, ValueError, TypeError):
        return str(item.get("video_path", "?"))


def _safe_dirname(name: str) -> str:
    """Convert an arbitrary string to a safe directory name."""
    name = re.sub(r"[^\w\s-]", "", name).strip()
    return re.sub(r"\s+", "_", name).lower()


def prune_alignment_artifacts(alignment_dir: Path, max_age_days: int = 30) -> None:
    """
    Delete alignment report files (.txt, .json, .png) older than *max_age_days*.

    The alignment directory accumulates one set of files per run and is never
    automatically trimmed. At ~5 files per episode it can grow to thousands of
    entries, which slows _find_report()'s glob and increases the risk of picking
    the wrong report if the mtime filter has any imprecision.

    Called once per day from the midnight drain loop; safe to call at any time
    since no alignment run can overlap (protected by _lock in server.py).
    """
    if not alignment_dir.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for p in alignment_dir.iterdir():
        if p.is_file() and p.suffix.lower() in {".txt", ".json", ".png"} and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("Pruned %d alignment artifact(s) older than %d days.", removed, max_age_days)


_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".wav", ".aac", ".flac", ".ac3", ".mka"}


def _mark_episode_done(
    zip_cache_dir: Path,
    season: int,
    episode: int,
    extract_dir: Optional[Path] = None,
    zip_path: Optional[Path] = None,
) -> None:
    """
    Record *episode* as successfully processed for this season.

    When the set of done episodes equals the number of audio files in the
    extracted zip, the zip and its extracted directory are deleted — they're
    no longer needed and just waste disk space.

    *extract_dir*/*zip_path* are None for an extra (per-episode, no-zip)
    source: there is no season zip to reclaim, so the episode is recorded in
    the ledger and the zip-cleanup branch is skipped.

    The done-episodes file lives at the show level (not inside the season dir)
    so that the zip cache cleanup doesn't erase it.

    Progress is stored as {"total": N, "done": [...]} where *total* is snapped
    on first write from the live filesystem and reused thereafter, so a partially-
    cleaned extract_dir on a later call can't cause premature zip deletion.
    """
    season_dir = zip_cache_dir / f"season_{season:02d}"
    progress_path = zip_cache_dir / f".done_s{season:02d}.json"

    done: set[int] = set()
    stored_total: int = 0

    if progress_path.exists():
        try:
            raw = json.loads(progress_path.read_text())
            if isinstance(raw, list):
                # Migrate legacy format (plain list) to dict on next write.
                done = set(raw)
            else:
                done = set(raw.get("done", []))
                stored_total = int(raw.get("total", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Determine the canonical episode count. Prefer the in-file `total` when
    # available so a partially-cleaned extract_dir on a later call doesn't
    # trigger premature zip deletion. When the file is legacy list-format
    # (`stored_total == 0`), fall back to counting audio entries inside the
    # zip itself — that lets a long-completed season whose .done file pre-
    # dates the cleanup logic still trigger cleanup on the next alignment,
    # rather than stranding the zip on disk forever (~36 GB of leak observed
    # before this fallback existed).
    stored_total = _resolve_audio_total(stored_total, extract_dir, zip_path)

    # Capture saturation *before* adding this episode. Cleanup must fire only
    # on the transition into "season complete", never on every call once the
    # done-set is already full. Otherwise reprocessing an already-complete
    # season — e.g. Sonarr re-grabs/upgrades episodes that were merged on a
    # prior drain — deletes the freshly-downloaded season zip after *every*
    # episode, turning one season into one AudioVault download per episode and
    # burning the daily download cap on a single show. A season that stays
    # complete is reclaimed by the midnight `prune_completed_seasons` sweep, so
    # skipping the lazy delete here cannot leak disk.
    was_complete = stored_total > 0 and len(done) >= stored_total
    done.add(episode)

    season_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(progress_path, {"total": stored_total, "done": sorted(done)})

    if zip_path is not None and stored_total > 0 and len(done) >= stored_total and not was_complete:
        _cleanup_completed_season(zip_cache_dir, season_dir, zip_path, season, stored_total)


def _resolve_audio_total(
    stored_total: int,
    extract_dir: Path,
    zip_path: Path,
    *,
    trust_extract_dir: bool = True,
) -> int:
    """Decide the canonical audio-entry count for a season's cleanup gate.

    Returns *stored_total* unchanged if it's already set; otherwise tries
    the live extract_dir first (cheap, no zip open), then the zip namelist
    as a fallback so legacy-format .done files can still trigger cleanup
    after the extract_dir has been cleaned.

    *trust_extract_dir* must be False when called from a background sweep
    (e.g. `prune_completed_seasons`) that runs concurrently with the
    worker: mid-extraction, `extract_dir.rglob` returns a partial count
    that can racily satisfy `len(done) >= total` and trigger premature
    deletion of an actively-extracting zip. The zip namelist is the
    only stable source of truth in that path.
    """
    if stored_total > 0:
        return stored_total
    if trust_extract_dir and extract_dir is not None and extract_dir.exists():
        live = sum(
            1 for f in extract_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in _AUDIO_EXTS
        )
        if live > 0:
            return live
    if zip_path is not None and zip_path.exists():
        try:
            with zipfile.ZipFile(zip_path) as zf:
                return sum(
                    1 for n in zf.namelist()
                    if Path(n).suffix.lower() in _AUDIO_EXTS
                )
        except (zipfile.BadZipFile, OSError) as exc:
            logger.warning("Could not read zip %s for total count: %s", zip_path, exc)
    return 0


def _cleanup_completed_season(
    zip_cache_dir: Path,
    season_dir: Path,
    zip_path: Path,
    season: int,
    total: int,
) -> None:
    """Delete the zip + extract dir for a fully-processed season and prune
    the download manifest entry. Called from `_mark_episode_done` and from
    the midnight `prune_completed_seasons` sweep."""
    logger.info(
        "All %d episode(s) of season %d done — clearing zip cache.", total, season
    )
    zip_path.unlink(missing_ok=True)
    manifest_path = zip_cache_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            manifest = {k: v for k, v in manifest.items() if Path(v) != zip_path}
            _atomic_write_json(manifest_path, manifest)
        except (json.JSONDecodeError, KeyError):
            pass
    shutil.rmtree(season_dir, ignore_errors=True)


def prune_completed_seasons(cache_dir: Path) -> None:
    """Scan every show's `.done_sNN.json` and reclaim zip+extract for any
    season whose done-set already covers the zip's audio entries.

    Designed for the midnight drain: completed seasons whose .done files
    pre-date the dict-format / cleanup logic never get a new alignment
    that would trigger the lazy cleanup path. Without this, their zips
    sit on disk indefinitely. Safe to run alongside the worker because
    the only seasons it touches are those whose done-set is already
    saturated (no in-flight work can race a finished season).
    """
    shows_root = cache_dir / "shows"
    if not shows_root.is_dir():
        return
    reclaimed = 0
    for show_dir in shows_root.iterdir():
        if not show_dir.is_dir():
            continue
        for done_path in show_dir.glob(".done_s*.json"):
            m = re.match(r"\.done_s(\d+)\.json$", done_path.name)
            if not m:
                continue
            season = int(m.group(1))
            try:
                raw = json.loads(done_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(raw, list):
                done = set(raw)
                stored_total = 0
            else:
                done = set(raw.get("done", []))
                stored_total = int(raw.get("total", 0))
            if not done:
                continue
            season_dir = show_dir / f"season_{season:02d}"
            # Find any zip in the show dir whose name names this season. The
            # manifest maps URLs → local paths; we don't have the URL here,
            # but the zip filename embeds the season number.
            zip_candidates = [
                z for z in show_dir.glob("*.zip")
                if re.search(rf"\bseason\s*0?{season}\b", z.name, re.IGNORECASE)
            ]
            for zip_path in zip_candidates:
                # trust_extract_dir=False so a worker that's mid-extracting
                # this zip can't have its partial extract dir falsely
                # satisfy the cleanup gate from underneath it.
                total = _resolve_audio_total(
                    stored_total, season_dir, zip_path, trust_extract_dir=False
                )
                if total > 0 and len(done) >= total:
                    _cleanup_completed_season(
                        show_dir, season_dir, zip_path, season, total
                    )
                    # Re-write the .done file in dict format so the next pass
                    # doesn't re-evaluate this season.
                    _atomic_write_json(
                        done_path, {"total": total, "done": sorted(done)}
                    )
                    reclaimed += 1
    if reclaimed:
        logger.info("prune_completed_seasons: reclaimed %d zip(s).", reclaimed)
