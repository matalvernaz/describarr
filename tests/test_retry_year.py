"""A manual /retry carries no ``sonarr_series_year``, so the series year is
read off the disk layout — folder suffix, then the Sonarr-style episode
filename, then ``tvshow.nfo`` — and travels with every queued item into
``process_episode``.

Without it ``find_season`` cannot tell a show from a same-titled reboot: on
2026-09-16 "Avatar: The Last Airbender - Season 1 (2005)" and "... Season 1
(2024)" both scored 1.00 and catalogue order alone picked the winner.
"""

import types

import describarr.server as srv
from describarr.retry_queue import RetryQueue


class _FakePending:
    def __init__(self):
        self.pushed = []

    def push(self, item):
        self.pushed.append(item)


def _layout(tmp_path, folder="Show", filename="Show - S01E01.mkv", nfo=None):
    show = tmp_path / "tv" / folder
    season_dir = show / "Season 1"
    season_dir.mkdir(parents=True)
    video = season_dir / filename
    video.write_bytes(b"x")
    if nfo is not None:
        (show / "tvshow.nfo").write_text(nfo, encoding="utf-8")
    return show, video


# ── reading the year off the layout ─────────────────────────────────────────

def test_filename_year_before_the_episode_marker_is_the_series_year():
    name = (
        "Avatar The Last Airbender (2005) - S01E01 - The Boy in the Iceberg "
        "(1080p WEB-DL x265 RCVR).mkv"
    )
    assert srv._series_year_from_filename(name) == "2005"


def test_a_year_inside_the_episode_title_is_not_the_series_year():
    assert srv._series_year_from_filename("Show - S01E05 - Party Like It's (1999).mkv") == ""


def test_scene_style_name_without_a_year_gives_nothing():
    assert srv._series_year_from_filename("Show.S01E01.720p.WEB-DL.mkv") == ""


def test_nfo_year_tag_survives_bom_and_cdata(tmp_path):
    show, _ = _layout(
        tmp_path,
        nfo='﻿<?xml version="1.0" encoding="utf-8"?>\n<tvshow>\n'
            "  <plot><![CDATA[A boy (2024) wakes up.]]></plot>\n"
            "  <year>1996</year>\n  <premiered>1996-01-09</premiered>\n</tvshow>\n",
    )
    assert srv._series_year_from_nfo(show) == "1996"


def test_nfo_premiered_date_when_there_is_no_year_tag(tmp_path):
    show, _ = _layout(tmp_path, nfo="<tvshow><premiered>1996-01-09</premiered></tvshow>")
    assert srv._series_year_from_nfo(show) == "1996"


def test_missing_nfo_gives_nothing(tmp_path):
    show, _ = _layout(tmp_path)
    assert srv._series_year_from_nfo(show) == ""


def test_folder_suffix_outranks_filename_and_nfo(tmp_path):
    show, video = _layout(
        tmp_path, folder="Show (2009)", filename="Show (2005) - S01E01.mkv",
        nfo="<tvshow><year>1999</year></tvshow>",
    )
    assert srv._infer_series_year(show, video.name) == "2009"


def test_filename_outranks_nfo(tmp_path):
    show, video = _layout(
        tmp_path, filename="Show (2005) - S01E01.mkv", nfo="<tvshow><year>1999</year></tvshow>",
    )
    assert srv._infer_series_year(show, video.name) == "2005"


def test_nfo_is_the_last_resort(tmp_path):
    show, video = _layout(tmp_path, nfo="<tvshow><year>1999</year></tvshow>")
    assert srv._infer_series_year(show, video.name) == "1999"


def test_no_year_anywhere_is_empty(tmp_path):
    show, video = _layout(tmp_path)
    assert srv._infer_series_year(show, video.name) == ""


def test_series_dir_is_above_the_season_folder(tmp_path):
    show, video = _layout(tmp_path)
    assert srv._series_dir_of(video) == show


def test_series_dir_is_the_parent_when_there_is_no_season_layer(tmp_path):
    flat = tmp_path / "tv" / "Flat"
    flat.mkdir(parents=True)
    video = flat / "Flat - S01E01.mkv"
    assert srv._series_dir_of(video) == flat


def test_infer_retry_params_reads_the_year_off_the_filename(tmp_path):
    show, video = _layout(
        tmp_path, folder="Avatar The Last Airbender",
        filename="Avatar The Last Airbender (2005) - S01E01 - The Boy in the Iceberg.mkv",
    )
    out = srv._infer_retry_params(str(video), "")
    assert out["title"] == "Avatar The Last Airbender"
    assert out["year"] == "2005"
    assert (out["season"], out["episode"]) == ("1", "1")


def test_infer_retry_params_reads_the_year_off_the_nfo_for_a_season_dir(tmp_path):
    show, _ = _layout(tmp_path, nfo="<tvshow><year>1996</year></tvshow>")
    out = srv._infer_retry_params("", str(show / "Season 1"))
    assert out == {"title": "Show", "year": "1996"}


def test_infer_retry_params_still_has_no_year_when_the_layout_has_none(tmp_path):
    show, video = _layout(tmp_path)
    assert "year" not in srv._infer_retry_params(str(video), "")


# ── the year travels with the queued items ──────────────────────────────────

