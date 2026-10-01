"""What a refusal says it tried, and a library file that is itself broken.

Family Guy, 2026-10-01: S06E12, S07E05 and S07E06 were refused after BOTH
catalogues' recordings had been aligned, and S23E06's second copy was skipped as
byte-identical, yet every Pushover read "Found an audio description, but it did
not line up", as if the second catalogue had never been asked. S23E06's own file
stops at 8:38 of 21:37, so no recording could ever line up with it, and the
notice said only "describarr hit an error".
"""

import json
import shutil
import subprocess

import pytest

import describarr.server as srv
import describarr.workflow as workflow
from describarr.aligner import OUTPUT_VALIDATION_FAILED, AlignResult, EngineFailure
from describarr.config import Config
from describarr.workflow import DamagedSource, Refusal, process_episode


# ── the message names every source asked ─────────────────────────────────────

def test_one_recording_names_its_source():
    msg = srv._notify_message("no_match", Refusal("similarity 18.0% (coverage 99.4%)", ["AudioVault"]))
    assert msg == ("Found an audio description on AudioVault, but it did not line up with this "
                   "copy, so the file was left alone. (match score 18%)")


def test_several_recordings_are_counted_by_source():
    reason = Refusal("similarity 1.8% (coverage 100.0%)", ["AudioVault", "AudioVault", "SecondCatalogue"])
    msg = srv._notify_message("no_match", reason)
    assert msg.startswith("Tried 3 audio descriptions (2 from AudioVault, 1 from SecondCatalogue); "
                          "none lined up with this copy")
    assert msg.endswith("(match score 2%)")


def test_an_identical_copy_is_named():
    reason = Refusal("similarity 18.0%", ["AudioVault"], same=["SecondCatalogue"])
    assert "SecondCatalogue had the same recording." in srv._notify_message("no_match", reason)


def test_recordings_tried_nearby_are_counted_apart():
    reason = Refusal("No obvious cause from energy analysis.", ["AudioVault", "SecondCatalogue"], nearby=3)
    msg = srv._notify_message("no_match", reason)
    assert "(1 from AudioVault, 1 from SecondCatalogue) and 3 more filed near this episode" in msg
    assert msg.endswith("(the sound did not match)")


def test_a_plain_reason_still_reads_as_before():
    msg = srv._notify_message("no_match", "similarity 20.8% (coverage 99.9%)")
    assert msg.startswith("Found an audio description, but it did not line up with this copy")


def test_a_damaged_file_says_so():
    msg = srv._notify_message("no_match", DamagedSource(
        "this copy is cut short (its sound stops at 8:38 of 21:37), so no description can "
        "line up with it; a fresh download should fix it"))
    assert msg.startswith("The file was left alone: this copy is cut short")
    assert "did not line up" not in msg


# ── the walk records where each refused recording came from ──────────────────

class _Source:
    """An extra source with one recording for the episode."""

    def __init__(self, path, label=None):
        self.path = path
        if label is not None:
            self.label = label

    def episode_candidates(self, cache_dir, series_title, season, episode, episode_title=""):
        return [self.path]

    def close(self):
        pass


def _refused_walk(monkeypatch, tmp_path, extra_bytes, label="SecondCatalogue"):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Family.Guy.S17E16.You.Cant.Handle.the.Booth.1080p.DSNP.WEB-DL.DDP5.1.H.264-FLUX.mkv"
    video.write_bytes(b"v")
    av = tmp_path / "av" / "16 You Can't Handle the Booth.mp3"
    av.parent.mkdir()
    av.write_bytes(b"the AudioVault recording")
    la = tmp_path / "la" / "17.16 You Can't Handle the Booth.mp3"
    la.parent.mkdir()
    la.write_bytes(extra_bytes)

    class Client:
        def search_shows(self, title):
            return [{"name": "Family Guy - Season 17 (2018)", "url": "https://av/17"}]

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: tmp_path / "s17.zip")
    monkeypatch.setattr(workflow, "_episode_donors", lambda *a, **k: [av])
    monkeypatch.setattr(workflow, "neighbour_donors", lambda *a, **k: [])
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [_Source(la, label)])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda *a, **k: (False, "similarity 18.0% (coverage 99.4%) — refused"))
    return process_episode(Client(), config, video, "Family Guy", 17, 16, series_year="1999")


def test_both_catalogues_are_recorded(monkeypatch, tmp_path):
    described, reason = _refused_walk(monkeypatch, tmp_path, b"a different recording")
    assert not described
    assert isinstance(reason, Refusal)
    assert reason.sources == ("AudioVault", "SecondCatalogue")
    assert reason.startswith("similarity 18.0%")           # the reason itself is unchanged


