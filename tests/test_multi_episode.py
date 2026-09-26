"""Multi-episode files are described with every covered episode's AD.

Avatar S02E12-E13 (2026-09-16): a 47-minute double episode was aligned
against E12's AD alone and published with its second half silent — 1372 s
undescribed, reported only as a "different cut". The retry path parsed the
first SxxEyy and nothing else; the webhook path carried the extra episodes
but still aligned one donor. Now every covered episode's file is joined in
order, and an entry that lacks one of them is skipped rather than half used.
"""

import json
import shutil
import subprocess
import types
from pathlib import Path

import pytest

import describarr.server as srv
from conftest import fake_config
import describarr.workflow as workflow
from describarr.config import Config
from describarr.workflow import _concat_audio, _episode_donor, process_episode


# ── parsing every style Sonarr writes ─────────────────────────────────────────

@pytest.mark.parametrize("name, expected", [
    ("Show - S02E12 - Title.mkv", (2, [12])),
    ("Avatar (2005) - S02E12-E13 - The Serpent's Pass & The Drill.mkv", (2, [12, 13])),
    ("Avatar (2005) - S03E18-E21 - Sozin's Comet.mkv", (3, [18, 19, 20, 21])),
    ("Show.S01E01E02.720p.mkv", (1, [1, 2])),
    ("Show.S01E01-02.mkv", (1, [1, 2])),
    ("Show.S01E01-02-03.mkv", (1, [1, 2, 3])),
    ("Show.S01E01-E02-E03.mkv", (1, [1, 2, 3])),
    ("Show.S01E01.S01E02.mkv", (1, [1, 2])),
    ("Show.S01E05.1080p.WEB-DL.mkv", (1, [5])),
    ("Show.S01E01-1080p.mkv", (1, [1])),                 # a resolution is not an episode
    ("Show - S01E01 - Title (2005).mkv", (1, [1])),
    ("Show.S01E09.S02E01.mkv", (1, [9])),                 # another season is another file
])
def test_parse_episode_marker(name, expected):
    assert srv._parse_episode_marker(name) == expected


def test_parse_episode_marker_without_a_marker():
    assert srv._parse_episode_marker("Inception.2010.mkv") is None


# ── the covered episodes travel with the retry items ─────────────────────────

class _FakePending:
    def __init__(self):
        self.pushed = []

    def push(self, item):
        self.pushed.append(item)


def _double(tmp_path):
    show = tmp_path / "tv" / "Avatar The Last Airbender"
    season = show / "Season 2"
    season.mkdir(parents=True)
    video = season / "Avatar The Last Airbender (2005) - S02E12-E13 - The Serpent's Pass & The Drill.mkv"
    video.write_bytes(b"x")
    return show, video


def test_infer_retry_params_carries_the_extra_episodes(tmp_path):
    _, video = _double(tmp_path)
    out = srv._infer_retry_params(str(video), "")
    assert (out["season"], out["episode"], out["extra_episodes"]) == ("2", "12", [13])


def test_retry_dir_queues_the_double_episode_as_one_item(tmp_path, monkeypatch):
    show, _ = _double(tmp_path)
    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: False)
    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Avatar: The Last Airbender", "dir": str(show)},
        fake_config(tmp_path), pending,
    )
    [item] = pending.pushed
    assert (item["season"], item["episode"], item["extra_episodes"]) == (2, 12, [13])


def test_retry_dir_clears_every_covered_episode_from_a_stale_ledger(tmp_path, monkeypatch):
    show, _ = _double(tmp_path)
    cache = tmp_path / "cache"
    done_path = cache / "shows" / srv._safe_dirname("Avatar: The Last Airbender") / ".done_s02.json"
    done_path.parent.mkdir(parents=True)
    done_path.write_text(json.dumps({"total": 20, "done": [11, 12, 13]}))
    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: False)   # AD track gone
    srv._worker_handle_retry_dir(
        {"title": "Avatar: The Last Airbender", "dir": str(show)},
        fake_config(cache_dir=cache), _FakePending(),
    )
    assert json.loads(done_path.read_text())["done"] == [11]


