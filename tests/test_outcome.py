"""/outcome: what became of one file, for whoever asked for its description.

Defender asked for Heat's described track on 2026-09-18 and again on 09-23;
no source had one, and nothing ever said so (EchoFin audit 2026-09-23,
feature 5). These pin the per-file record and the order the state is read in.
"""

import describarr.server as srv
from conftest import fake_config
from describarr.outcome_log import OutcomeLog


class _Queue:
    def __init__(self, items=None, claimed=None):
        self.items = list(items or [])
        self.claimed = list(claimed or [])

    def load(self):
        return list(self.items)

    def inflight(self):
        return list(self.claimed)


def _quiet(monkeypatch, pending=None, retry=None, claimed=None):
    monkeypatch.setattr(srv, "_get_pending_queue", lambda config: _Queue(pending, claimed))
    monkeypatch.setattr(srv, "_get_retry_queue", lambda config: _Queue(retry))
    monkeypatch.setattr(srv, "_current_job", None)
    monkeypatch.setattr(srv.notify, "send", lambda *args, **kwargs: None)


def test_a_file_is_found_by_name_whichever_mount_it_was_reached_through(tmp_path):
    log = OutcomeLog(tmp_path / "outcomes.json")

    log.record("/movies/Heat (1995)/Heat (1995).mkv", "no_match", label="Heat (1995)")

    entry = log.get("/media/movies/Heat (1995)/Heat (1995).mkv")
    assert entry["outcome"] == "no_match"
    assert entry["label"] == "Heat (1995)"


def test_the_log_stays_bounded(tmp_path):
    log = OutcomeLog(tmp_path / "outcomes.json", max_entries=2)

    for name in ("a.mkv", "b.mkv", "c.mkv"):
        log.record(f"/movies/{name}", "described")

    kept = [name for name in ("a.mkv", "b.mkv", "c.mkv") if log.get(f"/x/{name}")]
    assert len(kept) == 2


