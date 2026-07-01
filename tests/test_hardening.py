"""Regression tests for the 2026-07-01 audit fixes:
  - _mark_episode_done tolerates an extra (no-zip) source.
  - DecisionLog is a bounded, newest-last ring.
  - aligner._read_failure_sidecar surfaces the engine's mismatch cause.
  - RetryQueue serialises concurrent appends (no lost updates).
  - Config parses the new api_key / history_size knobs.
  - _validate_media_output rejects an output that dropped subtitle streams.
"""
import json
import sys
import threading
import types

import pytest

from describarr import aligner
from describarr import sources
from describarr import workflow as wf
from describarr.config import Config
from describarr.decision_log import DecisionLog
from describarr.retry_queue import RetryQueue


# --- Part 1: extra-source (no-zip) mark-done gap ---------------------------

def test_mark_episode_done_without_zip_records_and_skips_cleanup(tmp_path):
    """An extra source passes no zip/extract_dir; the episode must still land
    in the season ledger, and the (missing) zip cleanup must be skipped without
    raising."""
    wf._mark_episode_done(tmp_path, season=3, episode=5)
    progress = tmp_path / ".done_s03.json"
    assert progress.exists()
    data = json.loads(progress.read_text())
    assert data["done"] == [5]
    assert data["total"] == 0  # unknown without a zip → cleanup can't fire

    wf._mark_episode_done(tmp_path, season=3, episode=6)
    data = json.loads((tmp_path / ".done_s03.json").read_text())
    assert data["done"] == [5, 6]


def test_resolve_audio_total_is_none_safe():
    assert wf._resolve_audio_total(0, None, None) == 0
    assert wf._resolve_audio_total(7, None, None) == 7


# --- Part 4: decision log ---------------------------------------------------

def test_decision_log_is_bounded_ring(tmp_path):
    log = DecisionLog(tmp_path / "decisions.json", max_entries=3)
    for i in range(5):
        log.append({"title": f"E{i}", "outcome": "described", "detail": ""})
    items = log.load()
    assert len(items) == 3
    assert [i["title"] for i in items] == ["E2", "E3", "E4"]  # newest kept, oldest dropped
    assert all("ts" in i for i in items)  # timestamp stamped on append


def test_decision_log_recent_is_newest_first(tmp_path):
    log = DecisionLog(tmp_path / "decisions.json", max_entries=10)
    log.append({"title": "first", "outcome": "rejected", "detail": ""})
    log.append({"title": "second", "outcome": "described", "detail": ""})
    recent = log.recent(limit=1)
    assert len(recent) == 1
    assert recent[0]["title"] == "second"


def test_decision_log_zero_cap_is_noop(tmp_path):
    log = DecisionLog(tmp_path / "decisions.json", max_entries=0)
    log.append({"title": "x", "outcome": "described", "detail": ""})
    assert log.load() == []


# --- Part 3: failure-reason sidecar ----------------------------------------

def test_read_failure_sidecar_returns_summary(tmp_path):
    from pathlib import Path
    (tmp_path / "Show S01E01.fail.json").write_text(json.dumps({
        "error": "alignment_mismatch",
        "summary": "AD is 0.20× the video duration — likely wrong episode",
    }))
    reason = aligner._read_failure_sidecar(Path("/tv/Show S01E01.mkv"), tmp_path)
    assert reason is not None
    assert "wrong episode" in reason


def test_read_failure_sidecar_absent_returns_none(tmp_path):
    from pathlib import Path
    assert aligner._read_failure_sidecar(Path("/tv/Nope.mkv"), tmp_path) is None


# --- Part 1: retry-queue lock ----------------------------------------------

def test_retry_queue_concurrent_appends_lose_nothing(tmp_path):
    """Without the lock, interleaved load→append→save races drop entries.
    Every distinct video_path enqueued from many threads must survive."""
    q = RetryQueue(tmp_path / "retry_queue.json")
    n = 60

    def add(i):
        q.add_movie(f"Movie {i}", "2020", f"/movies/m{i}.mkv")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    paths = {item["video_path"] for item in q.load()}
    assert len(paths) == n


# --- Part 1/4: config knobs -------------------------------------------------