def test_retry_episode_passes_the_extra_episodes_on(tmp_path, monkeypatch):
    _, video = _double(tmp_path)
    seen = {}

    def fake_process_episode(client, config, video_path, title, season, episode, **kwargs):
        seen.update(kwargs, episode=episode)
        return True, None

    monkeypatch.setattr(srv, "process_episode", fake_process_episode)
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(srv, "_notify_outcome", lambda *a, **k: None)
    srv._worker_handle_retry_episode(
        {"title": "Avatar: The Last Airbender", "path": str(video), "season": 2, "episode": 12,
         "extra_episodes": [13]},
        fake_config(tmp_path), _FakePending(),
    )
    assert seen["episode"] == 12 and seen["extra_episodes"] == [13]


def test_handle_retry_on_a_double_episode_file(tmp_path, monkeypatch):
    _, video = _double(tmp_path)
    pending = _FakePending()
    monkeypatch.setattr(srv, "_get_pending_queue", lambda config: pending)
    monkeypatch.setattr(srv, "Config", types.SimpleNamespace(from_env=lambda: fake_config(tmp_path)))
    handler = srv._HookHandler.__new__(srv._HookHandler)
    handler._respond = lambda code, msg: None
    handler._handle_retry({"path": str(video)})
    assert pending.pushed[0]["extra_episodes"] == [13]
    # An explicit episode= asks for exactly that episode.
    pending.pushed.clear()
    handler._handle_retry({"path": str(video), "episode": "12"})
    assert "extra_episodes" not in pending.pushed[0]


# ── the donor for a double episode is both files, joined ─────────────────────

def _fake_zip_with(monkeypatch, tmp_path, present):
    files = {}
    for ep in present:
        f = tmp_path / f"[S02.E{ep:02d}] Title.mp3"
        f.write_bytes(b"x")
        files[ep] = f

    def fake_extract(zip_path, extract_dir, episode, episode_title=""):
        return files.get(episode)

    monkeypatch.setattr(workflow, "extract_episode", fake_extract)
    return files


def test_single_episode_donor_is_its_own_file(monkeypatch, tmp_path):
    files = _fake_zip_with(monkeypatch, tmp_path, [12])
    assert _episode_donor(tmp_path / "s.zip", tmp_path / "extract", [12], "x") == files[12]


def test_double_episode_donor_joins_both_files_beside_the_extract_dir(monkeypatch, tmp_path):
    files = _fake_zip_with(monkeypatch, tmp_path, [12, 13])
    joined = {}

    def fake_concat(parts, out):
        joined["parts"] = parts
        joined["out"] = out
        return out

    monkeypatch.setattr(workflow, "_concat_audio", fake_concat)
    extract_dir = tmp_path / "season_02" / "entry"
    result = _episode_donor(tmp_path / "s.zip", extract_dir, [12, 13], "x")
    assert result == joined["out"]
    assert joined["parts"] == [files[12], files[13]]
    assert joined["out"] == tmp_path / "season_02" / "entry_multi" / "E12E13.mp3"
    assert extract_dir not in joined["out"].parents             # never inside the extract dir


def test_double_episode_with_a_missing_part_is_refused(monkeypatch, tmp_path):
    _fake_zip_with(monkeypatch, tmp_path, [12])                 # no E13 in this entry
    monkeypatch.setattr(workflow, "_concat_audio", lambda parts, out: pytest.fail("must not join"))
    assert _episode_donor(tmp_path / "s.zip", tmp_path / "extract", [12, 13], "x") is None


