"""A film's recording named exactly for it can take the measured-sync rescue.

Other Mommy (2026-11-25): both recordings, AudioVault's and LivingAudio's,
scored 29.6-29.7 % over one native-rate line covering the whole recording, the
engine confirmed 39 % of the film in sync with every anchor within 40 ms, and
the only picture left uncovered was the last 75 s of the credits. A TV episode
with those numbers publishes on the measured-sync rescue because its donor
names the episode; a film has no episode title, so it was refused, and the
speech check after a hand publish found it in sync. These pin the film's
version of that rescue: the recording's name must give the film's title and
year; it opens the measured-sync path and no other; a film still may not leave
more than a tenth of itself undescribed, since in a film that is another cut
(Big, 2026-10-25); and nothing an episode is judged by changes.
"""

import json

import pytest

import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.titles import donor_names_movie
from describarr.workflow import _acceptance_decision

# ── the name ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("title, year, donor", [
    ("Other Mommy", "2026", "Other Mommy (2026).mp3"),
    ("Other Mommy", "2026", "other mommy (2026).MP3"),
    ("Barbie as The Princess & the Pauper", "2004", "Barbie as the Princess and the Pauper (2004).mp3"),
    ("Forrest Gump", "1994", "Forrest Gump [New Description] (1994).mp3"),
    ("The Lion King", "1994", "Lion King (1994).mp3"),
    ("Amélie", "2001", "Amelie (2001).mp3"),
])
def test_the_films_own_name_agrees(title, year, donor):
    assert donor_names_movie(title, year, donor) is True


@pytest.mark.parametrize("title, year, donor", [
    ("Other Mommy", "2026", "Other Mommy 2 (2027).mp3"),        # a sequel
    ("Other Mommy", "2026", "Other Mommy (2019).mp3"),          # a film of the same name, another year
    ("Other Mommy", "2026", "The Other Mommy Returns (2026).mp3"),
    ("Big", "1988", "Big Fish (2003).mp3"),
    ("Other Mommy", "2026", "Other Mommy.mp3"),                 # no year to compare
])
def test_another_films_name_does_not(title, year, donor):
    assert donor_names_movie(title, year, donor) is False


@pytest.mark.parametrize("title, year, donor", [
    ("", "2026", "Other Mommy (2026).mp3"),
    ("Other Mommy", "", "Other Mommy (2026).mp3"),
    ("Other Mommy", "2026", "[Audio Description].mp3"),
])
def test_an_unknown_name_is_no_evidence(title, year, donor):
    assert donor_names_movie(title, year, donor) is None


# ── the gate ──────────────────────────────────────────────────────────────────

# Other Mommy as describarr read it on 2026-11-25 (decision history): score,
# coverage, described trunk, described rate, described runtime, sync_ok, and
# the engine's sync check.
OTHER_MOMMY = dict(score=29.6, content_coverage=100.0, stable_fraction=100.0, median_rate=-0.00002,
                   total_runtime=5581.4, sync_ok=True)
OTHER_MOMMY_MEASURED = {"confirmed_pct": 39.1, "within_pct": 99.99, "over_pct": 0.0, "anchored_pct": 39.6,
                        "passthrough": True, "bent": 0, "max_correction_ms": 0.0}


def _decide(movie=True, english=True, measured=OTHER_MOMMY_MEASURED, **change):
    values = dict(OTHER_MOMMY, **change)
    return _acceptance_decision(min_score=60.0, title_corroborated=False, primary_audio_english=english,
                                measured_sync=measured, movie_corroborated=movie, **values)


def test_other_mommy_publishes_when_its_recording_is_named_for_it():
    accepted, path, detail = _decide()
    assert accepted and path == "measured-sync", detail
    assert "named for this film and year" in detail


def test_other_mommy_stays_refused_without_the_name_or_the_english_track():
    assert _decide(movie=False)[1] == "reject"
    assert _decide(english=False)[1] == "reject"


