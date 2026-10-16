"""A recording that runs 6-10 % off speed on one straight line, named for the episode.

Phineas and Ferb season 1 (2026-10-15): AudioVault's recordings come at two
speeds, the PAL 4.27 % and a second batch at 7.2-9.2 %. Above the 60 % floor
the fast ones published on similarity, and the speech offset checker puts two
of them (S01E02 at 8.52 %, S01E16 at 8.65 %) in sync. Six more, each the
recording named for the episode, sat on one straight line just under the floor
and were refused, because the drift rescue trusts only native speed or 2-6 %.
These pin the narrow rescue for them: a straight line up to the engine's own
10 % replace limit, and only when the donor names the episode on an English
track, the corroborated rescue's two checks of content.
"""

import json

import pytest

import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.workflow import _acceptance_decision

# The six refused that night, as the gate read them (decision history):
# score, coverage, described trunk, described rate, described runtime.
REFUSED_STEEP = {
    "S01E05 Lights, Candace, Action!": (55.67825751902931, 100.0, 100.0, 8.255416464011468, 653.2131277873509),
    "S01E08 I, Brobot": (57.42037724180581, 99.99082778178371, 96.69334157563715, 9.200131081344388, 602.0778318395085),
    "S01E11 S'Winter": (57.800819328987494, 99.99831289623555, 99.99834427828497, 9.020080649594675, 636.9691977232427),
    "S01E20 Journey to the Center of Candace": (57.30764597932446, 99.89298403507492, 94.76450484143957, 7.750009769463517, 658.1913357163576),
    "S01E24 The Ballad of Badbeard": (57.334309405079445, 99.97324890920811, 100.0, 8.518587845121672, 656.809963440796),
    "S01E30 Voyage to the Bottom of Buford": (59.64187828514515, 99.99356238698338, 100.0, 8.70546142548838, 654.9625952329501),
}


def _decide(score, coverage, trunk, rate, runtime=650.0, sync_ok=True, title=True, english=True):
    return _acceptance_decision(
        score=score, content_coverage=coverage, stable_fraction=trunk, median_rate=rate,
        total_runtime=runtime, sync_ok=sync_ok, min_score=60.0,
        title_corroborated=title, primary_audio_english=english,
    )


# ── the gate ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("episode", sorted(REFUSED_STEEP))
def test_the_six_refused_that_night_are_accepted_when_named_and_english(episode):
    accepted, path, detail = _decide(*REFUSED_STEEP[episode])
    assert accepted and path == "steep-drift-rescue", detail


@pytest.mark.parametrize("episode", sorted(REFUSED_STEEP))
def test_without_the_title_or_the_english_track_they_stay_refused(episode):
    shape = REFUSED_STEEP[episode]
    assert _decide(*shape, title=False)[1] == "reject"
    assert _decide(*shape, english=False)[1] == "reject"


@pytest.mark.parametrize("change, why", [
    ({"trunk": 83.74188448135598}, "S01E15: only 84 % of it is one line"),
    ({"sync_ok": False}, "the sync check fails"),
    ({"score": 29.9}, "under the rescue floor"),
    ({"coverage": 98.9}, "seams beyond the ordinary"),
    ({"runtime": 299.0}, "too short to judge"),
])
def test_anything_less_than_a_clean_line_stays_refused(change, why):
    shape = dict(zip(("score", "coverage", "trunk", "rate", "runtime"), REFUSED_STEEP["S01E24 The Ballad of Badbeard"]))
    shape.update(change)
    sync_ok = shape.pop("sync_ok", True)
    assert _decide(**shape, sync_ok=sync_ok)[1] == "reject", why


@pytest.mark.parametrize("rate, path", [
    (6.0, "drift-rescue"),            # PAL/NTSC's band still ends at 6 %, inclusive
    (6.01, "steep-drift-rescue"),
    (10.0, "steep-drift-rescue"),     # the engine replaces up to 10 %, inclusive
    (10.01, "reject"),                # past it the engine replaces nothing
    (-8.52, "steep-drift-rescue"),    # a slower recording reads the same
    (-10.01, "reject"),
])
def test_the_band_is_past_six_up_to_ten(rate, path):
    assert _decide(57.3, 100.0, 100.0, rate)[1] == path


