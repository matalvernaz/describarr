"""
Pushover notifier for describarr.

Reads PUSHOVER_TOKEN and PUSHOVER_USER from the environment. When unset, send()
is a no-op so the server still works for users who haven't configured Pushover.
"""

from __future__ import annotations

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
