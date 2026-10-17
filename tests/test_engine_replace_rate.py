"""describarr's copy of the engine's replace limit, the right way up.

describealaign replaces a segment when the recording's length over the video's,
audio/video, is within 0.1 of 1. A report gives each segment's rate the other way
up, rate_pct = (video/audio - 1) * 100, so the engine's band there is -9.09 % to
+11.11 %, not +-10 %. The old +-10 % here called Phineas and Ferb S01E07 (one line
at +10.29 %, which the engine replaced in full and measured in sync) undescribed,
refusing it with nothing described (2026-10-17); and the other way, it credited a
segment at -9.5 % with narration the engine never placed. These pin the engine's
rate test everywhere describarr counts what is described.
"""

import json

import pytest

import describarr.aligner as aligner
import describarr.workflow as workflow
from describarr.aligner import AlignResult, described_seconds, undescribed_seconds, undescribed_spans
from describarr.config import Config

engine_replaces_rate = getattr(aligner, "engine_replaces_rate", None)

# Phineas and Ferb S01E07's real map (describealign 2.3.0, 2026-10-17): one line at
# +10.29 % with two junction stubs, against "[S01.E01b] Phineas and Ferb - Candace
# Loses Her Head.mp3".
S01E07_SEGMENTS = [
    {"rate_pct": 10.285858701304228, "video_start_sec": 0.0, "video_end_sec": 405.53321177917826,
     "audio_start_sec": 51.10282957168761, "audio_end_sec": 418.8138095238095},
    {"rate_pct": 12.953773442696303, "video_start_sec": 405.53321177917826, "video_end_sec": 409.06274826170676,
     "audio_start_sec": 418.8138095238095, "audio_end_sec": 421.93857142857144},
    {"rate_pct": 10.285377723093681, "video_start_sec": 409.06274826170676, "video_end_sec": 525.9957084191763,
     "audio_start_sec": 421.93857142857144, "audio_end_sec": 527.9661904761905},
    {"rate_pct": 30580.688312545073, "video_start_sec": 525.9957084191763, "video_end_sec": 526.287905450734,
     "audio_start_sec": 527.9661904761905, "audio_end_sec": 527.9671428571429},
    {"rate_pct": 10.293158007776082, "video_start_sec": 526.287905450734, "video_end_sec": 555.995238095238,
     "audio_start_sec": 527.9671428571429, "audio_end_sec": 554.9020254746209},
]


def _report(tmp_path, segments, similarity=57.4, name="ep"):
    path = tmp_path / f"{name}.txt"
    path.write_text(f"Input file similarity: {similarity}%\n")
    path.with_suffix(".json").write_text(json.dumps({"similarity_pct": similarity, "median_rate_pct": 10.28,
                                                      "stable_trunk_fraction_pct": 99.3, "segments": segments}))
    return path


def _seg(rate, v0, v1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": v0 / (1 + rate / 100), "audio_end_sec": v1 / (1 + rate / 100)}


# ── the rate test ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rate, replaced", [
    (0.0, True), (4.27, True), (-4.27, True), (8.52, True),
    (10.0, True), (10.29, True), (10.5, True), (11.11, True),
    (11.12, False), (11.13, False), (12.95, False), (30580.69, False),          # audio/video 0.899928 at 11.12
    (-9.0, True), (-9.09, True), (-9.10, False), (-9.5, False), (-10.0, False),
    (-100.0, False), (-150.0, False),
])
def test_the_engines_rate_test(rate, replaced):
    assert engine_replaces_rate(rate) is replaced


@pytest.mark.parametrize("hundredths", range(-900, 1001, 5))
def test_nothing_inside_the_old_and_new_bands_alike_changes(hundredths):
    # From -9 % to +10 % the old +-10 % and the engine's band agree.
    rate = hundredths / 100.
    assert engine_replaces_rate(rate) is (abs(rate) <= 10.0)


