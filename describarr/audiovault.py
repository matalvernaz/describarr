"""AudioVault client: login, search, and download audio description files."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://audiovault.net"


# Punctuation collapsed to spaces when a literal search returns nothing.
# AudioVault's search is a literal substring match, so a hyphenation or
# colon difference between the request title and the catalog spelling
# ("The 40 Year-Old Virgin" vs "The 40 Year Old Virgin") yields zero rows.
_SEARCH_FALLBACK_PUNCT_RE = re.compile(r"[-–—:]")


def _normalize_search_query(query: str) -> str:
    """Collapse dashes/colons to spaces and squeeze runs of whitespace."""
    return " ".join(_SEARCH_FALLBACK_PUNCT_RE.sub(" ", query).split())


def _is_login_url(url: str) -> bool:
    """True iff *url* is the AudioVault login endpoint, with or without
    query string. ``url.endswith("/login")`` alone misses ``/login?next=…``
    and silently caches the login page as media."""
    try:
        path = urlparse(url).path
    except ValueError:
        return False
    return path.rstrip("/").endswith("/login")


# Content-Type values AudioVault legitimately serves for downloads.
# Anything else (most importantly ``text/html``) is rejected so a login
# / error / quota page can never be cached as media.
_ACCEPTABLE_MEDIA_PREFIXES = ("audio/", "video/")
_ACCEPTABLE_MEDIA_TYPES = frozenset({
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
})


def _is_acceptable_media_type(content_type: str) -> bool:
    if not content_type:
        return True  # absent header → trust Content-Disposition / extension
    if any(content_type.startswith(p) for p in _ACCEPTABLE_MEDIA_PREFIXES):
        return True
    return content_type in _ACCEPTABLE_MEDIA_TYPES

# Mimic a real Firefox request so the server doesn't reject us outright.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.5",
}


class LoginError(RuntimeError):
    """Raised when AudioVault login fails."""


class DailyLimitReached(RuntimeError):
    """Raised when the AudioVault 25-downloads-per-day limit would be exceeded."""


class DownloadLimiter:
    """
    Tracks AudioVault downloads against the 25-per-day limit.

    State is persisted to *state_path* as a small JSON file so the count
    survives process restarts.  The date resets automatically at midnight.
    """

    DAILY_LIMIT = 25

    def __init__(self, state_path: Path) -> None:
        self._path = state_path

    def check_and_increment(self) -> None:
        """Increment the counter or raise DailyLimitReached."""
        today = date.today().isoformat()
        state = self._load()
        if state.get("date") != today:
            state = {"date": today, "count": 0}
        if state["count"] >= self.DAILY_LIMIT:
            raise DailyLimitReached(
                f"AudioVault daily download limit ({self.DAILY_LIMIT}) reached. "
                "The counter resets at midnight."
            )
        state["count"] += 1
        self._save(state)
        logger.info(
            "AudioVault downloads today: %d/%d", state["count"], self.DAILY_LIMIT
        )

    def would_exceed(self) -> bool:
        """Return True iff a fresh increment right now would hit the cap.

        Used by callers that want to pre-flight a slot without consuming it,
        so a transient network error during the actual download doesn't
        burn one of the 25 daily AudioVault slots.
        """
        today = date.today().isoformat()
        state = self._load()
        if state.get("date") != today:
            return False
        return int(state.get("count", 0)) >= self.DAILY_LIMIT

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, ValueError):
                pass
        return {}

    def _save(self, state: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write so a crash mid-write can't reset count back to 0 and
        # let us double-spend the AudioVault daily quota.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self._path)


class AudioVaultClient:
    """Authenticated session for AudioVault."""

    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._login()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _login(self) -> None:
        # Fetch the login page to collect the Laravel CSRF token.
        resp = self._session.get(f"{BASE_URL}/login", timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        token_input = soup.find("input", {"name": "_token"})
        if not token_input:
            raise LoginError("Could not find CSRF token on login page.")

        payload = {
            "_token": token_input["value"],
            "email": self._email,
            "password": self._password,
            "remember": "on",
        }

        resp = self._session.post(
            f"{BASE_URL}/login",
            data=payload,
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()

        # A successful login redirects away from /login.
        if _is_login_url(resp.url):
            raise LoginError(
                "Login failed — check your AUDIOVAULT_EMAIL and AUDIOVAULT_PASSWORD."
            )

        logger.info("Logged in to AudioVault successfully.")

    def _relogin(self) -> None:
        """Re-create the session and log in again after a session expiry."""
        logger.info("Session expired — re-logging in to AudioVault.")
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._login()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_shows(self, title: str) -> list[dict]:
        """Return a list of {'name': ..., 'url': ...} for matching TV seasons."""
        return self._search("/shows", title)

    def search_movies(self, title: str) -> list[dict]:
        """Return a list of {'name': ..., 'url': ...} for matching movies."""
        return self._search("/movies", title)

    def _search(self, path: str, query: str) -> list[dict]:
        results = self._search_once(path, query)
        if results:
            return results
        normalized = _normalize_search_query(query)
        if normalized and normalized != query:
            logger.info(
                "No results for %r — retrying with punctuation stripped: %r",
                query,
                normalized,
            )
            return self._search_once(path, normalized)
        return results

    def _search_once(self, path: str, query: str) -> list[dict]:
        resp = self._session.get(
            f"{BASE_URL}{path}",
            params={"search": query},
            timeout=30,
        )
        resp.raise_for_status()
        if _is_login_url(resp.url):
            self._relogin()
            resp = self._session.get(
                f"{BASE_URL}{path}",
                params={"search": query},
                timeout=30,
            )
            resp.raise_for_status()
            if _is_login_url(resp.url):
                logger.error(
                    "AudioVault search failed after re-login — credentials may be invalid."
                )
                return []
        return _parse_results_table(resp.text)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download(self, url: str, dest_dir: Path) -> Path:
        """
        Download *url* into *dest_dir* and return the saved file path.

        The filename is taken from the Content-Disposition header when
        present, falling back to the last URL path segment.

        Writes to a sibling ``<name>.part`` and ``os.replace``s into place
        on success, so a SIGKILL or crash mid-download never leaves a
        truncated file masquerading as a valid cache entry.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        resp = self._get_with_relogin(url, stream=True, timeout=120)

        with resp:
            resp.raise_for_status()

            # Reject HTML responses (200 OK login/error pages from AudioVault)
            # so they never get cached as ``.mp3``/``.zip``. The CDN
            # occasionally serves text/plain JSON-ish error pages too, so
            # we positively require audio/* or application/{zip,octet-stream}.
            content_type = resp.headers.get("Content-Type", "").lower().split(";", 1)[0].strip()
            if content_type and not _is_acceptable_media_type(content_type):
                snippet = ""
                try:
                    snippet = next(resp.iter_content(chunk_size=256), b"").decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"AudioVault returned unexpected content-type {content_type!r} for {url}"
                    + (f" — preview: {snippet[:120]!r}" if snippet else "")
                )

            content_disp = resp.headers.get("Content-Disposition", "")
            match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', content_disp)
            if match:
                filename = match.group(1).strip()
            else:
                filename = url.rstrip("/").split("/")[-1]

            # Defeat path traversal: `Path(...).name` drops every parent
            # component, so a malicious Content-Disposition like
            # `filename=../../etc/passwd` becomes `passwd`. Doing this BEFORE
            # the character sanitiser also strips Windows drive prefixes.
            filename = Path(filename).name
            # Sanitise the filename so it is safe for all filesystems.
            filename = re.sub(r'[\\/:*?"<>|]', "_", filename)
            if not filename:
                filename = "audiovault_download.bin"
            dest = dest_dir / filename
            tmp = dest.with_suffix(dest.suffix + ".part")

            try:
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65_536):
                        fh.write(chunk)
                os.replace(tmp, dest)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise

        logger.info("Downloaded: %s", dest)
        return dest

    def _get_with_relogin(self, url: str, **kwargs) -> requests.Response:
        """GET *url* with one transparent re-login if the session has expired.

        AudioVault redirects expired sessions to ``/login``; without this,
        ``download`` would silently stream the login page HTML into the
        cache file. If the second GET *also* lands on ``/login`` (e.g.
        permanent auth failure) we raise rather than returning that
        response, so the caller never writes the login page to disk
        thinking it's audio.
        """
        resp = self._session.get(url, **kwargs)
        if _is_login_url(resp.url):
            resp.close()
            self._relogin()
            resp = self._session.get(url, **kwargs)
            if _is_login_url(resp.url):
                resp.close()
                raise LoginError(
                    f"AudioVault still redirecting to /login after relogin for {url}"
                )
        return resp


# ------------------------------------------------------------------
# HTML parsing helpers
# ------------------------------------------------------------------

def _parse_results_table(html: str) -> list[dict]:
    """
    Parse the search-results table (ID | Name | Download) from a shows/movies page.
    Returns a list of dicts with 'name' and 'url' keys.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    results: list[dict] = []
    for row in table.find_all("tr")[1:]:  # skip the header row
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        name = cells[1].get_text(strip=True)
        link = cells[2].find("a", href=True)
        if not link:
            continue

        href: str = link["href"]
        if not href.startswith("http"):
            href = BASE_URL + href

        results.append({"name": name, "url": href})

    return results
