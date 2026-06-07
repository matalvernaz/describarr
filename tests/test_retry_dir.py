"""/retry?dir= expansion: skip genuinely-done episodes, but reprocess (and
clear from .done) episodes whose merged file lost its AD track."""

import json
import types

import describarr.server as srv


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

    config = types.SimpleNamespace(cache_dir=cache)
    pending = _FakePending()
    srv._worker_handle_retry_dir(
        {"title": "Show", "dir": str(show)}, config, pending,
    )

    queued = {(i["season"], i["episode"]) for i in pending.pushed}
    assert queued == {(1, 2)}                       # only the stale one re-queued

    after = json.loads(done_path.read_text())
    assert after["done"] == [1]                     # stale entry cleared
    assert after["total"] == 2                       # total preserved
