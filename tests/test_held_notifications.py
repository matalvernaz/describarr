"""A show that arrives all at once is one notification and then summaries, not
one notification per episode.

Friends on 2026-10-04 was 177 outcomes in eight hours, each a Pushover to the
operator and each description a message to everyone on the notifications hub.
"""

import json
import os
import time as _time

import pytest

import describarr.notify as notify
import describarr.server as srv
from conftest import fake_config
from describarr.held_notifications import FILMS, HELD_FILENAME, HeldNotifications, show_group
from describarr.outcome_log import OutcomeLog

_DESCRIBED = "Described."
_NOTHING = "No audio description found."


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


def _wire(monkeypatch, tmp_path, **overrides):
    clock = _Clock()
    monkeypatch.setattr(srv, "time", clock)
    pushes, hub = [], []
    monkeypatch.setattr(srv.notify, "send", lambda title, message: pushes.append((title, message)))
    monkeypatch.setattr(srv.notify, "send_hub",
                        lambda category, title, message, click=None: hub.append(message))
    return fake_config(tmp_path, **overrides), clock, pushes, hub


def _land(config, clock, label, outcome="described", reason=None, minutes=2.0):
    """One outcome, *minutes* after the last, then the background check."""
    clock.advance(minutes)
    srv._notify_outcome(config, label, outcome, reason, path=f"/tv/{label}.mkv")
    srv._send_held_notifications(config)


def _go_quiet(config, clock, minutes=31):
    clock.advance(minutes)
    srv._send_held_notifications(config)


