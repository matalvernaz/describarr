"""A source outage must wait before retrying and retain a final outcome."""
import json
from types import SimpleNamespace

from conftest import fake_config
from describarr import pending_queue, server
from describarr.outcome_log import OutcomeLog
from describarr.pending_queue import PendingQueue


def test_delayed_retry_survives_restart_without_blocking_ready_work(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pending_queue.time, "time", lambda: clock[0])
    config = fake_config(tmp_path)
    path = tmp_path / "pending.json"
    queue = PendingQueue(path)
    failed_calls = []
    def handle(item, *_):
        if item["title"] == "Unavailable":
            failed_calls.append(clock[0])
            raise ConnectionError("source unavailable")
    monkeypatch.setattr(server, "_worker_handle_retry_movie", handle)
    queue.push({"type": "retry_movie", "title": "Unavailable", "path": "/movies/A.mkv"})
    server._process_item(queue.claim_first(), config, queue)
    assert queue.claim_first() is None
    assert failed_calls == [1000]
    assert queue.inflight() == []
    queued = queue.load()[0]
    assert queued["attempts"] == 1
    assert queued["retry_at"] == 1300

    queue = PendingQueue(path)
    assert queue.claim_first() is None
    queue.push({"type": "retry_movie", "title": "Ready", "path": "/movies/B.mkv"})
    ready = queue.claim_first()
    assert ready["title"] == "Ready"
    server._process_item(ready, config, queue)
    assert len(queue.load()) == 1
    clock[0] = 1300
    server._process_item(queue.claim_first(), config, queue)
    assert failed_calls == [1000, 1300]
    assert queue.load()[0]["retry_at"] == 1900


def test_delayed_queue_waits_instead_of_busy_spinning(tmp_path, monkeypatch):
    monkeypatch.setattr(pending_queue.time, "time", lambda: 1000)
    queue = PendingQueue(tmp_path / "pending.json")
    queue.push({"retry_at": 1005})
    waits = []
    monkeypatch.setattr(queue._cv, "wait", lambda timeout: waits.append(timeout))
    queue.wait_for_item(timeout=10)
    assert waits == [5]


def test_crash_between_deferral_writes_keeps_the_new_retry_schedule(tmp_path):
    path = tmp_path / "pending.json"
    queue = PendingQueue(path)
    queue.push({"type": "retry_movie", "path": "/movies/A.mkv"})
    claimed = queue.claim_first()
    deferred = {**claimed, "attempts": 1, "retry_at": 9999999999}
    # Simulate the crash window after the new pending state was saved but
    # before the previous claim was acknowledged.
    path.write_text(json.dumps([deferred]))
    recovered = PendingQueue(path)
    assert recovered.load() == [deferred]
    assert recovered.inflight() == []
    assert recovered.claim_first() is None


def test_exhausted_retries_report_error_instead_of_disappearing(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pending_queue.time, "time", lambda: clock[0])
    config = fake_config(tmp_path)
    queue = PendingQueue(tmp_path / "pending.json")
    def unavailable(*_):
        raise ConnectionError("both sources unavailable")
    monkeypatch.setattr(server, "_worker_handle_retry_movie", unavailable)
    monkeypatch.setattr(server, "_get_pending_queue", lambda _: queue)
    monkeypatch.setattr(server, "_get_retry_queue", lambda _: SimpleNamespace(load=lambda: []))
    monkeypatch.setattr(server, "_current_job", None)
    queue.push({"type": "retry_movie", "title": "A Film", "path": "/movies/A.mkv"})
    for attempt in range(server._MAX_WORKER_ATTEMPTS):
        server._process_item(queue.claim_first(), config, queue)
        if attempt < server._MAX_WORKER_ATTEMPTS - 1:
            assert queue.claim_first() is None
            outcome = server._outcome_for(config, "/movies/A.mkv")
            assert outcome["state"] == "queued"
            assert outcome["retryAt"] > clock[0]
            clock[0] = outcome["retryAt"]
    assert queue.load() == []
    assert queue.inflight() == []
    assert server._outcome_for(config, "/movies/A.mkv")["state"] == "error"
    assert "5 attempts" in OutcomeLog.in_cache(config.cache_dir).get("/movies/A.mkv")["detail"]


def test_old_jobs_without_a_schedule_are_immediately_claimable(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text(json.dumps([{"type": "retry_movie", "title": "Old job"}]))
    claimed = PendingQueue(path).claim_first()
    assert claimed["title"] == "Old job"
    assert claimed["queue_id"]
