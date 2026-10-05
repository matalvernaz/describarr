"""A copy whose first audio track is a dub is aligned against its English track,
and a copy with no English audio is refused at once.

Friends Seasons 1 and 3 (2026-10-04) were HDMAN "Dual Audio" BluRays: a
Portuguese dub first and flagged default, the English second. The engine
aligned only against the first track, so all 49 episodes were refused, each
after 3-10 minutes of trying every recording.
"""
from pathlib import Path

import pytest

import describarr.aligner as aligner
import describarr.server as srv
import describarr.workflow as workflow


def _probe(monkeypatch, languages):
    streams = [{"codec_type": "video"}] + [
        {"codec_type": "audio", "tags": ({"language": lang} if lang is not None else {})}
        for lang in languages
    ]
    monkeypatch.setattr(aligner, "_ffprobe_json", lambda path, extra_args=None: {"streams": streams})


@pytest.mark.parametrize("languages, stream", [
    (["eng"], 0),
    (["por", "eng"], 1),            # Friends' HDMAN layout
    (["fre", "spa", "eng"], 2),
    (["eng", "por"], 0),
    ([None, "eng"], 0),             # an untagged first track keeps the old behaviour
    (["und", "eng"], 0),
    (["por", None], 0),             # an untagged track might be the English one
    ([], 0),
    (["por", "spa"], None),         # no English anywhere
    (["por"], None),
])
def test_the_track_aligned_against(monkeypatch, languages, stream):
    _probe(monkeypatch, languages)
    assert aligner.reference_audio_stream(Path("v.mkv")) == stream


@pytest.mark.parametrize("languages, stream", [
    (["por", "en-US"], 1),          # a BCP 47 English tag
    (["por", "ENG"], 1),
    (["por", "English"], 1),
    (["pt-BR", "eng"], 1),
    (["mul", "eng"], 0),            # tags that do not say keep the first
    (["qaa", "eng"], 0),
    (["zxx", "eng"], 0),
])
def test_language_tags_are_read_for_what_they_say(monkeypatch, languages, stream):
    _probe(monkeypatch, languages)
    assert aligner.reference_audio_stream(Path("v.mkv")) == stream


def test_a_commentary_track_is_passed_over_for_the_programme(monkeypatch):
    streams = [{"codec_type": "audio", "tags": {"language": "por"}},
               {"codec_type": "audio", "tags": {"language": "eng", "title": "Director's Commentary"}},
               {"codec_type": "audio", "tags": {"language": "eng"}, "disposition": {"visual_impaired": 0}},
               {"codec_type": "audio", "tags": {"language": "eng"}, "disposition": {"comment": 1}}]
    monkeypatch.setattr(aligner, "_ffprobe_json", lambda path, extra_args=None: {"streams": streams})
    assert aligner.reference_audio_stream(Path("v.mkv")) == 2
    streams[2]["disposition"] = {"hearing_impaired": 1}
    # Only extra English tracks: the first of them, rather than the dub.
    assert aligner.reference_audio_stream(Path("v.mkv")) == 1


@pytest.mark.parametrize("languages", [["en-US"], ["mul"], ["qaa"], ["zxx"], ["xho"],
                                       ["por", "xho"], ["hun"], ["por", None]])
def test_only_known_foreign_languages_are_refused_at_once(monkeypatch, languages):
    _probe(monkeypatch, languages)
    assert aligner.foreign_only_audio(Path("v.mkv")) == []


def test_known_foreign_languages_are(monkeypatch):
    _probe(monkeypatch, ["pt-BR", "SPA"])
    assert aligner.foreign_only_audio(Path("v.mkv")) == ["pt", "spa"]
    assert str(workflow._no_english_reason(["pt", "spa"])) == (
        "this copy has no English audio, only Portuguese and Spanish")


def test_the_cut_short_check_measures_the_track_aligned_against(monkeypatch, tmp_path):
    _probe(monkeypatch, ["por", "eng"])
    seen = {}

    class _Done:
        stdout = "out_time_us=1000000\n"

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(workflow.subprocess, "run", fake_run)
    assert workflow._decoded_audio_seconds(tmp_path / "v.mkv") == 1.0
    assert seen["cmd"][seen["cmd"].index("-map") + 1] == "0:a:1"


