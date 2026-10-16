"""A retry asks Sonarr for the episode's title, and Sonarr is believed only when sure.

Law & Order: SVU S22E13 (2026-10-15): a retry of
"Law.and.Order.SVU.S22E13.1080p.WEB.H264-STRONTiUM.mkv" had no title (no .nfo,
none in the name), so AudioVault's "22.13 Trick-Rolled at the Moulin.mp3",
the right recording on a 99.6 % straight line at 22 % similarity, could not be
corroborated and was refused. Sonarr's hook would have sent the title; these
pin asking Sonarr for it, and every way that answer is refused.

The fake below answers with the shapes Sonarr v3 really returns (checked
against the live server that day): series carry a ``path``; episode files a
``relativePath`` below the series folder; episodes a ``seasonNumber``,
``episodeNumber``, ``title`` and an ``episodeFileId`` that is 0 when the
episode has no file.
"""

import json
import logging
import zipfile

import pytest

import describarr.sonarr as sonarr
import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.sonarr import SonarrTitles
from describarr.workflow import process_episode

KEY = "0123456789abcdef0123456789abcdef"
SVU = "/media/TV shows/Law & Order - Special Victims Unit"
S22E13 = "Law.and.Order.SVU.S22E13.1080p.WEB.H264-STRONTiUM.mkv"


class FakeResponse:
    def __init__(self, status, data=None, broken=False):
        self.status_code, self._data, self._broken = status, data, broken

    def json(self):
        if self._broken:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._data


class FakeSonarr:
    """Sonarr v3's /series, /episodefile and /episode, with switchable failures."""

    def __init__(self):
        self.series = [{"id": 26, "title": "Law & Order: Special Victims Unit", "path": SVU, "year": 1999}]
        self.files = {26: [{"id": 4276, "seriesId": 26, "seasonNumber": 22,
                            "relativePath": f"Season 22/{S22E13}", "path": f"{SVU}/Season 22/{S22E13}"}]}
        self.episodes = {26: [
            {"id": 1, "seriesId": 26, "seasonNumber": 22, "episodeNumber": 13,
             "title": "Trick-Rolled at the Moulin (1)", "episodeFileId": 4276, "hasFile": True},
            {"id": 2, "seriesId": 26, "seasonNumber": 22, "episodeNumber": 14,
             "title": "Post-Graduate Psychopath", "episodeFileId": 0, "hasFile": False},
        ]}
        self.calls = []
        self.leaks = []           # requests that carried the key outside the header
        self.untimed = []         # requests made without a timeout
        self.fail = None          # None, or a callable(path) -> FakeResponse | raises

    def __call__(self, url, params=None, headers=None, timeout=None):
        # Every request is counted before anything is judged: the code under
        # test swallows exceptions, so an assertion raised in here would hide
        # the very request it objects to.
        path = url.split("/api/v3/", 1)[1]
        self.calls.append(path)
        if KEY in url or KEY in json.dumps(params or {}):
            self.leaks.append(path)
        if not timeout:
            self.untimed.append(path)
        if (headers or {}).get("X-Api-Key") != KEY:
            return FakeResponse(401, {"error": "Unauthorized"})     # what Sonarr answers
        if self.fail is not None:
            response = self.fail(path)
            if response is not None:
                return response
        if path == "series":
            return FakeResponse(200, self.series)
        series_id = (params or {}).get("seriesId")
        if path == "episodefile":
            return FakeResponse(200, self.files.get(series_id, []))
        if path == "episode":
            return FakeResponse(200, self.episodes.get(series_id, []))
        return FakeResponse(404, {"message": "NotFound"})

    def count(self, path):
        return self.calls.count(path)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def fake():
    fake = FakeSonarr()
    yield fake
    assert fake.leaks == [] and fake.untimed == []


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def titles(fake, clock):
    return SonarrTitles("http://sonarr:8989/", KEY, clock=clock, http_get=fake)


# ── what Sonarr is asked, and when it is believed ────────────────────────────

def test_the_title_of_the_episode_sonarr_maps_to_the_file(titles):
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == "Trick-Rolled at the Moulin (1)"


