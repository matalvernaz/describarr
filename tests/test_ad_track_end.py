"""An output whose description track stops before the source does is not published.

Players end a file when the track they are playing ends. EchoFin treats an
ending more than 5 s short as a broken stream, so an episode never moves on to
the next and a film ends in "Playback failed" (2026-11-11: 99 described files,
mostly recordings the engine copied in whole that stop before the video does;
Captain Underpants 53 s short on 37 of 39 episodes). The container's duration
hides it, being the longest track's, so validation reads where the description
track really ends and where the source already ends: the earlier of its own
sound and its picture, each file counted from its own video origin. Built from
real files where the point is what ffprobe reads from the packets.
"""

import io
import shutil
import subprocess

import pytest

import describarr.aligner as aligner

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="needs ffmpeg and ffprobe")


def _run(*arguments):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *arguments], check=True)


def _source(tmp_path, seconds=20, sound_seconds=None, starts_at=0):
    """A small video with its own English sound. *sound_seconds* lets the sound
    stop before, or run on past, the picture; *starts_at* starts the file's
    clock there, as a broadcast capture's does."""
    path = tmp_path / f"source-{seconds}-{sound_seconds}-{starts_at}.mkv"
    offset = ["-output_ts_offset", str(starts_at)] if starts_at else []
    _run("-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=64x48:rate=10",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={sound_seconds or seconds}",
         "-c:v", "mpeg4", "-c:a", "aac", "-b:a", "64k", "-metadata:s:a:0", "language=eng", *offset, str(path))
    return path


def _described(tmp_path, source, description_seconds):
    """The source with a description track of *description_seconds* in front,
    laid out as describealaign writes it: AD first, default and visually
    impaired. A plain remux, so a source's clock is moved to start at 0."""
    description = tmp_path / f"description-{description_seconds}.mka"
    _run("-f", "lavfi", "-i", f"sine=frequency=880:duration={description_seconds}",
         "-c:a", "aac", "-b:a", "64k", str(description))
    output = tmp_path / f"ad_{source.stem}-{description_seconds}.mkv"
    _run("-i", str(source), "-i", str(description),
         "-map", "1:a", "-map", "0:v", "-map", "0:a", "-c", "copy",
         "-disposition:a:0", "default+visual_impaired", "-disposition:a:1", "0",
         "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=AD", str(output))
    return output


# ── the gate ──────────────────────────────────────────────────────────────────

def test_a_full_length_description_is_published(tmp_path):
    source = _source(tmp_path)
    assert aligner._validate_media_output(source, _described(tmp_path, source, 20)) is True


def test_a_description_a_few_seconds_short_is_published(tmp_path):
    # Within the 5 s players allow: an mp3 recording rarely ends on the video's
    # last frame, and this is not what stops a player early.
    source = _source(tmp_path)
    assert aligner._validate_media_output(source, _described(tmp_path, source, 17)) is True


def test_a_description_that_stops_early_is_refused(tmp_path, caplog):
    source = _source(tmp_path)
    output = _described(tmp_path, source, 10)
    assert aligner._validate_media_output(source, output) is False
    assert "before the source does" in caplog.text


def _shortfall(source, output):
    src_probe, out_probe = aligner._ffprobe_json(source), aligner._ffprobe_json(output)
    measured = aligner._description_shortfall(source, output, src_probe, out_probe,
                                              aligner._container_duration(src_probe),
                                              aligner._container_duration(out_probe))
    return measured[0]


def test_a_source_whose_clock_starts_late_is_compared_on_its_own_timeline(tmp_path):
    # Starting at 10 s, the source's sound ends at 30 s; the remux starts at 0,
    # so a full-length description ends at 20 s. Compared as raw times that
    # looks 10 s short; from each file's own video origin it is not. (On a
    # 20 s file the container-duration gate refuses such a remux first; on a
    # film it allows up to 30 s, so this gate must not misread it.)
    source = _source(tmp_path, starts_at=10)
    assert aligner._video_origin(source, aligner._ffprobe_json(source)) == pytest.approx(10.0, abs=0.1)
    assert abs(_shortfall(source, _described(tmp_path, source, 20))) < 0.5
    assert _shortfall(source, _described(tmp_path, source, 10)) == pytest.approx(10.0, abs=0.5)


def test_sound_that_runs_on_past_the_picture_does_not_raise_the_bar(tmp_path):
    # The file is done when its picture is: a description to the picture's
    # end is full length, though the source's own sound runs 8 s longer.
    source = _source(tmp_path, seconds=20, sound_seconds=28)
    assert aligner._validate_media_output(source, _described(tmp_path, source, 20)) is True


def test_a_description_as_long_as_a_short_source_sound_is_published(tmp_path):
    # The source's own sound stops 10 s before its picture, so it already ends
    # early in a player whatever track plays (EchoFin finding D-1, files
    # describarr never touched). A description built on that sound is as long
    # as it, and is not refused for the source's own fault.
    source = _source(tmp_path, seconds=30, sound_seconds=20)
    assert aligner._validate_media_output(source, _described(tmp_path, source, 20)) is True
    assert aligner._validate_media_output(source, _described(tmp_path, source, 10)) is False