# ── counting what is described ──────────────────────────────────────────────

def test_s01e07_is_described_as_the_engine_rendered_it(tmp_path):
    report = _report(tmp_path, S01E07_SEGMENTS)
    described = described_seconds(report)
    assert described == pytest.approx(405.53 + 116.93 + 29.71, abs=0.05)
    undescribed, _ = undescribed_seconds(report)
    assert undescribed == pytest.approx(3.53 + 0.29, abs=0.05)              # the two junction stubs
    assert [round(end - start, 1) for start, end in undescribed_spans(report)] == [3.5, 0.3]


@pytest.mark.parametrize("rate", [-9.5, 11.12])
def test_a_segment_just_past_the_engines_limit_is_not_described(tmp_path, rate):
    # A long stretch the engine left with the original soundtrack, on either
    # side of its limit, counts as undescribed in every figure: -9.5 % (the old
    # +-10 % credited it) and +11.12 % (audio/video 0.899928, just outside).
    segments = [_seg(0.5, 0., 600.), _seg(rate, 600., 1600.), _seg(0.5, 1600., 2200.)]
    report = _report(tmp_path, segments)
    assert described_seconds(report) == pytest.approx(1200.)
    undescribed, dropped = undescribed_seconds(report)
    assert undescribed == pytest.approx(1000.) and dropped > 800.
    assert undescribed_spans(report) == [(600., 1600.)]
    assert workflow._uncovered_stretches(report, 2200.) == [(600., 1600.)]


# ── the steep-drift band ────────────────────────────────────────────────────

@pytest.mark.parametrize("rate, inside", [
    (6.0, False), (6.01, True), (10.0, True), (10.5, True), (11.11, True), (11.12, False),
    (-6.01, True), (-9.09, True), (-9.10, False), (-9.5, False),
])
def test_the_steep_band_is_past_six_up_to_the_engines_limit(rate, inside):
    assert workflow._steep_band(rate) is inside


# ── through _align_and_keep ─────────────────────────────────────────────────

def _keep(monkeypatch, tmp_path, segments, *, donor, title, similarity=57.4, picture=556.0):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "Phineas.and.Ferb.S01E07.Candace.Loses.Her.Head.1080p.DSNP.WEBRip.DDP.2.0.H.265-iVy.mkv"
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
    report.with_suffix(".json").write_text(json.dumps({"similarity_pct": similarity, "median_rate_pct": 10.28,
                                                        "stable_trunk_fraction_pct": 99.3, "segments": segments}))
    published = []
    monkeypatch.setattr(workflow, "align", lambda *a, **k: AlignResult(combined, report, None, returncode=0, aligned_english=True))
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: published.append(a))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: picture)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: True)
    ok, reason = workflow._align_and_keep(config, video, audio, label="Phineas and Ferb S01E07", episode_title=title,
                                          series_title="Phineas and Ferb", source="AudioVault")
    decision = json.loads((config.cache_dir / "decisions.json").read_text())[-1]
    return ok, reason, published, decision


def test_s01e07_publishes_from_its_own_recording(monkeypatch, tmp_path):
    ok, _, published, decision = _keep(monkeypatch, tmp_path, S01E07_SEGMENTS,
                                       donor="[S01.E01b] Phineas and Ferb - Candace Loses Her Head.mp3",
                                       title="Candace Loses Her Head")
    assert ok and len(published) == 1
    assert decision["path"] == "steep-drift-rescue"
    assert decision["described_rate"] == pytest.approx(10.29, abs=0.01)


def test_s01e07_against_another_episodes_recording_stays_refused(monkeypatch, tmp_path):
    ok, _, published, decision = _keep(monkeypatch, tmp_path, S01E07_SEGMENTS,
                                       donor="[S01.E01a] Phineas and Ferb - Rollercoaster.mp3",
                                       title="Candace Loses Her Head")
    assert not ok and not published
    assert decision["path"] == "reject"