def test_the_series_is_found_whatever_the_mount_is_called(titles):
    # describarr reaches the library as /tv while Sonarr says /media/TV shows.
    path = f"/tv/Law & Order - Special Victims Unit/Season 22/{S22E13}"
    assert titles.episode_title(path, 22, 13) == "Trick-Rolled at the Moulin (1)"


def test_sonarr_on_windows_is_read_too(fake, titles):
    fake.series[0]["path"] = r"D:\TV\Law & Order - Special Victims Unit"
    fake.files[26][0]["relativePath"] = rf"Season 22\{S22E13}"
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == "Trick-Rolled at the Moulin (1)"


def test_a_season_folder_never_names_a_series(fake, titles):
    fake.series.append({"id": 99, "title": "Odd", "path": "/media/TV shows/Season 22"})
    fake.files[99] = [{"id": 1, "relativePath": S22E13}]
    fake.episodes[99] = [{"seasonNumber": 22, "episodeNumber": 13, "title": "Wrong", "episodeFileId": 1}]
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == "Trick-Rolled at the Moulin (1)"


def test_two_series_in_folders_of_one_name_get_no_guess(fake, titles):
    fake.series.append({"id": 27, "title": "Another", "path": "/media/Anime/Law & Order - Special Victims Unit"})
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""
    assert fake.count("episodefile") == 0


def test_one_name_in_two_seasons_is_told_apart_by_its_folder(fake, titles):
    fake.files[26] = [{"id": 1, "relativePath": "Season 1/Episode 01.mkv"},
                      {"id": 2, "relativePath": "Season 2/Episode 01.mkv"}]
    fake.episodes[26] = [
        {"seasonNumber": 1, "episodeNumber": 1, "title": "Payback", "episodeFileId": 1},
        {"seasonNumber": 2, "episodeNumber": 1, "title": "Wrath", "episodeFileId": 2},
    ]
    assert titles.episode_title(f"{SVU}/Season 2/Episode 01.mkv", 2, 1) == "Wrath"
    assert titles.episode_title(f"{SVU}/Season 1/Episode 01.mkv", 1, 1) == "Payback"


def test_one_name_that_its_folder_cannot_settle_gets_no_guess(fake, titles):
    fake.files[26] = [{"id": 1, "relativePath": "Season 1/Episode 01.mkv"},
                      {"id": 2, "relativePath": "Season 2/Episode 01.mkv"}]
    fake.episodes[26] = [
        {"seasonNumber": 1, "episodeNumber": 1, "title": "Payback", "episodeFileId": 1},
        {"seasonNumber": 2, "episodeNumber": 1, "title": "Wrath", "episodeFileId": 2},
    ]
    # Moved since Sonarr last looked: neither listed path is where it is now.
    assert titles.episode_title(f"{SVU}/Season 3/Episode 01.mkv", 1, 1) == ""


def test_a_file_sonarr_numbers_otherwise_gets_no_title(fake, titles):
    # Deep Space Nine's AIU season 4 is numbered one behind TVDB: the file says
    # E25, Sonarr maps it to E24. Searching E25 with E24's title would put
    # another episode's recording first and corroborate it.
    fake.episodes[26][0]["episodeNumber"] = 12
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""


def test_a_double_episode_takes_its_first_title_only_when_the_numbers_agree(fake, titles):
    double = "Show.S02E12E13.mkv"
    fake.files[26] = [{"id": 7, "relativePath": f"Season 2/{double}"}]
    fake.episodes[26] = [
        {"seasonNumber": 2, "episodeNumber": 13, "title": "Second", "episodeFileId": 7},
        {"seasonNumber": 2, "episodeNumber": 12, "title": "First", "episodeFileId": 7},
    ]
    assert titles.episode_title(f"{SVU}/Season 2/{double}", 2, 12) == "First"
    # Sonarr has the file as E13-E14 while its name says E12-E13: no title.
    fake.episodes[26][0]["episodeNumber"], fake.episodes[26][1]["episodeNumber"] = 14, 13
    titles_fresh = SonarrTitles("http://sonarr:8989", KEY, http_get=fake)
    assert titles_fresh.episode_title(f"{SVU}/Season 2/{double}", 2, 12) == ""


