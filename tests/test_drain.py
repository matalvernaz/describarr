"""drain_retry_queue: after the download cap, cache-served seasons keep
draining; only seasons that need a new download (and their siblings) defer."""

import describarr.workflow as wf


class _FakeQueue:
    def __init__(self, items):
        self._items = items
        self.saved = None
        self.cleared = False

    def load(self):
        return list(self._items)

    def save(self, items):
        self.saved = items

    def clear(self):
        self.cleared = True


def test_capped_season_defers_but_cached_seasons_drain(monkeypatch):
    # Season 1 needs a (capped) download; season 2 is "cached" and succeeds.
    # Queue interleaves them so the old global short-circuit would have
    # wrongly deferred the season-2 episodes sitting behind season 1.
    items = [
        {"type": "episode", "series_title": "Show", "season": 1, "episode": 1, "video_path": __file__},
        {"type": "episode", "series_title": "Show", "season": 2, "episode": 1, "video_path": __file__},
        {"type": "episode", "series_title": "Show", "season": 1, "episode": 2, "video_path": __file__},
        {"type": "episode", "series_title": "Show", "season": 2, "episode": 2, "video_path": __file__},
    ]
    calls = []

    def fake_process_episode(client, config, video_path, series, season, episode, extra_episodes=None):
        calls.append((season, episode))
        if season == 1:
            raise wf.DailyLimitReached("cap")
        return True

    monkeypatch.setattr(wf, "process_episode", fake_process_episode)
    queue = _FakeQueue(items)

    summary = wf.drain_retry_queue(queue, client=None, config=None)

    # Season 2 (cached) both processed; season 1 ep1 tried once and capped;
    # season 1 ep2 skipped via capped_keys (no wasted re-search).
    assert calls == [(1, 1), (2, 1), (2, 2)]
    # Only the two season-1 episodes are left queued.
    assert queue.saved is not None
    deferred = {(i["season"], i["episode"]) for i in queue.saved}
    assert deferred == {(1, 1), (1, 2)}

    # Summary drives the drain-completion notification.
    assert summary["described"] == 2
    assert set(summary["described_labels"]) == {"Show S02E01", "Show S02E02"}
    assert summary["no_match"] == 0
    assert summary["abandoned"] == 0
    assert summary["deferred"] == 2
    assert summary["remaining"] == 2


def test_drain_empty_queue_returns_none():
    assert wf.drain_retry_queue(_FakeQueue([]), client=None, config=None) is None
