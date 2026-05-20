"""
Webhook server for describarr.

Listens for POST /hook requests from Sonarr/Radarr shell wrappers.
Request body is application/x-www-form-urlencoded (curl --data-urlencode).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import notify
from .audiovault import AudioVaultClient, DailyLimitReached, DownloadLimiter, LoginError
from .config import Config
from .pending_queue import PendingQueue
from .retry_queue import RetryQueue
from .workflow import drain_retry_queue, process_episode, process_movie, prune_alignment_artifacts, _safe_dirname

logger = logging.getLogger(__name__)

# Shared AudioVault session — created once and reused across all requests.
_client: Optional[AudioVaultClient] = None
_client_lock = threading.Lock()

# Shared retry queue (items deferred by AudioVault's daily download cap).
_retry_queue: Optional[RetryQueue] = None
_retry_queue_lock = threading.Lock()

# Persistent pending queue: every incoming webhook / /retry / /drain request
# is appended here before responding 202, then drained by a single worker
# thread. This is what makes container restarts (Watchtower at 04:00, manual
# `docker compose up -d`, OOM kills) non-destructive — work in flight survives.
_pending_queue: Optional[PendingQueue] = None
_pending_queue_lock = threading.Lock()

# Current job being processed (set by the worker as it pops each item).
_current_job: Optional[dict] = None


@contextmanager
def _set_current_job(info: dict):
    global _current_job
    _current_job = {"started_at": datetime.now().isoformat(), **info}
    try:
        yield
    finally:
        _current_job = None


def _elapsed(iso_start: str) -> str:
    """Human-readable elapsed time from an ISO datetime string."""
    try:
        delta = datetime.now() - datetime.fromisoformat(iso_start)
        secs = int(delta.total_seconds())
    except Exception:
        return "unknown"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h {rem // 60}m"


def _get_client(config: Config) -> AudioVaultClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AudioVaultClient(config.email, config.password)
    return _client


def _get_retry_queue(config: Config) -> RetryQueue:
    global _retry_queue
    with _retry_queue_lock:
        if _retry_queue is None:
            _retry_queue = RetryQueue(config.cache_dir / "retry_queue.json")
    return _retry_queue


def _get_pending_queue(config: Config) -> PendingQueue:
    global _pending_queue
    with _pending_queue_lock:
        if _pending_queue is None:
            _pending_queue = PendingQueue(config.cache_dir / "pending.json")
    return _pending_queue


# Cap on per-item retries before the worker drops a stuck pending item, to
# stop a truly broken Sonarr/Radarr payload from looping forever.
_MAX_WORKER_ATTEMPTS = 5

_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}
_EPISODE_RE = re.compile(r"[Ss](\d+)[Ee](\d+)")
_SEASON_DIR_RE = re.compile(r"^Season\s+\d+$", re.IGNORECASE)
_YEAR_SUFFIX_RE = re.compile(r"^(.+?)\s*\((\d{4})\)\s*$")


def _split_title_year(name: str) -> tuple[str, str | None]:
    """Split 'Inception (2010)' into ('Inception', '2010'); pass through unchanged otherwise."""
    m = _YEAR_SUFFIX_RE.match(name)
    if m:
        return m.group(1).strip(), m.group(2)
    return name, None


def _infer_retry_params(path_str: str, dir_str: str) -> dict:
    """Infer title, year, season, episode from a Sonarr/Radarr-style path layout.

    Sonarr lays out TV as ``<root>/<Series Folder>/Season N/<file>``.
    Radarr lays out movies as ``<root>/<Title (Year)>/<file>``.
    The series/movie folder name (with optional ``(YYYY)`` suffix) is the
    AudioVault search title; ``SxxExx`` in the filename gives season/episode.

    Returned dict only contains keys it could infer.
    """
    target = Path(path_str or dir_str)
    is_file = bool(path_str)
    out: dict = {}

    if is_file:
        m = _EPISODE_RE.search(target.name)
        if m:
            out["season"] = str(int(m.group(1)))
            out["episode"] = str(int(m.group(2)))

    season_dir_idx = next(
        (i for i, p in enumerate(target.parts) if _SEASON_DIR_RE.match(p)),
        None,
    )

    if season_dir_idx is not None:
        # TV layout: parent of "Season N" is the series folder.
        series_folder = target.parts[season_dir_idx - 1] if season_dir_idx > 0 else ""
        title, year = _split_title_year(series_folder)
        if title:
            out["title"] = title
        if year:
            out.setdefault("year", year)
        return out

    if "season" in out:
        # Filename has SxxExx but no "Season N" parent — assume parent dir is the series folder.
        series_folder = target.parent.name if is_file else target.name
        title, year = _split_title_year(series_folder)
        if title:
            out["title"] = title
        if year:
            out.setdefault("year", year)
        return out

    # Otherwise treat as movie: the immediate folder name is "Title (Year)".
    movie_folder = target.parent.name if is_file else target.name
    title, year = _split_title_year(movie_folder)
    if title:
        out["title"] = title
    if year:
        out["year"] = year
    return out


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def serve(port: int = 8686) -> None:
    server = _ThreadingHTTPServer(("0.0.0.0", port), _HookHandler)
    logger.info("describarr webhook server listening on port %d", port)
    threading.Thread(target=_midnight_drain_loop, daemon=True).start()
    threading.Thread(target=_worker_loop, daemon=True).start()
    server.serve_forever()


def _midnight_drain_loop() -> None:
    """Background thread: at 00:05 each day prune old artifacts and enqueue
    a retry-queue drain for the worker."""
    while True:
        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        sleep_secs = (next_run - now).total_seconds()
        logger.info("Retry queue drain scheduled in %.0f seconds.", sleep_secs)
        time.sleep(sleep_secs)
        try:
            config = Config.from_env()
        except ValueError as exc:
            logger.error("Cannot drain retry queue: %s", exc)
            continue
        # Pruning is read-only relative to the worker (touches a different
        # subtree) so it's safe to run here without blocking the queue.
        prune_alignment_artifacts(config.cache_dir / "alignments")

        if _get_retry_queue(config).load():
            _get_pending_queue(config).push({"type": "drain"})


def _worker_loop() -> None:
    """Single-threaded background worker. Pops one pending item at a time and
    processes it under no lock — being the only consumer of the queue
    enforces the "one describealaign run at a time" invariant that used to
    be guarded by an in-memory threading.Lock."""
    while True:
        try:
            config = Config.from_env()
        except ValueError as exc:
            logger.error("Worker cannot load config (%s) — pausing 60s.", exc)
            time.sleep(60)
            continue

        pending = _get_pending_queue(config)
        item = pending.pop_first()
        if item is None:
            pending.wait_for_item(timeout=10.0)
            continue

        try:
            _process_item(item, config, pending)
        except Exception:
            logger.error("Worker: unhandled error on item %r", item, exc_info=True)


def _process_item(item: dict, config: Config, pending: PendingQueue) -> None:
    """Dispatch one pending item to its handler."""
    item_type = item.get("type")
    handlers = {
        "hook": _worker_handle_hook,
        "retry_episode": _worker_handle_retry_episode,
        "retry_movie": _worker_handle_retry_movie,
        "retry_dir": _worker_handle_retry_dir,
        "drain": _worker_handle_drain,
    }
    handler = handlers.get(item_type)
    if handler is None:
        logger.warning("Unknown pending item type %r — dropping: %r", item_type, item)
        return
    try:
        handler(item, config, pending)
    except _TRANSIENT_WORKER_ERRORS as exc:
        attempts = int(item.get("attempts", 0)) + 1
        if attempts >= _MAX_WORKER_ATTEMPTS:
            logger.error(
                "Dropping pending item after %d attempts (%s): %r",
                attempts, exc, item,
            )
            return
        item["attempts"] = attempts
        # Push to the BACK so other items get a fair chance to make progress
        # while this one waits out whatever transient condition tripped it.
        pending.push(item)
        logger.warning(
            "Transient error processing %s (attempt %d/%d), re-queued: %s",
            item_type, attempts, _MAX_WORKER_ATTEMPTS, exc,
        )


# Errors that should re-queue the pending item rather than drop it. Anything
# else is treated as a terminal/programming bug and dropped after logging.
import requests as _requests  # local alias to keep the public import surface clean
_TRANSIENT_WORKER_ERRORS = (
    _requests.ConnectionError,
    _requests.Timeout,
    ConnectionError,
    TimeoutError,
)


class _HookHandler(BaseHTTPRequestHandler):
    close_connection = True
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/status")
            self.end_headers()
        elif path == "/status":
            self._handle_status()
        elif path == "/queue":
            self._handle_queue_get()
        elif path == "/retry":
            self._handle_retry(params)
        else:
            self._respond(404, "Not found.")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/hook":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            env = {k: v[0] for k, v in parse_qs(body.decode()).items()}

            # "Test" events are synchronous so Sonarr/Radarr show their green
            # tick when you click "Test" in the UI.
            sonarr_event = env.get("sonarr_eventtype", "").lower()
            radarr_event = env.get("radarr_eventtype", "").lower()
            if sonarr_event == "test" or radarr_event == "test":
                logger.info("Test event received — configuration looks good.")
                self._respond(200, "OK")
                return

            # Persist the payload BEFORE responding 202. If we crashed/restarted
            # after responding but before processing, Sonarr would already have
            # treated the hook as delivered and never retry — the persistent
            # queue is what saves the work across container lifecycles.
            try:
                config = Config.from_env()
            except ValueError as exc:
                self._respond(500, str(exc))
                return
            _get_pending_queue(config).push({"type": "hook", "env": env})
            self._respond(202, "Accepted — queued for background processing.")
        elif parsed.path == "/drain":
            self._handle_drain()
        else:
            self._respond(404, "Not found.")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/queue":
            self._handle_queue_delete()
        else:
            self._respond(404, "Not found.")

    # ------------------------------------------------------------------
    # Endpoint handlers
    # ------------------------------------------------------------------

    def _handle_status(self) -> None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return

        limiter_state = DownloadLimiter(config.cache_dir / "daily_limit.json")._load()
        today = datetime.now().strftime("%Y-%m-%d")
        if limiter_state.get("date") == today:
            count = limiter_state.get("count", 0)
        else:
            count = 0
        limit = DownloadLimiter.DAILY_LIMIT
        queue = _get_retry_queue(config)
        queued = len(queue.load())
        pending = _get_pending_queue(config).size()
        now = datetime.now()
        next_drain = (now + timedelta(days=1)).replace(
            hour=0, minute=5, second=0, microsecond=0
        )
        data = {
            "date": today,
            "downloads_today": count,
            "limit": limit,
            "remaining": max(0, limit - count),
            "retry_queue": queued,
            "pending_queue": pending,
            "next_drain": next_drain.strftime("%Y-%m-%dT%H:%M:%S"),
            "current_job": _current_job,
        }

        accept = self.headers.get("Accept", "")
        parsed = urlparse(self.path)
        fmt = parse_qs(parsed.query).get("format", [None])[0]
        if fmt == "json" or ("text/html" not in accept and fmt != "html"):
            self._respond_json(200, data)
        else:
            self._respond_html(200, _render_status_html(data))

    def _handle_queue_get(self) -> None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return
        items = _get_retry_queue(config).load()
        self._respond_json(200, items)

    def _handle_queue_delete(self) -> None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return
        queue = _get_retry_queue(config)
        n = len(queue.load())
        queue.clear()
        self._respond(200, f"Cleared {n} item(s) from retry queue.")

    def _handle_drain(self) -> None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return
        queue = _get_retry_queue(config)
        if not queue.load():
            self._respond(200, "Retry queue is empty — nothing to drain.")
            return
        _get_pending_queue(config).push({"type": "drain"})
        self._respond(202, "Accepted — drain queued for the background worker.")

    def _handle_retry(self, params: dict) -> None:
        title = params.get("title", "").strip()
        path_str = params.get("path", "").strip()
        dir_str = params.get("dir", "").strip()
        season_str = params.get("season", "").strip()
        episode_str = params.get("episode", "").strip()
        year_str = params.get("year", "").strip()

        if not (path_str or dir_str):
            self._respond(400, "Provide path= (single file) or dir= (season or show directory)")
            return

        inferred = _infer_retry_params(path_str, dir_str)
        title = title or inferred.get("title", "")
        year_str = year_str or inferred.get("year", "")
        season_str = season_str or inferred.get("season", "")
        episode_str = episode_str or inferred.get("episode", "")

        if not title:
            self._respond(
                400,
                "Could not infer title from path. Pass title= explicitly, or use a "
                "Sonarr-style /tv/<series>/Season N/<file> or Radarr-style "
                "/movies/<title (year)>/<file> layout.",
            )
            return

        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return
        pending = _get_pending_queue(config)

        # Single-file retry (one episode or one movie).
        if path_str:
            if season_str and episode_str:
                try:
                    s, e = int(season_str), int(episode_str)
                except ValueError:
                    self._respond(400, "season and episode must be integers")
                    return
                pending.push({
                    "type": "retry_episode",
                    "title": title,
                    "path": path_str,
                    "season": s,
                    "episode": e,
                })
                label = f"S{s:02d}E{e:02d} of {title!r}"
            else:
                pending.push({
                    "type": "retry_movie",
                    "title": title,
                    "path": path_str,
                    "year": year_str,
                })
                year_label = f" ({year_str})" if year_str else ""
                label = f"movie {title!r}{year_label}"
            self._respond(202, f"Accepted — queued {label}, check container logs for progress")
            return

        # Directory retry (whole season or whole show).
        if dir_str:
            scan_dir = Path(dir_str)
            if not scan_dir.is_dir():
                self._respond(400, f"Directory does not exist: {dir_str}")
                return
            season_filter: Optional[int] = None
            if season_str:
                try:
                    season_filter = int(season_str)
                except ValueError:
                    self._respond(400, "season must be an integer")
                    return
            pending.push({
                "type": "retry_dir",
                "title": title,
                "dir": str(scan_dir),
                "season": season_filter,
            })
            label = f"season {season_filter} of {title!r}" if season_filter else f"all seasons of {title!r}"
            self._respond(202, f"Accepted — queued {label}, check container logs for progress")
            return

        self._respond(400, "Provide path= (single file) or dir= (season or show directory)")

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _respond(self, code: int, message: str) -> None:
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, code: int, data) -> None:
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_html(self, code: int, html: str) -> None:
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info(fmt, *args)


def _render_status_html(data: dict) -> str:
    job = data["current_job"]
    if job:
        jtype = job.get("type", "")
        if jtype == "movie":
            year = f" ({job['year']})" if job.get("year") else ""
            job_label = f"{job['title']}{year}"
        elif jtype == "episode":
            job_label = f"{job['title']} S{job['season']:02d}E{job['episode']:02d}"
        else:
            job_label = job.get("title", "unknown")
        elapsed = _elapsed(job["started_at"])
        job_html = f"""
  <div class="card active">
    <h2>Currently converting</h2>
    <div class="value">{job_label}</div>
    <div class="meta">Running for {elapsed}</div>
  </div>"""
    else:
        job_html = """
  <div class="card">
    <h2>Currently converting</h2>
    <div class="value idle">Idle</div>
  </div>"""

    next_drain_dt = datetime.fromisoformat(data["next_drain"])
    next_drain_str = next_drain_dt.strftime("%b %-d at %-I:%M %p")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>describarr status</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 560px; margin: 2rem auto; padding: 0 1rem; color: #111; }}
    h1 {{ margin-bottom: 0.1rem; }}
    .subtitle {{ color: #666; margin-top: 0; font-size: 0.9rem; }}
    .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin-top: 1rem; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 0.85rem 1rem; }}
    .card.wide {{ grid-column: 1 / -1; }}
    .card.active {{ border-color: #f0a000; background: #fffbec; }}
    h2 {{ margin: 0 0 0.35rem; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: #888; }}
    .value {{ font-size: 1.35rem; font-weight: 600; }}
    .value.idle {{ color: #aaa; font-weight: 400; }}
    .meta {{ color: #888; font-size: 0.8rem; margin-top: 0.2rem; }}
    .footer {{ color: #bbb; font-size: 0.75rem; margin-top: 1.5rem; }}
  </style>
</head>
<body>
  <h1>describarr</h1>
  <p class="subtitle">Audio description sync &mdash; {data['date']}</p>
  <div class="cards">
  <div class="card wide">{job_html.strip()}</div>
  <div class="card">
    <h2>Downloads today</h2>
    <div class="value">{data['downloads_today']} <span style="font-size:1rem;color:#888">/ {data['limit']}</span></div>
    <div class="meta">{data['remaining']} remaining</div>
  </div>
  <div class="card">
    <h2>Pending queue</h2>
    <div class="value">{data['pending_queue']}</div>
    <div class="meta">Webhooks &amp; retries waiting</div>
  </div>
  <div class="card">
    <h2>Retry queue</h2>
    <div class="value">{data['retry_queue']}</div>
    <div class="meta">Next drain: {next_drain_str}</div>
  </div>
  </div>
  <p class="footer">Auto-refreshes every 30 seconds &middot; <a href="/status?format=json">JSON</a></p>
</body>
</html>"""


