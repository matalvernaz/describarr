"""Tests for the PAL-timebase AD conform rescue (aligner._conform_pal_audio).

The rescue tempo-conforms a PAL-mastered AD (~4.27% longer than a native
video) to the video's duration so describealign produces a video-length,
in-sync output instead of one that overshoots the duration gate. It must
trigger only inside the PAL band and fall back to the original AD on any
probe/encode failure.
"""
from pathlib import Path

import pytest

from describarr import aligner


def _probe(dur):
    return {"format": {"duration": str(dur)}}


def _patch_probes(monkeypatch, vdur, adur):
    """Return video duration for the .mkv probe, audio duration for the .mp3."""
    def fake_probe(path, extra_args=None):
        return _probe(vdur) if Path(path).suffix == ".mkv" else _probe(adur)
    monkeypatch.setattr(aligner, "_ffprobe_json", fake_probe)


def _forbid_ffmpeg(monkeypatch):
    monkeypatch.setattr(aligner.subprocess, "run",
                        lambda *a, **k: pytest.fail("ffmpeg must not run"))


def test_conform_triggers_in_pal_band(monkeypatch, tmp_path):
    _patch_probes(monkeypatch, vdur=3004.0, adur=3136.2)  # +4.40%, in band
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"conformed")  # simulate ffmpeg writing output
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(aligner.subprocess, "run", fake_run)
    out = aligner._conform_pal_audio(tmp_path / "ep.mkv", tmp_path / "ad.mp3", tmp_path)

    assert out == tmp_path / "ep.conformed.mp3"
    assert out.exists()
    assert len(calls) == 1
    assert any(str(a).startswith("atempo=1.0440") for a in calls[0])


def test_conform_skips_same_timebase(monkeypatch, tmp_path):
    _patch_probes(monkeypatch, vdur=3624.9, adur=3624.9)  # ratio 1.00
    _forbid_ffmpeg(monkeypatch)
    ad = tmp_path / "ad.mp3"
    assert aligner._conform_pal_audio(tmp_path / "ep.mkv", ad, tmp_path) == ad


def test_conform_skips_different_cut(monkeypatch, tmp_path):
    _patch_probes(monkeypatch, vdur=3000.0, adur=3600.0)  # +20% — not PAL drift
    _forbid_ffmpeg(monkeypatch)
    ad = tmp_path / "ad.mp3"
    assert aligner._conform_pal_audio(tmp_path / "ep.mkv", ad, tmp_path) == ad


def test_conform_falls_back_on_ffmpeg_error(monkeypatch, tmp_path):
    _patch_probes(monkeypatch, vdur=3004.0, adur=3136.2)
    monkeypatch.setattr(aligner.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stderr": "boom"})())
    ad = tmp_path / "ad.mp3"
    assert aligner._conform_pal_audio(tmp_path / "ep.mkv", ad, tmp_path) == ad


def test_conform_falls_back_when_probe_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(aligner, "_ffprobe_json", lambda *a, **k: None)
    _forbid_ffmpeg(monkeypatch)
    ad = tmp_path / "ad.mp3"
    assert aligner._conform_pal_audio(tmp_path / "ep.mkv", ad, tmp_path) == ad
