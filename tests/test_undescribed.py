"""Different-cut sources: report how much picture ends up without narration.

Scary Movie 3 (2026-09-14): the video was the unrated cut, the AudioVault TTS
description the theatrical one — 73 s of inserted footage in eight places.
The engine correctly leaves such footage on the original soundtrack, but the
listener had no way of knowing that stretches without narration were
expected. These tests pin the metric and the success note that now carries
that fact into the decision log and the Pushover message.
"""
import json

import describarr.workflow as workflow
from describarr.aligner import undescribed_seconds, undescribed_spans
from describarr.config import Config
from describarr.workflow import _undescribed_note, process_movie


def _write_report(tmp_path, segments):
    report = tmp_path / "ad_movie.txt"
    report.write_text("Input file similarity: 40.0%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": 40.0, "median_rate_pct": 0.0, "segments": segments,
    }))
    return report


def _seg(rate, v0, v1, a0, a1):
    return {"rate_pct": rate, "video_start_sec": v0, "video_end_sec": v1,
            "audio_start_sec": a0, "audio_end_sec": a1}


def test_unreplaced_segments_add_up_to_undescribed_and_dropped(tmp_path):
    report = _write_report(tmp_path, [
        _seg(0.0, 0.0, 79.0, 3.6, 82.6),
        _seg(2049604.0, 79.0, 98.5, 82.6, 82.6),        # inserted footage, no audio lost
        _seg(0.0, 98.5, 687.4, 82.6, 671.5),
        _seg(687.6, 687.4, 706.7, 659.9, 662.3),        # both skip: 19.3 s video, 2.4 s audio
        _seg(0.0, 706.7, 5136.0, 662.3, 5067.0),
    ])
    undescribed, dropped = undescribed_seconds(report)
    assert abs(undescribed - (19.5 + 19.3)) < 1e-6
    assert abs(dropped - 2.4) < 1e-6


def test_same_cut_seams_stay_below_the_note_threshold(tmp_path):
    # Broadcast commercial-break seams: sub-second video jumps, no audio lost.
    report = _write_report(tmp_path, [
        _seg(0.0, 1.0, 269.0, 0.0, 268.0),
        _seg(92033.0, 269.0, 269.9, 268.0, 268.0),
        _seg(0.0, 269.9, 707.1, 268.0, 705.2),
        _seg(96307.0, 707.1, 708.0, 705.2, 705.2),
        _seg(0.0, 708.0, 1308.0, 705.2, 1304.5),
    ])
    undescribed, dropped = undescribed_seconds(report)
    assert undescribed < 2.0 and dropped == 0.0
    assert _undescribed_note(undescribed, dropped) is None


def test_missing_report_is_zero(tmp_path):
    assert undescribed_seconds(None) == (0.0, 0.0)
    assert undescribed_seconds(tmp_path / "nope.txt") == (0.0, 0.0)


def test_note_wording_and_threshold():
    assert _undescribed_note(19.9, 0.0) is None
    note = _undescribed_note(74.7, 2.8)
    assert "different cut" in note
    assert "75 s of the picture" in note
    assert "3 s of narration" in note
    # Negligible dropped narration is not mentioned.
    assert "narration" not in _undescribed_note(30.0, 1.0)


def test_process_movie_passes_the_success_note_through(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Movie (2020).mkv"
    video.write_bytes(b"x")

    class FakeClient:
        def search_movies(self, title):
            return [{"name": "Movie (2020)", "url": "https://av/dl/1"}]

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a: tmp_path / "ad.mp3")
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(
        workflow, "_align_and_keep",
        lambda config, video_path, audio_path, label=None: (True, "AD source is a different cut: 75 s of the picture has no description"),
    )
    described, reason = process_movie(FakeClient(), config, video, "Movie", "2020")
    assert described
    assert reason.startswith("AD source is a different cut")


def test_notification_carries_the_note(monkeypatch, tmp_path):
    import describarr.server as server
    sent = []
    monkeypatch.setattr(server.notify, "send", lambda title, message: sent.append((title, message)))
    monkeypatch.setattr(server, "_log_terminal_decision", lambda *a, **k: None)
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    server._notify_outcome(config, "Scary Movie 3 (2003)", "described",
                           "AD source is a different cut: 75 s of the picture has no description")
    assert sent == [("describarr: Scary Movie 3 (2003)",
                     "Added and described. (AD source is a different cut: 75 s of the picture has no description)")]
    sent.clear()
    server._notify_outcome(config, "Scary Movie 3 (2003)", "described", None)
    assert sent == [("describarr: Scary Movie 3 (2003)", "Added and described.")]


# ── the note says where, and changes its words when most of the picture is bare ──

def test_spans_are_the_unreplaced_video_ranges_in_order(tmp_path):
    report = _write_report(tmp_path, [
        _seg(0.0, 1.4, 50.2, 0.0, 48.9),
        _seg(1619.8, 50.2, 94.4, 48.9, 51.4),           # the recap the AD omits
        _seg(0.0, 94.4, 513.8, 51.4, 470.9),
        _seg(118213.0, 513.8, 514.9, 470.9, 470.91),    # a seam
        _seg(0.0, 514.9, 1449.2, 470.91, 1401.8),
    ])
    assert undescribed_spans(report) == [(50.2, 94.4), (513.8, 514.9)]


def test_adjacent_spans_merge(tmp_path):
    report = _write_report(tmp_path, [
        _seg(500.0, 10.0, 20.0, 5.0, 6.0),
        _seg(0.0, 20.0, 20.5, 6.0, 6.5),
        _seg(500.0, 20.5, 30.0, 6.5, 7.5),
    ])
    assert undescribed_spans(report) == [(10.0, 30.0)]


def test_note_names_the_recap_position():
    note = _undescribed_note(44.2, 0.0, [(50.2, 94.4)], runtime=1449.0)
    assert note == "AD source is a different cut: 44 s of the picture has no description at 0:50–1:34"


def test_note_keeps_the_dropped_narration_tail():
    note = _undescribed_note(51.0, 5.3, [(50.2, 94.4), (513.8, 520.0)], runtime=1449.0)
    assert note.endswith("at 0:50–1:34, 8:34–8:40, 5 s of narration could not be placed")


def test_note_lists_the_longest_three_spans_and_counts_the_rest():
    spans = [(10.0, 30.0), (100.0, 101.0), (200.0, 240.0), (300.0, 302.0), (400.0, 470.0)]
    note = _undescribed_note(133.0, 0.0, spans, runtime=5000.0)
    assert " at 0:10–0:30, 3:20–4:00, 6:40–7:50 and 2 shorter" in note
    assert "2 min of the picture" in note                # minutes past two minutes


def test_half_a_double_episode_is_called_what_it_is():
    # Avatar S02E12-E13 (2026-09-16): a 47 min file described with E12's AD only.
    note = _undescribed_note(1371.7, 2.8, [(1451.9, 2798.4)], runtime=2842.6)
    assert note == ("description covers only part of the picture: 23 min of 47 min "
                    "has no description at 24:12–46:38, 3 s of narration could not be placed")


def test_note_without_spans_or_runtime_keeps_the_old_shape():
    assert _undescribed_note(75.0, 0.0) == "AD source is a different cut: 75 s of the picture has no description"
