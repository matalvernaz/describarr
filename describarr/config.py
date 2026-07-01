"""Configuration loaded from a .env file or environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Search for .env in the current directory, then ~/.config/describarr/.env
_CONFIG_PATHS = [
    Path.cwd() / ".env",
    Path.home() / ".config" / "describarr" / ".env",
]

for _path in _CONFIG_PATHS:
    if _path.exists():
        load_dotenv(_path)
        break


_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on", "t"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off", "f", ""})


def _parse_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var with the same vocabulary in both directions.

    Previously each boolean had its own ad-hoc parsing rule
    (``DESCRIBARR_STRETCH_AUDIO`` treated anything but literal ``"false"`` as
    True, but ``DESCRIBARR_ALLOW_VIDEO_RETIME`` required literal ``"true"``).
    A Docker-Compose user setting ``DESCRIBARR_STRETCH_AUDIO: "0"`` got the
    opposite of what they asked for. One helper, one vocabulary, predictable
    behaviour either way.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be a boolean (1/0, true/false, yes/no, on/off); got {raw!r}"
    )


@dataclass
class Config:
    email: str
    password: str
    min_score: float = 65.0
    cache_dir: Path = field(
        default_factory=lambda: Path.home() / ".cache" / "describarr"
    )
    stretch_audio: bool = True
    # When stretch_audio is False, describealaign rewrites video PTS/DTS via
    # an ffmpeg setts bitstream filter. The output is structurally valid but
    # can break HRD buffering on hardware decoders (Apple TV, webOS, …).
    # Describarr refuses to run that path for an in-place library mutation
    # unless this flag is explicitly opted in.
    allow_video_retime: bool = False
    # In-place replacement is destructive. Before overwriting the original we
    # hardlink it into a backup location so a mis-accepted alignment is always
    # recoverable. A hardlink is instant and costs no extra space until the
    # original content has no other links. backup_dir=None ⇒ a
    # ".describarr_backup" dir beside each file (guaranteed same filesystem,
    # so the hardlink can't fail cross-device). Set DESCRIBARR_BACKUP_DIR to
    # collect every backup in one place — it must be on the same filesystem as
    # the media or the code falls back to the per-file sibling dir.
    backup_originals: bool = True
    backup_dir: Optional[Path] = None
    backup_retention_days: int = 14
    # Optional shared secret. When set, mutating HTTP endpoints (/hook, /retry,
    # /drain, DELETE /queue) require a matching X-Api-Key header. Unset ⇒ open,
    # relying on docker-network isolation (the historical behaviour).
    api_key: Optional[str] = None
    # How many recent accept/reject/skip decisions to retain for /status. The
    # log is a screen-reader-friendly audit trail replacing container-log
    # grepping; it is capped so it can't grow without bound.
    history_size: int = 50

    @classmethod
    def from_env(cls) -> "Config":
        email = os.environ.get("AUDIOVAULT_EMAIL", "").strip()
        password = os.environ.get("AUDIOVAULT_PASSWORD", "").strip()

        if not email or not password:
            raise ValueError(
                "AUDIOVAULT_EMAIL and AUDIOVAULT_PASSWORD must be set. "
                "Copy .env.example to ~/.config/describarr/.env and fill in your credentials."
            )

        min_score = float(os.environ.get("DESCRIBARR_MIN_SCORE", "65"))
        if not 0.0 <= min_score <= 100.0:
            raise ValueError(
                f"DESCRIBARR_MIN_SCORE must be between 0 and 100, got {min_score!r}"
            )

        raw_cache = os.environ.get("DESCRIBARR_CACHE_DIR", "")
        cache_dir = Path(raw_cache).expanduser() if raw_cache else Path.home() / ".cache" / "describarr"

        stretch_audio = _parse_bool("DESCRIBARR_STRETCH_AUDIO", default=True)
        allow_video_retime = _parse_bool("DESCRIBARR_ALLOW_VIDEO_RETIME", default=False)

        backup_originals = _parse_bool("DESCRIBARR_BACKUP_ORIGINALS", default=True)
        raw_backup_dir = os.environ.get("DESCRIBARR_BACKUP_DIR", "").strip()
        backup_dir = Path(raw_backup_dir).expanduser() if raw_backup_dir else None
        raw_retention = os.environ.get("DESCRIBARR_BACKUP_RETENTION_DAYS", "14").strip()
        try:
            backup_retention_days = int(raw_retention)
        except ValueError:
            raise ValueError(
                f"DESCRIBARR_BACKUP_RETENTION_DAYS must be an integer; got {raw_retention!r}"
            )
        if backup_retention_days < 0:
            raise ValueError("DESCRIBARR_BACKUP_RETENTION_DAYS must be ≥ 0.")

        api_key = os.environ.get("DESCRIBARR_API_KEY", "").strip() or None

        raw_history = os.environ.get("DESCRIBARR_HISTORY_SIZE", "50").strip()
        try:
            history_size = int(raw_history)
        except ValueError:
            raise ValueError(
                f"DESCRIBARR_HISTORY_SIZE must be an integer; got {raw_history!r}"
            )
        if history_size < 0:
            raise ValueError("DESCRIBARR_HISTORY_SIZE must be ≥ 0.")

        return cls(
            email=email,
            password=password,
            min_score=min_score,
            cache_dir=cache_dir,
            stretch_audio=stretch_audio,
            allow_video_retime=allow_video_retime,
            backup_originals=backup_originals,
            backup_dir=backup_dir,
            backup_retention_days=backup_retention_days,
            api_key=api_key,
            history_size=history_size,
        )
