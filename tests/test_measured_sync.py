"""A recording the engine measures in sync, whatever the shape of its map.

Law & Order: SVU's AudioVault recordings (2026-10-13 to 16) are sped up act by
act, by different amounts: every shape test refused them (stable trunk 13-64 %,
sync_ok False) while speech and onset checks found the narration on time apart
from short slips, which describealign 2.3.0's pass 3 now bends out. The engine
also reports how well the map it rendered holds (sync_check). These pin the
measured-sync rescue: the calibration shapes publish when the donor names the
episode and the track is English; other episodes' recordings that share only
the opening theme do not; nothing the gate decided before changes; a
measurement that cannot be read accepts nothing; and the guards after the gate
still apply.
"""

import json
import math

import pytest

import describarr.aligner as aligner
import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.workflow import _acceptance_decision

# looked up rather than imported, so the module loads on a tree without them
sync_check = getattr(aligner, "sync_check", None)
_measured_sync_holds = getattr(workflow, "_measured_sync_holds", None)

# Refused before pass 3, as describarr read them (decision history), with what
# the engine measured after it (2026-10-16): score, coverage, described trunk,
# described rate, described runtime, sync_ok; confirmed, within, over.
RIGHT = {
    "SVU S10E03 Swing": (59.0, 99.76, 47.4, 2.55, 2525., False, 60.8, 97.4, 0.41),
    "SVU S11E08 Turmoil": (32.0, 99.67, 34.6, 2.56, 2535., False, 56.9, 94.0, 0.32),
    "SVU S01E21 Nocturne": (19.7, 100.0, 33.0, 3.05, 2545., False, 39.6, 99.4, 0.02),
    "P&F S02E12 The Chronicles of Meap": (59.4, 99.87, 49.0, 5.18, 1310., False, 73.4, 95.4, 0.19),
    "P&F S02E14 De Plane! De Plane!": (56.3, 99.73, 83.1, 8.49, 603., True, 72.8, 95.0, 0.21),
    "P&F S02E30 Fireside Girl Jamboree": (50.6, 99.80, 82.9, 7.96, 666., True, 70.1, 97.7, 0.15),
    "P&F S03E06 The Belly of the Beast": (49.0, 99.84, 33.9, 6.10, 631., False, 69.6, 94.1, 0.36),
}
# Other episodes' recordings: the shared opening theme lines up perfectly, and
# is all of the video they confirm (P&F S02E27 against "The Beak", ...).
WRONG = {
    "P&F S02E27 vs The Beak": (10.7, 99.0, 100.0, 3.3, 600., True, 7.7, 100.0, 0.0),
    "P&F S03E16 vs A Phineas and Ferb Family Christmas": (7.5, 99.9, 100.0, 0.0, 600., True, 7.8, 97.7, 1.88),
}


def _measured(confirmed, within, over):
    return {"confirmed_pct": confirmed, "within_pct": within, "over_pct": over, "anchored_pct": None,
            "passthrough": False, "bent": 0, "max_correction_ms": 0.}


def _decide(shape, title=True, english=True, measured="from shape", **change):
    score, coverage, trunk, rate, runtime, sync_ok, confirmed, within, over = shape
    values = dict(score=score, content_coverage=coverage, stable_fraction=trunk, median_rate=rate,
                  total_runtime=runtime, sync_ok=sync_ok)
    values.update(change)
    if measured == "from shape":
        measured = _measured(confirmed, within, over)
    return _acceptance_decision(min_score=60.0, title_corroborated=title, primary_audio_english=english,
                                measured_sync=measured, **values)


# ── the gate ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("episode", sorted(RIGHT))
def test_the_refused_recordings_publish_when_named_and_english(episode):
    accepted, path, detail = _decide(RIGHT[episode])
    assert accepted and path == "measured-sync", detail


@pytest.mark.parametrize("episode", sorted(RIGHT))
def test_without_the_title_or_the_english_track_they_stay_refused(episode):
    assert _decide(RIGHT[episode], title=False)[1] == "reject"
    assert _decide(RIGHT[episode], english=False)[1] == "reject"


@pytest.mark.parametrize("pairing", sorted(WRONG))
def test_another_episodes_recording_is_refused_even_named(pairing):
    assert _decide(WRONG[pairing])[1] == "reject"


@pytest.mark.parametrize("confirmed, within, over, holds", [
    (30.0, 90.0, 2.0, True),
    (29.9, 90.0, 2.0, False),
    (30.0, 89.9, 2.0, False),
    (30.0, 90.0, 2.01, False),
])
def test_the_bar(confirmed, within, over, holds):
    assert _measured_sync_holds(_measured(confirmed, within, over)) is holds
    shape = RIGHT["SVU S11E08 Turmoil"][:6] + (confirmed, within, over)
    assert (_decide(shape)[1] == "measured-sync") is holds


