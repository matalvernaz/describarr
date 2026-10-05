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
