"""The notifications hub hears about every description that lands, and nothing else."""

import json

import describarr.notify as notify
import describarr.server as srv
from conftest import fake_config


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def test_the_hub_is_silent_unless_configured(monkeypatch):
    monkeypatch.delenv("NOTIFY_HUB_URL", raising=False)
    monkeypatch.delenv("NOTIFY_HUB_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(notify, "urlopen", lambda *a, **k: calls.append(a) or _Response())
    notify.send_hub("described", "Audio description added", "Heat (1995)")
    assert calls == []


def test_the_hub_gets_the_category_title_and_label(monkeypatch):
    monkeypatch.setenv("NOTIFY_HUB_URL", "http://notify:8000/")
    monkeypatch.setenv("NOTIFY_HUB_TOKEN", "tok")
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data)
        return _Response()

    monkeypatch.setattr(notify, "urlopen", fake_urlopen)
    notify.send_hub("described", "Audio description added", "Heat (1995)")
    assert seen["url"] == "http://notify:8000/"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"topic": "described", "title": "Audio description added",
                            "message": "Heat (1995)", "click": None}


def test_a_hub_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("NOTIFY_HUB_URL", "http://notify:8000")
    monkeypatch.setenv("NOTIFY_HUB_TOKEN", "tok")

    def broken(*a, **k):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(notify, "urlopen", broken)
    notify.send_hub("described", "Audio description added", "Heat (1995)")  # no raise


def test_only_a_described_outcome_reaches_the_hub(tmp_path, monkeypatch):
    config = fake_config(cache_dir=tmp_path)
    monkeypatch.setattr(srv.notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_log_terminal_decision", lambda *a, **k: None)
    hub = []
    monkeypatch.setattr(srv.notify, "send_hub", lambda *a, **k: hub.append(a))

    srv._notify_outcome(config, "Heat (1995)", "described", None, path="/movies/Heat (1995)/Heat.mkv")
    srv._notify_outcome(config, "Ronin (1998)", "no_match", None)
    srv._notify_outcome(config, "Something", "error", "unhandled error")
    srv._notify_outcome(config, "Heat (1995)", "already_described", srv.ALREADY_DESCRIBED)

    assert hub == [("described", "Audio description added", "Heat (1995)")]
