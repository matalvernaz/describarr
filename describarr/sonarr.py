"""Episode titles from Sonarr, for requests that arrive without one.

A Sonarr import hands describarr the episode's title, and the matcher uses the
title twice: it tries the recording named for the episode first, and it lets
the corroborated rescue accept a recording whose timing is perfect but whose
similarity score is low. A retry arrives with a file and nothing else; its
title came from a media server's .nfo or the release name, and many files have
neither. Law & Order: SVU S22E13 (2026-10-15) was refused at a 99.6 % straight
line because "Law.and.Order.SVU.S22E13.1080p.WEB.H264-STRONTiUM.mkv" names no
episode, while AudioVault's "22.13 Trick-Rolled at the Moulin.mp3" was the
right recording.

This asks Sonarr which episode a file is, as its hook would have said: the
series whose folder holds the file, the episode file Sonarr has by that name,
and the episodes Sonarr maps to that file. It answers only when sure: exactly
one series with that folder name, exactly one file by that name (or by its
path inside the series), and Sonarr's first episode for the file numbered as
the one being searched. Otherwise "", and the .nfo and the file name are read
as before. A file Sonarr numbers differently from its name (a release one
episode behind TVDB), a special in season 0, or an absolute-numbered show
therefore gets no title from here.

A failure is remembered at the scope it happened (the series list for every
lookup, one series' files for that series), so a dead Sonarr costs one slow
call per cooldown, not one per episode. The API key travels only in the
X-Api-Key header and is never logged.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

#: How long a good answer from Sonarr is reused.
CACHE_SECONDS = 600
#: How long Sonarr is left alone after a failure, at the failure's scope.
FAILURE_COOLDOWN_SECONDS = 600
#: A file Sonarr did not list may have been imported since; one fresh look per
#: series this often, at most.
MISS_REFRESH_SECONDS = 60
TIMEOUT_SECONDS = 10

# Folders that can sit between a series folder and its files, and never name one.
_GENERIC_FOLDER = re.compile(r"^(season\s*\d+|specials|extras|featurettes)$", re.IGNORECASE)


def _parts(path: str) -> list[str]:
    """A path's components, whichever separator the other side writes: Sonarr
    on Windows answers ``C:\\TV\\Show`` and ``Season 01\\Episode.mkv``."""
    return [p for p in str(path).replace("\\", "/").split("/") if p]


class SonarrError(Exception):
    """A request that did not give a usable answer; says what, never where."""

    def __init__(self, what: str, status: Optional[int] = None) -> None:
        super().__init__(f"HTTP {status}" if status is not None else what)
        self.status = status


@dataclass
class _SeriesFiles:
    fetched_at: float
    # basename (casefold) -> [(path inside the series, casefold parts), file id]
    by_name: dict[str, list[tuple[tuple[str, ...], int]]]
    # file id -> [(season, number, title)]
    by_file: dict[int, list[tuple[int, int, str]]]


class SonarrTitles:
    """Sonarr's title for a library file's episode, when it is sure of it."""

    def __init__(
        self,
        url: str,
        api_key: str,
        clock: Callable[[], float] = time.monotonic,
        http_get: Optional[Callable] = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._key = api_key
        self._clock = clock
        self._http_get = http_get
        # Guards the caches only; no request is ever made while holding it, so
        # a slow Sonarr cannot stall another thread's lookup.
        self._lock = threading.Lock()
        self._series: Optional[dict[str, list[int]]] = None
        self._series_at = float("-inf")
        self._series_down_until = float("-inf")
        self._files: dict[int, _SeriesFiles] = {}
        self._files_down_until: dict[int, float] = {}
        self._refreshed_at: dict[int, float] = {}

    def episode_title(self, video_path: str | Path, season: int, episode: int) -> str:
        """Sonarr's title for the episode *video_path* holds, or "". Never raises."""
        try:
            return self._episode_title(Path(str(video_path)), season, episode)
        except Exception as exc:
            logger.warning("Sonarr title lookup for %s failed (%s); reading the .nfo and file name.",
                           Path(str(video_path)).name, type(exc).__name__)
            return ""

    # -- the lookup -----------------------------------------------------------

    def _episode_title(self, video: Path, season: int, episode: int) -> str:
        series_map = self._series_map()
        if not series_map:
            return ""
        series_id, inside = self._series_holding(series_map, video)
        if series_id is None:
            return ""
        files = self._series_files(series_id)
        if files is None:
            return ""
        name = video.name.casefold()
        candidates = files.by_name.get(name, [])
        if not candidates and self._may_refresh(series_id):
            # Only an outright miss earns a fresh look: an ambiguous name or a
            # number Sonarr disagrees with would read the same the next time.
            files = self._series_files(series_id, force=True)
            if files is None:
                return ""
            candidates = files.by_name.get(name, [])
        if not candidates:
            return ""
        if len(candidates) > 1:
            narrowed = [file_id for rel, file_id in candidates if rel == inside]
            if len(narrowed) != 1:
                logger.info("Sonarr lists %d files named %s in that series; not guessing which.",
                            len(candidates), video.name)
                return ""
            file_id = narrowed[0]
        else:
            file_id = candidates[0][1]
        episodes = sorted(files.by_file.get(file_id, []))
        if not episodes:
            return ""
        first_season, first_number, title = episodes[0]
        if (first_season, first_number) != (season, episode):
            logger.info(
                "Sonarr maps %s to S%02dE%02d, but it is being searched as S%02dE%02d; "
                "not using Sonarr's title.",
                video.name, first_season, first_number, season, episode,
            )
            return ""
        return title.strip()

    def _series_holding(self, series_map: dict[str, list[int]],
                        video: Path) -> tuple[Optional[int], tuple[str, ...]]:
        """The one series whose folder holds *video*, and the file's path inside it."""
        folders = _parts(str(video.parent))
        for depth in range(len(folders) - 1, -1, -1):
            folder = folders[depth]
            if _GENERIC_FOLDER.match(folder):
                continue
            ids = series_map.get(folder.casefold())
            if not ids:
                continue
            if len(ids) > 1:
                logger.info("Sonarr has %d series in folders named %r; not guessing which holds %s.",
                            len(ids), folder, video.name)
                return None, ()
            inside = tuple(p.casefold() for p in folders[depth + 1:] + [video.name])
            return ids[0], inside
        return None, ()

    def _may_refresh(self, series_id: int) -> bool:
        now = self._clock()
        with self._lock:
            if now - self._refreshed_at.get(series_id, float("-inf")) < MISS_REFRESH_SECONDS:
                return False
            self._refreshed_at[series_id] = now
            return True

    # -- Sonarr, cached -------------------------------------------------------

    def _series_map(self) -> Optional[dict[str, list[int]]]:
        """Folder name (casefold) -> ids of the series kept in a folder so named."""
        now = self._clock()
        with self._lock:
            if self._series is not None and now - self._series_at < CACHE_SECONDS:
                return self._series
            if now < self._series_down_until:
                return None
        try:
            mapping: dict[str, list[int]] = {}
            for series in self._get("series"):
                folders = _parts(series.get("path") or "")
                if folders and isinstance(series.get("id"), int):
                    mapping.setdefault(folders[-1].casefold(), []).append(series["id"])
        except (SonarrError, AttributeError, TypeError, KeyError) as exc:
            with self._lock:
                self._series_down_until = now + FAILURE_COOLDOWN_SECONDS
            logger.warning(
                "Sonarr's series list could not be read (%s); retries take titles from the "
                ".nfo and file names for the next %d min.",
                _describe(exc), FAILURE_COOLDOWN_SECONDS // 60,
            )
            return None
        with self._lock:
            self._series, self._series_at = mapping, now
        return mapping

    def _series_files(self, series_id: int, force: bool = False) -> Optional[_SeriesFiles]:
        now = self._clock()
        with self._lock:
            cached = self._files.get(series_id)
            if cached is not None and not force and now - cached.fetched_at < CACHE_SECONDS:
                return cached
            if now < self._files_down_until.get(series_id, float("-inf")):
                return None
        try:
            by_name: dict[str, list[tuple[tuple[str, ...], int]]] = {}
            for record in self._get("episodefile", {"seriesId": series_id}):
                inside = _parts(record.get("relativePath") or "") or _parts(record.get("path") or "")[-1:]
                if inside and isinstance(record.get("id"), int):
                    by_name.setdefault(inside[-1].casefold(), []).append(
                        (tuple(p.casefold() for p in inside), record["id"]))
            by_file: dict[int, list[tuple[int, int, str]]] = {}
            for record in self._get("episode", {"seriesId": series_id}):
                file_id = record.get("episodeFileId")
                season, number = record.get("seasonNumber"), record.get("episodeNumber")
                if isinstance(file_id, int) and file_id and isinstance(season, int) \
                        and isinstance(number, int):
                    by_file.setdefault(file_id, []).append((season, number, str(record.get("title") or "")))
        except (SonarrError, AttributeError, TypeError, KeyError) as exc:
            with self._lock:
                self._files_down_until[series_id] = now + FAILURE_COOLDOWN_SECONDS
            logger.warning(
                "Sonarr's files for series %d could not be read (%s); its retries take titles "
                "from the .nfo and file names for the next %d min.",
                series_id, _describe(exc), FAILURE_COOLDOWN_SECONDS // 60,
            )
            return None
        entry = _SeriesFiles(now, by_name, by_file)
        with self._lock:
            self._files[series_id] = entry
        return entry

    def _get(self, path: str, params: Optional[dict] = None):
        http_get = self._http_get or requests.get
        try:
            response = http_get(
                f"{self._url}/api/v3/{path}", params=params,
                headers={"X-Api-Key": self._key}, timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # The exception's text can carry the request; only its kind is kept.
            raise SonarrError(type(exc).__name__) from None
        if response.status_code != 200:
            raise SonarrError("HTTP", response.status_code)
        try:
            return response.json()
        except ValueError:
            raise SonarrError("a reply that is not JSON") from None


def _describe(exc: Exception) -> str:
    if isinstance(exc, SonarrError):
        if exc.status == 401:
            return "HTTP 401: the API key was refused"
        return str(exc)
    return f"an unexpected reply ({type(exc).__name__})"


_shared: Optional[SonarrTitles] = None
_shared_settings: Optional[tuple[str, str]] = None
_shared_lock = threading.Lock()


def episode_title(video_path: str | Path, season: int, episode: int) -> str:
    """Sonarr's title for this file's episode, or "" (also when SONARR_URL and
    SONARR_API_KEY are not both set, in which case nothing is asked)."""
    client = _client()
    return client.episode_title(video_path, season, episode) if client is not None else ""


def _client() -> Optional[SonarrTitles]:
    global _shared, _shared_settings
    url = os.environ.get("SONARR_URL", "").strip()
    key = os.environ.get("SONARR_API_KEY", "").strip()
    if not (url and key):
        return None
    with _shared_lock:
        if _shared is None or _shared_settings != (url, key):
            _shared, _shared_settings = SonarrTitles(url, key), (url, key)
        return _shared


def reset() -> None:
    """Forget the shared client and its caches (tests)."""
    global _shared, _shared_settings
    with _shared_lock:
        _shared, _shared_settings = None, None