def test_retry_dir_stamps_the_inferred_year_on_every_episode(tmp_path, monkeypatch):
    show, _ = _layout(
        tmp_path, folder="Avatar The Last Airbender",
        filename="Avatar The Last Airbender (2005) - S01E01.mkv",
    )
    (show / "Season 1" / "Avatar The Last Airbender (2005) - S01E02.mkv").write_bytes(b"x")
    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: False)
    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Avatar: The Last Airbender", "dir": str(show)},
        types.SimpleNamespace(cache_dir=tmp_path / "cache"), pending,
    )
    assert [i["series_year"] for i in pending.pushed] == ["2005", "2005"]


def test_retry_dir_prefers_the_year_the_request_carried(tmp_path, monkeypatch):
    show, _ = _layout(tmp_path, filename="Show (2005) - S01E01.mkv")
    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: False)
    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Show", "dir": str(show), "year": "2009"},
        types.SimpleNamespace(cache_dir=tmp_path / "cache"), pending,
    )
    assert pending.pushed[0]["series_year"] == "2009"


def test_retry_dir_omits_the_key_when_no_year_is_known(tmp_path, monkeypatch):
    show, _ = _layout(tmp_path)
    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: False)
    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Show", "dir": str(show)},
        types.SimpleNamespace(cache_dir=tmp_path / "cache"), pending,
    )
    assert "series_year" not in pending.pushed[0]


def _run_retry_episode(monkeypatch, item):
    seen = {}

    def fake_process_episode(client, config, video_path, title, season, episode, **kwargs):
        seen.update(kwargs)
        return True, None

    monkeypatch.setattr(srv, "process_episode", fake_process_episode)
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(srv, "_notify_outcome", lambda *a, **k: None)
    srv._worker_handle_retry_episode(item, types.SimpleNamespace(), _FakePending())
    return seen


def test_retry_episode_passes_the_carried_year_to_process_episode(tmp_path, monkeypatch):
    _, video = _layout(tmp_path)
    seen = _run_retry_episode(
        monkeypatch,
        {"title": "Show", "path": str(video), "season": 1, "episode": 1, "series_year": "2005"},
    )
    assert seen["series_year"] == "2005"


def test_retry_episode_infers_the_year_for_an_item_queued_without_one(tmp_path, monkeypatch):
    # Items queued before the year travelled with them; the layout still knows it.
    _, video = _layout(tmp_path, filename="Show (2005) - S01E01.mkv")
    seen = _run_retry_episode(
        monkeypatch, {"title": "Show", "path": str(video), "season": 1, "episode": 1},
    )
    assert seen["series_year"] == "2005"


def test_retry_episode_without_any_year_passes_an_empty_string(tmp_path, monkeypatch):
    _, video = _layout(tmp_path)
    seen = _run_retry_episode(
        monkeypatch, {"title": "Show", "path": str(video), "season": 1, "episode": 1},
    )
    assert seen["series_year"] == ""


def test_daily_limit_requeue_keeps_the_year(tmp_path, monkeypatch):
    _, video = _layout(tmp_path, filename="Show (2005) - S01E01.mkv")

    def at_the_cap(*args, **kwargs):
        raise srv.DailyLimitReached("cap")

    monkeypatch.setattr(srv, "process_episode", at_the_cap)
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(srv, "_notify_outcome", lambda *a, **k: None)
    queue = RetryQueue(tmp_path / "retry_queue.json")
    monkeypatch.setattr(srv, "_get_retry_queue", lambda config: queue)
    srv._worker_handle_retry_episode(
        {"title": "Show", "path": str(video), "season": 1, "episode": 1},
        types.SimpleNamespace(), _FakePending(),
    )
    assert queue.load()[0]["series_year"] == "2005"


def _handler(monkeypatch, pending):
    monkeypatch.setattr(srv, "_get_pending_queue", lambda config: pending)
    monkeypatch.setattr(
        srv, "Config", types.SimpleNamespace(from_env=lambda: types.SimpleNamespace()),
    )
    handler = srv._HookHandler.__new__(srv._HookHandler)
    handler.responses = []
    handler._respond = lambda code, msg: handler.responses.append((code, msg))
    return handler


def test_handle_retry_carries_the_filename_year_on_a_single_episode(tmp_path, monkeypatch):
    _, video = _layout(
        tmp_path, folder="Avatar The Last Airbender",
        filename="Avatar The Last Airbender (2005) - S01E01.mkv",
    )
    pending = _FakePending()
    handler = _handler(monkeypatch, pending)
    handler._handle_retry({"path": str(video)})
    assert handler.responses[0][0] == 202
    assert pending.pushed[0]["type"] == "retry_episode"
    assert pending.pushed[0]["series_year"] == "2005"


def test_handle_retry_carries_an_explicit_year_on_a_directory(tmp_path, monkeypatch):
    show, _ = _layout(tmp_path)
    pending = _FakePending()
    handler = _handler(monkeypatch, pending)
    handler._handle_retry({"dir": str(show), "title": "Show", "year": "2005"})
    assert handler.responses[0][0] == 202
    assert pending.pushed[0]["type"] == "retry_dir"
    assert pending.pushed[0]["year"] == "2005"


def test_handle_retry_leaves_the_year_off_when_nothing_knows_it(tmp_path, monkeypatch):
    show, _ = _layout(tmp_path)
    pending = _FakePending()
    handler = _handler(monkeypatch, pending)
    handler._handle_retry({"dir": str(show), "title": "Show"})
    assert "year" not in pending.pushed[0]
