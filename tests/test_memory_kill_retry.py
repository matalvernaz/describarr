"""An alignment the server stops for memory is run again, not reported as the file failing.

Forrest Gump, 2026-11-01: a 142-minute remux was killed twice by the host's
memory watchdog (describealaign exit -9) while a build loop on the same host
swung between 1.5 and 4 GB. describarr took each kill for its own container's
memory limit, aborted the film's candidate walk and reported an error; the
host monitor's one requeue restarted the walk from the top, spent an hour
re-running the recording already refused, and the second recording, the one
likely to fit, was killed four minutes in. Nothing in the film or the
recordings was wrong.

Now a kill that the container's own OOM-kill count does not account for is
taken for what it is: the same recording runs again once the server has the
memory back, a bounded number of times, and the walk keeps its place. A kill
the count does account for (the container's limit) still aborts at once.
"""

import logging
import types

import pytest

import describarr.host_memory as host_memory
import describarr.server as srv
import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.decision_log import DecisionLog
from describarr.workflow import AlignmentResourceKill, process_movie
from conftest import fake_config


# ── host_memory: reading the machine ────────────────────────────────────────

def test_available_memory_is_read_from_meminfo(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16341504 kB\n"
        "MemFree:          218000 kB\n"
        "MemAvailable:    3588892 kB\n"
    )
    assert host_memory.available_mb(meminfo) == 3504