def test_the_rescue_uses_the_runs_own_answer(monkeypatch, tmp_path):
    def second_probe(path):
        raise AssertionError("probed again")

    monkeypatch.setattr(workflow, "primary_audio_is_english", second_probe)
    monkeypatch.setattr(workflow, "donor_names_episode", lambda *a, **k: True)
    score = (workflow._CORROBORATED_MIN_SCORE + workflow._RESCUE_MIN_SCORE) / 2
    for aligned in (True, False):
        _, english = workflow._corroboration(tmp_path / "v.mkv", tmp_path / "Show S01E01 Pilot.mp3",
                                             "Pilot", score, "Show", aligned_english=aligned)
        assert english is aligned


def test_the_run_records_whether_it_aligned_english(monkeypatch, tmp_path):
    _probe(monkeypatch, ["por", "eng"])
    video = tmp_path / "v.mkv"
    video.write_bytes(b"v")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"a")
    out = tmp_path / "out"

    def fake_run(cmd):
        run_dir = Path(cmd[cmd.index("--output_dir") + 1])
        (run_dir / "ad_v.mkv").write_bytes(b"x")
        return 0, "", ""

    monkeypatch.setattr(aligner, "_run_subprocess", fake_run)
    monkeypatch.setattr(aligner, "_conform_pal_audio", lambda v, a, d: a)
    monkeypatch.setattr(aligner, "_find_output", lambda video_path, run_dir, min_mtime=0.0: run_dir / "ad_v.mkv")
    monkeypatch.setattr(aligner, "_find_report", lambda *a, **k: None)
    monkeypatch.setattr(aligner, "_validate_media_output", lambda source, output: True)
    result = aligner.run(video, audio, out, tmp_path / "align", stretch_audio=True)
    assert result is not None and result.aligned_english is True


def test_a_failed_probe_keeps_the_first_track(monkeypatch):
    monkeypatch.setattr(aligner, "_ffprobe_json", lambda path, extra_args=None: None)
    assert aligner.reference_audio_stream(Path("v.mkv")) == 0
    assert aligner.foreign_only_audio(Path("v.mkv")) == []


def _engine_command(monkeypatch, tmp_path):
    video = tmp_path / "Friends S01E01.mkv"
    video.write_bytes(b"v")
    audio = tmp_path / "ad.mp3"
    audio.write_bytes(b"a")
    seen = {}

    def fake_run(cmd):
        seen["cmd"] = cmd
        return 1, "", ""

    monkeypatch.setattr(aligner, "_run_subprocess", fake_run)
    monkeypatch.setattr(aligner, "_conform_pal_audio", lambda v, a, d: a)
    aligner.run(video, audio, tmp_path / "out", tmp_path / "align", stretch_audio=True)
    return seen["cmd"]


def test_the_engine_is_told_the_english_track(monkeypatch, tmp_path):
    _probe(monkeypatch, ["por", "eng"])
    cmd = _engine_command(monkeypatch, tmp_path)
    assert cmd[cmd.index("--audio_stream") + 1] == "1"


def test_an_english_first_copy_runs_as_before(monkeypatch, tmp_path):
    _probe(monkeypatch, ["eng", "por"])
    assert "--audio_stream" not in _engine_command(monkeypatch, tmp_path)


def test_the_image_pins_an_engine_that_knows_audio_stream():
    root = Path(__file__).resolve().parent.parent
    for name in ("Dockerfile", "pyproject.toml"):
        pins = [line for line in (root / name).read_text().splitlines() if "describealaign.git@" in line]
        assert len(pins) == 1, name
        version = pins[0].split("@v", 1)[1].split('"', 1)[0]
        assert tuple(int(x) for x in version.split(".")) >= (2, 2, 7), (name, version)


# --- a copy with no English audio --------------------------------------------

class _NoSearch:
    """A client that fails the test if anything is looked up."""

    def __getattr__(self, name):
        raise AssertionError(f"searched ({name}) a copy with no English audio")


