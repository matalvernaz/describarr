"""The rescue judges uniformity where the description lands.

The stable trunk used to be measured over every segment of the map, so video
the recording never covers (a feature cut's extra footage, the recap between
two joined halves, end credits) counted against uniformity although the
engine leaves the original soundtrack there and the undescribed gate already
weighs it. Deep Space Nine's finale, joined from its two halves, had every
replaced stretch at rate 0.00 and a trunk of 89.5 % (floor 90); Family Guy
S09E18's join 73 % because the DVD cut runs 12 minutes longer than the
broadcast halves (2026-10-10). The joins that passed that night (S04E01,
S01E01-E02) had the same shape with less extra footage: replaced-only trunk
100 %, all-segments 92.8 % and 94.9 %.

So the rescue now measures the trunk, the rate and the sync check over the
replaced segments alone (|rate| <= 10 %), and the undescribed gate keeps its
40 % line (20 % for a whole double). What that gate refuses stays refused:
half a double, a clip show's scattered matches. What time compression breaks
stays broken: SVU's AudioVault pack runs its acts at 2.8-4 %, varied, and
that is still no trunk at all.
"""

import json

import describarr.workflow as workflow
from describarr.aligner import AlignResult, slope_stability, sync_quality
from describarr.config import Config
from describarr.workflow import _acceptance_decision


