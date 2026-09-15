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
from describarr.aligner import undescribed_seconds
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
