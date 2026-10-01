"""Pluggable audio-description source providers.

describarr's built-in source is AudioVault. *Additional* sources — e.g. a
private paid provider — live entirely outside this repository and are loaded at
runtime by dotted import path from the ``DESCRIBARR_EXTRA_SOURCES`` environment
variable. This module defines only the interface such a provider implements and
the loader that discovers it; it never names, contains, or depends on any
specific provider.

A provider module exposes a zero-argument factory (``get_source`` by default,
or an explicit ``module:factory``) returning an object satisfying
:class:`AudioSource`. The provider owns its own configuration, search, download
and caching; describarr only asks it for local audio-file candidates to align,
tried after AudioVault has been exhausted.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_ENV_VAR = "DESCRIBARR_EXTRA_SOURCES"


@runtime_checkable
class AudioSource(Protocol):
    """A supplementary audio-description source tried after AudioVault."""

    def is_configured(self) -> bool:
        """True when the source has everything it needs (creds, host, …)."""

    def episode_candidates(
        self, cache_dir: Path, series_title: str, season: int, episode: int,
        episode_title: str = "",
    ) -> Iterable[Path]:
        """Yield local paths to candidate AD audio files for one episode.

        Candidates are aligned in order and the first that passes the
        acceptance gate wins, so a source may yield lazily (download-on-demand).

        *episode_title* is Sonarr's title for the episode, or ``""`` when none
        is known. It is evidence a source may use to accept a file it would
        otherwise pass over, never a requirement. A source written before the
        parameter existed keeps working: see :func:`episode_candidates_from`.
        """

    def movie_candidates(
        self, cache_dir: Path, movie_title: str, movie_year: str
    ) -> Iterable[Path]:
        """Yield local paths to candidate AD audio files for one movie."""

    def close(self) -> None:
        """Release any held resources (connections, sockets). Idempotent."""


def load_extra_sources() -> list[AudioSource]:
    """Instantiate the extra sources named in ``DESCRIBARR_EXTRA_SOURCES``.

    The value is a comma-separated list of ``module`` or ``module:factory``
    dotted paths; each factory takes no arguments and returns an
    :class:`AudioSource`. Unset ⇒ AudioVault only. A source that fails to
    import, whose factory raises, or that reports itself unconfigured is logged
    and skipped — never fatal to the run.
    """
    spec = os.environ.get(_ENV_VAR, "").strip()
    if not spec:
        return []
    sources: list[AudioSource] = []
    for entry in (e.strip() for e in spec.split(",")):
        if not entry:
            continue
        module_path, _, factory_name = entry.partition(":")
        factory_name = factory_name or "get_source"
        try:
            module = importlib.import_module(module_path)
            source = getattr(module, factory_name)()
            configured = source.is_configured()
        except Exception as exc:
            logger.warning("Could not load extra source %r: %s", entry, exc)
            continue
        if configured:
            logger.info("Loaded extra audio-description source: %s", entry)
            sources.append(source)
        else:
            logger.info("Extra source %r loaded but not configured — skipping.", entry)
    return sources


def episode_candidates_from(
    source: AudioSource, cache_dir: Path, series_title: str, season: int, episode: int,
    episode_title: str = "",
) -> Iterable[Path]:
    """Ask *source* for one episode's candidates, passing the title if it takes one.

    ``episode_title`` joined :class:`AudioSource` after sources existed that do
    not accept it, so a source whose method has no such parameter is asked
    exactly as before rather than failing with a ``TypeError``.
    """
    if episode_title and _accepts_keyword(source.episode_candidates, "episode_title"):
        return source.episode_candidates(
            cache_dir, series_title, season, episode, episode_title=episode_title
        )
    return source.episode_candidates(cache_dir, series_title, season, episode)


def _accepts_keyword(func, name: str) -> bool:
    """True when *func* takes a keyword argument called *name* (or any keyword)."""
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params
    )