def test_an_episode_with_no_english_audio_is_refused_without_a_search(monkeypatch, tmp_path):
    _probe(monkeypatch, ["por", "spa"])
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda path: False)
    video = tmp_path / "Friends S01E01.mkv"
    video.write_bytes(b"v")
    described, reason = workflow.process_episode(
        _NoSearch(), object(), video, "Friends", 1, 1)
    assert described is False
    assert isinstance(reason, workflow.NoEnglishAudio)
    assert str(reason) == "this copy has no English audio, only Portuguese and Spanish"


def test_a_film_with_no_english_audio_is_refused_without_a_search(monkeypatch, tmp_path):
    _probe(monkeypatch, ["fre"])
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda path: False)
    video = tmp_path / "Amelie.mkv"
    video.write_bytes(b"v")
    described, reason = workflow.process_movie(_NoSearch(), object(), video, "Amelie", "2001")
    assert (described, str(reason)) == (False, "this copy has no English audio, only French")


def test_the_refusal_reads_as_the_file_left_alone_not_as_a_mismatch():
    reason = workflow.NoEnglishAudio("this copy has no English audio, only Portuguese")
    assert srv._episode_outcome(False, reason) == "no_match"
    assert srv._notify_message("no_match", reason) == (
        "The file was left alone: this copy has no English audio, only Portuguese.")


def test_an_unknown_language_is_named_by_its_code(monkeypatch):
    assert str(workflow._no_english_reason(["por", "xho", "por"])) == (
        "this copy has no English audio, only Portuguese and xho")


# --- a double episode catalogued once, under its first number ---------------

def _catalogue(monkeypatch, tmp_path, files, lengths, video_length):
    """extract_episode finds *files* (episode -> name); lengths by file name."""
    paths = {ep: tmp_path / name for ep, name in files.items()}
    for path in paths.values():
        path.write_bytes(b"a")
    video = tmp_path / "Friends.S09E23E24.mkv"
    video.write_bytes(b"v")
    monkeypatch.setattr(workflow, "extract_episode", lambda zip_path, extract_dir, ep: paths.get(ep))
    monkeypatch.setattr(workflow, "_audio_duration",
                        lambda p: video_length if p == video else lengths.get(p.name, 0.0))
    monkeypatch.setattr(workflow, "_concat_audio", lambda parts, out: out)
    return video


def test_a_finale_filed_once_stands_for_the_whole_double_episode(monkeypatch, tmp_path):
    video = _catalogue(monkeypatch, tmp_path, {23: "Friends S09E23 The One in Barbados.mp3"},
                       {"Friends S09E23 The One in Barbados.mp3": 3000.0}, video_length=3006.0)
    donors = workflow._episode_donors(tmp_path / "s9.zip", tmp_path / "x", [23, 24],
                                      "Friends S09E23E24", video_path=video)
    assert [d.name for d in donors] == ["Friends S09E23 The One in Barbados.mp3"]


def test_half_a_double_episode_still_does_not(monkeypatch, tmp_path):
    # Avatar S02E12-E13: one half's description left 23 minutes silent.
    video = _catalogue(monkeypatch, tmp_path, {12: "Avatar S02E12.mp3"},
                       {"Avatar S02E12.mp3": 1380.0}, video_length=2760.0)
    assert workflow._episode_donors(tmp_path / "s2.zip", tmp_path / "x", [12, 13],
                                    "Avatar S02E12E13", video_path=video) == []


def test_both_halves_on_file_are_still_joined(monkeypatch, tmp_path):
    video = _catalogue(monkeypatch, tmp_path, {23: "9.23 Part 1.mp3", 24: "9.24 Part 2.mp3"},
                       {"9.23 Part 1.mp3": 1200.0, "9.24 Part 2.mp3": 1200.0}, video_length=2400.0)
    donors = workflow._episode_donors(tmp_path / "s9.zip", tmp_path / "x", [23, 24],
                                      "Friends S09E23E24", video_path=video)
    assert [d.name for d in donors] == ["E23E24.mp3"]


# --- second review round ------------------------------------------------------

@pytest.mark.parametrize("tag", ["qaa", "mul", "zxx", "mis"])
def test_a_tag_that_says_nothing_is_not_evidence_of_english(monkeypatch, tag):
    # A lone "original audio" track can be a dub; only no tag at all counts.
    _probe(monkeypatch, [tag])
    assert aligner.reference_audio_stream(Path("v.mkv")) == 0
    assert aligner.foreign_only_audio(Path("v.mkv")) == []
    assert not aligner.primary_audio_is_english(Path("v.mkv"))


