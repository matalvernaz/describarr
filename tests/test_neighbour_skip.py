"""The neighbour search is for recordings filed under the wrong number, not for a
recording that matched and was refused on its timing.

SVU (2026-10-16): S10E04's own recordings, AudioVault "05 Lunacy" at 53.6 % and
LivingAudio "10.04 Lunacy" at 45.0 %, were refused for act-level speed-ups, and
the walk then aligned four other episodes' recordings at 0.8-1.7 %, about five
minutes each. A recording that scores like that holds this episode; the search
exists for one that held something else (Deep Space Nine's season 4 numbered one
behind: its own pick does not even score, the engine refuses the pair). The
search now runs only when no own recording scored at describealign's own
"likely mismatched" line, 20 %.
"""

import json
import logging
import zipfile

import pytest

import describarr.workflow as workflow
from describarr.aligner import AlignResult, EngineFailure
from describarr.config import Config
from describarr.workflow import process_episode

SHOW = "Law & Order: Special Victims Unit"
FOLDER = "Law & Order - Special Victims Unit - Season 10 (2008)"
PACK = ["01 Trials.mp3", "02 Confession.mp3", "03 Swing.mp3", "04 Retro.mp3",
        "05 Lunacy.mp3", "06 Babes.mp3"]
TITLE_PICK, NUMBER_PICK = "05 Lunacy.mp3", "04 Retro.mp3"     # library S10E04 is "Lunacy"
NEIGHBOURS = {"03 Swing.mp3", "02 Confession.mp3", "06 Babes.mp3", "01 Trials.mp3"}
WOBBLY = "similarity 53.6% (coverage 99.9%, stable trunk 31.4%, median rate 2.30%, sync_ok=False) — no trusted sync signal"
MISMATCH = "No obvious cause from energy analysis."


class Measured(str):
    """What the gate hands back for a refusal it measured: the detail, and the score."""

    def __new__(cls, detail, score):
        obj = super().__new__(cls, detail)
        obj.score = score
        return obj