def test_process_episode_aligns_the_joined_donor_and_marks_both_done(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Avatar (2005) - S02E12-E13.mkv"
    video.write_bytes(b"x")
    files = _fake_zip_with(monkeypatch, tmp_path, [12, 13])

    class FakeClient:
        def search_shows(self, title):
            return [{"name": "Avatar: The Last Airbender - Season 2 (2006)", "url": "https://av/dl/1"}]

    aligned, marked = [], []
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "_get_cached", lambda client, url, cache_dir, limiter: tmp_path / "s2.zip")
    monkeypatch.setattr(workflow, "_concat_audio", lambda parts, out: out)
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda config, video_path, audio_path, label=None, **kwargs: (aligned.append(audio_path), (True, None))[1])
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda cache, season, ep, *a, **k: marked.append((season, ep)))
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])

    described, reason = process_episode(FakeClient(), config, video, "Avatar: The Last Airbender", 2, 12,
                                        extra_episodes=[13], series_year="2005")
    assert described
    assert aligned == [aligned[0]] and aligned[0].name == "E12E13.mp3"
    assert marked == [(2, 12), (2, 13)]


def test_process_episode_skips_an_entry_missing_the_second_part(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Avatar (2005) - S02E12-E13.mkv"
    video.write_bytes(b"x")
    _fake_zip_with(monkeypatch, tmp_path, [12])

    class FakeClient:
        def search_shows(self, title):
            return [{"name": "Avatar: The Last Airbender - Season 2 (2006)", "url": "https://av/dl/1"}]

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "_get_cached", lambda client, url, cache_dir, limiter: tmp_path / "s2.zip")
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda *a, **k: pytest.fail("half a double episode must never be aligned"))
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])

    described, _ = process_episode(FakeClient(), config, video, "Avatar: The Last Airbender", 2, 12,
                                   extra_episodes=[13], series_year="2005")
    assert not described


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="needs ffmpeg")
def test_concat_audio_joins_real_files_and_reuses_the_result(tmp_path):
    parts = []
    for i, freq in enumerate((440, 880)):
        part = tmp_path / f"[S02.E1{i}] The Serpent's Pass.mp3"     # an apostrophe, as in the wild
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency={freq}:duration=2", "-c:a", "libmp3lame", "-q:a", "4", str(part)],
                       check=True)
        parts.append(part)
    out = tmp_path / "multi" / "E10E11.mp3"
    result = _concat_audio(parts, out)
    assert result == out and out.exists()
    assert abs(workflow._audio_duration(out) - 4.0) < 0.5
    assert not list(out.parent.glob("*.list")) and not list(out.parent.glob("*.part"))
    stamp = out.stat().st_mtime_ns
    assert _concat_audio(parts, out) == out and out.stat().st_mtime_ns == stamp   # reused, not rebuilt


# ── extra sources cover a multi-episode file too ─────────────────────────────

class _FakeSource:
    """An extra source in the shape LivingAudio has: 0 or 1 candidate per episode."""

    def __init__(self, have, root):
        self.have = have
        self.root = root
        self.asked = []
        self.closed = False

    def is_configured(self):
        return True

    def episode_candidates(self, cache_dir, series_title, season, episode):
        self.asked.append(episode)
        if episode in self.have:
            path = self.root / f"la_s{season:02d}e{episode:02d}.mp3"
            path.write_bytes(b"x")
            yield path

    def close(self):
        self.closed = True


def test_extra_source_joins_every_covered_episode(monkeypatch, tmp_path):
    source = _FakeSource({12, 13}, tmp_path)
    monkeypatch.setattr(workflow, "_concat_audio", lambda parts, out: out)
    donor = workflow._extra_source_multi_donor(
        source, tmp_path / "cache", "Avatar: The Last Airbender", 2, [12, 13], "x",
    )
    assert source.asked == [12, 13]
    assert donor == tmp_path / "cache" / "extra_multi" / workflow._safe_dirname(
        "Avatar: The Last Airbender") / "s02E12E13.mp3"


