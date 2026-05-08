"""
Wrapper around the describealaign CLI.

describealaign is invoked as a subprocess so that:
  - its own stdout/stderr are captured and logged,
  - import-time side-effects (wxPython GUI init, etc.) don't affect us.

The alignment score is read from the .txt report that describealaign writes
alongside its PNG plot in alignment_dir.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# describealaign prefixes output filenames with this by default.
OUTPUT_PREFIX = "ad_"

# Matches rate-change lines in describealaign .txt reports, e.g.:
#   Rate change of  10253.9% from  0:15:20.876 to  0:15:21.467 ...
_SEG_RE = re.compile(
    r"Rate change of\s+([-\d.]+)%\s+from\s+([\d:]+\.\d+)\s+to\s+([\d:]+\.\d+)"
)

_MEDIAN_RE = re.compile(r"Median Rate Change:\s+([-\d.]+)%")

# Headline-line emitted by describealaign ≥2.1.1. When present we read it
# directly instead of re-deriving from per-segment rates.
_TRUNK_RE = re.compile(r"Stable Trunk Fraction:\s+([-\d.]+)%")

# Fallback per-segment tolerance for re-deriving stable fraction from
# pre-2.1.1 reports. Matches the constant inside describealaign so the two
# implementations stay in lockstep.
_STABLE_RATE_TOLERANCE_PP = 0.3


def _read_metrics(report: Optional[Path]) -> Optional[dict]:
    """
    Load alignment metrics, preferring the JSON sibling (describealaign
    ≥3.1.0) and falling back to regex-parsing the .txt report for older
    output. Returns a dict with normalised keys regardless of source format,
    or None if the report path is missing/unreadable.

    Keys returned (any may be missing if absent from the source):
      similarity_pct, median_rate_pct, stable_trunk_fraction_pct,
      segments: [{rate_pct, video_start_sec, video_end_sec, ...}, ...]
    """
    if report is None:
        return None

    json_path = report.with_suffix(".json")
    if json_path.exists():
        try:
            return json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read JSON sibling %s, falling back to text: %s",
                json_path.name, exc,
            )

    try:
        content = report.read_text(errors="replace")
    except OSError:
        return None

    metrics: dict = {}
    m = re.search(
        r"(?:similarity|match)[^\d]*(\d+(?:\.\d+)?)\s*%",
        content, re.IGNORECASE,
    )
    if m:
        metrics["similarity_pct"] = float(m.group(1))
    m = _MEDIAN_RE.search(content)
    if m:
        metrics["median_rate_pct"] = float(m.group(1))
    m = _TRUNK_RE.search(content)
    if m:
        metrics["stable_trunk_fraction_pct"] = float(m.group(1))

    segments: list = []
    for sm in _SEG_RE.finditer(content):
        rate = float(sm.group(1))
        v_start = _parse_tc(sm.group(2))
        v_end = _parse_tc(sm.group(3))
        segments.append({
            "rate_pct": rate,
            "video_start_sec": v_start,
            "video_end_sec": v_end,
        })
    metrics["segments"] = segments
    return metrics


class AlignResult:
    """Outputs of a describealaign run."""

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
    Run describealaign on *video_path* + *audio_path*.

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
        sys.executable, "-m", "describealaign",
        str(video_path),
        str(audio_path),
        "--yes",
        "--output_dir", str(output_dir),
        "--alignment_dir", str(alignment_dir),
    ]
    if stretch_audio:
        cmd.append("--stretch_audio")

    logger.info("Running describealaign: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1-hour hard cap
        )
    except subprocess.TimeoutExpired:
        logger.error("describealaign timed out after 1 hour.")
        return None
    except FileNotFoundError:
        logger.error(
            "describealaign not found. Install it with: pip install describealaign"
        )
        return None

    if result.stdout:
        for line in result.stdout.splitlines():
            logger.debug("[describealaign] %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            logger.debug("[describealaign stderr] %s", line)

    if result.returncode != 0:
        logger.error("describealaign exited with code %d.", result.returncode)
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
    """Return the most relevant describealaign .txt report for *video_path*.

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


