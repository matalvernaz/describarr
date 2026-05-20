"""Persistent queue for items skipped due to the AudioVault daily download limit."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* via a sibling .tmp + os.replace, so a crash
    mid-write cannot leave a half-written/corrupt file at the destination."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


class RetryQueue:
    """
    Persists episodes/movies that couldn't be downloaded because the daily
    limit was reached.  Stored as a JSON list at *state_path*.
    """

    def __init__(self, state_path: Path) -> None:
        self._path = state_path

    def add_episode(self, series_title: str, season: int, episode: int, video_path: str) -> None:
        """Queue a single-episode retry."""
        self.add_episodes(series_title, season, [episode], video_path)

    def add_episodes(
        self,
        series_title: str,
        season: int,
        episodes: list[int],
        video_path: str,
    ) -> None:
        """Queue a (possibly multi-) episode retry as ONE item.

        A multi-episode Sonarr download (``S01E01E02.mkv``) ships in a
        single file. Previously the daily-limit branch queued one entry
        per episode pointing at the same ``video_path``, and the
        ``_append`` dedup-by-video-path silently dropped E02..N. Worse,
        if dedup were ever loosened, ``drain_retry_queue`` would re-align
        the now-E01-merged file against each subsequent audio in turn and
        clobber earlier merges. One item carrying the full list lets the
        drain dispatch via ``process_episode(..., extra_episodes=…)`` and
        record every covered episode in the season's ``.done_sNN.json``
        without any second alignment.
        """
        if not episodes:
            return
        primary = episodes[0]
        extras = [e for e in episodes[1:] if e != primary]
        item = {
            "type": "episode",
            "series_title": series_title,
            "season": season,
            "episode": primary,
            "video_path": video_path,
        }
        if extras:
            item["extra_episodes"] = extras
        self._append(item)

    def add_movie(self, movie_title: str, movie_year: str, video_path: str) -> None:
        self._append({
            "type": "movie",
            "movie_title": movie_title,
            "movie_year": movie_year,
            "video_path": video_path,
        })

    def load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, ValueError):
            logger.warning("Corrupt retry queue at %s — ignoring.", self._path)
            return []

    def save(self, items: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self._path, json.dumps(items, indent=2))

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)

    def _append(self, item: dict) -> None:
        items = self.load()
        key = item.get("video_path", "")
        if any(i.get("video_path") == key for i in items):
            logger.debug("Already in retry queue, skipping: %s", key)
            return
        items.append(item)
        self.save(items)
        logger.info("Queued for retry (%d total): %s", len(items), key)