def test_extra_source_missing_a_part_offers_nothing(monkeypatch, tmp_path):
    source = _FakeSource({12}, tmp_path)                     # no E13
    monkeypatch.setattr(workflow, "_concat_audio",
                        lambda parts, out: pytest.fail("must not join a partial set"))
    assert workflow._extra_source_multi_donor(
        source, tmp_path / "cache", "Avatar: The Last Airbender", 2, [12, 13], "x",
    ) is None


def test_multi_episode_falls_through_to_an_extra_source(monkeypatch, tmp_path):
    """AudioVault has no entry; the extra source's joined donor is used."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Avatar (2005) - S02E12-E13.mkv"
    video.write_bytes(b"x")
    source = _FakeSource({12, 13}, tmp_path)
    aligned, marked = [], []

    class FakeClient:
        def search_shows(self, title):
            return []

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "_concat_audio", lambda parts, out: out)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [source])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda config, video_path, audio_path, label=None, **kwargs:
                            (aligned.append(audio_path), (True, None))[1])
    monkeypatch.setattr(workflow, "_mark_episode_done",
                        lambda cache, season, ep, *a, **k: marked.append((season, ep)))

    described, _ = process_episode(FakeClient(), config, video, "Avatar: The Last Airbender",
                                   2, 12, extra_episodes=[13], series_year="2005")
    assert described
    assert [p.name for p in aligned] == ["s02E12E13.mp3"]
    assert marked == [(2, 12), (2, 13)]
    assert source.closed


def test_a_show_audiovault_never_heard_of_still_reaches_the_extra_source(monkeypatch, tmp_path):
    """An empty AudioVault search used to return before the extra sources ran,
    so a private provider could only ever cover shows AudioVault also had."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Show - S01E05.mkv"
    video.write_bytes(b"x")
    source = _FakeSource({5}, tmp_path)
    aligned = []

    class NoResults:
        def search_shows(self, title):
            return []

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [source])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda config, video_path, audio_path, label=None, **kwargs:
                            (aligned.append(audio_path), (True, None))[1])
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)

    described, _ = process_episode(NoResults(), config, video, "Show", 1, 5)
    assert described and len(aligned) == 1 and source.closed


def test_a_season_with_no_audiovault_entry_still_reaches_the_extra_source(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Show - S09E01.mkv"
    video.write_bytes(b"x")
    source = _FakeSource({1}, tmp_path)
    aligned = []

    class OnlyOtherSeasons:
        def search_shows(self, title):
            return [{"name": "Show - Season 1 (2001)", "url": "https://av/dl/1"}]

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [source])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda config, video_path, audio_path, label=None, **kwargs:
                            (aligned.append(audio_path), (True, None))[1])
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)

    described, _ = process_episode(OnlyOtherSeasons(), config, video, "Show", 9, 1)
    assert described and len(aligned) == 1


def test_a_movie_audiovault_lacks_still_reaches_the_extra_source(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Film (2020).mkv"
    video.write_bytes(b"x")
    ad = tmp_path / "la_film.mp3"
    ad.write_bytes(b"x")
    aligned = []

    class NoResults:
        def search_movies(self, title):
            return []

    class MovieSource(_FakeSource):
        def movie_candidates(self, cache_dir, movie_title, movie_year):
            yield ad

    source = MovieSource(set(), tmp_path)
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [source])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda config, video_path, audio_path, label=None, **kwargs:
                            (aligned.append(audio_path), (True, None))[1])

    described, _ = workflow.process_movie(NoResults(), config, video, "Film", "2020")
    assert described and aligned == [ad] and source.closed


def test_no_extra_sources_configured_still_reports_no_match(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Show - S01E05.mkv"
    video.write_bytes(b"x")

    class NoResults:
        def search_shows(self, title):
            return []

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda *a, **k: pytest.fail("nothing to align"))

    described, reason = process_episode(NoResults(), config, video, "Show", 1, 5)
    assert not described and reason is None