def test_available_memory_is_unknown_without_the_figure(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16341504 kB\n")
    assert host_memory.available_mb(meminfo) is None
    assert host_memory.available_mb(tmp_path / "missing") is None


def _cgroup(tmp_path, own_path, events):
    proc = tmp_path / "proc_self_cgroup"
    proc.write_text(f"0::{own_path}\n")
    root = tmp_path / "cgroup"
    folder = root / own_path.lstrip("/")
    folder.mkdir(parents=True, exist_ok=True)
    if events is not None:
        (folder / "memory.events").write_text(events)
    return proc, root


def test_oom_kills_are_read_from_the_containers_own_cgroup(tmp_path):
    # A Docker container on cgroup v2 sees itself as the root: "0::/".
    proc, root = _cgroup(tmp_path, "/", "low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\noom_group_kill 0\n")
    assert host_memory.oom_kills(proc, root) == 1


def test_oom_kills_follow_a_nested_cgroup_path(tmp_path):
    proc, root = _cgroup(tmp_path, "/system.slice/docker-abc.scope", "oom_kill 4\n")
    assert host_memory.oom_kills(proc, root) == 4


def test_oom_kills_are_unknown_without_cgroup_v2(tmp_path):
    proc = tmp_path / "proc_self_cgroup"
    proc.write_text("4:memory:/docker/abc\n1:name=systemd:/docker/abc\n")
    assert host_memory.oom_kills(proc, tmp_path / "cgroup") is None
    proc_v2, root = _cgroup(tmp_path, "/", None)
    assert host_memory.oom_kills(proc_v2, root) is None


def test_only_a_signal_kill_the_cgroup_did_not_count_is_from_outside():
    assert host_memory.stopped_from_outside(-9, 0, 0)
    assert host_memory.stopped_from_outside(-15, 2, 2)
    # The container's own limit killed it.
    assert not host_memory.stopped_from_outside(-9, 0, 1)
    # Nobody can tell: the old answer, the limit.
    assert not host_memory.stopped_from_outside(-9, None, 0)
    assert not host_memory.stopped_from_outside(-9, 0, None)
    # Not killed by a signal at all.
    assert not host_memory.stopped_from_outside(1, 0, 0)
    assert not host_memory.stopped_from_outside(0, 0, 0)
    assert not host_memory.stopped_from_outside(None, 0, 0)


def test_a_crash_is_never_taken_for_a_kill():
    # The engine's own crash ends in a signal too, and would crash again.
    assert not host_memory.stopped_from_outside(-11, 0, 0)  # SIGSEGV
    assert not host_memory.stopped_from_outside(-6, 0, 0)   # SIGABRT
    assert not host_memory.stopped_from_outside(-7, 0, 0)   # SIGBUS


def test_the_need_grows_with_the_runtime():
    # Forrest Gump: 8526 s, held 2.4 GB when it was stopped.
    assert host_memory.engine_need_mb(8526) == 300 + 2558
    assert host_memory.engine_need_mb(0) == 300
    assert host_memory.engine_need_mb(-5) == 300


# ── host_memory: the wait ───────────────────────────────────────────────────

class _Machine:
    """A clock that moves only when the wait sleeps, and a free-memory reading per tick."""

    def __init__(self, readings):
        self.readings = list(readings)
        self.now = 0.0
        self.reads = 0

    def read(self):
        value = self.readings[min(self.reads, len(self.readings) - 1)]
        self.reads += 1
        return value

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now


def _wait(machine, need=5000, **kwargs):
    options = dict(settle_seconds=60, max_wait_seconds=600, poll_seconds=10,
                   log_every_seconds=120, label="Forrest Gump.mkv",
                   read=machine.read, sleep=machine.sleep, clock=machine.clock)
    options.update(kwargs)
    return host_memory.wait_for_room(need, **options)


def test_the_wait_ends_once_memory_has_held_for_the_whole_window():
    machine = _Machine([3000, 3000, 6000])
    assert _wait(machine) is True
    # Good from t=20; done once 60 s of good readings have passed.
    assert machine.now == 80


def test_a_dip_inside_the_window_starts_it_over():
    # Good at t=0..40, a dip at t=50 (a build peaking), good again from t=60.
    machine = _Machine([6000] * 5 + [2000] + [6000] * 20)
    assert _wait(machine) is True
    assert machine.now == 120


def test_the_wait_gives_up_when_memory_never_comes_back(caplog):
    machine = _Machine([2500])
    with caplog.at_level(logging.INFO, logger="describarr.host_memory"):
        assert _wait(machine) is False
    assert machine.now == 600
    assert any("Gave up waiting for memory" in r.getMessage() for r in caplog.records)


def test_a_good_window_under_way_at_the_limit_is_let_finish():
    # Memory comes back at t=590, ten seconds before the limit.
    machine = _Machine([2500] * 59 + [6000] * 20)
    assert _wait(machine) is True
    assert machine.now == 650


def test_unreadable_memory_counts_as_room_at_once():
    machine = _Machine([None])
    assert _wait(machine) is True
    assert machine.now == 0


def test_the_wait_says_so_in_the_log_for_the_monitor(caplog):
    machine = _Machine([3000] * 30 + [6000] * 10)
    with caplog.at_level(logging.INFO, logger="describarr.host_memory"):
        _wait(machine)
    waiting = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Waiting for memory:")]
    # At the start, then every two minutes of the five it waited.
    assert len(waiting) == 3
    assert "Forrest Gump.mkv needs 5000 MB free" in waiting[0]


def test_status_sees_the_wait_while_it_lasts():
    seen = []

    def read():
        seen.append(host_memory.current_wait())
        return 6000

    host_memory.wait_for_room(5000, label="Forrest Gump.mkv", settle_seconds=0, read=read,
                              sleep=lambda s: None, clock=lambda: 0.0)
    assert seen[0]["label"] == "Forrest Gump.mkv"
    assert seen[0]["need_mb"] == 5000
    assert host_memory.current_wait() is None


# ── the workflow: the same recording, again ─────────────────────────────────

def _files(tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Forrest.Gump.1994.mkv"
    video.write_bytes(b"x")
    audio = tmp_path / "Forrest Gump [New Description] (1994).mp3"
    audio.write_bytes(b"y")
    return config, video, audio


def _engine(monkeypatch, runs, oom_counts):
    """Fake the engine with *runs* (one AlignResult per call) and the cgroup's
    OOM-kill count with *oom_counts* (one reading per call)."""
    calls = []
    counts = list(oom_counts)

    def fake_align(video_path, audio_path, *args, **kwargs):
        calls.append(audio_path)
        return runs[len(calls) - 1]

    def fake_oom_kills():
        return counts.pop(0)

    monkeypatch.setattr(workflow, "align", fake_align)
    monkeypatch.setattr(workflow, "oom_kills", fake_oom_kills)
    monkeypatch.setattr(workflow, "_audio_duration", lambda path: 8526.0)
    return calls


def _killed():
    return AlignResult(None, None, "alignment failed (describealaign exit -9)", returncode=-9)


def _refused():
    return AlignResult(None, None, "No obvious cause from energy analysis.", returncode=1)


def _decisions(config):
    return DecisionLog(config.cache_dir / "decisions.json", config.history_size).load()


def test_a_kill_from_outside_runs_the_same_recording_again(tmp_path, monkeypatch):
    config, video, audio = _files(tmp_path)
    calls = _engine(monkeypatch, [_killed(), _refused()], [0, 0, 0, 0])
    waits = []
    monkeypatch.setattr(workflow, "wait_for_room", lambda need, label="": waits.append(need) or True)

    published, reason = workflow._align_and_keep(config, video, audio, label="Forrest Gump (1994)")

    assert calls == [audio, audio]
    # The reserve on top of what a 142-minute film needs.
    assert waits == [2000 + 300 + 2558]
    assert not published
    assert reason == "No obvious cause from energy analysis."
    outcomes = [d["outcome"] for d in _decisions(config)]
    assert outcomes == ["interrupted", "failed"]
    interrupted = _decisions(config)[0]
    assert "short of memory" in interrupted["detail"]
    # Not the marker the host monitor requeues on: the run is not over.
    assert interrupted["outcome"] != "failed"


def test_the_containers_own_limit_still_aborts_at_once(tmp_path, monkeypatch):
    config, video, audio = _files(tmp_path)
    calls = _engine(monkeypatch, [_killed()], [0, 1])
    monkeypatch.setattr(workflow, "wait_for_room", lambda *a, **k: pytest.fail("waited"))

    with pytest.raises(AlignmentResourceKill, match="container memory cap"):
        workflow._align_and_keep(config, video, audio)
    assert calls == [audio]


def test_an_engine_crash_is_not_waited_on(tmp_path, monkeypatch):
    config, video, audio = _files(tmp_path)
    crashed = AlignResult(None, None, "alignment failed (describealaign exit -11)", returncode=-11)
    calls = _engine(monkeypatch, [crashed], [0, 0])
    monkeypatch.setattr(workflow, "wait_for_room", lambda *a, **k: pytest.fail("waited"))

    with pytest.raises(AlignmentResourceKill):
        workflow._align_and_keep(config, video, audio)
    assert calls == [audio]
    assert [d["outcome"] for d in _decisions(config)] == ["failed"]


def test_a_kill_nobody_can_place_keeps_the_old_answer(tmp_path, monkeypatch):
    config, video, audio = _files(tmp_path)
    calls = _engine(monkeypatch, [_killed()], [None, None])
    monkeypatch.setattr(workflow, "wait_for_room", lambda *a, **k: pytest.fail("waited"))

    with pytest.raises(AlignmentResourceKill, match="container memory cap"):
        workflow._align_and_keep(config, video, audio)
    assert calls == [audio]


def test_the_runs_again_are_bounded(tmp_path, monkeypatch):
    config, video, audio = _files(tmp_path)
    calls = _engine(monkeypatch, [_killed(), _killed(), _killed()], [0] * 6)
    monkeypatch.setattr(workflow, "wait_for_room", lambda need, label="": True)

    with pytest.raises(AlignmentResourceKill) as raised:
        workflow._align_and_keep(config, video, audio)

    assert calls == [audio] * 3
    assert "stopped it 3 times for lack of memory" in str(raised.value)
    final = _decisions(config)[-1]
    assert final["outcome"] == "failed"
    # The host monitor's backstop requeue still recognises it.
    assert "describealaign exit -9" in final["detail"]
    assert [d["outcome"] for d in _decisions(config)] == ["interrupted", "interrupted", "failed"]


def test_a_wait_that_gives_up_ends_the_walk_with_why(tmp_path, monkeypatch):
    config, video, audio = _files(tmp_path)
    calls = _engine(monkeypatch, [_killed()], [0, 0])
    monkeypatch.setattr(workflow, "wait_for_room", lambda need, label="": False)

    with pytest.raises(AlignmentResourceKill) as raised:
        workflow._align_and_keep(config, video, audio)

    assert calls == [audio]
    assert "did not free 4858 MB within 6 hours" in str(raised.value)


def test_the_walk_keeps_its_place(tmp_path, monkeypatch):
    """The first recording, refused, is not aligned a second time."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Forrest.Gump.1994.mkv"
    video.write_bytes(b"x")
    first = tmp_path / "Forrest Gump.mp3"
    first.write_bytes(b"1")
    second = tmp_path / "Forrest Gump [New Description] (1994).mp3"
    second.write_bytes(b"2")
    donors = {"https://av/dl/1": first, "https://av/dl/2": second}

    class FakeClient:
        def search_movies(self, title):
            return [{"name": "Forrest Gump (1994)", "url": "https://av/dl/1"},
                    {"name": "Forrest Gump (1994) [New Description]", "url": "https://av/dl/2"}]

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: [])
    monkeypatch.setattr(workflow, "_get_cached", lambda client, url, cache_dir, limiter: donors[url])
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    calls = _engine(monkeypatch, [_refused(), _killed(), _refused()], [0] * 6)
    monkeypatch.setattr(workflow, "wait_for_room", lambda need, label="": True)

    described, _ = process_movie(FakeClient(), config, video, "Forrest Gump", "1994")

    assert not described
    # Whichever the matcher ranks first: one recording once, the other twice.
    assert sorted(calls.count(donor) for donor in (first, second)) == [1, 2]
    assert calls[1] == calls[2]


# ── /status: what the job is doing ──────────────────────────────────────────

def test_the_status_page_says_a_job_is_waiting_for_memory():
    data = {
        "date": "2026-11-01",
        "current_job": {"type": "movie", "title": "Forrest Gump", "year": "1994",
                        "started_at": "2026-11-01T13:00:00"},
        "waiting_for_memory": {"label": "Forrest.Gump.1994.mkv", "need_mb": 4858, "since": 0},
        "downloads_today": 2, "limit": 25, "remaining": 23,
        "pending_queue": 0, "retry_queue": 0,
        "next_drain": "2026-11-02T00:05:00",
        "recent_decisions": [],
    }
    page = srv._render_status_html(data)
    assert "waiting for the server to free 4858 MB of memory" in page
    data["waiting_for_memory"] = None
    assert "waiting for the server" not in srv._render_status_html(data)


def test_status_json_carries_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "Config", types.SimpleNamespace(from_env=lambda: fake_config(tmp_path)))
    monkeypatch.setattr(srv, "_get_retry_queue", lambda config: types.SimpleNamespace(load=lambda: []))
    monkeypatch.setattr(srv, "_get_pending_queue", lambda config: types.SimpleNamespace(size=lambda: 0))
    monkeypatch.setattr(srv.host_memory, "current_wait",
                        lambda: {"label": "Forrest.Gump.1994.mkv", "need_mb": 4858, "since": 0})
    handler = srv._HookHandler.__new__(srv._HookHandler)
    handler.headers = {"Accept": "application/json"}
    handler.path = "/status?format=json"
    sent = []
    handler._respond_json = lambda code, data: sent.append((code, data))

    handler._handle_status()

    code, data = sent[0]
    assert code == 200
    assert data["waiting_for_memory"]["need_mb"] == 4858
