"""
Notifiers for describarr: Pushover for the operator, and a notifications hub for
everyone who asked to hear about new descriptions.

Reads PUSHOVER_TOKEN and PUSHOVER_USER from the environment. When unset, send()
is a no-op so the server still works for users who haven't configured Pushover.
The hub works the same way with NOTIFY_HUB_URL and NOTIFY_HUB_TOKEN: it takes
ntfy's JSON form (topic, title, message, click), so an ntfy server fits there too.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_API_URL = "https://api.pushover.net/1/messages.json"


def _creds() -> tuple[str, str] | None:
    token = os.environ.get("PUSHOVER_TOKEN", "").strip()
    user = os.environ.get("PUSHOVER_USER", "").strip()
    if not token or not user:
        return None
    return token, user


def send(title: str, message: str) -> None:
    """Fire-and-forget Pushover notification. Logs and swallows any failure."""
    creds = _creds()
    if creds is None:
        return
    token, user = creds
    data = urlencode({
        "token": token,
        "user": user,
        "title": title,
        "message": message,
    }).encode()
    try:
        with urlopen(Request(_API_URL, data=data), timeout=10) as resp:
            if resp.status >= 400:
                logger.warning("Pushover returned HTTP %d", resp.status)
    except Exception:
        logger.warning("Pushover notification failed.", exc_info=True)


def _hub() -> tuple[str, str] | None:
    url = os.environ.get("NOTIFY_HUB_URL", "").strip().rstrip("/")
    token = os.environ.get("NOTIFY_HUB_TOKEN", "").strip()
    if not url or not token:
        return None
    return url, token


def send_hub(category: str, title: str, message: str, click: str | None = None) -> None:
    """Fire-and-forget post to the notifications hub. Logs and swallows any failure."""
    hub = _hub()
    if hub is None:
        return
    url, token = hub
    data = json.dumps({"topic": category, "title": title, "message": message, "click": click}).encode()
    request = Request(f"{url}/", data=data, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "User-Agent": "describarr",
    })
    try:
        with urlopen(request, timeout=10) as resp:
            if resp.status >= 400:
                logger.warning("Notifications hub returned HTTP %d", resp.status)
    except Exception:
        logger.warning("Notifications hub post failed.", exc_info=True)