@pytest.mark.parametrize("tag", [None, "und", "UND"])
def test_a_lone_untagged_track_still_counts(monkeypatch, tag):
    _probe(monkeypatch, [tag])
    assert aligner.primary_audio_is_english(Path("v.mkv"))


@pytest.mark.parametrize("title", ["English [Audio Description]", "Descriptive Audio",
                                   "Described Video", "AD", "Commentary"])
def test_an_unflagged_description_track_is_passed_over(monkeypatch, title):
    streams = [{"codec_type": "audio", "tags": {"language": "por"}},
               {"codec_type": "audio", "tags": {"language": "eng", "title": title}},
               {"codec_type": "audio", "tags": {"language": "eng", "title": "English"}}]
    monkeypatch.setattr(aligner, "_ffprobe_json", lambda path, extra_args=None: {"streams": streams})
    assert aligner.reference_audio_stream(Path("v.mkv")) == 2


def _gate_run(monkeypatch, tmp_path, undescribed_share, **kwargs):
    """_align_and_keep through the real gate, *undescribed_share* of the picture bare."""
    import json
    from describarr.config import Config
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    video = tmp_path / "v.mkv"
    video.write_bytes(b"v")
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"a")
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir(exist_ok=True)
    report.write_text("Input file similarity: 80%\n")
    covered = 3600 * (1 - undescribed_share)
    report.with_suffix(".json").write_text(json.dumps({"similarity_pct": 80.0, "median_rate_pct": 0.0,
        "segments": [{"rate_pct": 0.0, "video_start_sec": 0.0, "video_end_sec": covered,
                      "audio_start_sec": 0.0, "audio_end_sec": covered}]}))
    combined = tmp_path / "out" / "ad.mkv"
    combined.parent.mkdir(exist_ok=True)
    combined.write_bytes(b"z")
    monkeypatch.setattr(workflow, "align", lambda *a, **k: aligner.AlignResult(combined, report, None, returncode=0))
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: 3600.0 if p == video else 0.0)
    return workflow._align_and_keep(config, video, audio, label="x", **kwargs)[0]


def test_a_whole_hour_recording_is_held_to_a_tighter_limit(monkeypatch, tmp_path):
    # One part described, the other (30% of the hour) bare: under the usual
    # 40% it publishes; standing for a double episode it must not.
    assert _gate_run(monkeypatch, tmp_path / "a", 0.30)
    assert not _gate_run(monkeypatch, tmp_path / "b", 0.30,
                         max_undescribed_fraction=workflow._WHOLE_DOUBLE_MAX_UNDESCRIBED)
    assert _gate_run(monkeypatch, tmp_path / "c", 0.05,
                     max_undescribed_fraction=workflow._WHOLE_DOUBLE_MAX_UNDESCRIBED)


@pytest.mark.parametrize("joined, tighter", [(False, True), (True, False)])
def test_only_the_lone_recording_gets_the_tighter_limit(monkeypatch, tmp_path, joined, tighter):
    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: [])
    video = tmp_path / "Friends.S09E23E24.mkv"
    video.write_bytes(b"v")
    from describarr.config import Config
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")

    class Client:
        def search_shows(self, title):
            return [{"name": "Friends - Season 09 [US New Description] (2002)", "url": "u"}]

    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: tmp_path / "s9.zip")
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)

    def donors(zip_path, extract_dir, episodes, label, **k):
        if joined:
            return [extract_dir.with_name(extract_dir.name + "_multi") / "E23E24.mp3"]
        return [extract_dir / "Friends - Season 09" / "Friends S09E23 The One in Barbados.mp3"]

    seen = {}

    def keep(config, video_path, audio_path, **kwargs):
        seen.update(kwargs)
        return True, None

    monkeypatch.setattr(workflow, "_episode_donors", donors)
    monkeypatch.setattr(workflow, "_align_and_keep", keep)
    described, _ = workflow.process_episode(Client(), config, video, "Friends", 9, 23,
                                            extra_episodes=[24])
    assert described
    assert ("max_undescribed_fraction" in seen) is tighter
