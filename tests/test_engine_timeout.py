"""The engine's time limit grows with the video.

A flat hour was set when 3-hour PAL BluRay films aligned in under 30 minutes.
Since describealign 2.3.0's third pass a long film takes far longer: on
2026-11-01 a 142-minute remux (Forrest Gump, 33 GB) took 59 min 34 s with its
sound already decoded, 26 s inside the hour, after 23 minutes of decoding on
the first try. A slower second run, or a cold one, would have been killed half
done and reported as a timeout.
"""

import subprocess

import describarr.aligner as aligner


def test_the_limit_is_an_hour_or_one_and_a_half_times_the_video():
    assert aligner._engine_timeout(8526.0) == 12789
    assert aligner._engine_timeout(1320.0) == 3600
    assert aligner._engine_timeout(2400.0) == 3600


def test_an_unknown_length_keeps_the_hour():
    assert aligner._engine_timeout(None) == 3600
    assert aligner._engine_timeout(0.0) == 3600
    assert aligner._engine_timeout(float("nan")) == 3600


def _files(tmp_path):
    video = tmp_path / "Forrest.Gump.1994.mkv"
    video.write_bytes(b"v")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"a")
    return video, audio


def _probe(monkeypatch, duration):
    probe = {
        "format": {"duration": duration},
        "streams": [{"codec_type": "audio", "tags": {"language": "eng"}}],
    }
    monkeypatch.setattr(aligner, "_ffprobe_json", lambda path, extra_args=None: probe)
    monkeypatch.setattr(aligner, "_conform_pal_audio", lambda v, a, d: a)


def test_the_engine_gets_the_films_limit(tmp_path, monkeypatch):
    video, audio = _files(tmp_path)
    _probe(monkeypatch, "8526.0")
    seen = {}

    def engine(cmd, timeout=None):
        seen["timeout"] = timeout
        return 1, "", ""

    monkeypatch.setattr(aligner, "_run_subprocess", engine)
    aligner.run(video, audio, tmp_path / "out", tmp_path / "align", stretch_audio=True)
    assert seen["timeout"] == 12789


def test_a_timeout_reports_the_limit_it_had(tmp_path, monkeypatch):
    video, audio = _files(tmp_path)
    _probe(monkeypatch, "8526.0")

    def engine(cmd, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(aligner, "_run_subprocess", engine)
    result = aligner.run(video, audio, tmp_path / "out", tmp_path / "align", stretch_audio=True)
    assert result.output is None
    assert result.failure_reason == "alignment timed out after 213 minutes"
    assert isinstance(result.failure_reason, aligner.EngineFailure)


def test_the_runner_enforces_the_limit_it_is_given(tmp_path):
    # The real runner, on a real process that outlives its limit.
    try:
        aligner._run_subprocess(["sleep", "30"], timeout=1)
    except subprocess.TimeoutExpired as raised:
        assert raised.timeout == 1
    else:
        raise AssertionError("a 30-second process ran past a 1-second limit")