def test_nothing_known_is_unknown(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    config = fake_config(cache_dir=tmp_path)

    assert srv._outcome_for(config, "/media/movies/Heat.mkv")["state"] == "unknown"


def test_finding_nothing_and_refusing_a_candidate_read_differently(tmp_path, monkeypatch):
    """Only a miss with no reason is "no source has one". A reason means a
    candidate was found and refused, which may well succeed with a better
    donor, and is said differently."""
    _quiet(monkeypatch)
    config = fake_config(cache_dir=tmp_path)

    srv._notify_outcome(config, "Heat (1995)", "no_match", None,
                        path="/movies/Heat.mkv")
    srv._notify_outcome(config, "Ronin (1998)", "no_match",
                        "alignment score too low", path="/movies/Ronin.mkv")

    heat = srv._outcome_for(config, "/media/movies/Heat.mkv")
    ronin = srv._outcome_for(config, "/media/movies/Ronin.mkv")
    assert heat["state"] == "no_match"
    assert heat["at"]
    assert ronin["state"] == "rejected"
    assert ronin["detail"] == "alignment score too low"


def test_a_file_asked_for_again_is_waiting_not_missed(tmp_path, monkeypatch):
    """Waiting is read before how it last ended: a request after a miss is a
    new request, and "no source had one" would answer the old one."""
    _quiet(monkeypatch, pending=[{"type": "retry_movie", "title": "Heat",
                                  "path": "/media/movies/Heat.mkv"}])
    config = fake_config(cache_dir=tmp_path)
    OutcomeLog.in_cache(tmp_path).record("/movies/Heat.mkv", "no_match")

    assert srv._outcome_for(config, "/media/movies/Heat.mkv")["state"] == "queued"


def test_a_hook_waiting_its_turn_counts_as_queued(tmp_path, monkeypatch):
    _quiet(monkeypatch, pending=[{"type": "hook", "env": {
        "radarr_moviefile_path": "/movies/Heat (1995)/Heat.mkv"}}])
    config = fake_config(cache_dir=tmp_path)

    assert srv._outcome_for(config, "/media/movies/Heat (1995)/Heat.mkv")["state"] == "queued"


def test_a_file_held_for_tomorrows_allowance_says_so(tmp_path, monkeypatch):
    _quiet(monkeypatch, retry=[{"type": "movie", "movie_title": "Heat",
                                "video_path": "/movies/Heat.mkv"}])
    config = fake_config(cache_dir=tmp_path)

    found = srv._outcome_for(config, "/media/movies/Heat.mkv")
    assert found["state"] == "queued"
    assert "allowance" in found["detail"]


def test_the_file_being_worked_on_says_so(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(srv, "_current_job", {
        "type": "movie", "title": "Heat", "path": "/movies/Heat.mkv",
        "started_at": "2026-09-24T02:00:00"})
    config = fake_config(cache_dir=tmp_path)

    found = srv._outcome_for(config, "/media/movies/Heat.mkv")
    assert found["state"] == "working"
    assert found["at"] == "2026-09-24T02:00:00"


def test_an_outcome_without_a_file_is_not_kept(tmp_path, monkeypatch):
    """An unhandled error knows only a label. Nothing is filed under an empty
    name for every later lookup to match."""
    _quiet(monkeypatch)
    config = fake_config(cache_dir=tmp_path)

    srv._notify_outcome(config, "Something", "error", "unhandled error")

    assert not (tmp_path / "outcomes.json").exists()


def test_a_drained_request_is_kept_as_its_files_outcome(tmp_path, monkeypatch):
    """The drain is where a request that hit the daily download cap ends, and
    whoever asked for it is owed the ending too."""
    import describarr.workflow as wf

    class _Retry:
        def __init__(self, items):
            self.items = items

        def load(self):
            return list(self.items)

        def save(self, items):
            self.items = items

        def clear(self):
            self.items = []

    film = tmp_path / "Heat.mkv"
    film.write_bytes(b"x")
    monkeypatch.setattr(wf, "process_movie", lambda *args, **kwargs: (False, None))
    queue = _Retry([{"type": "movie", "movie_title": "Heat", "movie_year": "1995",
                     "video_path": str(film)}])

    wf.drain_retry_queue(queue, client=None, config=fake_config(cache_dir=tmp_path))

    entry = OutcomeLog.in_cache(tmp_path).get("/media/movies/Heat.mkv")
    assert entry["outcome"] == "no_match"


def test_a_claimed_item_is_under_way_not_unknown(tmp_path, monkeypatch):
    """Between the worker's claim and the job it starts there is a login and a
    search. Measured on the first live request: "unknown" in that gap."""
    _quiet(monkeypatch, claimed=[{"type": "retry_movie", "title": "Heat",
                                  "path": "/media/movies/Heat.mkv"}])
    config = fake_config(cache_dir=tmp_path)

    assert srv._outcome_for(config, "/media/movies/Heat.mkv")["state"] == "working"


def test_the_real_queue_reports_what_it_has_claimed(tmp_path):
    from describarr.pending_queue import PendingQueue

    queue = PendingQueue(tmp_path / "pending.json")
    queue.push({"type": "retry_movie", "path": "/movies/Heat.mkv"})
    claimed = queue.claim_first()

    assert queue.inflight() == [claimed]
    assert queue.load() == []
    queue.ack(claimed)
    assert queue.inflight() == []


def test_an_engine_failure_is_an_error_not_a_rejection(tmp_path, monkeypatch):
    """Heartland S10E14 (2026-09-24): the engine crashed after a sound match
    and the file was filed as refused, so a listener was told the source did
    not match and to look again."""
    _quiet(monkeypatch)
    sent = []
    monkeypatch.setattr(srv.notify, "send", lambda title, message: sent.append(message))
    config = fake_config(cache_dir=tmp_path)
    crash = srv.EngineFailure("alignment failed (describealaign exit 1)")

    outcome = srv._episode_outcome(False, crash)
    srv._notify_outcome(config, "Heartland S10E14", outcome, crash, path="/tv/S10E14.mkv")

    state = srv._outcome_for(config, "/media/TV shows/S10E14.mkv")
    assert outcome == "error"
    assert state["state"] == "error"
    assert state["detail"] == "alignment failed (describealaign exit 1)"
    assert "no audio description available" not in sent[0]
    assert srv._episode_outcome(False, "similarity 12.0% — no trusted sync signal") == "no_match"


def test_a_drained_engine_failure_is_kept_as_an_error(tmp_path, monkeypatch):
    import describarr.workflow as wf

    class _Retry:
        def __init__(self, items):
            self.items = items

        def load(self):
            return list(self.items)

        def save(self, items):
            self.items = items

        def clear(self):
            self.items = []

    film = tmp_path / "Heat.mkv"
    film.write_bytes(b"x")
    crash = wf.EngineFailure("alignment failed (describealaign exit 1)")
    monkeypatch.setattr(wf, "process_movie", lambda *args, **kwargs: (False, crash))
    queue = _Retry([{"type": "movie", "movie_title": "Heat", "movie_year": "1995",
                     "video_path": str(film)}])

    wf.drain_retry_queue(queue, client=None, config=fake_config(cache_dir=tmp_path))

    entry = OutcomeLog.in_cache(tmp_path).get("/media/movies/Heat.mkv")
    assert entry["outcome"] == "error"
