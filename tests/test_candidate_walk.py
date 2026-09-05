"""Movie candidate walk: resource-kill abort and the per-movie attempt cap.

Regression for the 2026-07 cap burn: a memcg OOM SIGKILL (describealaign
exit -9) was indistinguishable from "candidate below threshold", so the walk
downloaded and aligned candidate after candidate — every one dying the same
way — until the AudioVault daily cap tripped and starved the rest of the
queue.
"""

import pytest

import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.workflow import (
    _MAX_MOVIE_CANDIDATES,
    AlignmentResourceKill,
    process_movie,
)


def _setup(monkeypatch, tmp_path, n_candidates, align_behavior):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Movie (2020).mkv"
    video.write_bytes(b"x")

    results = [
        {"name": f"Movie (2020) [v{i}]", "url": f"https://av/dl/{i}"}
        for i in range(n_candidates)
    ]

    class FakeClient:
        def search_movies(self, title):
            return results

    downloads = []

    def fake_get_cached(client, url, cache_dir, limiter):
        downloads.append(url)
        return tmp_path / "ad.mp3"

    attempts = []

    def fake_align_and_keep(config, video_path, audio_path, label=None):
        attempts.append(audio_path)
        return align_behavior(len(attempts))

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "_get_cached", fake_get_cached)
    monkeypatch.setattr(workflow, "_align_and_keep", fake_align_and_keep)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    return config, video, FakeClient(), downloads, attempts


def test_resource_kill_aborts_candidate_walk(monkeypatch, tmp_path):
    def behavior(n):
        raise AlignmentResourceKill(
            "alignment failed (describealaign exit -9) — killed by signal"
        )

    config, video, client, downloads, _ = _setup(monkeypatch, tmp_path, 5, behavior)
    described, reason = process_movie(client, config, video, "Movie", "2020")
    assert not described
    assert "killed by signal" in reason
    assert len(downloads) == 1  # no further download slots burned


def test_candidate_walk_is_capped(monkeypatch, tmp_path):
    def behavior(n):
        return False, "below threshold"

    config, video, client, downloads, _ = _setup(monkeypatch, tmp_path, 6, behavior)
    described, _ = process_movie(client, config, video, "Movie", "2020")
    assert not described
    assert len(downloads) == _MAX_MOVIE_CANDIDATES


def test_align_and_keep_raises_on_signal_kill(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Movie (2020).mkv"
    video.write_bytes(b"x")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"y")

    monkeypatch.setattr(
        workflow, "align",
        lambda *a, **k: AlignResult(
            None, None, "alignment failed (describealaign exit -9)", returncode=-9
        ),
    )
    with pytest.raises(AlignmentResourceKill):
        workflow._align_and_keep(config, video, audio)


def test_align_and_keep_returns_on_ordinary_exit_failure(monkeypatch, tmp_path):
    # A positive exit code is a normal quality/diagnosis failure — the walk
    # must keep trying further candidates, not abort.
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Movie (2020).mkv"
    video.write_bytes(b"x")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"y")

    monkeypatch.setattr(
        workflow, "align",
        lambda *a, **k: AlignResult(
            None, None, "wrong-duration AD rejected", returncode=1
        ),
    )
    published, reason = workflow._align_and_keep(config, video, audio)
    assert not published
    assert reason == "wrong-duration AD rejected"


def test_align_and_keep_raises_when_source_vanishes_mid_alignment(monkeypatch, tmp_path):
    """Live 2026-09-04, Gossip Girl S01E01: Sonarr upgraded the episode during
    the four minutes the alignment was running, so `_validate_media_output`
    could no longer ffprobe the source. The generic failure read as "candidate
    below threshold" and the walk moved on to spend another download slot on a
    file that no longer existed."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Movie (2020).mkv"
    video.write_bytes(b"x")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"y")

    def fake_align(*a, **k):
        video.unlink()  # the arr upgrade lands mid-run
        return AlignResult(None, None, "alignment produced no validated output", returncode=1)

    monkeypatch.setattr(workflow, "align", fake_align)
    with pytest.raises(workflow.SourceVanished):
        workflow._align_and_keep(config, video, audio)


def test_source_vanished_aborts_candidate_walk(monkeypatch, tmp_path):
    def behavior(n):
        raise workflow.SourceVanished("source video was replaced while the alignment was running")

    config, video, client, downloads, _ = _setup(monkeypatch, tmp_path, 5, behavior)
    described, reason = process_movie(client, config, video, "Movie", "2020")
    assert not described
    assert "replaced while the alignment was running" in reason
    assert len(downloads) == 1  # no further download slots burned


def test_ordinary_failure_still_walks_when_source_is_intact(monkeypatch, tmp_path):
    """The abort must key on the file being gone, not on any failure — a real
    quality rejection has to keep trying further candidates."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Movie (2020).mkv"
    video.write_bytes(b"x")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"y")

    monkeypatch.setattr(
        workflow, "align",
        lambda *a, **k: AlignResult(None, None, "score too low", returncode=1),
    )
    published, reason = workflow._align_and_keep(config, video, audio)
    assert not published
    assert video.exists()
