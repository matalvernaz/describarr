"""Configuration loaded from a .env file or environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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

        return cls(
            email=email,
            password=password,
            min_score=min_score,
            cache_dir=cache_dir,
            stretch_audio=stretch_audio,
            allow_video_retime=allow_video_retime,
        )