def _seg(rate, v0, v1, a0, a1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": a0, "audio_end_sec": a1}


# The finale's join as the engine mapped it (dry run, 2026-10-11): the halves
# straight at 0.00, a 128 s opening stretch the engine stretches at 2.21 %, the
# seam (239 s of the feature cut against 70 s of Part 2's recap), two bits of
# footage the halves lack. Whole-map trunk 89.9 %, replaced-only 97.3 %.
FINALE_JOIN = [
    _seg(0.0, 0.0, 291.0, 0.0, 290.0),
    _seg(2.21, 291.0, 419.0, 290.0, 415.0),
    _seg(0.0, 419.0, 672.0, 415.0, 668.0),
    _seg(1886.0, 672.0, 726.0, 668.0, 671.0),
    _seg(0.0, 726.0, 1814.0, 671.0, 1759.0),
    _seg(3.71, 1814.0, 1821.0, 1759.0, 1766.0),
    _seg(0.0, 1821.0, 2612.0, 1766.0, 2557.0),
    _seg(240.0, 2612.0, 2850.0, 2557.0, 2627.0),
    _seg(0.0, 2850.0, 3500.0, 2627.0, 3277.0),
    _seg(243618.0, 3500.0, 3629.0, 3277.0, 3277.0),
    _seg(0.0, 3629.0, 5516.0, 3277.0, 5164.0),
]

# The roundtable's counterexample (thread 349): a map the engine could in
# principle produce, two straight native blocks with 299 s between them where
# the audio barely moves, so the second block plays 298 s late if the first is
# the right one. The old gate refused it on the whole-map trunk (70 %); the
# described-part trunk is 100 %.
OFFSET_JUMP = [
    _seg(0.0, 0.0, 400.0, 0.0, 400.0),
    _seg(29800.0, 400.0, 699.0, 400.0, 401.0),
    _seg(0.0, 699.0, 999.0, 401.0, 701.0),
    _seg(-99.67, 999.0, 1000.0, 701.0, 1000.0),
]

# Three extra scenes of 280 s in a 3000 s picture: 28 % uncovered, none of it
# longer than a tenth.
THREE_SCENES = [
    _seg(0.0, 0.0, 700.0, 0.0, 700.0), _seg(28000.0, 700.0, 980.0, 700.0, 701.0),
    _seg(0.0, 980.0, 1700.0, 701.0, 1421.0), _seg(28000.0, 1700.0, 1980.0, 1421.0, 1422.0),
    _seg(0.0, 1980.0, 2700.0, 1422.0, 2142.0), _seg(28000.0, 2700.0, 2980.0, 2142.0, 2143.0),
    _seg(0.0, 2980.0, 3000.0, 2143.0, 2163.0),
]

def _with_extra_scenes(lengths, total=3329.0):
    """A straight native map over *total* s of video, with extra scenes of the
    given lengths spread evenly through it (the audio barely moves there)."""
    segs, v, a = [], 0.0, 0.0
    gap = (total - sum(lengths)) / (len(lengths) + 1)
    for extra in lengths:
        segs.append(_seg(0.0, v, v + gap, a, a + gap))
        v += gap
        a += gap
        segs.append(_seg(9000.0, v, v + extra, a, a + 1.0))
        v += extra
        a += 1.0
    segs.append(_seg(0.0, v, total, a, a + (total - v)))
    return segs


# S09E18's join (dry run, 2026-10-11): the DVD cut has seventeen stretches the
# broadcast halves lack, 181 s at most, 778 s in all; whole-map trunk 77 %.
TRAP_JOIN = _with_extra_scenes(
    [181, 27, 35, 31, 31, 26, 32, 59, 21, 39, 30, 79, 35, 25, 86, 21, 20]
)

# SVU's AudioVault pack: every act replaced, at its own speed.
SVU_AUDIOVAULT = [
    _seg(2.6, 0.0, 600.0, 0.0, 585.0),
    _seg(3.9, 600.0, 1200.0, 585.0, 1162.0),
    _seg(2.3, 1200.0, 1800.0, 1162.0, 1748.0),
    _seg(3.5, 1800.0, 2500.0, 1748.0, 2424.0),
]

# SVU's LivingAudio recording: straight at 0.00 but for a 31 s title-card
# stretch at +5.5 %, which the engine replaces, stretched.
SVU_LIVINGAUDIO = [
    _seg(0.0, 1.0, 84.0, 0.0, 83.0),
    _seg(5.49, 84.0, 115.0, 83.0, 112.0),
    _seg(0.0, 115.0, 764.0, 112.0, 762.0),
    _seg(54207.0, 764.4, 764.9, 762.0, 762.0),
    _seg(0.0, 764.9, 2568.0, 762.0, 2565.0),
]

# Half a double episode described by one part's recording.
HALF_DOUBLE = [
    _seg(0.0, 0.0, 1400.0, 0.0, 1400.0),
    _seg(20000.0, 1400.0, 2800.0, 1400.0, 1407.0),
]

# A clip show: a few straight minutes where a clip lines up, jumps between.
CLIP_SHOW = [
    _seg(0.0, 0.0, 60.0, 0.0, 60.0), _seg(9000.0, 60.0, 240.0, 60.0, 62.0),
    _seg(0.0, 240.0, 300.0, 62.0, 122.0), _seg(9000.0, 300.0, 480.0, 122.0, 124.0),
    _seg(0.0, 480.0, 540.0, 124.0, 184.0), _seg(9000.0, 540.0, 720.0, 184.0, 186.0),
    _seg(0.0, 720.0, 780.0, 186.0, 246.0), _seg(9000.0, 780.0, 960.0, 246.0, 248.0),
    _seg(0.0, 960.0, 1020.0, 248.0, 308.0), _seg(9000.0, 1020.0, 1300.0, 308.0, 310.0),
]


def _report(tmp_path, segments, similarity, median=0.0):
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"Input file similarity: {similarity}%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": similarity, "median_rate_pct": median, "segments": segments,
    }))
    return report


# ── the metrics ───────────────────────────────────────────────────────────────

def test_the_trunk_over_the_described_part_ignores_what_was_never_replaced(tmp_path):
    report = _report(tmp_path, FINALE_JOIN, 57.4)
    _, over_all, _ = slope_stability(report)
    rate, over_described, runtime = slope_stability(report, described_only=True)
    assert round(over_all, 1) == 89.9
    assert abs(over_described - 97.35) < 0.05
    assert rate == 0.0
    assert runtime == 5095.0