def _segment_duration(seg: dict) -> float:
    """Duration in seconds for a normalised segment dict."""
    return seg["video_end_sec"] - seg["video_start_sec"]


def parse_score(report: Optional[Path]) -> float:
    """
    Return the describealaign similarity score (0–100), or 0.0 if missing.
    """
    metrics = _read_metrics(report)
    if metrics is None:
        logger.warning("No describealaign report to parse.")
        return 0.0
    if "similarity_pct" not in metrics:
        logger.warning("Could not find similarity in %s.", report.name if report else "?")
        return 0.0
    score = float(metrics["similarity_pct"])
    logger.info("Alignment score: %.1f%%", score)
    return score


def content_score(report: Optional[Path]) -> float:
    """
    Content-coverage score (0–100): percentage of runtime *not* identified
    as a commercial-break seam artifact (|rate| > 500% AND duration < 5 s).

    Returns 0.0 if the report is missing or contains no segment data.
    """
    metrics = _read_metrics(report)
    if metrics is None:
        return 0.0

    total_dur = 0.0
    stable_dur = 0.0
    for seg in metrics.get("segments", []):
        dur = _segment_duration(seg)
        if dur <= 0:
            continue
        total_dur += dur
        if not (abs(seg["rate_pct"]) > 500.0 and dur < 5.0):
            stable_dur += dur

    if total_dur == 0.0:
        return 0.0

    score = (stable_dur / total_dur) * 100.0
    logger.info("Content coverage score: %.1f%%", score)
    return score


def slope_stability(report: Optional[Path]) -> tuple[float, float, float]:
    """
    Summarise the structural stability of an alignment report.

    Returns ``(median_rate_pct, stable_fraction_pct, total_runtime_sec)``.

    The stable-trunk fraction is read directly from the JSON sibling (or
    the ``Stable Trunk Fraction`` text line for older reports). When neither
    is present, it's re-derived locally from the per-segment rates using the
    same tolerance describealaign uses internally so both paths agree.
    """
    metrics = _read_metrics(report)
    if metrics is None:
        return 0.0, 0.0, 0.0

    median_rate = float(metrics.get("median_rate_pct", 0.0))

    total_dur = 0.0
    stable_dur = 0.0
    for seg in metrics.get("segments", []):
        dur = _segment_duration(seg)
        if dur <= 0:
            continue
        total_dur += dur
        if abs(seg["rate_pct"] - median_rate) <= _STABLE_RATE_TOLERANCE_PP:
            stable_dur += dur

    if "stable_trunk_fraction_pct" in metrics:
        fraction = float(metrics["stable_trunk_fraction_pct"])
    elif total_dur > 0.0:
        fraction = (stable_dur / total_dur) * 100.0
    else:
        fraction = 0.0

    return median_rate, fraction, total_dur


def sync_quality(report: Optional[Path]) -> tuple[bool, str]:
    """
    Return (ok, reason) where ok=False means the alignment is likely unreliable.

    Clean alignments — including ones with many commercial-break seams —
    have a tight cluster of segments around the median rate. The check
    requires the stable trunk to dominate the runtime AND have a small
    internal rate variance.
    """
    metrics = _read_metrics(report)
    if metrics is None:
        return True, ""

    median_rate = float(metrics.get("median_rate_pct", 0.0))
    stable: list[tuple[float, float]] = []
    total_dur = 0.0
    for seg in metrics.get("segments", []):
        dur = _segment_duration(seg)
        if dur <= 0:
            continue
        total_dur += dur
        if abs(seg["rate_pct"] - median_rate) <= _STABLE_RATE_TOLERANCE_PP:
            stable.append((seg["rate_pct"], dur))

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
    """Locate the combined file that describealaign created in output_dir."""
    stem = video_path.stem
    suffix = video_path.suffix

    # Expected name: ad_{original_stem}{original_ext}
    expected = output_dir / f"{OUTPUT_PREFIX}{stem}{suffix}"
    if expected.exists():
        return expected

    # describealaign may choose a slightly different extension; scan the dir.
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

    logger.error("No output file found in %s after describealaign run.", output_dir)
    return None