def test_an_identical_second_copy_is_recorded_as_the_same(monkeypatch, tmp_path):
    _, reason = _refused_walk(monkeypatch, tmp_path, b"the AudioVault recording")
    assert reason.sources == ("AudioVault",)
    assert reason.same == ("SecondCatalogue",)


def test_a_source_without_a_label_is_another_source(monkeypatch, tmp_path):
    _, reason = _refused_walk(monkeypatch, tmp_path, b"a different recording", label=None)
    assert reason.sources == ("AudioVault", "another source")


def test_an_engine_failure_keeps_its_type():
    assert workflow._as_refusal(EngineFailure("crash"), ["AudioVault"], []) == "crash"
    assert isinstance(workflow._as_refusal(EngineFailure("crash"), ["AudioVault"], []), EngineFailure)
    assert workflow._as_refusal(None, ["AudioVault"], []) is None


# ── a video whose sound stops early ──────────────────────────────────────────

def test_a_short_decode_is_a_cut(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: 1297.6)
    monkeypatch.setattr(workflow, "_decoded_audio_seconds", lambda p: 517.8)
    assert workflow._truncation(tmp_path / "v.mkv") == "its sound stops at 8:38 of 21:38"


@pytest.mark.parametrize("heard", [1297.0, 1280.0, 0.0])
def test_a_whole_or_unreadable_file_is_no_cut(monkeypatch, tmp_path, heard):
    # 17.6 s short of 1297.6 is inside the tolerance; 0.0 means it could not be read.
    monkeypatch.setattr(workflow, "_audio_duration", lambda p: 1297.6)
    monkeypatch.setattr(workflow, "_decoded_audio_seconds", lambda p: heard)
    assert workflow._truncation(tmp_path / "v.mkv") is None


def test_a_failed_validation_on_a_cut_file_ends_the_walk(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Family.Guy.S23E06.Dog.is.My.Co-Pilot.1080p.DSNP.WEB-DL.DDP5.1.H.264-STC.mkv"
    video.write_bytes(b"v")
    donors = []
    for name in ("[S23.E06] Dog is My Co-Pilot.mp3", "[S23.E06] Dog is My Co-Pilot [TTS].mp3"):
        donor = tmp_path / name
        donor.write_bytes(name.encode())
        donors.append(donor)
    aligned = []

    class Client:
        def search_shows(self, title):
            return [{"name": "Family Guy - Season 23 (2025)", "url": "https://av/23"}]

    def align(video_path, audio_path, *a, **k):
        aligned.append(audio_path.name)
        return AlignResult(None, None, EngineFailure(OUTPUT_VALIDATION_FAILED), returncode=0)

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: tmp_path / "s23.zip")
    monkeypatch.setattr(workflow, "_episode_donors", lambda *a, **k: donors)
    monkeypatch.setattr(workflow, "load_extra_sources",
                        lambda: pytest.fail("a cut file must not be offered to another source"))
    monkeypatch.setattr(workflow, "align", align)
    monkeypatch.setattr(workflow, "_truncation", lambda p: "its sound stops at 8:38 of 21:38")
    described, reason = process_episode(Client(), config, video, "Family Guy", 23, 6, series_year="1999")
    assert not described
    assert isinstance(reason, DamagedSource)
    assert "cut short (its sound stops at 8:38 of 21:38)" in reason
    assert aligned == ["[S23.E06] Dog is My Co-Pilot.mp3"]          # the walk stopped there


def test_a_failed_validation_on_a_whole_file_is_still_an_engine_failure(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "v.mkv"
    video.write_bytes(b"v")
    donor = tmp_path / "d.mp3"
    donor.write_bytes(b"d")
    monkeypatch.setattr(workflow, "align", lambda *a, **k: AlignResult(
        None, None, EngineFailure(OUTPUT_VALIDATION_FAILED), returncode=0))
    monkeypatch.setattr(workflow, "_truncation", lambda p: None)
    published, reason = workflow._align_and_keep(config, video, donor, label="x")
    assert not published and isinstance(reason, EngineFailure)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_really_truncated_file_is_measured(tmp_path):
    """A real incomplete download: the header still says 60 s, the data stops early."""
    whole = tmp_path / "whole.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
                    "-c:a", "aac", "-b:a", "128k", str(whole)], check=True)
    cut = tmp_path / "cut.mkv"
    data = whole.read_bytes()
    cut.write_bytes(data[: len(data) * 2 // 5])
    assert workflow._audio_duration(cut) == pytest.approx(60.0, abs=1.0)    # what the header promises
    heard = workflow._decoded_audio_seconds(cut)
    assert 10.0 < heard < 40.0                                              # what the file holds
    assert workflow._truncation(cut).startswith("its sound stops at 0:")
    assert workflow._truncation(whole) is None