def test_the_runtime_and_coverage_floors_still_apply():
    shape = RIGHT["SVU S11E08 Turmoil"]
    assert _decide(shape, total_runtime=299.)[1] == "reject"
    assert _decide(shape, content_coverage=98.9)[1] == "reject"
    assert _decide(shape, total_runtime=300.)[1] == "measured-sync"
    assert _decide(shape, content_coverage=99.0)[1] == "measured-sync"


def test_no_measurement_takes_no_new_path():
    assert _decide(RIGHT["SVU S11E08 Turmoil"], measured=None)[1] == "reject"


GRID = [
    dict(score=score, content_coverage=coverage, stable_fraction=trunk, median_rate=rate,
         total_runtime=runtime, sync_ok=sync_ok, piecewise_fraction=piecewise,
         title_corroborated=title, primary_audio_english=english)
    for score in (15., 25., 35., 59.9, 60., 75.)
    for coverage in (98., 99.5)
    for trunk in (40., 92., 99.)
    for rate in (0., 1.5, 4.27, 8.5, 11.)
    for runtime in (200., 2500.)
    for sync_ok in (False, True)
    for piecewise in (0., 95.)
    for title in (False, True)
    for english in (False, True)
]


def test_every_decision_the_gate_made_before_is_unchanged():
    # Against no measurement (an engine before 2.3.0), a passing one, and a
    # failing one: wherever the gate already decided, it decides the same; a
    # passing measurement may only turn a refusal into a measured-sync accept.
    passing, failing = _measured(70., 95., 0.3), _measured(8., 100., 0.)
    for values in GRID:
        before = _acceptance_decision(min_score=60.0, **values)
        assert _acceptance_decision(min_score=60.0, measured_sync=None, **values) == before
        assert _acceptance_decision(min_score=60.0, measured_sync=failing, **values) == before
        after = _acceptance_decision(min_score=60.0, measured_sync=passing, **values)
        if before[1] != "reject":
            assert after == before
        else:
            assert after[1] in ("reject", "measured-sync")


# ── reading the engine's measurement ─────────────────────────────────────────

GOOD_CHECK = {"points": 9000, "anchors": 6000, "within_40ms": 5800, "over_120ms": 20,
              "anchored_pct": 66.7, "within_pct": 96.7, "over_pct": 0.33, "confirmed_seconds": 1500.,
              "video_seconds": 2500., "confirmed_pct": 60.0}


def _report(tmp_path, payload, name="ep"):
    path = tmp_path / f"{name}.txt"
    path.write_text("Input file similarity: 32.0%\n")
    path.with_suffix(".json").write_text(json.dumps(payload))
    return path


def test_a_measurement_is_read(tmp_path):
    measured = sync_check(_report(tmp_path, {"sync_check": GOOD_CHECK, "passthrough": False,
                                             "refinement": {"applied": True, "bent": 12, "max_correction_ms": 180.5}}))
    assert measured == {"confirmed_pct": 60.0, "within_pct": 96.7, "over_pct": 0.33, "anchored_pct": 66.7,
                        "passthrough": False, "bent": 12, "max_correction_ms": 180.5}


@pytest.mark.parametrize("field", ["confirmed_pct", "within_pct", "over_pct"])
@pytest.mark.parametrize("bad", ["missing", "60", True, None, [60.], -0.5, 100.5, math.nan, math.inf, -math.inf])
def test_a_measurement_that_cannot_be_read_accepts_nothing(tmp_path, field, bad):
    check = dict(GOOD_CHECK)
    if bad == "missing":
        del check[field]
    else:
        check[field] = bad
    assert sync_check(_report(tmp_path, {"sync_check": check})) is None


@pytest.mark.parametrize("payload", [{}, {"sync_check": None}, {"sync_check": "60 %"}, {"sync_check": [GOOD_CHECK]}])
def test_no_measurement_reads_as_none(tmp_path, payload):
    assert sync_check(_report(tmp_path, payload)) is None


def test_an_engine_without_a_json_report_reads_as_none(tmp_path):
    path = tmp_path / "old.txt"
    path.write_text("Input file similarity: 32.0%\nRate change of 2.5% from 0:00:00 to 0:10:00 aligning with audio from 0:00:00 to 0:09:45\n")
    assert sync_check(path) is None
    assert sync_check(None) is None


def test_extras_that_are_not_finite_numbers_are_not_recorded(tmp_path):
    # NaN or infinity would reach the decision history, which is JSON lines.
    check = dict(GOOD_CHECK, anchored_pct=math.nan)
    measured = sync_check(_report(tmp_path, {"sync_check": check, "refinement": {"max_correction_ms": math.inf}}))
    assert measured["anchored_pct"] is None and measured["max_correction_ms"] is None
    assert measured["confirmed_pct"] == 60.0


def test_the_extras_are_optional(tmp_path):
    check = {key: GOOD_CHECK[key] for key in ("confirmed_pct", "within_pct", "over_pct")}
    measured = sync_check(_report(tmp_path, {"sync_check": check, "passthrough": "no", "refinement": {"bent": True}}))
    assert measured["confirmed_pct"] == 60.0
    assert measured["anchored_pct"] is None and measured["passthrough"] is None and measured["bent"] is None