def test_a_file_sonarr_does_not_have_gets_no_title(titles):
    assert titles.episode_title(f"{SVU}/Season 22/Law.and.Order.SVU.S22E99.mkv", 22, 99) == ""
    assert titles.episode_title("/media/TV shows/Not In Sonarr/Season 1/x.S01E01.mkv", 1, 1) == ""


# ── caching, misses and outages ──────────────────────────────────────────────

def test_a_whole_season_asks_sonarr_once(fake, titles):
    for _ in range(20):
        titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
    assert (fake.count("series"), fake.count("episodefile"), fake.count("episode")) == (1, 1, 1)


def test_answers_are_reused_for_ten_minutes(fake, titles, clock):
    titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
    clock.now += sonarr.CACHE_SECONDS - 1
    titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
    assert fake.count("series") == 1
    clock.now += 2
    titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
    assert (fake.count("series"), fake.count("episodefile")) == (2, 2)


def test_a_file_imported_since_is_found_once_the_cache_is_a_minute_old(fake, titles, clock):
    titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
    new = "Law.and.Order.SVU.S22E14.mkv"
    fake.files[26].append({"id": 4277, "relativePath": f"Season 22/{new}"})
    fake.episodes[26][1]["episodeFileId"] = 4277
    # Fetched a moment ago: a miss now is a miss, not a reason to ask again.
    assert titles.episode_title(f"{SVU}/Season 22/{new}", 22, 14) == ""
    assert fake.count("episodefile") == 1
    clock.now += sonarr.MISS_REFRESH_SECONDS
    assert titles.episode_title(f"{SVU}/Season 22/{new}", 22, 14) == "Post-Graduate Psychopath"
    assert fake.count("episodefile") == 2


def test_unknown_files_cost_one_fresh_look_a_minute(fake, titles, clock):
    titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
    for n in range(10):
        titles.episode_title(f"{SVU}/Season 22/Unknown.S22E{n + 50}.mkv", 22, n + 50)
    assert fake.count("episodefile") == 1
    clock.now += sonarr.MISS_REFRESH_SECONDS + 1
    for n in range(10):
        titles.episode_title(f"{SVU}/Season 22/Unknown.S22E{n + 70}.mkv", 22, n + 70)
    assert fake.count("episodefile") == 2
    clock.now += sonarr.MISS_REFRESH_SECONDS + 1
    titles.episode_title(f"{SVU}/Season 22/Unknown.S22E99.mkv", 22, 99)
    assert fake.count("episodefile") == 3


def test_a_failing_fresh_look_is_tried_once_per_cooldown(fake, titles, clock):
    titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
    clock.now += sonarr.MISS_REFRESH_SECONDS + 1
    fake.fail = lambda path: FakeResponse(500, {}) if path == "episodefile" else None
    for step in range(5):
        assert titles.episode_title(f"{SVU}/Season 22/Unknown.S22E{step + 50}.mkv", 22, step + 50) == ""
        clock.now += sonarr.MISS_REFRESH_SECONDS + 1
    # One failed refresh, then nothing until the cooldown ends; the file the
    # cache already knew is still answered from it meanwhile.
    assert fake.count("episodefile") == 2
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == "Trick-Rolled at the Moulin (1)"
    fake.fail = None
    clock.now += sonarr.FAILURE_COOLDOWN_SECONDS
    titles.episode_title(f"{SVU}/Season 22/Unknown.S22E99.mkv", 22, 99)
    assert fake.count("episodefile") == 3


def test_a_disagreement_or_an_ambiguity_never_earns_a_fresh_look(fake, titles):
    fake.episodes[26][0]["episodeNumber"] = 12
    for _ in range(5):
        assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""
    assert fake.count("episodefile") == 1


