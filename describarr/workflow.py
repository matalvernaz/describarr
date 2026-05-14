"""
High-level processing workflows for episodes and movies.

Each function:
  1. Searches AudioVault for the matching audio description.
  2. Downloads and caches the file.
  3. Runs describealaign.
  4. Keeps or discards the combined output based on the alignment score.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

import requests

from .aligner import run as align, parse_score, content_score, slope_stability, sync_quality
from .audiovault import AudioVaultClient, DailyLimitReached, DownloadLimiter
from .config import Config
from .matcher import extract_episode, find_movie, find_season
from .retry_queue import RetryQueue

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

# Trailing "(YYYY)" tokens in the title break AudioVault's search index. The
# year stripping is applied defensively here even though callers usually pass
# the title and year separately, because the /retry endpoint and some Sonarr/
# Radarr setups can pass a year-suffixed title through verbatim.
_TITLE_YEAR_SUFFIX_RE = re.compile(r"\s*\(\d{4}\)\s*$")


def _strip_year_suffix(title: str) -> str:
  """Return *title* with a trailing ``(YYYY)`` token removed."""
  return _TITLE_YEAR_SUFFIX_RE.sub("", title).strip()

# LivingAudio FTP fallback is a private add-on kept off the public repo.
# When the module is absent, describarr falls back to AudioVault-only and
# logs a single info-level note on first attempt to use it.
try:
    from . import living_audio as _la  # type: ignore[import-not-found]
    _LA_AVAILABLE = True
except ImportError:
    _la = None  # type: ignore[assignment]
    _LA_AVAILABLE = False

logger = logging.getLogger(__name__)


def process_episode(
    client: AudioVaultClient,
    config: Config,
    video_path: Path,
    series_title: str,
    season: int,
    episode: int,
) -> bool:
    """
    Find and align the audio description for a single TV episode.

    Returns True if a combined file was produced with an acceptable score.
    """
    logger.info(
        "Looking up: %s S%02dE%02d", series_title, season, episode
    )

    search_title = _strip_year_suffix(series_title)
    stripped_note = " (year stripped)" if search_title != series_title else ""
    results = client.search_shows(search_title)
    if not results:
        logger.warning(
            "AudioVault has no results for show: %r%s", series_title, stripped_note
        )
        return False

    candidates = find_season(results, series_title, season)
    if not candidates:
        logger.warning("No season %d entry found for %r.", season, series_title)
        return False

    # Season zips are cached by download URL so we only fetch each season once.
    # Each candidate gets its own extract subdirectory so different zips don't
    # overwrite each other's extracted contents.
    zip_cache_dir = config.cache_dir / "shows" / _safe_dirname(series_title)
    limiter = DownloadLimiter(config.cache_dir / "daily_limit.json")

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
        if _align_and_keep(config, video_path, audio_path):
            _mark_episode_done(zip_cache_dir, season, episode, extract_dir, zip_path)
            return True
        logger.info("Candidate %r below threshold — trying next.", candidate["name"])

    if not _LA_AVAILABLE:
        return False
    la = _la.LivingAudioClient()
    if la.is_configured():
        try:
            audio_path = la.find_episode(config.cache_dir, series_title, season, episode)
            if audio_path and _align_and_keep(config, video_path, audio_path):
                return True
        finally:
            la.close()

    return False


def process_movie(
    client: AudioVaultClient,
    config: Config,
    video_path: Path,
    movie_title: str,
    movie_year: str,
) -> bool:
    """
    Find and align the audio description for a movie.

    Returns True if a combined file was produced with an acceptable score.
    """
    logger.info("Looking up movie: %s (%s)", movie_title, movie_year)

    search_title = _strip_year_suffix(movie_title)
    stripped_note = " (year stripped)" if search_title != movie_title else ""
    results = client.search_movies(search_title)
    if not results:
        logger.warning(
            "AudioVault has no results for movie: %r%s", movie_title, stripped_note
        )
        return False

    candidates = find_movie(results, movie_title, movie_year)
    if not candidates:
        logger.warning("No suitable movie match found for %r.", movie_title)
        return False

    movie_cache_dir = config.cache_dir / "movies"
    limiter = DownloadLimiter(config.cache_dir / "daily_limit.json")

    for candidate in candidates:
        try:
            audio_path = _get_cached(client, candidate["url"], movie_cache_dir, limiter)
        except DailyLimitReached:
            raise
        if _align_and_keep(config, video_path, audio_path):
            return True
        logger.info("Candidate %r below threshold — trying next.", candidate["name"])

    if not _LA_AVAILABLE:
        return False
    la = _la.LivingAudioClient()
    if la.is_configured():
        try:
            la_cache = config.cache_dir / "la_movies"
            for la_candidate in la.search_movies(movie_title, movie_year):
                audio_path = la.download(la_candidate["url"], la_cache)
                if audio_path and _align_and_keep(config, video_path, audio_path):
                    return True
                logger.info(
                    "LivingAudio candidate %r below threshold — trying next.",
                    la_candidate["name"],
                )
        finally:
            la.close()

    return False


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _align_and_keep(config: Config, video_path: Path, audio_path: Path) -> bool:
    """Run alignment and either keep or discard the combined output."""
    alignment_dir = config.cache_dir / "alignments"
    tmp_output_dir = config.cache_dir / "output"

    result = align(video_path, audio_path, tmp_output_dir, alignment_dir, config.stretch_audio)
    if result is None:
        logger.error("Alignment produced no output file.")
        return False

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

    # Three independent acceptance paths:
    #   1. similarity score ≥ min_score (the headline describealaign metric).
    #   2. content coverage ≥ 90% (rescues episodes where commercial-break
    #      seams depress similarity but the trunk content lines up cleanly).
    #   3. slope stability ≥ 90% with a consistent non-trivial drift
    #      (rescues PAL/NTSC content where the 4.27% rate change correctly
    #      describes the entire alignment, but the inherited pitch shift
    #      drags the feature-match similarity score below threshold).
    desc_ok = score >= config.min_score
    coverage_ok = cscore >= 90.0
    slope_ok = (
        stable_fraction >= 90.0
        and score >= 30.0
        and total_runtime >= 300.0
    )

    if not desc_ok and not coverage_ok and not slope_ok:
        logger.warning(
            "Score %.1f%%, coverage %.1f%%, slope stability %.1f%% (median %.2f%%) "
            "— all below thresholds, discarding.",
            score, cscore, stable_fraction, median_rate,
        )
        combined.unlink(missing_ok=True)
        return False

    if not desc_ok and not coverage_ok and slope_ok:
        logger.info(
            "Score %.1f%% and coverage %.1f%% below thresholds, but slope "
            "stability %.1f%% at median %.2f%% — accepting (consistent-drift alignment).",
            score, cscore, stable_fraction, median_rate,
        )
    elif not desc_ok:
        logger.info(
            "Low similarity score (%.1f%%) but content coverage %.1f%% passes — accepting.",
            score, cscore,
        )

    sync_ok, sync_reason = sync_quality(report)

    # Replace the original video with the combined file.
    # shutil.move is non-atomic when source and destination are on different
    # filesystems (e.g. separate Docker volume mounts): it copies then deletes,
    # leaving the destination partially written if the process is killed mid-copy.
    # Instead, copy to a sibling temp file and then os.replace (always atomic on
    # POSIX even across most bind-mount configurations).
    tmp_dest = video_path.parent / (video_path.name + ".tmp")
    try:
        shutil.copy2(combined, tmp_dest)
        os.replace(tmp_dest, video_path)
    except Exception:
        tmp_dest.unlink(missing_ok=True)
        raise
    finally:
        # The combined file is the output_dir cached AD-merged copy; whether the
        # in-place replace succeeded or failed, we don't want a 1-2 GB orphan
        # sitting in the cache forever. If the replace failed the caller will
        # see the original video untouched and can retry, which re-runs the
        # alignment from scratch.
        combined.unlink(missing_ok=True)

    if not sync_ok:
        logger.warning(
            "SYNC QUALITY WARNING for %s — description may be out of sync: %s",
            video_path, sync_reason,
        )

    logger.info("Success (score=%.1f%% coverage=%.1f%%): replaced %s", score, cscore, video_path)
    return True


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
        try:
            limiter.check_and_increment()
        except DailyLimitReached:
            logger.error(
                "Skipping download of %s — AudioVault daily limit reached.", url
            )
            raise

    file_path = client.download(url, cache_dir)

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

    Stops as soon as the limit is hit again, leaving remaining items in the
    queue for the next day.

    Transient network errors re-queue the item with an attempt counter so a
    flaky AudioVault response can't silently drop a Sonarr-queued episode;
    items that hit `_MAX_DRAIN_ATTEMPTS` consecutive failures are dropped.
    """
    items = queue.load()
    if not items:
        return
    logger.info("Draining %d queued item(s).", len(items))
    remaining: list[dict] = []
    limit_hit = False
    for item in items:
        if limit_hit:
            remaining.append(item)
            continue
        video_path = Path(item["video_path"])
        if not video_path.is_file():
            logger.warning("Queued file no longer exists, dropping: %s", video_path)
            continue
        try:
            if item["type"] == "episode":
                process_episode(
                    client, config, video_path,
                    item["series_title"], item["season"], item["episode"],
                )
            elif item["type"] == "movie":
                process_movie(
                    client, config, video_path,
                    item["movie_title"], item.get("movie_year", ""),
                )
        except DailyLimitReached:
            remaining.append(item)
            limit_hit = True
            logger.info(
                "Daily limit hit during queue drain — %d item(s) remain queued.",
                len(items) - items.index(item),
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
        except Exception:
            logger.error(
                "Unexpected error processing queued item %s — dropping.",
                item["video_path"], exc_info=True,
            )
    if remaining:
        queue.save(remaining)
    else:
        queue.clear()
        logger.info("Retry queue drained successfully.")


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
    extract_dir: Path,
    zip_path: Path,
) -> None:
    """
    Record *episode* as successfully processed for this season.

    When the set of done episodes equals the number of audio files in the
    extracted zip, the zip and its extracted directory are deleted — they're
    no longer needed and just waste disk space.

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

    done.add(episode)

    # Determine the canonical episode count.  We use the value from the
    # progress file when available so that a partially-cleaned extract_dir
    # on a later call doesn't give us an artificially-low count and trigger
    # premature zip deletion.
    if stored_total == 0 and extract_dir.exists():
        stored_total = len([
            f for f in extract_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in _AUDIO_EXTS
        ])

    season_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(progress_path, {"total": stored_total, "done": sorted(done)})

    if stored_total > 0 and len(done) >= stored_total:
        logger.info(
            "All %d episode(s) of season %d done — clearing zip cache.", stored_total, season
        )
        zip_path.unlink(missing_ok=True)
        # Remove zip from the download manifest.
        manifest_path = zip_cache_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
                manifest = {k: v for k, v in manifest.items() if Path(v) != zip_path}
                _atomic_write_json(manifest_path, manifest)
            except (json.JSONDecodeError, KeyError):
                pass
        # Delete the extracted dirs and progress file for this season.
        shutil.rmtree(season_dir, ignore_errors=True)