def test_the_measurement_still_decides_for_a_film():
    assert _decide(measured=None)[1] == "reject"
    weak = dict(OTHER_MOMMY_MEASURED, confirmed_pct=29.9)
    assert _decide(measured=weak)[1] == "reject"
    loose = dict(OTHER_MOMMY_MEASURED, over_pct=2.5)
    assert _decide(measured=loose)[1] == "reject"
    assert _decide(content_coverage=98.9)[1] == "reject"
    assert _decide(total_runtime=299.)[1] == "reject"


GRID = [
    dict(score=score, content_coverage=coverage, stable_fraction=trunk, median_rate=rate,
         total_runtime=runtime, sync_ok=sync_ok, piecewise_fraction=piecewise,
         primary_audio_english=english)
    for score in (15., 25., 35., 59.9, 60., 75.)
    for coverage in (98., 99.5)
    for trunk in (40., 92., 99.)
    for rate in (0., 1.5, 4.27, 8.5, 11.)
    for runtime in (200., 2500.)
    for sync_ok in (False, True)
    for piecewise in (0., 95.)
    for english in (False, True)
]


def test_a_films_name_opens_the_measured_sync_path_and_no_other():
    # The corroborated and steep-drift rescues take an episode's title, which
    # the matcher never used; a film's name is what its recording was chosen
    # by, so it must not open them. Against no measurement, a failing one and a
    # passing one, the name changes nothing but a refusal into measured-sync.
    passing = {"confirmed_pct": 70., "within_pct": 95., "over_pct": 0.3}
    failing = {"confirmed_pct": 8., "within_pct": 100., "over_pct": 0.}
    for values in GRID:
        for measured in (None, failing, passing):
            unnamed = _acceptance_decision(min_score=60.0, measured_sync=measured, **values)
            named = _acceptance_decision(min_score=60.0, measured_sync=measured, movie_corroborated=True, **values)
            if unnamed[1] != "reject":
                assert named == unnamed
            elif named[1] != "reject":
                assert named[1] == "measured-sync" and measured is passing, (values, named)


def test_an_episodes_decisions_do_not_change():
    # movie_corroborated defaults to False, so every caller that does not pass
    # it is judged as before.
    for values in GRID:
        for title in (False, True):
            before = _acceptance_decision(min_score=60.0, title_corroborated=title, **values)
            after = _acceptance_decision(min_score=60.0, title_corroborated=title, movie_corroborated=False,
                                         **values)
            assert after == before


# ── through _align_and_keep ─────────────────────────────────────────────────

def _seg(rate, v0, v1, a0, a1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": a0, "audio_end_sec": a1}


# Other Mommy's map: one line from 0.57 s to 5582.0 s of a 5657.1 s film.
OTHER_MOMMY_MAP = [_seg(-0.00001, 0.57, 5582.02, 0.0, 5581.45)]


def _big_map(picture=7800., gaps=8, gap=234.9, rate=4.27):
    """Big (1988) as refused on 2026-10-25: the theatrical recording on the
    extended cut, nine described pieces at the PAL rate with eight holes of
    about four minutes between them, 31 of 130 minutes undescribed."""
    pieces = gaps + 1
    piece = (picture - gaps * gap) / pieces
    segments, video, audio = [], 0.0, 0.0
    for index in range(pieces):
        segments.append(_seg(rate, video, video + piece, audio, audio + piece / (1 + rate / 100)))
        video += piece + gap
        audio += piece / (1 + rate / 100)
    return segments


def _keep_movie(monkeypatch, tmp_path, *, segments, measurement, donor, similarity, picture,
                movie_title="Other Mommy", movie_year="2026", rate=0.0, trunk=100.0, english=True):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "Other.Mommy.2026.1080p.WEBRip.DDP.5.1.10bit.H.265-iVy.mkv"
    video.write_bytes(b"x")
    folder = config.cache_dir / "movies"
    folder.mkdir(parents=True)
    audio = folder / donor
    audio.write_bytes(b"y")
    combined = tmp_path / "out" / "ad_film.mkv"
    combined.parent.mkdir()
    combined.write_bytes(b"z")
    report = tmp_path / "alignments" / "film.txt"
    report.parent.mkdir()
    report.write_text(f"Input file similarity: {similarity}%\n")
    payload = {"similarity_pct": similarity, "median_rate_pct": rate, "stable_trunk_fraction_pct": trunk,
               "segments": segments, "refined_segments": segments, "passthrough": False,
               "refinement": {"applied": True, "bent": 0, "max_correction_ms": 0.0},
               "sync_check": measurement}
    report.with_suffix(".json").write_text(json.dumps(payload))
    published = []
    monkeypatch.setattr(workflow, "align",
                        lambda *a, **k: AlignResult(combined, report, None, returncode=0, aligned_english=english))
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: published.append(a))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: picture)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: english)
    monkeypatch.setattr(workflow.notify, "tell_jellyfin_file_changed", lambda path: True)
    ok, reason = workflow._align_and_keep(config, video, audio, label=f"{movie_title} ({movie_year})",
                                          source="AudioVault", movie_title=movie_title, movie_year=movie_year)
    decision = json.loads((config.cache_dir / "decisions.json").read_text())[-1]
    return ok, reason, published, decision