@pytest.mark.parametrize("failure", [
    lambda path: (_ for _ in ()).throw(ConnectionError("HTTPConnectionPool(host='sonarr')")),
    lambda path: (_ for _ in ()).throw(TimeoutError("timed out")),
    lambda path: FakeResponse(401, {"error": "Unauthorized"}),
    lambda path: FakeResponse(500, {"message": "boom"}),
    lambda path: FakeResponse(200, broken=True),
    lambda path: FakeResponse(200, {"not": "a list"}),
])
def test_a_failing_sonarr_gives_no_title_and_never_raises(fake, titles, failure):
    fake.fail = failure
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""


def test_an_outage_is_not_asked_again_until_its_cooldown_ends(fake, titles, clock, caplog):
    fake.fail = lambda path: FakeResponse(503, {"message": "down"})
    with caplog.at_level(logging.WARNING, logger="describarr.sonarr"):
        titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
        # Another show inside the cooldown: no request at all.
        titles.episode_title("/media/TV shows/Heartland/Season 1/Heartland.S01E01.mkv", 1, 1)
    assert fake.calls == ["series"]
    assert len([r for r in caplog.records if "HTTP 503" in r.getMessage()]) == 1
    fake.fail = None
    clock.now += sonarr.FAILURE_COOLDOWN_SECONDS + 1
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == "Trick-Rolled at the Moulin (1)"


def test_one_series_failing_rests_only_that_series(fake, titles, clock):
    fake.series.append({"id": 30, "title": "Heartland", "path": "/media/TV shows/Heartland (2007) (CA)"})
    fake.files[30] = [{"id": 9, "relativePath": "Season 1/Heartland.S01E01.mkv"}]
    fake.episodes[30] = [{"seasonNumber": 1, "episodeNumber": 1, "title": "Coming Home", "episodeFileId": 9}]
    fake.fail = lambda path: FakeResponse(500, {}) if path == "episodefile" else None
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""
    fake.fail = None
    # Inside SVU's cooldown: SVU is not asked again, Heartland is.
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""
    assert titles.episode_title("/tv/Heartland (2007) (CA)/Season 1/Heartland.S01E01.mkv", 1, 1) == "Coming Home"
    assert fake.count("episodefile") == 2
    clock.now += sonarr.FAILURE_COOLDOWN_SECONDS + 1
    assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == "Trick-Rolled at the Moulin (1)"


def test_a_refused_key_says_so(fake, titles, caplog):
    fake.fail = lambda path: FakeResponse(401, {"error": "Unauthorized"})
    with caplog.at_level(logging.WARNING, logger="describarr.sonarr"):
        titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13)
    assert any("401" in r.getMessage() and "key" in r.getMessage() for r in caplog.records)


def test_the_key_is_never_logged(fake, clock, caplog):
    def leaky(url, params=None, headers=None, timeout=None):
        raise ConnectionError(f"failed {url} with headers {headers}")

    titles = SonarrTitles("http://sonarr:8989", KEY, clock=clock, http_get=leaky)
    with caplog.at_level(logging.DEBUG):
        assert titles.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""
    assert caplog.records
    assert not any(KEY in r.getMessage() or (r.exc_text and KEY in r.exc_text) for r in caplog.records)


# ── the shared client and the matcher ────────────────────────────────────────

@pytest.fixture
def shared(monkeypatch, fake):
    sonarr.reset()
    monkeypatch.setenv("SONARR_URL", "http://sonarr:8989")
    monkeypatch.setenv("SONARR_API_KEY", KEY)
    monkeypatch.setattr(sonarr.requests, "get", fake, raising=False)
    yield fake
    sonarr.reset()


def test_nothing_is_asked_unless_both_settings_are_there(monkeypatch, fake):
    sonarr.reset()
    monkeypatch.setattr(sonarr.requests, "get", fake, raising=False)
    monkeypatch.delenv("SONARR_URL", raising=False)
    monkeypatch.setenv("SONARR_API_KEY", KEY)
    assert sonarr.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""
    monkeypatch.setenv("SONARR_URL", "http://sonarr:8989")
    monkeypatch.delenv("SONARR_API_KEY")
    assert sonarr.episode_title(f"{SVU}/Season 22/{S22E13}", 22, 13) == ""
    assert fake.calls == []
    sonarr.reset()


