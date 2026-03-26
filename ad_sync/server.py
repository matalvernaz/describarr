"""
Webhook server for ad-sync.

Listens for POST /hook requests from Sonarr/Radarr shell wrappers.
Request body is application/x-www-form-urlencoded (curl --data-urlencode).
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .audiovault import AudioVaultClient, LoginError
from .config import Config
from .workflow import process_episode, process_movie

logger = logging.getLogger(__name__)

# Prevent concurrent describealign runs (CPU/RAM heavy).
_lock = threading.Lock()


def serve(port: int = 8686) -> None:
    server = HTTPServer(("0.0.0.0", port), _HookHandler)
    logger.info("ad-sync webhook server listening on port %d", port)
    server.serve_forever()


class _HookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/hook":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        env = {k: v[0] for k, v in parse_qs(body.decode()).items()}

        with _lock:
            ok = _dispatch(env)

        self.send_response(200 if ok else 500)
        self.end_headers()

    def log_message(self, fmt, *args):
        logger.info(fmt, *args)


def _dispatch(env: dict[str, str]) -> bool:
    sonarr_event = env.get("sonarr_eventtype", "").lower()
    radarr_event = env.get("radarr_eventtype", "").lower()

    if sonarr_event == "test" or radarr_event == "test":
        logger.info("Test event received — configuration looks good.")
        return True

    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return False

    if sonarr_event == "download":
        return _sonarr(config, env)
    if radarr_event == "download":
        return _radarr(config, env)

    logger.error(
        "No recognised event type. Got sonarr_eventtype=%r radarr_eventtype=%r",
        sonarr_event, radarr_event,
    )
    return False


def _sonarr(config: Config, env: dict[str, str]) -> bool:
    series_title = env.get("sonarr_series_title", "").strip()
    season_str = env.get("sonarr_episodefile_seasonnumber", "0").strip()
    episode_str = env.get("sonarr_episodefile_episodenumbers", "1").strip()
    file_path_str = env.get("sonarr_episodefile_path", "").strip()

    if not series_title or not file_path_str:
        logger.error("Missing required Sonarr fields.")
        return False

    video_path = Path(file_path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return False

    try:
        season = int(season_str)
        episode = int(episode_str.split(",")[0].strip())
    except ValueError:
        logger.error("Could not parse season/episode: %r / %r", season_str, episode_str)
        return False

    try:
        client = AudioVaultClient(config.email, config.password)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        return False

    return process_episode(client, config, video_path, series_title, season, episode)


def _radarr(config: Config, env: dict[str, str]) -> bool:
    movie_title = env.get("radarr_movie_title", "").strip()
    movie_year = env.get("radarr_movie_year", "").strip()
    file_path_str = env.get("radarr_moviefile_path", "").strip()

    if not movie_title or not file_path_str:
        logger.error("Missing required Radarr fields.")
        return False

    video_path = Path(file_path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return False

    try:
        client = AudioVaultClient(config.email, config.password)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        return False

    return process_movie(client, config, video_path, movie_title, movie_year)
