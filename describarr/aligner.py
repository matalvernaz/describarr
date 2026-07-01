"""
Wrapper around the describealaign CLI.

describealaign is invoked as a subprocess in its own session so that:
  - its own stdout/stderr are captured and logged,
  - import-time side-effects (wxPython GUI init, etc.) don't affect us,
  - on timeout we can kill the *whole process group* and avoid orphaning
    the ffmpeg children that describealaign spawns.

Each run gets a fresh per-run output directory (``output_dir/run-<uuid>``)
so the alignment of one show can never see, or be confused by, the
in-flight or stale output of any other run.

The alignment score is read from the .txt report that describealaign writes
alongside its PNG plot in alignment_dir.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# describealaign prefixes output filenames with this by default.
OUTPUT_PREFIX = "ad_"

# Below this size we never even bother running ffprobe — the file is
# obviously truncated. The real validation lives in _validate_media_output.
_MIN_OUTPUT_BYTES = 1_000_000  # 1 MB

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

# Subprocess timeout: 1 hour is enough for everything Matt has thrown at
# describealaign so far (3-hour PAL BluRay films come in under 30 minutes).
_SUBPROCESS_TIMEOUT_SEC = 3600
# Grace window between SIGTERM and SIGKILL when we kill a runaway subprocess.
_SUBPROCESS_KILL_GRACE_SEC = 15


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
    """Outputs of a describealaign run.

    On success, *output* is the validated combined media and *report* the
    metrics sidecar. On failure, *output* is None and *failure_reason* carries
    a human-readable cause — from the engine's ``*.fail.json`` diagnosis when
    available, else a generic description of the failure mode — so the caller
    can tell a blind operator *why* nothing was produced instead of a bare
    "errored".
    """

    __slots__ = ("output", "report", "failure_reason")

    def __init__(
        self,
        output: Optional[Path],
        report: Optional[Path],
        failure_reason: Optional[str] = None,
    ) -> None:
        self.output = output
        self.report = report
        self.failure_reason = failure_reason


def run(
    video_path: Path,
    audio_path: Path,
    output_dir: Path,
    alignment_dir: Path,
    stretch_audio: bool = True,
) -> Optional[AlignResult]:
    """
    Run describealaign on *video_path* + *audio_path*.

    The combined output is written into a fresh subdirectory
    ``output_dir/run-<uuid>`` and validated before being returned, so a
    crashed previous run can never leak into this run's output discovery,
    and a structurally broken output can never be returned to the caller.

    Returns an :class:`AlignResult` with the combined output and report
    paths, or ``None`` if the run failed validation.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    alignment_dir.mkdir(parents=True, exist_ok=True)

    # Per-run scratch directory. Isolated from any other run so:
    #   - orphan ffmpeg from a previous timed-out run can't pollute discovery
    #   - we can clean up the whole tree at the end without touching peers
    run_id = uuid.uuid4().hex
    run_output_dir = output_dir / f"run-{run_id}"
    run_output_dir.mkdir(parents=True, exist_ok=True)

    # Record the wall-clock time just before we launch the subprocess so that
    # _find_output can reject any files that pre-date this run (defence in
    # depth — the per-run dir already guarantees no cross-run contamination).
    run_start = time.time()

    cmd = [
        sys.executable, "-m", "describealaign",
        str(video_path),
        str(audio_path),
        "--yes",
        "--output_dir", str(run_output_dir),
        "--alignment_dir", str(alignment_dir),
    ]
    if stretch_audio:
        cmd.append("--stretch_audio")

    logger.info("Running describealaign: %s", " ".join(cmd))

    try:
        returncode, stdout, stderr = _run_subprocess(cmd)
    except FileNotFoundError:
        logger.error(
            "describealaign not found. Install it with: pip install describealaign"
        )
        _cleanup_run_dir(run_output_dir)
        return AlignResult(None, None, "describealign is not installed")
    except subprocess.TimeoutExpired:
        logger.error(
            "describealaign timed out after %d seconds — process group killed.",
            _SUBPROCESS_TIMEOUT_SEC,
        )
        _cleanup_run_dir(run_output_dir)
        return AlignResult(
            None, None,
            f"alignment timed out after {_SUBPROCESS_TIMEOUT_SEC // 60} minutes",
        )

    if stdout:
        for line in stdout.splitlines():
            logger.debug("[describealaign] %s", line)
    if stderr:
        for line in stderr.splitlines():
            logger.debug("[describealaign stderr] %s", line)

    if returncode != 0:
        # describealaign ≥2.1.9 writes a <stem>.fail.json diagnosing *why* an
        # alignment was rejected (wrong/truncated episode, silent AD, …).
        # Surface it at ERROR (the raw stderr stays at DEBUG) and hand the
        # human summary back so the caller can tell the operator the cause.
        reason = _read_failure_sidecar(video_path, alignment_dir, run_start)
        if reason:
            logger.error("describealaign could not align %s: %s", video_path.name, reason)
        else:
            reason = f"alignment failed (describealaign exit {returncode})"
            logger.error("describealaign exited with code %d.", returncode)
        _cleanup_run_dir(run_output_dir)
        return AlignResult(None, None, reason)

    output = _find_output(video_path, run_output_dir, run_start)
    if output is None:
        _cleanup_run_dir(run_output_dir)
        return AlignResult(None, None, "describealign produced no output file")

    if not _validate_media_output(video_path, output):
        # The output is structurally broken — refuse to publish it.
        _cleanup_run_dir(run_output_dir)
        return AlignResult(None, None, "alignment output failed validation")

    report = _find_report(video_path, alignment_dir, min_mtime=run_start)
    # NOTE: caller is responsible for cleaning up `output` (and its parent
    # run dir) after copying. We can't blow away the run dir here without
    # making the caller copy first; that contract is documented in workflow.
    return AlignResult(output=output, report=report)


