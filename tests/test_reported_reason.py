"""Which reason a refused episode reports, and AudioVault's "[US]" title suffix.

Phineas and Ferb (2026-10-16): an episode whose recording, the one named for
it, was refused went on to long shots, another episode's recording by number
and its neighbours, and the episode then reported the LAST one's verdict.
S01E07 read "AD audio is 2.33x the video duration" (a 21-minute double tried
by number) when the right recording had been refused for running 10.3 % off;
S01E15 read "No obvious cause from energy analysis." That reason is what the
request app relays and the notification quotes. The walk now reports the first
refusal in rank order. And AudioVault's Season 3 pack ends six titles with
"[US]" ("Doofapus [US]"), which no reading dropped.
"""

import zipfile

import pytest

import describarr.server as srv
import describarr.workflow as workflow
from describarr.aligner import EngineFailure
from describarr.config import Config
from describarr.titles import donor_names_episode, donor_title_readings
from describarr.workflow import Refusal, process_episode, process_movie
from conftest import fake_config

SHOW = "Phineas and Ferb"
FOLDER = "Phineas and Ferb - Season 1 (2007)"
RIGHT = "[S01.E01b] Phineas and Ferb - Candace Loses Her Head.mp3"      # named for library S01E07
BY_NUMBER = "[S01.E07] Phineas and Ferb - It's About Time.mp3"           # library S01E07's number
OTHERS = ["[S01.E06a] Phineas and Ferb - Get that Bigfoot Outta My Face.mp3",
          "[S01.E08a] Phineas and Ferb - Jerk de Soleil.mp3"]
R1 = "similarity 57.4% (coverage 99.9%, stable trunk 0.0%, median rate 0.00%, sync_ok=True) — no trusted sync signal"
R2 = "AD audio is 2.33× the video duration (21.6 min vs 9.3 min) — likely a full-season AD against a single episode video."


class _Elsewhere:
    label = "Elsewhere"

    def __init__(self, recording):
        self.recording = recording

    def episode_candidates(self, cache_dir, series_title, season, episode, episode_title=""):
        return [self.recording]

    def movie_candidates(self, cache_dir, title, year):
        return [self.recording]

    def close(self):
        pass