def test_a_lone_episode_is_sent_at_once_and_only_once(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E10")
    assert pushes == [("describarr: Friends S04E10", _DESCRIBED)]
    assert hub == ["Friends S04E10"]

    _go_quiet(config, clock)
    assert len(pushes) == 1 and len(hub) == 1


def test_a_show_arriving_at_once_is_one_message_then_one_summary(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    for episode in range(1, 25):
        _land(config, clock, f"Friends S04E{episode:02d}")
    assert pushes == [("describarr: Friends S04E01", _DESCRIBED)]

    _go_quiet(config, clock)
    assert pushes[1:] == [("describarr: Friends, 23 more",
                           "23 described.\nS04E02 to E24: Described.")]
    assert hub == ["Friends S04E01", "Friends: 23 episodes, S04E02 to E24"]


def test_a_long_run_tells_the_operator_every_two_hours_and_everyone_at_the_end(
        tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    for episode in range(1, 101):  # 200 minutes, an episode every two
        _land(config, clock, f"Friends S01E{episode:02d}")
    assert [title for title, _ in pushes] == [
        "describarr: Friends S01E01", "describarr: Friends, 61 more"]
    assert pushes[1][1] == "61 described.\nS01E02 to E62: Described."
    assert hub == ["Friends S01E01"]

    _go_quiet(config, clock)
    assert pushes[2] == ("describarr: Friends, 38 more",
                         "38 described.\nS01E63 to E100: Described.")
    assert hub == ["Friends S01E01", "Friends: 99 episodes, S01E02 to E100"]


def test_problems_lead_the_summary_and_keep_their_reasons(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    _land(config, clock, "Friends S04E03", "no_match")
    _land(config, clock, "Friends S04E04")
    _land(config, clock, "Friends S04E05", "no_match")
    _land(config, clock, "Friends S04E06", "queued")
    _land(config, clock, "Friends S04E07", "error", "unhandled error — check logs")
    _go_quiet(config, clock)

    assert pushes[1] == ("describarr: Friends, 6 more", "\n".join([
        "2 described, 2 no match, 1 queued, 1 failed.",
        "S04E07: describarr hit an error — check its logs. (unhandled error — check logs)",
        f"S04E03, S04E05: {_NOTHING}",
        "S04E06: Description queued (AudioVault daily limit reached).",
        "S04E02, S04E04: Described.",
    ]))
    # Everyone hears only what was described.
    assert hub == ["Friends S04E01", "Friends: 2 episodes, S04E02, S04E04"]


def test_a_note_on_one_episode_stays_with_that_episode(tmp_path, monkeypatch):
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path)
    note = "AD source is a different cut: 75 s of the picture has no description at 1:19–1:45"

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    _land(config, clock, "Friends S04E03", reason=note)
    _land(config, clock, "Friends S04E04")
    _go_quiet(config, clock)

    assert pushes[1][1] == "\n".join([
        "3 described.",
        "S04E02, S04E04: Described.",
        f"S04E03: Described. ({note})",
    ])


def test_each_show_is_summarised_on_its_own(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    for episode in range(1, 4):
        _land(config, clock, f"Friends S01E{episode:02d}")
        _land(config, clock, f"Seinfeld S01E{episode:02d}")
    _go_quiet(config, clock)

    assert sorted(pushes) == sorted([
        ("describarr: Friends S01E01", _DESCRIBED),
        ("describarr: Seinfeld S01E01", _DESCRIBED),
        ("describarr: Friends, 2 more", "2 described.\nS01E02 to E03: Described."),
        ("describarr: Seinfeld, 2 more", "2 described.\nS01E02 to E03: Described."),
    ])
    assert sorted(hub) == sorted([
        "Friends S01E01", "Seinfeld S01E01",
        "Friends: 2 episodes, S01E02 to E03", "Seinfeld: 2 episodes, S01E02 to E03"])


def test_films_share_one_summary(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Heat (1995)")
    _land(config, clock, "Ronin (1998)")
    _land(config, clock, "Cars (2006)", "no_match")
    _go_quiet(config, clock)

    assert pushes == [
        ("describarr: Heat (1995)", _DESCRIBED),
        ("describarr: 2 more films",
         f"1 described, 1 no match.\nCars (2006): {_NOTHING}\nRonin (1998): Described."),
    ]
    assert hub == ["Heat (1995)", "Ronin (1998)"]


def test_a_single_held_outcome_reads_as_it_would_have_alone(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02", "no_match")
    _go_quiet(config, clock)

    assert pushes[1] == ("describarr: Friends S04E02", _NOTHING)
    assert hub == ["Friends S04E01"]


def test_a_show_heard_from_again_after_going_quiet_is_sent_at_once(tmp_path, monkeypatch):
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E01")
    _go_quiet(config, clock)
    _land(config, clock, "Friends S04E02")
    assert pushes[-1] == ("describarr: Friends S04E02", _DESCRIBED)

    # Even when the background check has not come round since it went quiet.
    clock.advance(45)
    srv._notify_outcome(config, "Friends S04E03", "described")
    assert pushes[-1] == ("describarr: Friends S04E03", _DESCRIBED)


def test_the_outcome_is_on_record_at_once_while_its_notification_waits(tmp_path, monkeypatch):
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    _land(config, clock, "Friends S04E03", "no_match")

    assert len(pushes) == 1
    log = OutcomeLog.in_cache(config.cache_dir)
    assert log.get("/tv/Friends S04E02.mkv")["outcome"] == "described"
    assert log.get("/tv/Friends S04E03.mkv")["outcome"] == "no_match"
    decisions = json.loads((config.cache_dir / "decisions.json").read_text())
    assert decisions[-1]["title"] == "Friends S04E03"


def test_what_is_held_survives_a_restart(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    _land(config, clock, "Friends S04E03")

    kept = json.loads((config.cache_dir / HELD_FILENAME).read_text())
    assert [e["label"] for e in kept[show_group("Friends")]["operator"]] == [
        "Friends S04E02", "Friends S04E03"]
    # A new process reads the same file: every call does.
    _go_quiet(config, clock)
    assert pushes[-1][0] == "describarr: Friends, 2 more"
    assert not (config.cache_dir / HELD_FILENAME).read_text().strip("{}\n ")


def test_holding_nothing_sends_every_outcome_at_once(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path, notify_quiet_minutes=0)

    for episode in range(1, 4):
        _land(config, clock, f"Friends S04E{episode:02d}")

    assert [title for title, _ in pushes] == [
        "describarr: Friends S04E01", "describarr: Friends S04E02", "describarr: Friends S04E03"]
    assert hub == ["Friends S04E01", "Friends S04E02", "Friends S04E03"]


def test_turning_holding_off_sends_what_was_held(tmp_path, monkeypatch):
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    _land(config, clock, "Friends S04E03")
    srv._send_held_notifications(fake_config(tmp_path, notify_quiet_minutes=0))

    assert pushes[-1][0] == "describarr: Friends, 2 more"


def test_nothing_is_held_where_it_cannot_be_kept(tmp_path, monkeypatch):
    not_a_folder = tmp_path / "cache"
    not_a_folder.write_text("a file where the cache folder should be")
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path, cache_dir=not_a_folder)

    for episode in range(1, 4):
        _land(config, clock, f"Friends S04E{episode:02d}")

    assert len(pushes) == 3


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through the read-only modes")
def test_an_outcome_that_cannot_be_written_down_is_sent_at_once(tmp_path):
    """The show is known, so it would be held, but a hold that is not on file
    is lost: it goes now instead."""
    held = HeldNotifications(tmp_path / HELD_FILENAME, quiet_seconds=1800, max_wait_seconds=7200)
    assert held.offer(FILMS, "Heat (1995)", "described", _DESCRIBED, now=0)
    tmp_path.chmod(0o555)
    try:
        assert held.offer(FILMS, "Ronin (1998)", "described", _DESCRIBED, now=60)
    finally:
        tmp_path.chmod(0o755)
    assert held.due(now=4000) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through the read-only modes")
def test_a_summary_that_cannot_be_written_down_is_not_sent(tmp_path):
    """Sending it with the file unchanged would send it again at every check."""
    path = tmp_path / HELD_FILENAME
    held = HeldNotifications(path, quiet_seconds=1800, max_wait_seconds=7200)
    assert held.offer(FILMS, "Heat (1995)", "described", _DESCRIBED, now=0)
    assert not held.offer(FILMS, "Ronin (1998)", "described", _DESCRIBED, now=60)
    path.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        assert held.due(now=4000) == []
    finally:
        tmp_path.chmod(0o755)
        path.chmod(0o644)
    assert [due.operator[0]["label"] for due in held.due(now=4000)] == ["Ronin (1998)"]


def test_a_long_summary_fits_pushover(tmp_path, monkeypatch):
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path, notify_max_wait_minutes=0)

    _land(config, clock, "Friends S01E01")
    for episode in range(2, 200):
        _land(config, clock, f"Friends S01E{episode:02d}", "no_match",
              f"similarity {episode % 50}.0% — no trusted sync signal")
    _go_quiet(config, clock)

    title, body = pushes[-1]
    assert title == "describarr: Friends, 198 more"
    assert len(body) <= notify.MESSAGE_LIMIT
    assert body.startswith("198 no match.\n")
    assert body.endswith(" …")


def test_pushover_gets_no_more_than_its_limits(monkeypatch):
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER", "usr")
    sent = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        from urllib.parse import parse_qs
        sent.update({k: v[0] for k, v in parse_qs(request.data.decode()).items()})
        return _Response()

    monkeypatch.setattr(notify, "urlopen", fake_urlopen)
    notify.send("t" * 300, "m" * 2000)
    assert len(sent["title"]) == notify.TITLE_LIMIT == 250
    assert len(sent["message"]) == notify.MESSAGE_LIMIT == 1024


def test_episodes_run_together_across_doubles_and_seasons():
    assert srv._episode_runs(
        ["S04E07", "S04E02E03", "S04E01", "S04E04", "S05E02", "S05E01", "Special"]
    ) == "S04E01 to E04, S04E07, S05E01 to E02, Special"


def test_the_group_is_read_from_a_label_when_not_given():
    assert srv._notify_group("Friends S04E10") == show_group("Friends")
    assert srv._notify_group("Friends S02E12E13") == show_group("Friends")
    assert srv._notify_group("Heartland (2007) (CA) S20E01") == show_group("Heartland (2007) (CA)")
    assert srv._notify_group("Heat (1995)") == FILMS


def test_hooks_hold_by_the_shows_title_and_films_together(tmp_path, monkeypatch):
    video = tmp_path / "Friends.S04E10.mkv"
    video.write_bytes(b"x")
    film = tmp_path / "Heat.1995.mkv"
    film.write_bytes(b"x")
    monkeypatch.setattr(srv, "process_episode", lambda *a, **k: (True, None))
    monkeypatch.setattr(srv, "process_movie", lambda *a, **k: (True, None))
    monkeypatch.setattr(srv, "_get_client", lambda config: object())

    episode = srv._sonarr(fake_config(tmp_path), {
        "sonarr_series_title": "Friends",
        "sonarr_episodefile_seasonnumber": "4",
        "sonarr_episodefile_episodenumbers": "10",
        "sonarr_episodefile_path": str(video),
    })
    movie = srv._radarr(fake_config(tmp_path), {
        "radarr_movie_title": "Heat",
        "radarr_movie_year": "1995",
        "radarr_moviefile_path": str(film),
    })
    assert episode["group"] == show_group("Friends")
    assert movie["group"] == FILMS

    seen = []
    monkeypatch.setattr(srv, "_dispatch", lambda env: episode)
    monkeypatch.setattr(srv, "_notify_outcome", lambda *a, **k: seen.append(k.get("group")))
    srv._worker_handle_hook({"env": {}}, fake_config(tmp_path), None)
    assert seen == [show_group("Friends")]


def test_retries_hold_by_the_shows_title(tmp_path, monkeypatch):
    video = tmp_path / "Friends.S04E10.mkv"
    video.write_bytes(b"x")
    film = tmp_path / "Heat.1995.mkv"
    film.write_bytes(b"x")
    monkeypatch.setattr(srv, "process_episode", lambda *a, **k: (True, None))
    monkeypatch.setattr(srv, "process_movie", lambda *a, **k: (True, None))
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    seen = []
    monkeypatch.setattr(srv, "_notify_outcome", lambda *a, **k: seen.append(k.get("group")))

    srv._worker_handle_retry_episode(
        {"title": "Friends", "path": str(video), "season": 4, "episode": 10},
        fake_config(tmp_path), None)
    srv._worker_handle_retry_movie(
        {"title": "Heat", "path": str(film), "year": "1995"}, fake_config(tmp_path), None)

    assert seen == [show_group("Friends"), FILMS]