def _dispatch(env: dict[str, str]) -> dict | None:
    """Run the requested job. Returns an outcome dict for notification,
    or None for events that should not trigger a notification (test / unknown)."""
    sonarr_event = env.get("sonarr_eventtype", "").lower()
    radarr_event = env.get("radarr_eventtype", "").lower()

    if sonarr_event == "test" or radarr_event == "test":
        logger.info("Test event received — configuration looks good.")
        return None

    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return None

    if sonarr_event == "download":
        return _sonarr(config, env)
    if radarr_event == "download":
        return _radarr(config, env)

    logger.error(
        "No recognised event type. Got sonarr_eventtype=%r radarr_eventtype=%r",
        sonarr_event, radarr_event,
    )
    return None


def _sonarr(config: Config, env: dict[str, str]) -> dict | None:
    series_title = env.get("sonarr_series_title", "").strip()
    season_str = env.get("sonarr_episodefile_seasonnumber", "0").strip()
    episode_str = env.get("sonarr_episodefile_episodenumbers", "1").strip()
    file_path_str = env.get("sonarr_episodefile_path", "").strip()

    if not series_title or not file_path_str:
        logger.error("Missing required Sonarr fields.")
        return None

    video_path = Path(file_path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return None

    try:
        season = int(season_str)
    except ValueError:
        logger.error("Could not parse season: %r", season_str)
        return None

    # Sonarr sends a comma-separated list when one file holds multiple episodes
    # (e.g. an S01E01E02 double-length). We need to mark every covered episode
    # as described/queued, not just the first one.
    try:
        episodes = [int(part.strip()) for part in episode_str.split(",") if part.strip()]
    except ValueError:
        logger.error("Could not parse episode list: %r", episode_str)
        return None
    if not episodes:
        logger.error("No episode numbers in Sonarr payload: %r", episode_str)
        return None

    primary_episode = episodes[0]
    extra_episodes = episodes[1:]
    if not extra_episodes:
        label = f"{series_title} S{season:02d}E{primary_episode:02d}"
    else:
        ep_label = "".join(f"E{e:02d}" for e in episodes)
        label = f"{series_title} S{season:02d}{ep_label}"

    client = _get_client(config)
    try:
        with _set_current_job({"type": "episode", "title": series_title, "season": season, "episode": primary_episode}):
            described = process_episode(
                client, config, video_path,
                series_title, season, primary_episode,
                extra_episodes=extra_episodes,
            )
    except DailyLimitReached:
        for ep in episodes:
            _get_retry_queue(config).add_episode(series_title, season, ep, str(video_path))
        return {"label": label, "outcome": "queued"}
    return {"label": label, "outcome": "described" if described else "no_match"}


def _radarr(config: Config, env: dict[str, str]) -> dict | None:
    movie_title = env.get("radarr_movie_title", "").strip()
    movie_year = env.get("radarr_movie_year", "").strip()
    file_path_str = env.get("radarr_moviefile_path", "").strip()

    if not movie_title or not file_path_str:
        logger.error("Missing required Radarr fields.")
        return None

    video_path = Path(file_path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return None

    label = f"{movie_title} ({movie_year})" if movie_year else movie_title
    client = _get_client(config)
    try:
        with _set_current_job({"type": "movie", "title": movie_title, "year": movie_year}):
            described = process_movie(client, config, video_path, movie_title, movie_year)
    except DailyLimitReached:
        _get_retry_queue(config).add_movie(movie_title, movie_year, str(video_path))
        return {"label": label, "outcome": "queued"}
    return {"label": label, "outcome": "described" if described else "no_match"}


# ------------------------------------------------------------------
# Worker handlers — called one at a time by _worker_loop, never concurrently.
# ------------------------------------------------------------------

_OUTCOME_MESSAGES = {
    "described": "Added and described.",
    "no_match": "Added — no audio description available.",
    "queued": "Added — description queued (AudioVault daily limit reached).",
    "error": "Added — describarr errored, check logs.",
}


def _worker_handle_hook(item: dict, config: Config, pending: PendingQueue) -> None:
    env = item.get("env", {})
    result: dict | None = None
    errored = False
    try:
        result = _dispatch(env)
    except _TRANSIENT_WORKER_ERRORS:
        raise  # bubble to _process_item so it re-queues
    except Exception:
        logger.error("Unhandled error processing hook.", exc_info=True)
        errored = True

    if errored:
        label = _label_from_env(env)
        if label:
            notify.send(f"describarr: {label}", _OUTCOME_MESSAGES["error"])
        return

    if not result:
        return

    notify.send(
        f"describarr: {result['label']}",
        _OUTCOME_MESSAGES.get(result["outcome"], result["outcome"]),
    )


def _worker_handle_retry_episode(item: dict, config: Config, pending: PendingQueue) -> None:
    title = item["title"]
    path_str = item["path"]
    try:
        season = int(item["season"])
        episode = int(item["episode"])
    except (KeyError, ValueError):
        logger.error("retry_episode item malformed: %r", item)
        return

    video_path = Path(path_str)
    if not video_path.is_file():
        logger.error("Retry episode: file not found, dropping: %s", video_path)
        return

    client = _get_client(config)
    label = f"{title} S{season:02d}E{episode:02d}"
    try:
        with _set_current_job({"type": "episode", "title": title, "season": season, "episode": episode}):
            described = process_episode(client, config, video_path, title, season, episode)
    except DailyLimitReached:
        _get_retry_queue(config).add_episode(title, season, episode, str(video_path))
        notify.send(f"describarr: {label}", _OUTCOME_MESSAGES["queued"])
        return
    outcome = "described" if described else "no_match"
    notify.send(f"describarr: {label}", _OUTCOME_MESSAGES.get(outcome, outcome))


def _worker_handle_retry_movie(item: dict, config: Config, pending: PendingQueue) -> None:
    title = item["title"]
    path_str = item["path"]
    year_str = item.get("year", "") or ""

    video_path = Path(path_str)
    if not video_path.is_file():
        logger.error("Retry movie: file not found, dropping: %s", video_path)
        return

    client = _get_client(config)
    label = f"{title} ({year_str})" if year_str else title
    try:
        with _set_current_job({"type": "movie", "title": title, "year": year_str}):
            described = process_movie(client, config, video_path, title, year_str)
    except DailyLimitReached:
        _get_retry_queue(config).add_movie(title, year_str, str(video_path))
        notify.send(f"describarr: {label}", _OUTCOME_MESSAGES["queued"])
        return
    outcome = "described" if described else "no_match"
    notify.send(f"describarr: {label}", _OUTCOME_MESSAGES.get(outcome, outcome))


def _worker_handle_retry_dir(item: dict, config: Config, pending: PendingQueue) -> None:
    """Expand a directory retry into per-episode pending items.

    Doing the scan from the worker (rather than at request time) keeps the
    /retry?dir= response fast and means a restart mid-expansion just re-runs
    the (idempotent) scan once we resume."""
    title = item["title"]
    scan_dir = Path(item["dir"])
    season_filter = item.get("season")
    if season_filter is not None:
        try:
            season_filter = int(season_filter)
        except (TypeError, ValueError):
            logger.error("retry_dir item has non-int season: %r", item)
            return

    if not scan_dir.is_dir():
        logger.error("Retry dir: directory not found, dropping: %s", scan_dir)
        return

    video_files = sorted(
        f for f in scan_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS
    )
    if not video_files:
        logger.warning("No video files found in %s", scan_dir)
        return

    show_cache_dir = config.cache_dir / "shows" / _safe_dirname(title)
    queued = 0
    skipped = 0
    for video_path in video_files:
        m = _EPISODE_RE.search(video_path.name)
        if not m:
            logger.warning("Could not parse SxxExx from %s — skipping", video_path.name)
            continue
        season = int(m.group(1))
        episode = int(m.group(2))
        if season_filter is not None and season != season_filter:
            continue

        done_path = show_cache_dir / f".done_s{season:02d}.json"
        if done_path.exists():
            try:
                raw = json.loads(done_path.read_text())
                done = set(raw) if isinstance(raw, list) else set(raw.get("done", []))
                if episode in done:
                    skipped += 1
                    continue
            except (json.JSONDecodeError, ValueError):
                pass

        pending.push({
            "type": "retry_episode",
            "title": title,
            "path": str(video_path),
            "season": season,
            "episode": episode,
        })
        queued += 1

    logger.info(
        "Retry dir %s: queued %d episode(s), skipped %d already-done.",
        scan_dir, queued, skipped,
    )


def _worker_handle_drain(item: dict, config: Config, pending: PendingQueue) -> None:
    queue = _get_retry_queue(config)
    if not queue.load():
        return
    client = _get_client(config)
    with _set_current_job({"type": "drain", "title": "retry queue drain"}):
        drain_retry_queue(queue, client, config)


def _label_from_env(env: dict[str, str]) -> str | None:
    """Best-effort label for a failed hook (used only when _dispatch raised)."""
    if env.get("sonarr_eventtype", "").lower() == "download":
        title = env.get("sonarr_series_title", "").strip()
        season = env.get("sonarr_episodefile_seasonnumber", "").strip()
        ep = env.get("sonarr_episodefile_episodenumbers", "").strip().split(",")[0].strip()
        if title and season and ep:
            try:
                return f"{title} S{int(season):02d}E{int(ep):02d}"
            except ValueError:
                return title
        return title or None
    if env.get("radarr_eventtype", "").lower() == "download":
        title = env.get("radarr_movie_title", "").strip()
        year = env.get("radarr_movie_year", "").strip()
        if title and year:
            return f"{title} ({year})"
        return title or None
    return None
