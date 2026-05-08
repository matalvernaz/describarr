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
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# describealign prefixes output filenames with this by default.
OUTPUT_PREFIX = "ad_"

# Matches rate-change lines in describealign .txt reports, e.g.:
#   Rate change of  10253.9% from  0:15:20.876 to  0:15:21.467 ...
_SEG_RE = re.compile(
    r"Rate change of\s+([-\d.]+)%\s+from\s+([\d:]+\.\d+)\s+to\s+([\d:]+\.\d+)"
)

_MEDIAN_RE = re.compile(r"Median Rate Change:\s+([-\d.]+)%")

# Per-segment rate must sit within this many percentage points of the
# report's median rate to count as part of the stable trunk. PAL/NTSC drift
# (4.27%) shows up identically across every stable segment in practice;
# 0.3 pp leaves headroom for the rounding in describealign's report.
_STABLE_RATE_TOLERANCE_PP = 0.3


class AlignResult:
    """Outputs of a describealign run."""

    __slots__ = ("output", "report")

    def __init__(self, output: Path, report: Optional[Path]) -> None:
        self.output = output
        self.report = report


def run(
    video_path: Path,
    audio_path: Path,
    output_dir: Path,
    alignment_dir: Path,
    stretch_audio: bool = True,
) -> Optional[AlignResult]:
    """
    Run describealign on *video_path* + *audio_path*.

    Returns an :class:`AlignResult` with the combined output and report paths,
    or ``None`` if the run failed.  Both paths are filtered to files created
    during this run, so a stale leftover from an earlier run for a different
    video can never be returned.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    alignment_dir.mkdir(parents=True, exist_ok=True)

    # Record the wall-clock time just before we launch the subprocess so that
    # _find_output can reject any files that pre-date this run (stale outputs
    # left over from a previous failed run).
    run_start = time.time()

    cmd = [
        sys.executable, "-m", "describealign",
        str(video_path),
        str(audio_path),
        "--yes",
        "--output_dir", str(output_dir),
        "--alignment_dir", str(alignment_dir),
    ]
    if stretch_audio:
        cmd.append("--stretch_audio")

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

    output = _find_output(video_path, output_dir, run_start)
    if output is None:
        return None
    report = _find_report(video_path, alignment_dir, min_mtime=run_start)
    return AlignResult(output=output, report=report)


def _find_report(
    video_path: Path,
    alignment_dir: Path,
    min_mtime: float = 0.0,
) -> Optional[Path]:
    """Return the most relevant describealign .txt report for *video_path*.

    *min_mtime* filters out stale reports left behind by earlier runs against
    other videos — important when the same alignment_dir is reused across
    every run (which is the default).
    """
    candidates = [
        p for p in alignment_dir.glob("*.txt")
        if p.stat().st_mtime >= min_mtime
    ]
    if not candidates:
        return None
    stem = video_path.stem.lower()
    # Prefer a file whose name contains the video stem; break ties by mtime.
    candidates.sort(key=lambda p: (stem not in p.name.lower(), -p.stat().st_mtime))
    return candidates[0]


def parse_score(report: Optional[Path]) -> float:
    """
    Parse the similarity score from a describealign text report.

    describealign writes a .txt report for each alignment run.  The file
    contains a line such as::

        Input file similarity: 78%

    Returns the score as a float (0–100), or 0.0 if it cannot be found.
    """
    if report is None:
        logger.warning("No describealign report to parse.")
        return 0.0

    content = report.read_text(errors="replace")
    match = re.search(
        r"(?:similarity|match)[^\d]*(\d+(?:\.\d+)?)\s*%",
        content,
        re.IGNORECASE,
    )
    if match:
        score = float(match.group(1))
        logger.info("Alignment score: %.1f%% (from %s)", score, report.name)
        return score

    logger.warning("Could not parse alignment score from %s.", report.name)
    return 0.0


def content_score(report: Optional[Path]) -> float:
    """
    Compute a content-coverage score (0–100) from a describealign report.

    Segments where |rate| > 500% and duration < 5 s are classified as
    commercial-break seam artifacts and excluded from the denominator.
    The returned value is the percentage of total video runtime covered by
    the remaining stable, well-aligned segments.

    Returns 0.0 if the report is missing or contains no segment data.
    """
    if report is None:
        return 0.0

    content = report.read_text(errors="replace")
    total_dur = 0.0
    stable_dur = 0.0

    for m in _SEG_RE.finditer(content):
        rate = float(m.group(1))
        dur = _parse_tc(m.group(3)) - _parse_tc(m.group(2))
        if dur <= 0:
            continue
        total_dur += dur
        if not (abs(rate) > 500.0 and dur < 5.0):
            stable_dur += dur

    if total_dur == 0.0:
        return 0.0

    score = (stable_dur / total_dur) * 100.0
    logger.info("Content coverage score: %.1f%% (from %s)", score, report.name)
    return score


def slope_stability(report: Optional[Path]) -> tuple[float, float, float]:
    """
    Summarise the structural stability of an alignment report.

    Returns ``(median_rate_pct, stable_fraction_pct, total_runtime_sec)``:

    * ``median_rate_pct`` — the report's headline ``Median Rate Change`` line,
      0.0 if missing. For PAL→NTSC sources this is exactly ~4.27.
    * ``stable_fraction_pct`` — what percentage of the total covered runtime
      sits in segments whose rate is within ``_STABLE_RATE_TOLERANCE_PP`` of
      the median. A clean PAL/NTSC alignment scores ~99 here even when the
      describealign similarity score lands in the 60s, because the seam
      artifacts between stable trunks are individually tiny.
    * ``total_runtime_sec`` — sum of all segment durations.

    All three numbers are 0 if the report is missing or contains no segments.
    """
    if report is None:
        return 0.0, 0.0, 0.0

    content = report.read_text(errors="replace")

    median_match = _MEDIAN_RE.search(content)
    median_rate = float(median_match.group(1)) if median_match else 0.0

    total_dur = 0.0
    stable_dur = 0.0

    for m in _SEG_RE.finditer(content):
        rate = float(m.group(1))
        dur = _parse_tc(m.group(3)) - _parse_tc(m.group(2))
        if dur <= 0:
            continue
        total_dur += dur
        if abs(rate - median_rate) <= _STABLE_RATE_TOLERANCE_PP:
            stable_dur += dur

    if total_dur == 0.0:
        return median_rate, 0.0, 0.0

    fraction = (stable_dur / total_dur) * 100.0
    return median_rate, fraction, total_dur


def sync_quality(report: Optional[Path]) -> tuple[bool, str]:
    """
    Return (ok, reason) where ok=False means the alignment is likely unreliable.

    Clean alignments — including ones with many commercial-break seams —
    have a tight cluster of segments around the median rate, with the rest
    being short artifact spikes. The check is therefore: collect the stable
    trunk (segments within tolerance of the median), require it to dominate
    the runtime, and require its internal rate variance to be small.

    Seam-count alone is not informative: a broadcast-source AD against a
    streaming video legitimately produces 5–10 seam jumps per episode.
    """
    if report is None:
        return True, ""

    content = report.read_text(errors="replace")

    median_match = _MEDIAN_RE.search(content)
    median_rate = float(median_match.group(1)) if median_match else 0.0

    stable: list[tuple[float, float]] = []
    total_dur = 0.0
    for m in _SEG_RE.finditer(content):
        rate = float(m.group(1))
        dur = _parse_tc(m.group(3)) - _parse_tc(m.group(2))
        if dur <= 0:
            continue
        total_dur += dur
        if abs(rate - median_rate) <= _STABLE_RATE_TOLERANCE_PP:
            stable.append((rate, dur))

    if not stable or total_dur == 0.0:
        return True, ""

    stable_dur = sum(dur for _, dur in stable)
    stable_fraction = (stable_dur / total_dur) * 100.0
    weighted_mean = sum(rate * dur for rate, dur in stable) / stable_dur
    variance = sum(dur * (rate - weighted_mean) ** 2 for rate, dur in stable) / stable_dur
    rate_std = variance ** 0.5

    problems: list[str] = []
    if stable_fraction < 80.0:
        problems.append(
            f"only {stable_fraction:.1f}% of runtime is in the stable trunk "
            f"(expected ≥80%)"
        )
    if rate_std > 0.5:
        problems.append(
            f"stable-trunk rate std dev {rate_std:.2f}pp "
            f"(expected ≤0.5pp for a consistent drift)"
        )

    if problems:
        return False, "; ".join(problems)
    return True, ""


def _parse_tc(tc: str) -> float:
    """Convert a H:MM:SS.fff or MM:SS.fff timecode string to seconds."""
    parts = tc.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _find_output(video_path: Path, output_dir: Path, min_mtime: float = 0.0) -> Optional[Path]:
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

    # Last resort: newest file in output_dir that was created during this run.
    # Filtering by min_mtime prevents returning a stale file left over from a
    # previous run when the current run produced no output.
    files = [
        f for f in output_dir.iterdir()
        if f.is_file() and f.stat().st_mtime >= min_mtime
    ]
    if files:
        newest = max(files, key=lambda f: f.stat().st_mtime)
        logger.warning("Using newest output file as fallback: %s", newest.name)
        return newest

    logger.error("No output file found in %s after describealign run.", output_dir)
    return None
