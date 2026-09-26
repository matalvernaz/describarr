"""Per-act rate profile and the first-audio-track language check.

Segment lists are the real describealaign maps from 2026-09-26: This Is Us
S05E06 (acts at 0.0 % and ~1.5 %, refused by the single-rate rescue) and a
fragmented map of the kind a wrong donor leaves.
"""

import json

import describarr.aligner as aligner
from describarr.aligner import piecewise_rate_fraction, primary_audio_is_english


def _seg(rate, v0, v1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1}


# This Is Us S05E06, 1080p AMZN WEB-DL: six long acts, two of them ~1.5 % fast.
THIS_IS_US_S05E06 = [
    _seg(0.0, 0.0, 23.66),
    _seg(-86.3, 23.66, 23.665),
    _seg(-0.0, 23.665, 654.279),
    _seg(158208.7, 654.279, 655.786),
    _seg(-0.0, 655.786, 956.171),
    _seg(36017.4, 956.171, 956.515),
    _seg(-0.0, 956.515, 1217.647),
    _seg(103459.5, 1217.647, 1218.633),
    _seg(1.4, 1218.633, 1721.504),
    _seg(101613.3, 1721.504, 1722.472),
    _seg(-0.0, 1722.472, 2016.095),
    _seg(-2.9, 2016.095, 2016.100),
    _seg(1.5, 2016.100, 2559.337),
]


def _report(tmp_path, segments):
    report = tmp_path / "ep.txt"
    report.write_text("Input file similarity: 46.3%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": 46.3, "median_rate_pct": 0.0, "segments": segments,
    }))
    return report


def test_per_act_compression_is_almost_all_long_straight_segments(tmp_path):
    fraction = piecewise_rate_fraction(_report(tmp_path, THIS_IS_US_S05E06))
    assert fraction > 98.0


def test_a_fragmented_map_is_not(tmp_path):
    # A wrong donor: short pieces at erratic rates, nothing long and straight.
    choppy = [_seg(r, i * 40.0, i * 40.0 + 38.0) for i, r in enumerate([0.0, 7.5, -3.0, 12.0, 0.4] * 8)]
    assert piecewise_rate_fraction(_report(tmp_path, choppy)) == 0.0


def test_long_segments_beyond_two_percent_do_not_count(tmp_path):
    # PAL speed-up belongs to the drift rescue, not to this measure.
    pal = [_seg(4.27, 0.0, 1300.0), _seg(4.27, 1301.0, 2600.0)]
    assert piecewise_rate_fraction(_report(tmp_path, pal)) == 0.0


def test_missing_report_measures_nothing():
    assert piecewise_rate_fraction(None) == 0.0


def _probe(monkeypatch, audio_streams):
    streams = [{"codec_type": "video"}] + [
        {"codec_type": "audio", "tags": ({"language": lang} if lang is not None else {})}
        for lang in audio_streams
    ]
    monkeypatch.setattr(aligner, "_ffprobe_json", lambda path, extra_args=None: {"streams": streams})


def test_english_first_track_is_english(monkeypatch, tmp_path):
    _probe(monkeypatch, ["eng"])
    assert primary_audio_is_english(tmp_path / "v.mkv")


def test_french_first_multi_release_is_not(monkeypatch, tmp_path):
    # This Is Us S01E12 MULTi: French default first, English second. The engine
    # decodes the FIRST audio stream (-map 0:a:0), so this must be refused.
    _probe(monkeypatch, ["fre", "eng"])
    assert not primary_audio_is_english(tmp_path / "v.mkv")


def test_a_lone_untagged_track_counts(monkeypatch, tmp_path):
    _probe(monkeypatch, [None])
    assert primary_audio_is_english(tmp_path / "v.mkv")


def test_an_untagged_first_track_among_several_does_not(monkeypatch, tmp_path):
    _probe(monkeypatch, ["und", "eng"])
    assert not primary_audio_is_english(tmp_path / "v.mkv")


def test_a_failed_probe_is_not_english(monkeypatch, tmp_path):
    monkeypatch.setattr(aligner, "_ffprobe_json", lambda path, extra_args=None: None)
    assert not primary_audio_is_english(tmp_path / "v.mkv")
