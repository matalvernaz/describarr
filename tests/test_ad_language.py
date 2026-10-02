"""Every published AD track is labelled English (engine v2.2.6).

Before this, describealaign gave the AD track a title and a disposition but no
language. Jellyfin's Smart subtitle mode read the unlabelled default track as
foreign and turned on full English subtitles over English audio, and EchoFin's
Spoken Subtitles read every line aloud over the show (2026-10-02, 3,149
described titles).
"""
from pathlib import Path

import pytest

import describarr.aligner as aligner


def _out_probe(language=None, *, ad=True):
    streams = [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}]
    if ad:
        tags = {"title": "AD"}
        if language is not None:
            tags["language"] = language
        streams.append({"codec_type": "audio", "tags": tags,
                        "disposition": {"default": 1, "visual_impaired": 1}})
    streams.append({"codec_type": "audio", "tags": {"title": "original", "language": "eng"},
                    "disposition": {"default": 0}})
    return {"streams": streams, "format": {"duration": "1350.0"}}


def _src_probe():
    return {"streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"codec_type": "audio", "tags": {"language": "eng"}, "disposition": {"default": 1}},
    ], "format": {"duration": "1350.0"}}


def _validate(monkeypatch, out, output_name):
    monkeypatch.setattr(aligner, "_ffprobe_json",
                        lambda p, *a, **k: _src_probe() if "src" in str(p) else out)
    monkeypatch.setattr(aligner, "_video_packet_count", lambda *a, **k: 1000)
    return aligner._validate_media_output(Path("/tv/src.mkv"), Path(f"/tv/{output_name}"))


@pytest.mark.parametrize("name", ["out.mkv", "out.mp4", "out.m4v"])
def test_an_english_ad_track_publishes(monkeypatch, name):
    assert _validate(monkeypatch, _out_probe("eng"), name) is True


@pytest.mark.parametrize("language", [None, "", "und", "fre"])
@pytest.mark.parametrize("name", ["out.mkv", "out.mp4"])
def test_an_unlabelled_or_wrong_ad_track_is_refused(monkeypatch, language, name):
    assert _validate(monkeypatch, _out_probe(language), name) is False


def test_the_refusal_says_why(monkeypatch, caplog):
    _validate(monkeypatch, _out_probe(None), "out.mkv")
    assert any("refusing to publish an unlabelled AD track" in r.getMessage()
               for r in caplog.records)


def test_avi_cannot_carry_a_language_and_still_publishes(monkeypatch):
    assert _validate(monkeypatch, _out_probe(None), "out.avi") is True


def test_the_language_check_reads_the_first_audio_track(monkeypatch):
    # The English "original" track behind an unlabelled AD must not satisfy it.
    probe = _out_probe(None)
    assert probe["streams"][2]["tags"]["language"] == "eng"
    assert aligner._has_expected_ad_language(probe, Path("/tv/out.mkv")) is False


def test_the_engine_is_asked_for_english(tmp_path, monkeypatch):
    video = tmp_path / "Show S01E01.mkv"
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
    cmd = seen["cmd"]
    assert cmd[cmd.index("--ad_language") + 1] == "eng"


def test_the_image_pins_an_engine_that_knows_the_flag():
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    pins = [line for line in dockerfile.splitlines() if "describealaign.git@" in line]
    assert len(pins) == 1
    version = pins[0].split("@v", 1)[1].split('"', 1)[0]
    assert tuple(int(x) for x in version.split(".")) >= (2, 2, 6), version
