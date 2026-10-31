"""Jellyfin hears about each description too, by the file's path, so it can tell
its own users who asked: the first of a show at once, the rest in the show's
one summary, the drain's a message a show, and a server that is down tried
again on its own without repeating the hub."""

import io
import json
import socket
import time as _time
from urllib.error import HTTPError

import describarr.notify as notify
import describarr.server as srv
from conftest import fake_config
from describarr.held_notifications import FILMS, HELD_FILENAME, HeldNotifications, show_group


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


class _Clock:
    """Stands in for the time module in the server: minutes pass when told."""

    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def time(self) -> float:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += minutes * 60

    def __getattr__(self, name):
        return getattr(_time, name)


def _configured(monkeypatch):
    monkeypatch.setenv("JELLYFIN_URL", "http://jellyfin:8096/")
    monkeypatch.setenv("JELLYFIN_API_KEY", "key123")


def _http_error(code):
    return HTTPError("http://jellyfin:8096/Podcasts/Push/Described", code, "refused", {}, io.BytesIO(b""))


def _wire(monkeypatch, tmp_path, up=None):
    """Every send is recorded, delivered or not; *up* says which take it."""
    clock = _Clock()
    monkeypatch.setattr(srv, "time", clock)
    up = up if up is not None else {}
    hub, jellyfin = [], []
    monkeypatch.setattr(srv.notify, "send", lambda title, message: up.get("pushover", True))
    monkeypatch.setattr(srv.notify, "send_hub", lambda category, title, message, click=None: (
        hub.append(message) or up.get("hub", True)))
    monkeypatch.setattr(srv.notify, "send_jellyfin_described", lambda paths, summary: (
        jellyfin.append((list(paths), summary)) or up.get("jellyfin", True)))
    monkeypatch.setattr(srv, "_log_terminal_decision", lambda *a, **k: None)
    return fake_config(tmp_path), clock, hub, jellyfin


def _land(config, clock, label, outcome="described", minutes=2.0, path=True):
    clock.advance(minutes)
    srv._notify_outcome(config, label, outcome, None, path=f"/media/TV shows/{label}.mkv" if path else None)
    srv._send_held_notifications(config)


def test_jellyfin_is_silent_unless_configured(monkeypatch):
    monkeypatch.delenv("JELLYFIN_URL", raising=False)
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(notify, "urlopen", lambda *a, **k: calls.append(a) or _Response())
    assert notify.send_jellyfin_described(["/media/movies/Heat.mkv"], "Heat (1995)")
    monkeypatch.setenv("JELLYFIN_URL", "http://jellyfin:8096")
    assert notify.send_jellyfin_described(["/media/movies/Heat.mkv"], "Heat (1995)")
    assert calls == []


def test_jellyfin_gets_the_paths_and_the_sentence_with_its_own_auth(monkeypatch):
    _configured(monkeypatch)
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["type"] = request.get_header("Content-type")
        seen["body"] = json.loads(request.data)
        return _Response()

    monkeypatch.setattr(notify, "urlopen", fake_urlopen)
    assert notify.send_jellyfin_described(
        ["/media/TV shows/Bluey/S01E01.mkv", "/media/TV shows/Bluey/S01E02.mkv", "/media/TV shows/Bluey/S01E01.mkv"],
        "Bluey: 2 episodes, S01E01 to E02")
    assert seen["url"] == "http://jellyfin:8096/Podcasts/Push/Described"
    assert seen["auth"] == 'MediaBrowser Token="key123"'
    assert seen["type"] == "application/json"
    assert seen["body"] == {"Paths": ["/media/TV shows/Bluey/S01E01.mkv", "/media/TV shows/Bluey/S01E02.mkv"],
                            "Summary": "Bluey: 2 episodes, S01E01 to E02"}


def test_jellyfin_gets_no_more_than_its_limits(monkeypatch):
    _configured(monkeypatch)
    seen = {}
    monkeypatch.setattr(notify, "urlopen", lambda request, timeout=None: seen.update(body=json.loads(request.data)) or _Response())
    notify.send_jellyfin_described([f"/media/{n}.mkv" for n in range(notify.JELLYFIN_MOST_PATHS + 20)], "x" * 900)
    assert len(seen["body"]["Paths"]) == notify.JELLYFIN_MOST_PATHS
    assert len(seen["body"]["Summary"]) == notify.JELLYFIN_SUMMARY_LIMIT


def test_a_refusal_is_for_good_and_only_an_unsent_request_is_tried_again(monkeypatch):
    """404: the server sends no library notices. 400: none of the files is in
    its library. Neither gets better with asking again. A request that never
    arrived is tried again; one whose answer was lost is not (it may have told
    everyone already)."""
    _configured(monkeypatch)
    for code in (400, 401, 404, 500):
        def refused(*a, code=code, **k):
            raise _http_error(code)
        monkeypatch.setattr(notify, "urlopen", refused)
        assert notify.send_jellyfin_described(["/media/movies/Heat.mkv"], "Heat (1995)"), code

    def down(*a, **k):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(notify, "urlopen", down)
    assert not notify.send_jellyfin_described(["/media/movies/Heat.mkv"], "Heat (1995)")

    def lost(*a, **k):
        raise socket.timeout("read timed out")

    monkeypatch.setattr(notify, "urlopen", lost)
    assert notify.send_jellyfin_described(["/media/movies/Heat.mkv"], "Heat (1995)")


