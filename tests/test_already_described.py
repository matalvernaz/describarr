"""An already-described file must not be reported as freshly described.

process_episode/process_movie guard on an existing AD track and return without
doing anything. That returned (True, None), indistinguishable from a publish,
so a rescan of a finished show sent a Pushover per episode claiming work had
been done and wrote "described" into the /status audit trail (2026-09-22).
"""

import types

import describarr.server as srv
from describarr.workflow import ALREADY_DESCRIBED
from conftest import fake_config


class _FakePending:
    def __init__(self):
        self.pushed = []

    def push(self, item):
        self.pushed.append(item)


def test_outcome_mapping_separates_a_publish_from_a_skip():
    assert srv._episode_outcome(True, None) == "described"
    assert srv._episode_outcome(True, "different cut") == "described"
    assert srv._episode_outcome(True, ALREADY_DESCRIBED) == "already_described"
    assert srv._episode_outcome(False, None) == "no_match"


def test_the_sentinel_never_reaches_the_operator_message(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(srv.notify, "send", lambda title, msg: sent.append((title, msg)))
    monkeypatch.setattr(srv, "_log_terminal_decision", lambda *a, **k: None)

    srv._notify_outcome(fake_config(tmp_path), "Show S01E01", "already_described",
                        ALREADY_DESCRIBED)

    assert sent == [("describarr: Show S01E01",
                     "Already had an audio description — left alone.")]


def test_a_skipped_episode_is_notified_as_skipped_not_described(tmp_path, monkeypatch):
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x")
    outcomes = []

    monkeypatch.setattr(
        srv, "process_episode",
        lambda *a, **k: (True, ALREADY_DESCRIBED),
    )
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(
        srv, "_notify_outcome",
        lambda config, label, outcome, reason=None: outcomes.append(outcome),
    )

    srv._worker_handle_retry_episode(
        {"title": "Show", "path": str(video), "season": 1, "episode": 1},
        fake_config(tmp_path), _FakePending(),
    )
    assert outcomes == ["already_described"]


def test_a_no_match_is_remembered_but_a_rejection_is_not(tmp_path, monkeypatch):
    """A rejected candidate may succeed next time with a better donor; a walk
    that found no candidate at all is the one worth not repeating."""
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x")
    config = fake_config(tmp_path)
    cache_path = (config.cache_dir / "shows" / srv._safe_dirname("Show")
                  / srv._NOMATCH_FILENAME)

    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(srv, "_notify_outcome", lambda *a, **k: None)

    monkeypatch.setattr(srv, "process_episode", lambda *a, **k: (False, "score 41 below threshold"))
    srv._worker_handle_retry_episode(
        {"title": "Show", "path": str(video), "season": 1, "episode": 1},
        config, _FakePending(),
    )
    assert not cache_path.exists()

    monkeypatch.setattr(srv, "process_episode", lambda *a, **k: (False, None))
    srv._worker_handle_retry_episode(
        {"title": "Show", "path": str(video), "season": 1, "episode": 1},
        config, _FakePending(),
    )
    assert cache_path.exists()


def test_describing_an_episode_forgets_an_earlier_miss(tmp_path, monkeypatch):
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x")
    config = fake_config(tmp_path)
    cache_path = (config.cache_dir / "shows" / srv._safe_dirname("Show")
                  / srv._NOMATCH_FILENAME)

    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(srv, "_notify_outcome", lambda *a, **k: None)
    monkeypatch.setattr(srv, "process_episode", lambda *a, **k: (False, None))
    srv._worker_handle_retry_episode(
        {"title": "Show", "path": str(video), "season": 1, "episode": 1},
        config, _FakePending(),
    )
    assert srv.NoMatchCache(cache_path, 30).is_fresh_miss(
        srv.NoMatchCache.key(1, 1), video)

    monkeypatch.setattr(srv, "process_episode", lambda *a, **k: (True, None))
    srv._worker_handle_retry_episode(
        {"title": "Show", "path": str(video), "season": 1, "episode": 1},
        config, _FakePending(),
    )
    assert not srv.NoMatchCache(cache_path, 30).is_fresh_miss(
        srv.NoMatchCache.key(1, 1), video)


def _handler_with(headers, peer):
    handler = srv._HookHandler.__new__(srv._HookHandler)
    if headers is not None:
        handler.headers = headers
    handler.client_address = peer
    return handler


def test_the_request_origin_is_logged_not_the_proxy(monkeypatch):
    """Through Traefik the socket peer is always the proxy's container IP, so
    logging it identifies nothing. A /retry that nothing accounted for could
    not be attributed at all (2026-09-22)."""
    # Behind the proxy: the forwarded client wins over the socket peer.
    handler = _handler_with({"X-Forwarded-For": "192.168.1.50, 172.18.0.4"}, ("172.18.0.4", 51000))
    assert handler._caller() == "192.168.1.50"

    # From inside the container (a cron helper on localhost): no header.
    handler = _handler_with({}, ("127.0.0.1", 40000))
    assert handler._caller() == "127.0.0.1"

    # Called before the request line is parsed — must not raise.
    handler = _handler_with(None, None)
    assert handler._caller() == "-"


def test_log_message_includes_the_origin(monkeypatch):
    lines = []
    monkeypatch.setattr(srv.logger, "info", lambda fmt, *a: lines.append(fmt % a))
    handler = _handler_with({}, ("127.0.0.1", 40000))
    handler.log_message('"%s" %s %s', "GET /retry?dir=/tv/Show HTTP/1.1", "202", "-")
    assert lines == ['127.0.0.1 "GET /retry?dir=/tv/Show HTTP/1.1" 202 -']