def test_config_parses_api_key_and_history_size(monkeypatch):
    monkeypatch.setenv("AUDIOVAULT_EMAIL", "a@b.c")
    monkeypatch.setenv("AUDIOVAULT_PASSWORD", "pw")
    monkeypatch.setenv("DESCRIBARR_API_KEY", "  secret123  ")
    monkeypatch.setenv("DESCRIBARR_HISTORY_SIZE", "12")
    cfg = Config.from_env()
    assert cfg.api_key == "secret123"  # trimmed
    assert cfg.history_size == 12


def test_config_api_key_absent_is_none(monkeypatch):
    monkeypatch.setenv("AUDIOVAULT_EMAIL", "a@b.c")
    monkeypatch.setenv("AUDIOVAULT_PASSWORD", "pw")
    monkeypatch.delenv("DESCRIBARR_API_KEY", raising=False)
    cfg = Config.from_env()
    assert cfg.api_key is None
    assert cfg.history_size == 50  # default


# --- Part 1: subtitle survival gate ----------------------------------------

def _probe(*, subs: int, add_ad: bool):
    streams = [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}]
    if add_ad:
        streams.append({"codec_type": "audio", "disposition": {"default": 1, "visual_impaired": 1}})
    streams.append({"codec_type": "audio", "disposition": {"default": 0}})
    for _ in range(subs):
        streams.append({"codec_type": "subtitle"})
    return {"streams": streams, "format": {"duration": "1350.0"}}


def test_validate_rejects_dropped_subtitles(monkeypatch):
    src = _probe(subs=2, add_ad=False)   # 1 audio + 2 subs
    out = _probe(subs=0, add_ad=True)    # AD + original audio, subs dropped

    monkeypatch.setattr(aligner, "_ffprobe_json",
                        lambda p, *a, **k: src if "src" in str(p) else out)
    monkeypatch.setattr(aligner, "_video_packet_count", lambda *a, **k: 1000)

    from pathlib import Path
    assert aligner._validate_media_output(Path("/tv/src.mkv"), Path("/tv/out.mkv")) is False


def test_validate_accepts_preserved_subtitles(monkeypatch):
    src = _probe(subs=2, add_ad=False)
    out = _probe(subs=2, add_ad=True)    # subs preserved, AD added

    monkeypatch.setattr(aligner, "_ffprobe_json",
                        lambda p, *a, **k: src if "src" in str(p) else out)
    monkeypatch.setattr(aligner, "_video_packet_count", lambda *a, **k: 1000)

    from pathlib import Path
    assert aligner._validate_media_output(Path("/tv/src.mkv"), Path("/tv/out.mkv")) is True


# --- extra-source plugin loader (no provider specifics live in the repo) ----

def _register_fake_source(name: str, *, configured: bool = True):
    mod = types.ModuleType(name)

    class _FakeSource:
        def is_configured(self):
            return configured

        def episode_candidates(self, *a, **k):
            return []

        def movie_candidates(self, *a, **k):
            return []

        def close(self):
            pass

    mod.get_source = lambda: _FakeSource()
    sys.modules[name] = mod


def test_load_extra_sources_unset_is_empty(monkeypatch):
    monkeypatch.delenv("DESCRIBARR_EXTRA_SOURCES", raising=False)
    assert sources.load_extra_sources() == []


def test_load_extra_sources_loads_configured_via_default_factory(monkeypatch):
    _register_fake_source("_fake_src_ok")
    monkeypatch.setenv("DESCRIBARR_EXTRA_SOURCES", "_fake_src_ok")  # default get_source
    loaded = sources.load_extra_sources()
    assert len(loaded) == 1
    assert isinstance(loaded[0], sources.AudioSource)


def test_load_extra_sources_skips_unconfigured(monkeypatch):
    _register_fake_source("_fake_src_no", configured=False)
    monkeypatch.setenv("DESCRIBARR_EXTRA_SOURCES", "_fake_src_no:get_source")
    assert sources.load_extra_sources() == []


def test_load_extra_sources_skips_unimportable(monkeypatch):
    monkeypatch.setenv("DESCRIBARR_EXTRA_SOURCES", "no.such.module:get_source")
    assert sources.load_extra_sources() == []  # logged and skipped, never fatal