def test_the_described_part_rate_is_measured_not_read_from_the_report(tmp_path):
    # The report's median is the whole map's; the replaced segments have their own.
    report = _report(tmp_path, FINALE_JOIN, 57.4, median=2.0)
    rate, over_described, _ = slope_stability(report, described_only=True)
    assert rate == 0.0 and abs(over_described - 97.35) < 0.05
    assert sync_quality(report, described_only=True)[0] is True


def test_the_sync_check_over_the_described_part(tmp_path):
    report = _report(tmp_path, TRAP_JOIN, 49.0)
    assert sync_quality(report)[0] is False          # 77.5 % of the map is extra footage or seam
    assert sync_quality(report, described_only=True)[0] is True


def test_time_compression_is_no_trunk_either_way(tmp_path):
    report = _report(tmp_path, SVU_AUDIOVAULT, 56.9, median=3.1)
    _, over_all, _ = slope_stability(report)
    _, over_described, _ = slope_stability(report, described_only=True)
    assert over_all < 50.0 and over_described < 50.0
    assert sync_quality(report, described_only=True)[0] is False


def test_a_replaced_stretch_at_another_rate_still_counts_against_the_trunk(tmp_path):
    report = _report(tmp_path, SVU_LIVINGAUDIO, 25.1)
    _, over_described, _ = slope_stability(report, described_only=True)
    assert 98.5 < over_described < 99.0


# ── the gate, through _align_and_keep ─────────────────────────────────────────

def _run(monkeypatch, tmp_path, segments, similarity, *, median=0.0, title="", english=True,
         max_undescribed_fraction=None, picture=None, keep_probe=False):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x")
    audio = tmp_path / "[S01.E01] Pilot.mp3"
    audio.write_bytes(b"y")
    combined = tmp_path / "out" / "ad_ep.mkv"
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_bytes(b"z")
    report = _report(tmp_path, segments, similarity, median)
    published_calls = []
    monkeypatch.setattr(workflow, "align",
                        lambda *a, **k: AlignResult(combined, report, None, returncode=0))
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: published_calls.append(a))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    if not keep_probe:
        monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: english)
    length = max(s["video_end_sec"] for s in segments) if picture is None else picture
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: length)
    kwargs = {"max_undescribed_fraction": max_undescribed_fraction} if max_undescribed_fraction else {}
    published, reason = workflow._align_and_keep(
        config, video, audio, label="Show S01E01", episode_title=title, **kwargs,
    )
    decisions = json.loads((config.cache_dir / "decisions.json").read_text())
    return published, reason, decisions[-1]


def test_a_joined_recording_with_extra_footage_in_the_cut_publishes(monkeypatch, tmp_path):
    # A manual retry: no title. Under a tenth of the picture is uncovered, so
    # the rescue needs no corroboration.
    published, reason, decision = _run(monkeypatch, tmp_path, FINALE_JOIN, 57.4)
    assert published
    assert decision["path"] == "drift-rescue"
    assert "no description" in (reason or "")          # the extra footage is said, not hidden
    # Both measures are on the record: the whole map's trunk and the one that decided.
    assert round(decision["stable_fraction"], 1) == 89.9
    assert abs(decision["described_trunk"] - 97.35) < 0.05 and decision["described_rate"] == 0.0


# ── more than a tenth uncovered: the donor must be corroborated ───────────────

def test_a_join_a_fifth_short_of_the_dvd_cut_publishes_when_the_donor_names_the_episode(
        monkeypatch, tmp_path):
    published, reason, decision = _run(monkeypatch, tmp_path, TRAP_JOIN, 49.0, title="Pilot")
    assert published
    assert decision["path"] == "drift-rescue"
    assert "13 min" in reason


def test_the_same_join_without_a_title_is_refused(monkeypatch, tmp_path):
    published, reason, _ = _run(monkeypatch, tmp_path, TRAP_JOIN, 49.0)
    assert not published
    assert "not corroborated" in reason