def test_the_first_description_of_a_show_reaches_jellyfin_at_once_with_its_file(tmp_path, monkeypatch):
    config, clock, hub, jellyfin = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E10")
    _land(config, clock, "Heat (1995)", outcome="no_match", minutes=60)
    _land(config, clock, "Ronin (1998)", minutes=60, path=False)

    assert jellyfin == [(["/media/TV shows/Friends S04E10.mkv"], "Friends S04E10")]
    assert hub == ["Friends S04E10", "Ronin (1998)"]


def test_a_shows_later_descriptions_reach_jellyfin_in_its_one_summary(tmp_path, monkeypatch):
    config, clock, hub, jellyfin = _wire(monkeypatch, tmp_path)

    for episode in range(1, 4):
        _land(config, clock, f"Friends S04E{episode:02d}")
    _land(config, clock, "Friends S04E04", outcome="no_match")
    assert len(jellyfin) == 1
    clock.advance(31)
    srv._send_held_notifications(config)

    assert jellyfin == [
        (["/media/TV shows/Friends S04E01.mkv"], "Friends S04E01"),
        (["/media/TV shows/Friends S04E02.mkv", "/media/TV shows/Friends S04E03.mkv"], "Friends: 2 episodes, S04E02 to E03"),
    ]
    assert hub[-1] == "Friends: 2 episodes, S04E02 to E03"


def test_jellyfin_down_is_tried_again_on_its_own_without_repeating_the_hub(tmp_path, monkeypatch):
    up = {"jellyfin": False}
    config, clock, hub, jellyfin = _wire(monkeypatch, tmp_path, up=up)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    clock.advance(31)
    srv._send_held_notifications(config)
    up["jellyfin"] = True
    clock.advance(0.5)
    srv._send_held_notifications(config)
    clock.advance(0.5)
    srv._send_held_notifications(config)

    # The hub took each once. Jellyfin was asked at every check while it was down; the first episode, not taken,
    # waited with the second, and the one that was taken carries both in one sentence. Nothing was asked after it.
    assert hub == ["Friends S04E01", "Friends S04E02"]
    assert jellyfin[0] == (["/media/TV shows/Friends S04E01.mkv"], "Friends S04E01")
    both = (["/media/TV shows/Friends S04E01.mkv", "/media/TV shows/Friends S04E02.mkv"], "Friends: 2 episodes, S04E01 to E02")
    assert jellyfin[-1] == both
    assert jellyfin[-2] == both
    assert not (config.cache_dir / HELD_FILENAME).read_text().strip("{}\n ")


def test_drained_descriptions_reach_jellyfin_a_message_a_show(tmp_path, monkeypatch):
    up = {"jellyfin": False}
    config, clock, hub, jellyfin = _wire(monkeypatch, tmp_path, up=up)
    srv._tell_everyone_drained(config, {
        "described_labels": ["Friends S04E01", "Heat (1995)", "Friends S04E02"],
        "described_items": [
            {"label": "Friends S04E01", "path": "/media/TV shows/Friends/S04E01.mkv"},
            {"label": "Heat (1995)", "path": "/media/movies/Heat (1995)/Heat.mkv"},
            {"label": "Friends S04E02", "path": "/media/TV shows/Friends/S04E02.mkv"},
        ]})
    assert sorted(jellyfin) == sorted([
        (["/media/TV shows/Friends/S04E01.mkv", "/media/TV shows/Friends/S04E02.mkv"], "Friends: 2 episodes, S04E01 to E02"),
        (["/media/movies/Heat (1995)/Heat.mkv"], "Heat (1995)"),
    ])

    # Not taken, so held and tried again at the next check, the hub not told twice.
    up["jellyfin"] = True
    clock.advance(0.5)
    srv._send_held_notifications(config)
    assert len(jellyfin) == 4
    assert len(hub) == 2

    jellyfin.clear()
    srv._tell_everyone_drained(config, {"described": 0, "described_labels": []})
    srv._tell_everyone_drained(config, None)
    assert jellyfin == []


def test_a_held_file_from_before_jellyfin_still_works(tmp_path):
    """A held-notifications file written before Jellyfin had a part: read as
    holding nothing for Jellyfin, and its operator and hub parts go as before."""
    path = tmp_path / HELD_FILENAME
    path.write_text(json.dumps({show_group("Friends"): {
        "last": 0.0, "operator": [{"label": "Friends S04E02", "outcome": "described", "message": "Described."}],
        "operator_since": 0.0, "everyone": ["Friends S04E02"], "everyone_since": 0.0}}))
    held = HeldNotifications(path, quiet_seconds=1800, max_wait_seconds=7200)

    (due,) = held.due(now=4000)
    assert (due.everyone, due.jellyfin) == (["Friends S04E02"], [])
    assert held.offer(FILMS, "Heat (1995)", "described", "Described.", now=4000)