# ── through _align_and_keep ─────────────────────────────────────────────────

def _seg(rate, v0, v1, a0, a1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": a0, "audio_end_sec": a1}


def _wobbly(picture=2540.):
    """An act-by-act map like SVU's: pieces at 2-4 %, no trunk, no long hole."""
    segments, video, audio = [], 0.5, 0.
    rates = [2.3, 3.9, 2.8, 3.5, 2.1, 3.7, 2.6, 3.3]
    step = (picture - 1.) / 40
    for index in range(40):
        rate = rates[index % len(rates)]
        segments.append(_seg(rate, video, video + step, audio, audio + step / (1 + rate / 100)))
        video += step
        audio += step / (1 + rate / 100)
    return segments


def _keep(monkeypatch, tmp_path, *, segments, measurement, donor="08 Turmoil.mp3", title="Turmoil",
          similarity=32.0, picture=2540., english=True):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "Law.and.Order.Special.Victims.Unit.S11E08.Turmoil.1080p.AMZN.WEB-DL.mkv"
    video.write_bytes(b"x")
    folder = config.cache_dir / "shows" / "law_order_special_victims_unit" / "season_11"
    folder.mkdir(parents=True)
    audio = folder / donor
    audio.write_bytes(b"y")
    combined = tmp_path / "out" / "ad_ep.mkv"
    combined.parent.mkdir()
    combined.write_bytes(b"z")
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir()
    report.write_text(f"Input file similarity: {similarity}%\n")
    payload = {"similarity_pct": similarity, "median_rate_pct": 2.9, "stable_trunk_fraction_pct": 34.6,
               "segments": segments, "refined_segments": segments, "passthrough": False,
               "refinement": {"applied": True, "bent": 85, "max_correction_ms": 265.1}}
    if measurement is not None:
        payload["sync_check"] = measurement
    report.with_suffix(".json").write_text(json.dumps(payload))
    published = []
    monkeypatch.setattr(workflow, "align",
                        lambda *a, **k: AlignResult(combined, report, None, returncode=0, aligned_english=english))
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: published.append(a))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: picture)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: english)
    ok, reason = workflow._align_and_keep(config, video, audio, label="Law & Order: Special Victims Unit S11E08",
                                          episode_title=title, series_title="Law & Order: Special Victims Unit",
                                          source="AudioVault")
    decision = json.loads((config.cache_dir / "decisions.json").read_text())[-1]
    return ok, reason, published, decision


MEASURED_TURMOIL = dict(GOOD_CHECK, confirmed_pct=56.9, within_pct=94.0, over_pct=0.32, anchored_pct=63.1)


def test_a_wobbly_recording_measured_in_sync_publishes(monkeypatch, tmp_path):
    ok, _, published, decision = _keep(monkeypatch, tmp_path, segments=_wobbly(), measurement=MEASURED_TURMOIL)
    assert ok and len(published) == 1
    assert decision["path"] == "measured-sync"
    # the title and language were read although 32 % is outside the corroborated band
    assert decision["title_corroborated"] is True and decision["english_track"] is True
    assert decision["sync_confirmed"] == 56.9 and decision["sync_within"] == 94.0 and decision["sync_over"] == 0.32
    assert decision["sync_anchored"] == 63.1 and decision["passthrough"] is False
    assert decision["refined_bent"] == 85 and decision["refined_max_ms"] == 265.1


def test_the_same_map_without_a_measurement_is_refused_as_before(monkeypatch, tmp_path):
    ok, _, published, decision = _keep(monkeypatch, tmp_path, segments=_wobbly(), measurement=None)
    assert not ok and not published
    assert decision["path"] == "reject"
    assert decision["title_corroborated"] is None              # never probed, as before
    assert decision["sync_confirmed"] is None and decision["passthrough"] is None


def test_a_recording_named_for_another_episode_is_refused(monkeypatch, tmp_path):
    ok, _, published, decision = _keep(monkeypatch, tmp_path, segments=_wobbly(), measurement=MEASURED_TURMOIL,
                                       donor="07 Users.mp3")
    assert not ok and not published
    assert decision["path"] == "reject" and decision["title_corroborated"] is False


def test_a_long_hole_is_still_refused_after_the_gate_accepts(monkeypatch, tmp_path):
    # Measured in sync where it is described, but 300 s of the picture has no
    # recording: the stretch cap after the gate still refuses it.
    segments = _wobbly()
    holed = [seg for seg in segments if not (1000. < seg["video_start_sec"] < 1300.)]
    ok, reason, published, decision = _keep(monkeypatch, tmp_path, segments=holed, measurement=MEASURED_TURMOIL)
    assert not ok and not published
    assert decision["path"] == "measured-sync"
    assert decision["outcome"] == "rejected" and "has no recording" in reason