def test_a_film_a_sixth_short_of_its_cut_stays_refused(monkeypatch, tmp_path):
    # Films carry no episode title, so above a tenth uncovered they stay where
    # the old gate left them.
    film = _with_extra_scenes([300, 300, 300], total=6000.0)
    published, reason, _ = _run(monkeypatch, tmp_path, film, 45.0)
    assert not published
    assert "not corroborated" in reason


def test_three_short_extra_scenes_pass_with_a_title_and_fail_without(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, THREE_SCENES, 45.0, title="Pilot")[0]
    assert not _run(monkeypatch, tmp_path, THREE_SCENES, 45.0)[0]
    assert not _run(monkeypatch, tmp_path, THREE_SCENES, 45.0, title="Pilot", english=False)[0]


def test_sonarrs_part_one_title_corroborates_the_joined_whole(monkeypatch, tmp_path):
    # Sonarr titles a two-parter "What You Leave Behind (1)"; the joined
    # recording is named as the whole.
    assert _run(monkeypatch, tmp_path, THREE_SCENES, 45.0, title="Pilot (1)")[0]
    assert not _run(monkeypatch, tmp_path, THREE_SCENES, 45.0, title="Pilot (2)")[0]


# The engine's map can leave a hole between two segments that is no segment at
# all (roundtable thread 349, round 2): video 383-670 here. The stretch cap
# must see it, and so must the total when the video's length is unknown.
HOLE = [
    _seg(0.0, 0.0, 300.0, 0.0, 300.0),
    _seg(5.0, 300.0, 363.0, 300.0, 360.0),
    _seg(1900.0, 363.0, 383.0, 360.0, 361.0),
    _seg(0.0, 670.0, 999.0, 361.0, 690.0),
    _seg(-99.68, 999.0, 1000.0, 690.0, 1000.0),
]


def test_a_hole_between_segments_counts_as_an_uncovered_stretch(monkeypatch, tmp_path):
    published, reason, _ = _run(monkeypatch, tmp_path, HOLE, 40.0, title="Pilot")
    assert not published
    # The unreplaced 363-383 s and the hole 383-670 s are one stretch.
    assert "6:03" in reason and "11:10" in reason


def test_an_unknown_video_length_does_not_hide_the_hole(monkeypatch, tmp_path):
    published, reason, _ = _run(monkeypatch, tmp_path, HOLE, 40.0, title="Pilot", picture=0.0)
    assert not published
    assert "6:03" in reason and "11:10" in reason


# The same three extra scenes, left as holes between segments rather than
# unreplaced segments, with the video's length unknown.
THREE_HOLES = [
    _seg(0.0, 0.0, 700.0, 0.0, 700.0), _seg(0.0, 980.0, 1700.0, 700.0, 1420.0),
    _seg(0.0, 1980.0, 2700.0, 1420.0, 2140.0), _seg(0.0, 2980.0, 3000.0, 2140.0, 2160.0),
]


def test_holes_with_the_length_unknown_still_need_corroboration(monkeypatch, tmp_path):
    published, reason, _ = _run(monkeypatch, tmp_path, THREE_HOLES, 45.0, picture=0.0)
    assert not published
    assert "not corroborated" in reason
    assert _run(monkeypatch, tmp_path, THREE_HOLES, 45.0, picture=0.0, title="Pilot")[0]


# Roundtable thread 349, round 3: holes totalling 51 % of a 1000 s picture, none
# over 95 s, 10 s of explicit unreplaced segment, length unknown. The 40 % gate
# used to need a known length or a note to fire at all.
MOSTLY_HOLES = [
    _seg(0.0, 0.0, 400.0, 0.0, 400.0), _seg(900.0, 400.0, 410.0, 400.0, 401.0),
    _seg(5.0, 410.0, 455.0, 401.0, 443.9), _seg(0.0, 550.0, 558.0, 443.9, 451.9),
    _seg(0.0, 653.0, 661.0, 451.9, 459.9), _seg(0.0, 756.0, 764.0, 459.9, 467.9),
    _seg(0.0, 859.0, 867.0, 467.9, 475.9), _seg(0.0, 962.0, 970.0, 475.9, 483.9),
    _seg(0.0, 995.0, 1000.0, 483.9, 488.9),
]


