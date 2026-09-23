"""/retry?dir= expansion: skip genuinely-done episodes, but reprocess (and
clear from .done) episodes whose merged file lost its AD track."""

import json
import types

import describarr.server as srv
from conftest import fake_config
from describarr.nomatch_cache import NoMatchCache


class _FakePending:
    def __init__(self):
        self.pushed = []

    def push(self, item):
        self.pushed.append(item)


def test_retry_dir_reprocesses_stale_done_and_keeps_real_done(tmp_path, monkeypatch):
    show = tmp_path / "tv" / "Show"
    season_dir = show / "Season 1"
    season_dir.mkdir(parents=True)
    e1 = season_dir / "Show.S01E01.mkv"   # done + still has AD → skip
    e2 = season_dir / "Show.S01E02.mkv"   # done + AD track gone → reprocess
    e1.write_bytes(b"x")
    e2.write_bytes(b"x")

    cache = tmp_path / "cache"
    done_path = cache / "shows" / srv._safe_dirname("Show") / ".done_s01.json"
    done_path.parent.mkdir(parents=True)
    done_path.write_text(json.dumps({"total": 2, "done": [1, 2]}))

    # E01 keeps its AD track; E02 has lost it (re-grab).
    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: p.name.endswith("E01.mkv"))

    config = fake_config(cache_dir=cache)
    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Show", "dir": str(show)}, config, pending,
    )

    queued = {(i["season"], i["episode"]) for i in pending.pushed}
    assert queued == {(1, 2)}                       # only the stale one re-queued

    after = json.loads(done_path.read_text())
    assert after["done"] == [1]                     # stale entry cleared
    assert after["total"] == 2                       # total preserved


def test_retry_dir_skips_a_described_episode_the_ledger_never_recorded(tmp_path, monkeypatch):
    """The file is the authority, not the ledger.

    Futurama S11 (2026-09-22) had six episodes carrying an AD track and one
    ledger entry, because a per-episode repair filed them under a different
    source season. The scan used to probe for an AD track only for episodes the
    ledger already claimed, so all six were re-queued.
    """
    show = tmp_path / "tv" / "Show"
    season_dir = show / "Season 1"
    season_dir.mkdir(parents=True)
    described = season_dir / "Show.S01E01.mkv"     # has AD, absent from .done
    bare = season_dir / "Show.S01E02.mkv"          # no AD anywhere
    described.write_bytes(b"x")
    bare.write_bytes(b"x")

    cache = tmp_path / "cache"
    done_path = cache / "shows" / srv._safe_dirname("Show") / ".done_s01.json"
    done_path.parent.mkdir(parents=True)
    done_path.write_text(json.dumps({"total": 2, "done": []}))

    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: p.name.endswith("E01.mkv"))

    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Show", "dir": str(show)}, fake_config(cache_dir=cache), pending,
    )

    queued = {(i["season"], i["episode"]) for i in pending.pushed}
    assert queued == {(1, 2)}                       # the described one is left alone

    # …and the ledger is repaired, so the next scan needn't probe it again.
    assert json.loads(done_path.read_text())["done"] == [1]


def test_retry_dir_heals_a_ledger_that_does_not_exist_yet(tmp_path, monkeypatch):
    show = tmp_path / "tv" / "Show"
    season_dir = show / "Season 1"
    season_dir.mkdir(parents=True)
    (season_dir / "Show.S01E01.mkv").write_bytes(b"x")

    cache = tmp_path / "cache"
    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: True)

    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Show", "dir": str(show)}, fake_config(cache_dir=cache), pending,
    )

    assert pending.pushed == []
    done_path = cache / "shows" / srv._safe_dirname("Show") / ".done_s01.json"
    assert json.loads(done_path.read_text()) == {"total": 0, "done": [1]}


def _no_source_show(tmp_path, monkeypatch):
    show = tmp_path / "tv" / "Show"
    season_dir = show / "Season 1"
    season_dir.mkdir(parents=True)
    episode = season_dir / "Show.S01E01.mkv"
    episode.write_bytes(b"x")
    monkeypatch.setattr(srv, "source_has_ad_track", lambda p: False)
    return show, episode


def test_retry_dir_does_not_re_search_an_episode_remembered_as_having_no_source(
    tmp_path, monkeypatch,
):
    """A show no catalogue carries cost 60 lookups a scan, every scan."""
    show, episode = _no_source_show(tmp_path, monkeypatch)
    cache = tmp_path / "cache"
    config = fake_config(cache_dir=cache)

    nomatch = NoMatchCache(
        cache / "shows" / srv._safe_dirname("Show") / srv._NOMATCH_FILENAME, 30,
    )
    nomatch.record_miss(NoMatchCache.key(1, 1), episode)

    pending = _FakePending()
    srv._worker_handle_retry_dir({"title": "Show", "dir": str(show)}, config, pending)
    assert pending.pushed == []

    # force=1 is the operator overriding that memory.
    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Show", "dir": str(show), "force": True}, config, pending,
    )
    assert [i["episode"] for i in pending.pushed] == [1]


def test_a_replaced_file_is_searched_again_even_while_remembered(tmp_path, monkeypatch):
    """A Sonarr re-grab is new content — the old miss says nothing about it."""
    show, episode = _no_source_show(tmp_path, monkeypatch)
    cache = tmp_path / "cache"
    config = fake_config(cache_dir=cache)

    nomatch = NoMatchCache(
        cache / "shows" / srv._safe_dirname("Show") / srv._NOMATCH_FILENAME, 30,
    )
    nomatch.record_miss(NoMatchCache.key(1, 1), episode)
    episode.write_bytes(b"a bigger, different file")   # the upgrade

    pending = _FakePending()
    srv._worker_handle_retry_dir({"title": "Show", "dir": str(show)}, config, pending)
    assert [i["episode"] for i in pending.pushed] == [1]


def test_an_expired_miss_is_searched_again(tmp_path, monkeypatch):
    """Catalogues gain titles, so a miss has a shelf life."""
    show, episode = _no_source_show(tmp_path, monkeypatch)
    cache = tmp_path / "cache"
    path = cache / "shows" / srv._safe_dirname("Show") / srv._NOMATCH_FILENAME
    NoMatchCache(path, 30).record_miss(NoMatchCache.key(1, 1), episode)

    # Same record, read back through a cache whose patience has run out.
    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Show", "dir": str(show)},
        fake_config(cache_dir=cache, nomatch_ttl_days=0), pending,
    )
    assert [i["episode"] for i in pending.pushed] == [1]
