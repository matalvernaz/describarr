"""
ntfy notifier for describarr.

Reads NTFY_URL (base server URL, e.g. http://ntfy:2586) and NTFY_TOPIC from the
environment; NTFY_TOKEN is optional bearer auth. When NTFY_URL or NTFY_TOPIC is
unset, send() is a no-op so the server still works for users who haven't
configured notifications.

Publishing uses ntfy's JSON endpoint rather than the header-based API so that
unicode titles and messages (media names with accents, CJK, etc.) survive — the
HTTP Title header is ASCII-only, but a JSON body is not.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SEC = 10


def _config() -> tuple[str, str, str] | None:
    base = os.environ.get("NTFY_URL", "").strip().rstrip("/")
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not base or not topic:
        return None
    token = os.environ.get("NTFY_TOKEN", "").strip()
    return base, topic, token


def send(title: str, message: str) -> None:
    """Fire-and-forget ntfy notification. Logs and swallows any failure."""
    config = _config()
    if config is None:
        return
    base, topic, token = config
    body = json.dumps({
        "topic": topic,
        "title": title,
        "message": message,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(base, data=body, headers=headers), timeout=_REQUEST_TIMEOUT_SEC) as resp:
            if resp.status >= 400:
                logger.warning("ntfy returned HTTP %d", resp.status)
    except Exception:
        logger.warning("ntfy notification failed.", exc_info=True)
