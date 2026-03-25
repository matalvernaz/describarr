"""
Wrapper around the describealign CLI.

describealign is invoked as a subprocess so that:
  - its own stdout/stderr are captured and logged,
  - import-time side-effects (wxPython GUI init, etc.) don't affect us.

The alignment score is read from the .txt report that describealign writes
alongside its PNG plot in alignment_dir.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# describealign prefixes output filenames with this by default.
OUTPUT_PREFIX = "ad_"


def run(
    video_path: Path,
    audio_path: Path,
    output_dir: Path,
    alignment_dir: Path,
) -> Optional[Path]:
    """
    Run describealign on *video_path* + *audio_path*.

    Returns the path of the combined output file, or None if the run failed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    alignment_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "describealign",
        str(video_path),
        str(audio_path),
        "--yes",
        "--output_dir", str(output_dir),
        "--alignment_dir", str(alignment_dir),
    ]

    logger.info("Running describealign: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1-hour hard cap
        )
    except subprocess.TimeoutExpired:
        logger.error("describealign timed out after 1 hour.")
        return None
    except FileNotFoundError:
        logger.error(
            "describealign not found. Install it with: pip install describealign"
        )
        return None

    if result.stdout:
        for line in result.stdout.splitlines():
            logger.debug("[describealign] %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            logger.debug("[describealign stderr] %s", line)

    if result.returncode != 0:
        logger.error("describealign exited with code %d.", result.returncode)
        return None

    return _find_output(video_path, output_dir)


def parse_score(video_path: Path, alignment_dir: Path) -> float:
    """
    Parse the similarity score from the describealign text report.

    describealign writes a .txt report for each alignment run.  The file
    contains a line such as::

        Input file similarity: 78%

    Returns the score as a float (0–100), or 0.0 if it cannot be found.
    """
    # Prefer a report whose name contains the video stem; fall back to the
    # most recently modified .txt in the directory.
    candidates = sorted(
        alignment_dir.glob("*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    stem = video_path.stem.lower()
    candidates = sorted(
        candidates,
        key=lambda p: (stem not in p.name.lower(), -p.stat().st_mtime),
    )

    for txt_path in candidates:
        content = txt_path.read_text(errors="replace")
        match = re.search(
            r"(?:similarity|match)[^\d]*(\d+(?:\.\d+)?)\s*%",
            content,
            re.IGNORECASE,
        )
        if match:
            score = float(match.group(1))
            logger.info("Alignment score: %.1f%% (from %s)", score, txt_path.name)
            return score

    logger.warning("Could not parse alignment score from any report in %s.", alignment_dir)
    return 0.0


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _find_output(video_path: Path, output_dir: Path) -> Optional[Path]:
    """Locate the combined file that describealign created in output_dir."""
    stem = video_path.stem
    suffix = video_path.suffix

    # Expected name: ad_{original_stem}{original_ext}
    expected = output_dir / f"{OUTPUT_PREFIX}{stem}{suffix}"
    if expected.exists():
        return expected

    # describealign may choose a slightly different extension; scan the dir.
    for candidate in output_dir.glob(f"{OUTPUT_PREFIX}{stem}*"):
        if candidate.is_file():
            return candidate

    # Last resort: newest file in output_dir.
    files = [f for f in output_dir.iterdir() if f.is_file()]
    if files:
        newest = max(files, key=lambda f: f.stat().st_mtime)
        logger.warning("Using newest output file as fallback: %s", newest.name)
        return newest

    logger.error("No output file found in %s after describealign run.", output_dir)
    return None