def _svu_walk(monkeypatch, tmp_path, similarity=22.0):
    """process_episode over a Season 22 pack with the real gate; the recording
    for E13 aligns on a perfect native-rate line at *similarity*."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    pack = "Law & Order - Special Victims Unit - Season 22 (2020)"
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in ("22.12 In the Year We All Fell Down.mp3", "22.13 Trick-Rolled at the Moulin.mp3",
                     "22.14 Post-Graduate Psychopath.mp3"):
            zf.writestr(f"{pack}/{name}", name.encode())
    video = tmp_path / "TV shows" / "Law & Order - Special Victims Unit" / "Season 22" / S22E13
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir(parents=True)
    report.write_text(f"Input file similarity: {similarity}%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": similarity, "median_rate_pct": 0.0,
        "segments": [{"rate_pct": 0.0, "video_start_sec": 2.0, "video_end_sec": 2467.0,
                      "audio_start_sec": 0.0, "audio_end_sec": 2465.0}],
    }))
    combined = tmp_path / "out" / "ad_ep.mkv"
    combined.parent.mkdir()
    combined.write_bytes(b"z")

    class Client:
        def search_shows(self, title):
            return [{"name": pack, "url": "https://av/pack"}]

    aligned, published = [], []

    def align(video_path, audio_path, *a, **k):
        aligned.append(audio_path.name)
        return AlignResult(combined, report, None, returncode=0, aligned_english=True)

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: None)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "align", align)
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: 2469.0)
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: published.append(a))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: True)
    described, _ = process_episode(Client(), config, video, "Law & Order: Special Victims Unit", 22, 13)
    decisions = json.loads((config.cache_dir / "decisions.json").read_text())
    return described, aligned, published, decisions


def test_s22e13_with_sonarrs_title_publishes_through_the_corroborated_rescue(monkeypatch, tmp_path, shared):
    described, aligned, published, decisions = _svu_walk(monkeypatch, tmp_path)
    assert described and len(published) == 1
    assert aligned == ["22.13 Trick-Rolled at the Moulin.mp3"]
    assert decisions[-1]["path"] == "corroborated-rescue"
    assert decisions[-1]["episode_title"] == "Trick-Rolled at the Moulin (1)"
    assert decisions[-1]["title_corroborated"] is True


def test_s22e13_without_sonarr_is_refused_as_before(monkeypatch, tmp_path):
    sonarr.reset()
    monkeypatch.delenv("SONARR_URL", raising=False)
    monkeypatch.delenv("SONARR_API_KEY", raising=False)
    described, aligned, published, decisions = _svu_walk(monkeypatch, tmp_path)
    assert not described and not published
    assert aligned[0] == "22.13 Trick-Rolled at the Moulin.mp3"
    first = next(d for d in decisions if d.get("donor", "").endswith("22.13 Trick-Rolled at the Moulin.mp3"))
    assert first["episode_title"] is None and first["title_corroborated"] is False


def test_a_title_from_the_hook_is_never_asked_of_sonarr(monkeypatch, tmp_path, shared):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Law & Order - Special Victims Unit" / "Season 22" / S22E13
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")

    class NoShow:
        def search_shows(self, title):
            return []

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    process_episode(NoShow(), config, video, "Law & Order: Special Victims Unit", 22, 13,
                    episode_title="Trick-Rolled at the Moulin (1)")
    assert shared.calls == []


def test_sonarrs_title_comes_before_the_nfo(monkeypatch, tmp_path, shared, caplog):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Law & Order - Special Victims Unit" / "Season 22" / S22E13
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")
    video.with_suffix(".nfo").write_text(
        '<?xml version="1.0" encoding="utf-8"?><episodedetails><title>Stale Title</title>'
        "<season>22</season><episode>13</episode></episodedetails>", encoding="utf-8")

    class NoShow:
        def search_shows(self, title):
            return []

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    with caplog.at_level(logging.INFO, logger="describarr.workflow"):
        process_episode(NoShow(), config, video, "Law & Order: Special Victims Unit", 22, 13)
    assert any("'Trick-Rolled at the Moulin (1)' (from Sonarr's records)" in r.getMessage()
               for r in caplog.records)
