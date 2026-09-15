"""An engine crash (non-zero exit, no *.fail.json) must leave its traceback in the
normal log, not only at DEBUG. Snow White, 2026-09-15: two 'exit 1' runs, no cause."""
import logging

import describarr.aligner as aligner


def test_engine_crash_traceback_is_logged_at_error(tmp_path, monkeypatch, caplog):
    video = tmp_path / "Movie (2012).mkv"
    video.write_bytes(b"v")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"a")
    stderr = "\n".join(["  memorizing video...", "Traceback (most recent call last):",
                        '  File "describealaign.py", line 1, in <module>', "ValueError: boom"])
    monkeypatch.setattr(aligner, "_run_subprocess", lambda cmd: (1, "", stderr))
    monkeypatch.setattr(aligner, "_conform_pal_audio", lambda v, a, d: a)

    with caplog.at_level(logging.INFO, logger="describarr.aligner"):
        res = aligner.run(video, audio, tmp_path / "out", tmp_path / "align", stretch_audio=True)

    assert res.output is None and res.returncode == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("ValueError: boom" in m for m in errors), errors


def test_engine_rejection_with_sidecar_does_not_dump_stderr(tmp_path, monkeypatch, caplog):
    video = tmp_path / "Movie (2012).mkv"
    video.write_bytes(b"v")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"a")
    monkeypatch.setattr(aligner, "_run_subprocess", lambda cmd: (1, "", "some engine noise"))
    monkeypatch.setattr(aligner, "_conform_pal_audio", lambda v, a, d: a)
    monkeypatch.setattr(aligner, "_read_failure_sidecar", lambda *a, **k: "AD audio is 95% silence")

    with caplog.at_level(logging.INFO, logger="describarr.aligner"):
        res = aligner.run(video, audio, tmp_path / "out", tmp_path / "align", stretch_audio=True)

    assert res.failure_reason == "AD audio is 95% silence"
    assert not any("some engine noise" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