def _run_subprocess(cmd: list[str]) -> tuple[int, str, str]:
    """
    Run describealaign in its own session so a timeout can kill the *whole*
    process group (describealaign + every ffmpeg child it spawned).

    Returns ``(returncode, stdout, stderr)``. Raises
    :class:`subprocess.TimeoutExpired` if the overall budget is exceeded
    after the kill-grace window — the caller treats that as a failed run.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # new session => unique process group
    )
    try:
        stdout, stderr = proc.communicate(timeout=_SUBPROCESS_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        # SIGTERM the whole tree; if it hasn't exited after the grace window,
        # SIGKILL it. Either way reap the child so we don't leak a zombie.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=_SUBPROCESS_KILL_GRACE_SEC)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=_SUBPROCESS_KILL_GRACE_SEC)
            except subprocess.TimeoutExpired:
                # Truly stuck — proc.wait would block forever. Re-raise so
                # the caller reports a timeout (the kernel will eventually
                # reap the orphan).
                raise
        raise subprocess.TimeoutExpired(cmd, _SUBPROCESS_TIMEOUT_SEC, output=stdout, stderr=stderr)
    return proc.returncode, stdout or "", stderr or ""


def _cleanup_run_dir(run_output_dir: Path) -> None:
    """Best-effort recursive delete of a per-run scratch directory."""
    shutil.rmtree(run_output_dir, ignore_errors=True)


def _find_report(
    video_path: Path,
    alignment_dir: Path,
    min_mtime: float = 0.0,
) -> Optional[Path]:
    """Return the most relevant describealaign .txt report for *video_path*.

    *min_mtime* filters out stale reports left behind by earlier runs against
    other videos — important when the same alignment_dir is reused across
    every run (which is the default; alignment_dir is shared so the prune
    job can age it out by mtime).
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


def _read_failure_sidecar(
    video_path: Path,
    alignment_dir: Path,
    min_mtime: float = 0.0,
) -> Optional[str]:
    """Return the human ``summary`` from describealaign's ``<stem>.fail.json``
    for this run, or None if absent/unreadable.

    Written by describealaign ≥2.1.9 when an alignment is rejected as a
    mismatch. Mirrors :func:`_find_report`'s stem+mtime matching so a stale
    sidecar from an earlier run against a different file is ignored.
    """
    candidates = [
        p for p in alignment_dir.glob("*.fail.json")
        if p.stat().st_mtime >= min_mtime
    ]
    if not candidates:
        return None
    stem = video_path.stem.lower()
    candidates.sort(key=lambda p: (stem not in p.name.lower(), -p.stat().st_mtime))
    try:
        data = json.loads(candidates[0].read_text())
    except (json.JSONDecodeError, OSError):
        return None
    summary = data.get("summary") if isinstance(data, dict) else None
    return summary or None


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
# Output discovery + media validation
# ------------------------------------------------------------------

