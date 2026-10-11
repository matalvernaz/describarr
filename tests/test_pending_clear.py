"""Stopping a run: ``DELETE /pending`` drops what waits and leaves the current job.

A show's directory retry expands into one item per episode (595 for one show,
2026-10-10), and ``DELETE /queue`` empties only the daily-limit retry queue, so
nothing short of stopping the container could end such a run.
"""

from describarr.pending_queue import PendingQueue


def _queue(tmp_path):
    return PendingQueue(tmp_path / "pending.json")


def test_clear_drops_every_waiting_item_and_says_how_many(tmp_path):
    q = _queue(tmp_path)
    for n in range(3):
        q.push({"type": "retry_episode", "title": "Show", "season": 1, "episode": n + 1})
    assert q.clear() == 3
    assert q.load() == []
    assert q.clear() == 0


def test_clear_leaves_the_claimed_item_to_finish(tmp_path):
    q = _queue(tmp_path)
    q.push({"type": "retry_episode", "title": "Show", "season": 1, "episode": 1})
    q.push({"type": "retry_episode", "title": "Show", "season": 1, "episode": 2})
    claimed = q.claim_first()
    assert claimed["episode"] == 1
    assert q.clear() == 1
    assert q.inflight() == [claimed]
    # The claim completes as usual, and nothing is recovered on the next start.
    q.ack(claimed)
    assert q.inflight() == []
    assert _queue(tmp_path).load() == []


def test_a_cleared_queue_takes_new_work(tmp_path):
    q = _queue(tmp_path)
    q.push({"type": "hook", "env": {}})
    q.clear()
    q.push({"type": "retry_episode", "title": "Show", "season": 2, "episode": 1})
    assert [i["episode"] for i in q.load()] == [1]
