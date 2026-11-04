"""Jellyfin is told at once that a published file changed.

describarr runs on the storage server, so its writes reach Jellyfin over NFS,
where inotify never reports them. Until the next scheduled scan (twelve hours
apart) the item kept the track list of the file it replaced: no description to
pick, and every stream number after it off by one. 77 Call the Midwife episodes
sat like that on 2026-11-04. A publish now makes the report Sonarr makes after
an import, and only a publish does.
"""

import io
import json
import socket
from urllib.error import HTTPError

import pytest

import describarr.notify as notify
import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _configured(monkeypatch):
    monkeypatch.setenv("JELLYFIN_URL", "http://jellyfin:8096/")
    monkeypatch.setenv("JELLYFIN_API_KEY", "key123")


# ── the report itself ────────────────────────────────────────────────────────

def test_the_report_names_the_file_with_the_servers_own_auth(monkeypatch):
    _configured(monkeypatch)
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.get_header("Authorization")
        seen["type"] = request.get_header("Content-type")
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(notify, "urlopen", fake_urlopen)
    path = "/media/TV shows/Call the Midwife/Season 6/Call.the.Midwife.S06E08.1080p.BluRay.x264-YELLOWBiRD.mkv"
    assert notify.tell_jellyfin_file_changed(path) is True
    assert seen["url"] == "http://jellyfin:8096/Library/Media/Updated"
    assert seen["method"] == "POST"
    assert seen["auth"] == 'MediaBrowser Token="key123"'
    assert seen["type"] == "application/json"
    assert seen["body"] == {"Updates": [{"Path": path, "UpdateType": "Modified"}]}
    assert seen["timeout"] == 10


def test_no_server_means_no_report(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "urlopen", lambda *a, **k: calls.append(a) or _Response())
    monkeypatch.delenv("JELLYFIN_URL", raising=False)
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    assert notify.tell_jellyfin_file_changed("/media/movies/Heat.mkv") is False
    monkeypatch.setenv("JELLYFIN_URL", "http://jellyfin:8096")
    assert notify.tell_jellyfin_file_changed("/media/movies/Heat.mkv") is False
    assert calls == []


@pytest.mark.parametrize("failure", [
    HTTPError("http://jellyfin:8096/Library/Media/Updated", 401, "no", {}, io.BytesIO(b"")),
    HTTPError("http://jellyfin:8096/Library/Media/Updated", 404, "no", {}, io.BytesIO(b"")),
    HTTPError("http://jellyfin:8096/Library/Media/Updated", 500, "no", {}, io.BytesIO(b"")),
    ConnectionRefusedError("down"),
    socket.timeout("read timed out"),
])
def test_a_failed_report_is_logged_never_raised(monkeypatch, caplog, failure):
    _configured(monkeypatch)

    def failing(*a, **k):
        raise failure

    monkeypatch.setattr(notify, "urlopen", failing)
    assert notify.tell_jellyfin_file_changed("/media/movies/Heat.mkv") is False
    assert "Jellyfin was not told that /media/movies/Heat.mkv changed" in caplog.text


# ── through _align_and_keep ──────────────────────────────────────────────────

def _seg(rate, v0, v1, a0, a1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": a0, "audio_end_sec": a1}


def _keep(monkeypatch, tmp_path, similarity, events, publish=None):
    """One recording through the real gate; *events* records the publish and
    the report in order. Returns (published, video)."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "Call.the.Midwife.S06E08.1080p.BluRay.x264-YELLOWBiRD.mkv"
    video.write_bytes(b"x")
    audio = tmp_path / "[S06.E08] Episode #6.8.mp3"
    audio.write_bytes(b"y")
    combined = tmp_path / "out" / "ad_ep.mkv"
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_bytes(b"z")
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"Input file similarity: {similarity}%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": similarity, "median_rate_pct": 0.0,
        "segments": [_seg(0.0, 0.5, 3660.0, 0.0, 3659.5)],
    }))
    monkeypatch.setattr(workflow, "align", lambda *a, **k: AlignResult(combined, report, None, returncode=0))
    monkeypatch.setattr(workflow, "_publish_in_place",
                        publish or (lambda combined, target, **k: events.append(("published", str(target)))))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: True)
    monkeypatch.setattr(workflow.notify, "tell_jellyfin_file_changed",
                        lambda path: events.append(("told", path)) or True)
    published, _ = workflow._align_and_keep(config, video, audio, label="Call the Midwife S06E08",
                                            episode_title="Episode 8")
    return published, video


def test_jellyfin_is_told_once_the_file_is_in_place(monkeypatch, tmp_path):
    events = []
    published, video = _keep(monkeypatch, tmp_path, 71.1, events)
    assert published
    # After the replace, never before it: a report ahead of the rename makes
    # Jellyfin read the old file again.
    assert events == [("published", str(video)), ("told", str(video))]


def test_a_refused_recording_tells_jellyfin_nothing(monkeypatch, tmp_path):
    events = []
    published, _ = _keep(monkeypatch, tmp_path, 12.0, events)
    assert not published
    assert events == []


def test_a_publish_that_fails_tells_jellyfin_nothing(monkeypatch, tmp_path):
    def changed_underneath(*a, **k):
        raise RuntimeError("Source video changed during alignment")

    events = []
    with pytest.raises(RuntimeError, match="Source video changed"):
        _keep(monkeypatch, tmp_path, 71.1, events, publish=changed_underneath)
    assert events == []