MEASURED_OTHER_MOMMY = {"points": 22537, "anchors": 8935, "within_40ms": 8934, "over_120ms": 0,
                        "anchored_pct": 39.6, "within_pct": 99.99, "over_pct": 0.0,
                        "confirmed_seconds": 2212.2, "video_seconds": 5657.1, "confirmed_pct": 39.1}


def test_other_mommy_publishes_through_the_whole_walk(monkeypatch, tmp_path):
    ok, _, published, decision = _keep_movie(monkeypatch, tmp_path, segments=OTHER_MOMMY_MAP,
                                             measurement=MEASURED_OTHER_MOMMY, donor="Other Mommy (2026).mp3",
                                             similarity=29.6, picture=5657.1)
    assert ok and len(published) == 1
    assert decision["path"] == "measured-sync"
    assert decision["movie_corroborated"] is True and decision["english_track"] is True
    assert decision["title_corroborated"] is None


def test_a_sequels_recording_is_refused(monkeypatch, tmp_path):
    ok, _, published, decision = _keep_movie(monkeypatch, tmp_path, segments=OTHER_MOMMY_MAP,
                                             measurement=MEASURED_OTHER_MOMMY, donor="Other Mommy 2 (2027).mp3",
                                             similarity=29.6, picture=5657.1)
    assert not ok and not published
    assert decision["path"] == "reject" and decision["movie_corroborated"] is False


def test_a_film_dubbed_away_from_english_is_refused(monkeypatch, tmp_path):
    ok, _, published, decision = _keep_movie(monkeypatch, tmp_path, segments=OTHER_MOMMY_MAP,
                                             measurement=MEASURED_OTHER_MOMMY, donor="Other Mommy (2026).mp3",
                                             similarity=29.6, picture=5657.1, english=False)
    assert not ok and not published
    assert decision["path"] == "reject"


MEASURED_BIG = dict(MEASURED_OTHER_MOMMY, confirmed_pct=33.5, within_pct=99.46, over_pct=0.09, anchored_pct=35.0)


def test_another_cut_stays_refused_however_well_named(monkeypatch, tmp_path):
    # The name opens no allowance for undescribed picture in a film: Big's
    # recording is named exactly for it and passes the drift rescue, and is
    # still refused for the 31 minutes it leaves without narration.
    ok, reason, published, decision = _keep_movie(monkeypatch, tmp_path, segments=_big_map(),
                                                  measurement=MEASURED_BIG, donor="Big (1988).mp3",
                                                  similarity=38.5, picture=7800., movie_title="Big",
                                                  movie_year="1988", rate=4.27, trunk=99.87)
    assert not ok and not published
    assert decision["outcome"] == "rejected"
    assert decision["movie_corroborated"] is True
    assert "another cut" in str(reason)


def test_a_named_film_without_a_measurement_is_refused(monkeypatch, tmp_path):
    # Without the engine's measurement the name opens nothing: the corroborated
    # rescue would take this straight line at 29.6 % on an episode's title.
    ok, _, published, decision = _keep_movie(monkeypatch, tmp_path, segments=OTHER_MOMMY_MAP,
                                             measurement=None, donor="Other Mommy (2026).mp3",
                                             similarity=29.6, picture=5657.1)
    assert not ok and not published
    assert decision["path"] == "reject"