def _walk(monkeypatch, tmp_path, verdicts, *, extra=None, extra_bytes=None, extra_verdict=None):
    """process_episode for library S01E07 over a small real-named pack; *verdicts*
    maps a donor name to what the gate says (a reason string or an EngineFailure)."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in [RIGHT, BY_NUMBER, *OTHERS]:
            zf.writestr(f"{FOLDER}/{name}", name.encode())
    video = tmp_path / SHOW / "Season 1" / "Phineas.and.Ferb.S01E07.Candace.Loses.Her.Head.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    sources = []
    if extra:
        recording = tmp_path / "elsewhere" / extra
        recording.parent.mkdir()
        recording.write_bytes(extra_bytes if extra_bytes is not None else b"another recording")
        sources = [_Elsewhere(recording)]
        verdicts = {**verdicts, extra: extra_verdict}

    class Client:
        def search_shows(self, query):
            return [{"name": FOLDER, "url": "https://av/pack"}]

    tried = []

    def gate(config, video_path, audio_path, label=None, **_):
        tried.append(audio_path.name)
        return False, verdicts.get(audio_path.name, "similarity 2.0% (coverage 100.0%) — no trusted sync signal")

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: None)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: sources)
    monkeypatch.setattr(workflow, "_align_and_keep", gate)
    described, reason = process_episode(Client(), config, video, SHOW, 1, 7, series_year="2007",
                                        episode_title="Candace Loses Her Head")
    return described, reason, tried


# ── the reason a refused episode reports ─────────────────────────────────────

def test_the_named_recordings_refusal_is_reported_not_a_long_shots(monkeypatch, tmp_path):
    described, reason, tried = _walk(monkeypatch, tmp_path, {RIGHT: R1, BY_NUMBER: R2})
    assert not described and tried[:2] == [RIGHT, BY_NUMBER]
    assert isinstance(reason, Refusal) and str(reason) == R1


def test_a_long_shot_failing_in_the_engine_does_not_make_it_an_error(monkeypatch, tmp_path):
    described, reason, _ = _walk(monkeypatch, tmp_path, {
        RIGHT: R1, BY_NUMBER: EngineFailure("alignment failed (describealaign exit 1)")})
    assert str(reason) == R1 and not isinstance(reason, EngineFailure)
    assert srv._episode_outcome(described, reason) == "no_match"


def test_after_an_engine_failure_the_first_refusal_is_reported(monkeypatch, tmp_path):
    described, reason, tried = _walk(
        monkeypatch, tmp_path, {RIGHT: EngineFailure("alignment failed (describealaign exit 1)"), BY_NUMBER: R1},
        extra="7.07 Candace Loses Her Head.mp3", extra_verdict=R2)
    assert tried[:3] == [RIGHT, BY_NUMBER, "7.07 Candace Loses Her Head.mp3"]
    assert str(reason) == R1


def test_when_every_candidate_failed_in_the_engine_it_is_still_an_error(monkeypatch, tmp_path):
    crash = "alignment failed (describealaign exit 1)"
    described, reason, _ = _walk(monkeypatch, tmp_path, {
        RIGHT: EngineFailure(crash), BY_NUMBER: EngineFailure(crash),
        OTHERS[0]: EngineFailure(crash), OTHERS[1]: EngineFailure(crash)})
    assert isinstance(reason, EngineFailure)
    assert srv._episode_outcome(described, reason) == "error"


def test_a_skipped_identical_copy_changes_nothing(monkeypatch, tmp_path):
    # The extra source offers the same bytes as the refused recording: skipped,
    # never aligned, and the reason stays the first refusal's.
    described, reason, tried = _walk(monkeypatch, tmp_path, {RIGHT: R1, BY_NUMBER: R2},
                                     extra="7.07 Candace Loses Her Head.mp3", extra_bytes=RIGHT.encode())
    assert "7.07 Candace Loses Her Head.mp3" not in tried
    assert str(reason) == R1 and "Elsewhere" in reason.same


def test_every_refusing_source_is_still_counted(monkeypatch, tmp_path):
    described, reason, _ = _walk(monkeypatch, tmp_path, {RIGHT: R1, BY_NUMBER: R2},
                                 extra="7.07 Candace Loses Her Head.mp3", extra_verdict=R2)
    assert str(reason) == R1
    assert reason.sources.count("AudioVault") >= 2 and "Elsewhere" in reason.sources


def test_a_film_reports_its_first_refusal(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Movie (2020).mkv"
    video.write_bytes(b"x")
    vault = tmp_path / "vault.mp3"
    vault.write_bytes(b"one recording")
    elsewhere = tmp_path / "elsewhere.mp3"
    elsewhere.write_bytes(b"another recording")

    class Client:
        def search_movies(self, title):
            return [{"name": "Movie (2020)", "url": "https://av/dl/1"}]

    verdicts = {"vault.mp3": R1, "elsewhere.mp3": R2}
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_movie", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: vault)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [_Elsewhere(elsewhere)])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda config, video_path, audio_path, **_: (False, verdicts[audio_path.name]))
    described, reason = process_movie(Client(), config, video, "Movie", "2020")
    assert not described and str(reason) == R1


def test_a_refusal_is_not_remembered_as_nothing_out_there(tmp_path, monkeypatch):
    """The cache that makes a rescan skip an episode holds only walks that found
    no recording at all; a refused one (now reported by its first refusal) must
    not land there, nor in the daily-limit retry queue."""
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x")
    config = fake_config(tmp_path)
    outcomes = []
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(srv, "process_episode", lambda *a, **k: (False, Refusal(R1, ("AudioVault",))))
    monkeypatch.setattr(srv, "_notify_outcome",
                        lambda config, label, outcome, reason=None, path=None, group=None: outcomes.append(outcome))
    pushed = []

    class Pending:
        def push(self, item):
            pushed.append(item)

    misses = []
    monkeypatch.setattr(srv.NoMatchCache, "record_miss", lambda self, key, video_path: misses.append(key))
    srv._worker_handle_retry_episode({"title": "Show", "path": str(video), "season": 1, "episode": 1},
                                     config, Pending())
    assert outcomes == ["no_match"]
    assert misses == []
    assert not srv._get_retry_queue(config).load() and not pushed
    # The control: a walk that found nothing at all IS remembered, so the hook above is live.
    monkeypatch.setattr(srv, "process_episode", lambda *a, **k: (False, None))
    srv._worker_handle_retry_episode({"title": "Show", "path": str(video), "season": 1, "episode": 1},
                                     config, Pending())
    assert misses == [srv.NoMatchCache.key(1, 1)]


# ── AudioVault's "[US]" ──────────────────────────────────────────────────────

SEASON_3_NAMES = [
    '[S03.E01a] Phineas & Ferb - Run, Candace, Run.mp3',
    '[S03.E01b] Phineas & Ferb - Last Train to Bustville.mp3',
    '[S03.E02a] Phineas & Ferb - The Great Indoors.mp3',
    '[S03.E02b] Phineas & Ferb - Canderemy.mp3',
    '[S03.E03a] Phineas & Ferb - The Belly of the Beast.mp3',
    '[S03.E03b] Phineas & Ferb - Moon Farm.mp3',
    "[S03.E04] Phineas & Ferb - Phineas' Birthday Clip-O-Rama!.mp3",
    '[S03.E05a] Phineas & Ferb - Ask a Foolish Question.mp3',
    '[S03.E05b] Phineas & Ferb - Misperceived Monotreme.mp3',
    '[S03.E06a] Phineas & Ferb - Candace Disconnected.mp3',
    '[S03.E06b] Phineas & Ferb - Magic Carpet Ride.mp3',
    '[S03.E07a] Phineas & Ferb - Bad Hair Day.mp3',
    '[S03.E07b] Phineas & Ferb - Meatloaf Surprise.mp3',
    '[S03.E08a] Phineas & Ferb - Tri-Stone Area.mp3',
    '[S03.E08b] Phineas & Ferb - Doof Dynasty.mp3',
    '[S03.E09a] Phineas & Ferb - Phineas and Ferb Interrupted.mp3',
    '[S03.E09b] Phineas & Ferb - A Real Boy.mp3',
    '[S03.E10a] Phineas & Ferb - Mommy Can You Hear Me.mp3',
    '[S03.E10b] Phineas & Ferb - Road Trip.mp3',
    '[S03.E11a] Phineas & Ferb - Skiddley Whiffers.mp3',
    '[S03.E11b] Phineas & Ferb - Tour de Ferb.mp3',
    '[S03.E12] Phineas & Ferb - My Fair Goalie.mp3',
    '[S03.E13a] Phineas & Ferb - Perry the Actorpus.mp3',
    '[S03.E13b] Phineas & Ferb - Bullseye!.mp3',
    "[S03.E14a] Phineas & Ferb - That's the Spirit [US].mp3",
    '[S03.E14b] Phineas & Ferb - The Curse of Candace [US].mp3',
    '[S03.E15a] Phineas & Ferb - Escape from Phineas Tower.mp3',
    '[S03.E15b] Phineas & Ferb - The Remains of the Platypus.mp3',
    '[S03.E16a] Phineas & Ferb - Ferb Latin.mp3',
    '[S03.E16b] Phineas & Ferb - Lotsa Latkes.mp3',
    '[S03.E17a] Phineas & Ferb - A Phineas and Ferb Family Christmas.mp3',
    '[S03.E18a] Phineas & Ferb - What a Croc!.mp3',
    '[S03.E18b] Phineas & Ferb - Ferb TV.mp3',
    "[S03.E19a] Phineas & Ferb - Mom's in the House.mp3",
    '[S03.E19b] Phineas & Ferb - Minor Monogram.mp3',
    '[S03.E20] Phineas & Ferb - Excaliferb.mp3',
    '[S03.E21a] Phineas & Ferb - Monster from the Id.mp3',
    '[S03.E21b] Phineas & Ferb - Gi-Ants.mp3',
    '[S03.E22a] Phineas & Ferb - Agent Doof.mp3',
    '[S03.E22b] Phineas & Ferb - Phineas and Ferb and the Temple of Juatchadoon.mp3',
    '[S03.E24a] Phineas & Ferb - Quietest Day Ever.mp3',
    '[S03.E24b] Phineas & Ferb - Bully Bromance Breakup.mp3',
    '[S03.E25a] Phineas & Ferb - The Doonkelberry Imperative.mp3',
    '[S03.E25b] Phineas & Ferb - Buford Confidential.mp3',
    '[S03.E26a] Phineas & Ferb - Sleepwalk Surprise.mp3',
    '[S03.E26b] Phineas & Ferb - Sci-fi Pie Fly.mp3',
    '[S03.E27] Phineas & Ferb - Meapless in Seattle.mp3',
    '[S03.E29a] Phineas & Ferb - Sipping with the Enemy! [US].mp3',
    '[S03.E29b] Phineas & Ferb - Tri State Treasure, Boot of Secrets [US].mp3',
    '[S03.E30a] Phineas & Ferb - Doofapus [US].mp3',
    '[S03.E30b] Phineas & Ferb - Norm Unleashed [US].mp3',
    '[S03.E31a] Phineas & Ferb - When Worlds Collide.mp3',
    '[S03.E31b] Phineas & Ferb - Road to Danville.mp3',
    '[S03.E34a] Phineas & Ferb - Blackout!.mp3',
    "[S03.E34b] Phineas & Ferb - What'd I Miss.mp3",
    '[S03.E35] Phineas & Ferb - This is Your Backstory.mp3',
]


DOOFAPUS = "[S03.E30a] Phineas & Ferb - Doofapus [US].mp3"


def test_a_bracketed_region_is_read_past():
    readings = donor_title_readings(DOOFAPUS, SHOW)
    assert readings[0] == "Phineas & Ferb - Doofapus [US]"          # the literal stays first, as it was
    assert "doofapus" in readings
    assert donor_names_episode("Doofapus", DOOFAPUS, series_title=SHOW) is True


@pytest.mark.parametrize("name", [
    "[S03.E30a] Phineas & Ferb - Doofapus (UK).mp3",
    "[S03.E30a] Phineas & Ferb - Doofapus [us].mp3",
])
def test_round_and_lowercase_brackets_too(name):
    assert "doofapus" in donor_title_readings(name, SHOW)


@pytest.mark.parametrize("name, kept, absent", [
    ("[S03.E30a] Phineas & Ferb - Doofapus [US).mp3", "doofapus us", "doofapus"),    # mismatched brackets
    ("[S03.E30a] Phineas & Ferb - Doofapus (UK].mp3", "doofapus uk", "doofapus"),
    ("[S03.E30a] Phineas & Ferb - Doofapus [FR].mp3", "doofapus fr", "doofapus"),    # not a region this reads
    ("[S01.E26b] Phineas and Ferb - Leave the Busting to Us.mp3", "leave the busting to us",
     "leave the busting to"),                                                        # a bare word, never
    ("[S01.E26b] Phineas and Ferb - The [US] Open Final.mp3", "the us open final", "the open final"),
])
def test_anything_else_is_left_alone(name, kept, absent):
    readings = donor_title_readings(name, SHOW)
    assert kept in readings and absent not in readings


def test_a_stacked_qualifier_is_dropped_once():
    readings = donor_title_readings("[S03.E30a] Phineas & Ferb - Doofapus [US] [UK].mp3", SHOW)
    assert "doofapus us" in readings and "doofapus" not in readings


def test_the_season_3_recording_is_found_by_its_title(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    folder = "Phineas and Ferb - Season 3 (2011)"
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in SEASON_3_NAMES:
            zf.writestr(f"{folder}/{name}", name.encode())
    video = tmp_path / SHOW / "Season 3" / "Phineas.and.Ferb.S03E53.Doofapus.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")

    class Client:
        def search_shows(self, query):
            return [{"name": folder, "url": "https://av/pack"}]

    tried = []
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: None)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda config, video_path, audio_path, **_: (tried.append(audio_path.name), (False, "no"))[1])
    process_episode(Client(), config, video, SHOW, 3, 53, series_year="2011", episode_title="Doofapus")
    assert tried[0] == DOOFAPUS
