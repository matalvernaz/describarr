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
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_API_URL = "https://api.pushover.net/1/messages.json"

# Pushover's limits on a title and a message, in characters.
TITLE_LIMIT = 250
MESSAGE_LIMIT = 1024


def _creds() -> tuple[str, str] | None:
    token = os.environ.get("PUSHOVER_TOKEN", "").strip()
    user = os.environ.get("PUSHOVER_USER", "").strip()
    if not token or not user:
        return None
    return token, user


def _deliver(request: Request, service: str) -> bool:
    """Post *request*, logging and swallowing any failure. False only when
    trying again later could succeed: no answer, a server error, or being
    told to slow down. Anything else refused is refused for good."""
    try:
        with urlopen(request, timeout=10) as resp:
            if resp.status >= 400:
                logger.warning("%s returned HTTP %d", service, resp.status)
    except HTTPError as exc:
        logger.warning("%s refused the notification: HTTP %d.", service, exc.code)
        return exc.code < 500 and exc.code != 429
    except Exception:
        logger.warning("%s notification failed.", service, exc_info=True)
        return False
    return True


def send(title: str, message: str) -> bool:
    """Fire-and-forget Pushover notification. Logs and swallows any failure;
    False when it is worth trying again (see `_deliver`)."""
    creds = _creds()
    if creds is None:
        return True
    token, user = creds
    data = urlencode({
        "token": token,
        "user": user,
        "title": title[:TITLE_LIMIT],
        "message": message[:MESSAGE_LIMIT],
    }).encode()
    return _deliver(Request(_API_URL, data=data), "Pushover")


def _hub() -> tuple[str, str] | None:
    url = os.environ.get("NOTIFY_HUB_URL", "").strip().rstrip("/")
    token = os.environ.get("NOTIFY_HUB_TOKEN", "").strip()
    if not url or not token:
        return None
    return url, token


def send_hub(category: str, title: str, message: str, click: str | None = None) -> bool:
    """Fire-and-forget post to the notifications hub. Logs and swallows any
    failure; False when it is worth trying again (see `_deliver`)."""
    hub = _hub()
    if hub is None:
        return True
    url, token = hub
    data = json.dumps({"topic": category, "title": title, "message": message, "click": click}).encode()
    request = Request(f"{url}/", data=data, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "User-Agent": "describarr",
    })
    return _deliver(request, "Notifications hub")