def test_an_end_that_cannot_be_established_is_not_published(tmp_path, monkeypatch, caplog):
    source = _source(tmp_path)
    output = _described(tmp_path, source, 20)
    measure = aligner._stream_end
    monkeypatch.setattr(aligner, "_stream_end",
                        lambda path, stream, duration: None if path == output else measure(path, stream, duration))
    assert aligner._validate_media_output(source, output) is False
    assert "Could not establish where the audio ends" in caplog.text


# ── reading where a stream ends ─────────────────────────────────────────────

def test_where_each_stream_ends_is_read_from_its_packets(tmp_path):
    source = _source(tmp_path)
    output = _described(tmp_path, source, 10)
    assert aligner._last_packet_end(output, "a:0", 20.0) == pytest.approx(10.0, abs=0.2)
    assert aligner._last_packet_end(output, "a:1", 20.0) == pytest.approx(20.0, abs=0.2)
    assert aligner._last_packet_end(output, "v:0", 20.0) == pytest.approx(20.0, abs=0.2)


def test_a_tail_read_measures_a_track_that_stops_long_before_the_end(tmp_path, monkeypatch, caplog):
    # The probe reads the last _TRACK_END_READ_SECONDS, shrunk here so a 60 s
    # file takes the path a two-hour film does. ffprobe's seek for the selected
    # track lands where that track stops, so a 10 s description read from 55 s
    # is still measured (checked on mkv and mp4, 2026-11-25).
    monkeypatch.setattr(aligner, "_TRACK_END_READ_SECONDS", 5.0)
    source = _source(tmp_path, seconds=60)
    output = _described(tmp_path, source, 10)
    assert aligner._stream_end(output, "a:0", 60.0) == pytest.approx(10.0, abs=0.2)
    assert aligner._validate_media_output(source, output) is False
    assert "ends at 10.0s" in caplog.text


def test_a_full_length_description_passes_on_a_tail_read(tmp_path, monkeypatch):
    monkeypatch.setattr(aligner, "_TRACK_END_READ_SECONDS", 5.0)
    source = _source(tmp_path, seconds=60)
    assert aligner._validate_media_output(source, _described(tmp_path, source, 60)) is True


def test_a_stream_the_tail_read_misses_is_read_from_the_start(monkeypatch):
    # Where a demuxer's seek does not land on the short track, the tail read
    # finds no packet of it; that is "not measured", so the whole file is read.
    reads = []

    def last_end(path, stream, duration, from_start=False):
        reads.append((stream, from_start))
        if stream == "a:0":
            return 10.0 if from_start else None
        return 7200.0

    monkeypatch.setattr(aligner, "_last_packet_end", last_end)
    assert aligner._stream_end(aligner.Path("/tv/film.mkv"), "a:0", 7200.0) == 10.0
    assert reads == [("a:0", False), ("a:0", True)]
    reads.clear()
    assert aligner._stream_end(aligner.Path("/tv/film.mkv"), "a:1", 7200.0) == 7200.0
    assert reads == [("a:1", False)]                                     # measured on the tail, read once


class _FakeProbe:
    """ffprobe printing *lines*, as subprocess.Popen would hand them over."""

    def __init__(self, lines, returncode=0):
        self.stdout = io.StringIO("".join(lines))
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


def test_packets_without_timestamps_measure_nothing(monkeypatch):
    monkeypatch.setattr(aligner.subprocess, "Popen", lambda *a, **k: _FakeProbe(["N/A,0.023\n", "N/A,N/A\n", "\n"]))
    assert aligner._last_packet_end(aligner.Path("/tv/film.mkv"), "a:0", 7200.0) is None
    assert aligner._stream_end(aligner.Path("/tv/film.mkv"), "a:0", 7200.0) is None


def test_the_latest_packet_end_wins_and_a_missing_length_counts_as_none(monkeypatch):
    lines = ["5.000000,0.021000\n", "7.000000,N/A\n", "6.000000,0.500000\n"]
    monkeypatch.setattr(aligner.subprocess, "Popen", lambda *a, **k: _FakeProbe(lines))
    assert aligner._last_packet_end(aligner.Path("/tv/film.mkv"), "a:0", 7.0) == pytest.approx(7.0)


def test_a_failed_probe_measures_nothing(monkeypatch):
    monkeypatch.setattr(aligner.subprocess, "Popen", lambda *a, **k: _FakeProbe(["5.0,0.02\n"], returncode=1))
    assert aligner._last_packet_end(aligner.Path("/tv/film.mkv"), "a:0", 7200.0) is None


def test_a_video_origin_with_no_start_time_is_read_from_the_first_packet(tmp_path):
    source = _source(tmp_path, starts_at=10)
    probe = aligner._ffprobe_json(source)
    for stream in probe["streams"]:
        stream.pop("start_time", None)
    probe["format"].pop("start_time", None)
    assert aligner._video_origin(source, probe) == pytest.approx(10.0, abs=0.1)
