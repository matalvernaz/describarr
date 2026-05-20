"""
Entry point for describarr.

Usage:
    describarr serve [PORT]    # webhook server (default mode for Docker)
    describarr --test-auth     # verify AudioVault credentials and exit
    describarr                 # one-shot, driven by Sonarr/Radarr env vars

Sonarr/Radarr call this script automatically via their Custom Script connection
when running in bare-metal (non-server) mode.
"""

from __future__ import annotations

import logging
import os
import sys

from .audiovault import AudioVaultClient, LoginError
from .config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("describarr")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        from .server import serve
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8686
        serve(port)
        return

    if "--test-auth" in sys.argv:
        _test_auth()
        return

    # One-shot mode: process the event currently in the environment, then exit.
    # Re-uses the same dispatcher the server uses so the two modes can't drift.
    # Also opportunistically drain any items that were queued by an earlier
    # invocation hitting the AudioVault daily limit — bare-metal mode has no
    # background scheduler to do this otherwise.
    from .audiovault import DailyLimitReached
    from .retry_queue import RetryQueue
    from .server import _dispatch, _get_client
    from .workflow import drain_retry_queue

    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    try:
        client = _get_client(config)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        sys.exit(1)

    try:
        drain_retry_queue(RetryQueue(config.cache_dir / "retry_queue.json"), client, config)
    except DailyLimitReached:
        pass  # remaining items stay queued; we'll keep going for this event.

    env = dict(os.environ)
    # ``_dispatch`` returns:
    #   - a dict (described / no_match / queued) — the event ran to completion
    #   - None for Sonarr/Radarr ``Test`` events AND for unrecognised events
    # A Test event is a *success* from Sonarr's perspective (it expects the
    # script to exit 0 to confirm wiring), so we distinguish those by
    # inspecting the env directly. Everything else preserves the old behaviour.
    sonarr_event = env.get("sonarr_eventtype", "").lower()
    radarr_event = env.get("radarr_eventtype", "").lower()
    is_test = sonarr_event == "test" or radarr_event == "test"
    outcome = _dispatch(env)
    if outcome is not None:
        sys.exit(0)
    sys.exit(0 if is_test else 1)


def _test_auth() -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("Testing AudioVault credentials…")
    try:
        AudioVaultClient(config.email, config.password)
        logger.info("Login successful.")
    except LoginError as exc:
        logger.error("Login failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