def _find_output(video_path: Path, run_output_dir: Path, min_mtime: float = 0.0) -> Optional[Path]:
    """Return the *exact* expected describealaign output, or None.

    No glob fallback, no "newest file in dir" guesswork. If the expected
    ``ad_<stem><ext>`` file doesn't exist (or is older than this run, or is
    obviously a truncated stub), we return None and the caller treats the
    run as a failure rather than publishing something arbitrary.
    """
    expected = run_output_dir / f"{OUTPUT_PREFIX}{video_path.stem}{video_path.suffix}"
    if not expected.exists():
        logger.error("Expected describealaign output missing: %s", expected)
        return None
    try:
        st = expected.stat()
    except OSError as exc:
        logger.error("Cannot stat expected output %s: %s", expected, exc)
        return None
    if st.st_mtime < min_mtime:
        logger.error(
            "Expected output %s is older than this run (mtime %.0f < %.0f) — refusing.",
            expected, st.st_mtime, min_mtime,
        )
        return None
    if st.st_size < _MIN_OUTPUT_BYTES:
        logger.error(
            "Output file %s is only %d bytes — describealaign likely crashed mid-write.",
            expected.name, st.st_size,
        )
        return None
    return expected


# Container/codec validation thresholds.
_PACKET_RATIO_FLOOR = 0.95          # max fractional packet loss tolerated
_MAX_MISSING_SECONDS_OF_VIDEO = 10  # absolute floor — never lose more than ~10 s of video
_FFPROBE_TIMEOUT_SEC = 120          # long enough for a packet-count scan on a 4 GB BluRay


def _ffprobe_json(path: Path, extra_args: Optional[list[str]] = None) -> Optional[dict]:
    """Run ffprobe on *path* and return the parsed JSON, or None on failure."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(path))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_FFPROBE_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("ffprobe failed on %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        logger.warning("ffprobe returned %d for %s: %s", proc.returncode, path, proc.stderr.strip())
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("ffprobe JSON parse failed for %s: %s", path, exc)
        return None


def _primary_video_stream(probe: dict) -> tuple[Optional[dict], int]:
    """Return (stream_dict, video_specifier_index) for the first non-cover-art
    video stream in *probe*.

    The video_specifier_index is the ffmpeg ``v:N`` index (i.e. position
    among video streams, NOT the absolute stream index in the container).
    Returns (None, -1) if no real video stream exists. Callers that
    select a stream via ffprobe ``-select_streams v:N`` MUST use this
    index — hardcoding ``v:0`` lets cover art at position 0 hijack the
    selection while ``_primary_video_stream`` correctly picks v:1.
    """
    video_specifier_index = 0
    for s in probe.get("streams", []):
        if s.get("codec_type") != "video":
            continue
        if s.get("codec_name") not in {"mjpeg", "png", "bmp", "gif"}:
            # mjpeg/png/etc. in a video stream slot are usually cover art, not real video.
            return s, video_specifier_index
        video_specifier_index += 1
    return None, -1


def _audio_stream_count(probe: dict) -> int:
    return sum(1 for s in probe.get("streams", []) if s.get("codec_type") == "audio")


def _subtitle_stream_count(probe: dict) -> int:
    return sum(1 for s in probe.get("streams", []) if s.get("codec_type") == "subtitle")


def _has_expected_audio_disposition(probe: dict) -> bool:
    """Verify the AD track ended up as the default audio with the
    visual_impaired flag and no original-audio track was left as default.

    describealaign sets ``disposition:a:0=default+visual_impaired`` for the
    AD it just muxed in, and ``disposition:a:N=0`` for every original audio
    track. A future ffmpeg-python option-ordering quirk or a manually-
    intervened mux could break that contract and produce a file that
    auto-plays the wrong track on Apple TV / webOS / Jellyfin clients —
    structurally valid, semantically wrong. Reject those before publish.
    """
    audio_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        logger.error("Output has no audio streams.")
        return False

    ad_stream = audio_streams[0]
    disp = ad_stream.get("disposition", {}) or {}
    if not disp.get("visual_impaired"):
        logger.error(
            "Output a:0 is missing visual_impaired disposition — AD muxing "
            "contract violated. disposition=%r", disp,
        )
        return False
    if not disp.get("default"):
        logger.error(
            "Output a:0 is not default — players will not auto-play the AD. "
            "disposition=%r", disp,
        )
        return False

    # No other audio stream may carry the default flag. If one does, the
    # file has two "default" audio streams and player behaviour is undefined
    # (often: the lower-index one wins, which is the AD here, so it works
    # by accident — but we refuse to rely on that).
    for i, s in enumerate(audio_streams[1:], start=1):
        s_disp = s.get("disposition", {}) or {}
        if s_disp.get("default"):
            logger.error(
                "Output a:%d unexpectedly has default disposition; only the "
                "AD track (a:0) should. disposition=%r", i, s_disp,
            )
            return False

    return True


def source_has_ad_track(path: Path) -> bool:
    """True if *path* already carries an audio stream flagged
    ``visual_impaired`` — i.e. an audio description has already been muxed in.

    Used to skip re-aligning an already-described file. Without this guard a
    duplicate webhook or a mid-drain restart re-aligns the file and stacks a
    *second* AD track, because ``_validate_media_output`` only requires
    ``audio_count >= source + 1`` and a double-mux satisfies that.

    Returns False on a probe failure: a transient ffprobe error must never
    block a legitimate first-time alignment.
    """
    probe = _ffprobe_json(path)
    if probe is None:
        return False
    for s in probe.get("streams", []):
        if s.get("codec_type") != "audio":
            continue
        if (s.get("disposition", {}) or {}).get("visual_impaired"):
            return True
    return False


def _container_duration(probe: dict) -> Optional[float]:
    fmt = probe.get("format") or {}
    raw = fmt.get("duration")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_fps(stream: dict) -> float:
    """Best-effort frame rate parse from an ffprobe video stream dict."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw == "0/0":
            continue
        try:
            if "/" in raw:
                num, den = raw.split("/", 1)
                fps = float(num) / float(den)
            else:
                fps = float(raw)
            if 1.0 <= fps <= 240.0:
                return fps
        except (ValueError, ZeroDivisionError):
            continue
    return 24.0  # film-rate default; conservative for the absolute-loss floor