def test_holes_over_the_coverage_line_are_refused_with_the_length_unknown(monkeypatch, tmp_path):
    published, reason, _ = _run(monkeypatch, tmp_path, MOSTLY_HOLES, 40.0, title="Pilot", picture=0.0)
    assert not published
    assert "covers only part of the picture" in reason
    assert "8 min of 17 min" in reason


def test_a_map_far_shorter_than_its_own_extent_is_refused(monkeypatch, tmp_path):
    # Two segments 4,600 s apart with the length unknown: the extent says the
    # picture is at least 5,020 s and nearly all of it is uncovered.
    sparse = [_seg(0.0, 0.0, 380.0, 0.0, 380.0), _seg(0.0, 5000.0, 5020.0, 380.0, 400.0)]
    published, reason, _ = _run(monkeypatch, tmp_path, sparse, 40.0, title="Pilot", picture=0.0)
    assert not published


def test_no_probe_is_spent_on_a_map_the_stretch_cap_refuses(monkeypatch, tmp_path):
    probes = []
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: probes.append(p) or True)
    published, _, _ = _run(monkeypatch, tmp_path, OFFSET_JUMP, 40.0, title="Pilot", keep_probe=True)
    assert not published
    assert probes == []


def test_one_long_uncovered_stretch_is_refused_whatever_else_lines_up(monkeypatch, tmp_path):
    published, reason, _ = _run(monkeypatch, tmp_path, OFFSET_JUMP, 40.0, title="Pilot")
    assert not published
    assert "6:40" in reason and "11:39" in reason      # the stretch is named


def test_half_a_double_is_still_refused(monkeypatch, tmp_path):
    published, reason, decision = _run(monkeypatch, tmp_path, HALF_DOUBLE, 45.0)
    assert not published
    assert decision["outcome"] == "rejected"
    assert "covers only part of the picture" in reason


def test_a_clip_show_is_still_refused(monkeypatch, tmp_path):
    published, reason, _ = _run(monkeypatch, tmp_path, CLIP_SHOW, 35.0)
    assert not published
    assert "covers only part of the picture" in reason


def test_time_compressed_acts_are_still_refused(monkeypatch, tmp_path):
    published, reason, _ = _run(monkeypatch, tmp_path, SVU_AUDIOVAULT, 56.9, median=3.1)
    assert not published
    assert "no trusted sync signal" in reason


def test_one_title_card_seam_does_not_cost_a_corroborated_recording_its_rescue(monkeypatch, tmp_path):
    published, _, decision = _run(monkeypatch, tmp_path, SVU_LIVINGAUDIO, 25.1, title="Pilot")
    assert published
    assert decision["path"] == "corroborated-rescue"


def test_the_corroborated_rescue_still_needs_the_title_and_english(monkeypatch, tmp_path):
    assert not _run(monkeypatch, tmp_path, SVU_LIVINGAUDIO, 25.1, title="Other Episode")[0]
    assert not _run(monkeypatch, tmp_path, SVU_LIVINGAUDIO, 25.1, title="Pilot", english=False)[0]


def test_the_corroborated_floor_is_ninety_eight():
    base = dict(score=25.1, content_coverage=99.9, median_rate=0.0, total_runtime=2564.0,
                sync_ok=True, min_score=60.0, title_corroborated=True, primary_audio_english=True)
    assert _acceptance_decision(stable_fraction=98.7, **base)[0]
    assert _acceptance_decision(stable_fraction=98.0, **base)[0]
    assert not _acceptance_decision(stable_fraction=97.9, **base)[0]