def test_inside_pals_band_nothing_more_is_asked():
    # 5.9 % is the ordinary drift rescue, with no title or language needed.
    assert _decide(57.3, 100.0, 100.0, 5.9, title=False, english=False)[1] == "drift-rescue"
    assert _decide(57.3, 100.0, 100.0, 6.01, title=False, english=False)[1] == "reject"


def test_the_detail_says_what_was_accepted():
    _, _, detail = _decide(*REFUSED_STEEP["S01E24 The Ballad of Badbeard"])
    assert "8.52%" in detail and "names this episode" in detail and "English" in detail


# ── through _align_and_keep ──────────────────────────────────────────────────

def _seg(rate, v0, v1, a0, a1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": a0, "audio_end_sec": a1}


def _keep(monkeypatch, tmp_path, segments, *, donor, title="The Ballad of Badbeard",
          similarity=57.3, picture=657.0, english=True):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "Phineas.and.Ferb.S01E24.The.Ballad.of.Badbeard.1080p.DSNP.WEBRip.mkv"
    video.write_bytes(b"x")
    folder = config.cache_dir / "shows" / "phineas_and_ferb" / "season_01"
    folder.mkdir(parents=True)
    audio = folder / donor
    audio.write_bytes(b"y")
    combined = tmp_path / "out" / "ad_ep.mkv"
    combined.parent.mkdir()
    combined.write_bytes(b"z")
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir()
    report.write_text(f"Input file similarity: {similarity}%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": similarity, "median_rate_pct": segments[0]["rate_pct"], "segments": segments,
    }))
    published = []
    monkeypatch.setattr(workflow, "align",
                        lambda *a, **k: AlignResult(combined, report, None, returncode=0, aligned_english=english))
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: published.append(a))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: picture)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: english)
    ok, reason = workflow._align_and_keep(config, video, audio, label="Phineas and Ferb S01E24",
                                          episode_title=title, series_title="Phineas and Ferb",
                                          source="AudioVault")
    decision = json.loads((config.cache_dir / "decisions.json").read_text())[-1]
    return ok, reason, published, decision


# One straight line at 8.52 % over the whole 657 s picture.
STRAIGHT_852 = [_seg(8.52, 0.2, 656.8, 0.0, 605.3)]
BADBEARD = "[S01.E13b] Phineas and Ferb - The Ballad of Badbeard.mp3"


def test_the_named_recording_publishes_and_the_history_says_why(monkeypatch, tmp_path):
    ok, _, published, decision = _keep(monkeypatch, tmp_path, STRAIGHT_852, donor=BADBEARD)
    assert ok and len(published) == 1
    assert decision["path"] == "steep-drift-rescue"
    # Probed although the score is outside the 20-30 band, and recorded as asked.
    assert decision["title_corroborated"] is True and decision["english_track"] is True


def test_a_recording_named_for_another_episode_is_refused(monkeypatch, tmp_path):
    ok, _, published, decision = _keep(
        monkeypatch, tmp_path, STRAIGHT_852, donor="[S01.E13a] Phineas and Ferb - It's a Mud, Mud, Mud, Mud World.mp3")
    assert not ok and not published
    assert decision["path"] == "reject" and decision["title_corroborated"] is False


def test_a_long_hole_is_still_refused_after_the_gate_accepts(monkeypatch, tmp_path):
    # The S01E23 shape at a steep rate: one 68 s stretch with no recording,
    # more than a tenth of a 637 s picture. The gate's straight line passes;
    # the stretch cap that runs after it must still refuse.
    holed = [_seg(8.52, 0.2, 213.0, 0.0, 196.3), _seg(8.52, 281.0, 636.6, 196.4, 524.1)]
    ok, reason, published, decision = _keep(monkeypatch, tmp_path, holed, donor=BADBEARD, picture=637.0)
    assert not ok and not published
    assert decision["path"] == "steep-drift-rescue"            # what the gate chose ...
    assert decision["outcome"] == "rejected" and "more than" in reason   # ... and the cap refused