def _video_packet_count(path: Path, video_specifier_index: int = 0) -> Optional[int]:
    """Number of packets in video stream ``v:N`` of *path*, where N is
    *video_specifier_index*.

    Returns None on probe failure rather than 0, so the caller can
    distinguish "actually empty stream" (0) from "couldn't tell" (None) and
    treat the latter as a validation failure.

    The index must match the stream that ``_primary_video_stream`` picked,
    or cover art at position 0 will produce a misleading packet count
    (~1 packet) and either fail validation on good output or pass it on
    truncated output.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", f"v:{video_specifier_index}",
        "-count_packets",
        "-show_entries", "stream=nb_read_packets",
        "-print_format", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_FFPROBE_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("ffprobe -count_packets failed on %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "ffprobe -count_packets returned %d for %s: %s",
            proc.returncode, path, proc.stderr.strip(),
        )
        return None
    try:
        data = json.loads(proc.stdout)
        nb = data["streams"][0]["nb_read_packets"]
        return int(nb)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("ffprobe packet count parse failed for %s: %s", path, exc)
        return None


def _validate_media_output(source: Path, output: Path) -> bool:
    """
    Validate that *output* is structurally sound enough to overwrite *source*.

    Returns True if every gate passes. On any failure, logs why and returns
    False — the caller treats that as a discarded alignment.

    Gates:
      1. ffprobe both files cleanly.
      2. Output has a real video stream with matching codec/width/height.
      3. Output has at least source's audio stream count + 1 (the AD track).
      4. Output container duration is within tolerance of source.
      5. Output video packet count is within tolerance of source — both a
         relative ratio (≥95%) AND an absolute floor (lose no more than ~10 s
         worth of frames). Catches truncation that duration alone misses
         (e.g. a container header that claims the right duration but only
         has 3 s of video packets in it).
    """
    src_probe = _ffprobe_json(source)
    if src_probe is None:
        logger.error("Cannot ffprobe source video %s — refusing to publish output.", source)
        return False
    out_probe = _ffprobe_json(output)
    if out_probe is None:
        logger.error("Cannot ffprobe alignment output %s — refusing to publish.", output)
        return False

    src_video, src_video_idx = _primary_video_stream(src_probe)
    out_video, out_video_idx = _primary_video_stream(out_probe)
    if src_video is None:
        logger.error("Source has no primary video stream: %s", source)
        return False
    if out_video is None:
        logger.error("Output has no primary video stream: %s", output)
        return False

    # Stream-copy preserves codec and dimensions; if any of these change,
    # something has gone deeply wrong.
    for key in ("codec_name", "width", "height"):
        if src_video.get(key) != out_video.get(key):
            logger.error(
                "Output video %s mismatch: source=%r output=%r",
                key, src_video.get(key), out_video.get(key),
            )
            return False

    src_audio = _audio_stream_count(src_probe)
    out_audio = _audio_stream_count(out_probe)
    expected_audio = max(1, src_audio + 1)  # AD track on top of whatever was there
    if out_audio < expected_audio:
        logger.error(
            "Output audio stream count %d < expected %d (source had %d).",
            out_audio, expected_audio, src_audio,
        )
        return False

    # describealaign mux contract: output a:0 is the AD track with
    # `default+visual_impaired` disposition, every other audio is non-default.
    # If that contract is broken (e.g. ffmpeg-python option-ordering shift
    # demoted the AD track or left the source's default disposition intact),
    # Apple TV / Jellyfin clients will auto-play the wrong track even though
    # the file is structurally valid. Refuse to publish in that case.
    if not _has_expected_audio_disposition(out_probe):
        return False

    # describealaign maps every source subtitle stream through the mux
    # (original['s'] in each write path), so a drop means the mux went wrong
    # and would silently strip subtitles from the library file. describarr
    # only ever ADDS an audio track, so subtitle count must never regress.
    src_subs = _subtitle_stream_count(src_probe)
    out_subs = _subtitle_stream_count(out_probe)
    if out_subs < src_subs:
        logger.error(
            "Output dropped subtitle streams: source had %d, output has %d — "
            "refusing to publish.", src_subs, out_subs,
        )
        return False

    src_duration = _container_duration(src_probe)
    out_duration = _container_duration(out_probe)
    if src_duration is None or out_duration is None:
        logger.warning(
            "Could not read container duration (src=%r out=%r); skipping duration gate.",
            src_duration, out_duration,
        )
    else:
        lower_tol = max(5.0, min(30.0, src_duration * 0.005))
        upper_tol = max(30.0, min(120.0, src_duration * 0.02))
        if out_duration < src_duration - lower_tol:
            logger.error(
                "Output duration %.1fs is too short (source %.1fs, tolerance -%.1fs).",
                out_duration, src_duration, lower_tol,
            )
            return False
        if out_duration > src_duration + upper_tol:
            logger.error(
                "Output duration %.1fs is too long (source %.1fs, tolerance +%.1fs).",
                out_duration, src_duration, upper_tol,
            )
            return False

    # Count packets in the *primary* video stream — not v:0 — so a file
    # whose v:0 is cover art doesn't validate the 1-frame cover stream
    # while the real video is silently truncated.
    src_packets = _video_packet_count(source, src_video_idx)
    out_packets = _video_packet_count(output, out_video_idx)
    if src_packets is None or out_packets is None:
        logger.error(
            "Could not packet-count one of the files (src=%r out=%r) — refusing to publish.",
            src_packets, out_packets,
        )
        return False
    fps = _parse_fps(src_video)
    abs_loss_packets = max(1, int(fps * _MAX_MISSING_SECONDS_OF_VIDEO))
    required = max(int(src_packets * _PACKET_RATIO_FLOOR), src_packets - abs_loss_packets)
    if out_packets < required:
        logger.error(
            "Output has %d video packets, need ≥ %d (source %d, fps %.2f, "
            "ratio floor %.2f, absolute loss floor %ds=%d packets).",
            out_packets, required, src_packets, fps,
            _PACKET_RATIO_FLOOR, _MAX_MISSING_SECONDS_OF_VIDEO, abs_loss_packets,
        )
        return False

    logger.info(
        "Output validation passed: codec=%s %dx%d audio=%d→%d duration=%.1fs packets=%d→%d (%.1f%%)",
        out_video.get("codec_name"), out_video.get("width"), out_video.get("height"),
        src_audio, out_audio, out_duration or -1.0,
        src_packets, out_packets, 100.0 * out_packets / max(1, src_packets),
    )
    return True