def _walk(monkeypatch, tmp_path, verdicts, *, publish=None):
    """process_episode for library S10E04 "Lunacy" over a six-file pack; *verdicts*
    maps a donor to what the gate says; *publish* is a donor the gate accepts."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in PACK:
            zf.writestr(f"{FOLDER}/{name}", name.encode())
    video = tmp_path / "Law & Order - Special Victims Unit" / "Season 10" / "Law.and.Order.SVU.S10E04.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")

    class Client:
        def search_shows(self, query):
            return [{"name": FOLDER, "url": "https://av/pack"}]

    tried = []

    def gate(config, video_path, audio_path, label=None, **_):
        tried.append(audio_path.name)
        if audio_path.name == publish:
            return True, None
        return False, verdicts.get(audio_path.name, Measured("similarity 1.5% — no trusted sync signal", 1.5))

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: None)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "_align_and_keep", gate)
    described, reason = process_episode(Client(), config, video, SHOW, 10, 4, series_year="1999",
                                        episode_title="Lunacy")
    return described, reason, tried


def _nearby(tried):
    return [t for t in tried if t in NEIGHBOURS]


# ── the gate hands its score back ────────────────────────────────────────────

def _keep(monkeypatch, tmp_path, result):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "S10E04.mkv"
    video.write_bytes(b"x")
    audio = tmp_path / "05 Lunacy.mp3"
    audio.write_bytes(b"y")
    monkeypatch.setattr(workflow, "align", lambda *a, **k: result)
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: 2640.0)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: True)
    return workflow._align_and_keep(config, video, audio, label="SVU S10E04", episode_title="Lunacy")


def test_a_refusal_the_gate_measured_carries_its_score(monkeypatch, tmp_path):
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir()
    report.write_text("Input file similarity: 55.9%\n")
    segments = [{"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1, "audio_start_sec": a0,
                 "audio_end_sec": a1} for rate, v0, v1, a0, a1 in
                [(2.3, 0, 600, 0, 586), (3.9, 600, 1300, 586, 1260), (2.8, 1300, 2000, 1260, 1940),
                 (3.5, 2000, 2640, 1940, 2558)]]
    report.with_suffix(".json").write_text(json.dumps({"similarity_pct": 55.9, "median_rate_pct": 3.0,
                                                       "segments": segments}))
    combined = tmp_path / "ad_ep.mkv"
    combined.write_bytes(b"z")
    published, reason = _keep(monkeypatch, tmp_path, AlignResult(combined, report, None, returncode=0))
    assert not published
    assert isinstance(reason, str) and reason == str(reason) and reason.startswith("similarity 55.9%")
    assert reason.score == pytest.approx(55.9)


def test_a_refusal_the_engine_made_carries_none(monkeypatch, tmp_path):
    published, reason = _keep(monkeypatch, tmp_path, AlignResult(
        None, None, "AD audio is 2.33× the video duration (21.6 min vs 9.3 min)", returncode=1))
    assert not published and getattr(reason, "score", None) is None


# ── when the neighbours are searched ─────────────────────────────────────────

def test_an_own_recording_that_matched_skips_the_neighbours(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="describarr.workflow"):
        described, reason, tried = _walk(monkeypatch, tmp_path, {TITLE_PICK: Measured(WOBBLY, 53.6)})
    assert not described and _nearby(tried) == []
    assert str(reason) == WOBBLY and reason.nearby == 0        # never claims nearby recordings were tried
    skips = [r.getMessage() for r in caplog.records if "filed near it" in r.getMessage()]
    assert len(skips) == 1 and "53.6" in skips[0] and TITLE_PICK in skips[0]


def test_own_recordings_that_did_not_match_still_find_the_neighbour(monkeypatch, tmp_path):
    # The Deep Space Nine shape: the episode's own recordings hold something
    # else; its recording is filed under the next number.
    described, reason, tried = _walk(monkeypatch, tmp_path, {}, publish="06 Babes.mp3")
    assert described and "06 Babes.mp3" in tried


@pytest.mark.parametrize("score, searched", [(20.0, False), (19.9, True)])
def test_the_line_on_the_titles_pick(monkeypatch, tmp_path, score, searched):
    _, _, tried = _walk(monkeypatch, tmp_path, {TITLE_PICK: Measured(WOBBLY, score)})
    assert bool(_nearby(tried)) is searched


@pytest.mark.parametrize("score, searched", [(20.0, False), (19.9, True)])
def test_the_line_on_the_numbers_pick(monkeypatch, tmp_path, score, searched):
    _, _, tried = _walk(monkeypatch, tmp_path, {TITLE_PICK: MISMATCH, NUMBER_PICK: Measured(WOBBLY, score)})
    assert bool(_nearby(tried)) is searched


def test_refusals_without_a_score_still_search_nearby(monkeypatch, tmp_path):
    _, _, tried = _walk(monkeypatch, tmp_path, {TITLE_PICK: MISMATCH, NUMBER_PICK: MISMATCH})
    assert _nearby(tried)


def test_a_low_score_and_an_engine_failure_still_search_nearby(monkeypatch, tmp_path):
    _, _, tried = _walk(monkeypatch, tmp_path, {
        TITLE_PICK: Measured(WOBBLY, 15.0), NUMBER_PICK: EngineFailure("alignment failed (describealaign exit 1)")})
    assert _nearby(tried)


def test_engine_failures_alone_search_nothing_nearby(monkeypatch, tmp_path):
    crash = EngineFailure("alignment failed (describealaign exit 1)")
    _, reason, tried = _walk(monkeypatch, tmp_path, {name: crash for name in PACK})
    assert _nearby(tried) == [] and isinstance(reason, EngineFailure)


def test_a_neighbours_own_score_never_counts(monkeypatch, tmp_path):
    # The first neighbour tried scores 30 % and is refused: it is not this
    # episode's own recording, so the rest of the search goes on.
    verdicts = {TITLE_PICK: Measured("similarity 1.5%", 1.5), NUMBER_PICK: Measured("similarity 1.2%", 1.2),
                "03 Swing.mp3": Measured("similarity 30.0%", 30.0), "06 Babes.mp3": Measured("similarity 30.0%", 30.0)}
    _, _, tried = _walk(monkeypatch, tmp_path, verdicts)
    assert len(_nearby(tried)) >= 2
