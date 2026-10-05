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


def _wire(monkeypatch, tmp_path, up=None, **overrides):
    """Every send is recorded, delivered or not; *up* says whether Pushover
    and the hub take it."""
    clock = _Clock()
    monkeypatch.setattr(srv, "time", clock)
    up = up if up is not None else {}
    pushes, hub = [], []
    monkeypatch.setattr(srv.notify, "send", lambda title, message: (
        pushes.append((title, message)) or up.get("pushover", True)))
    monkeypatch.setattr(srv.notify, "send_hub", lambda category, title, message, click=None: (
        hub.append(message) or up.get("hub", True)))
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


def test_an_episode_landing_after_the_show_went_quiet_joins_its_summary(tmp_path, monkeypatch):
    """The background check comes round every 30 seconds. An episode landing
    between the show going quiet and that check used to start the quiet
    window again, holding a finished summary back for another 30 minutes."""
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    clock.advance(30.2)  # quiet; the check has not come round yet
    srv._notify_outcome(config, "Friends S04E03", "described")
    clock.advance(0.3)
    srv._send_held_notifications(config)

    assert pushes[1:] == [("describarr: Friends, 2 more",
                           "2 described.\nS04E02 to E03: Described.")]


def test_a_summary_pushover_did_not_take_is_tried_again(tmp_path, monkeypatch):
    up = {"pushover": True}
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path, up=up)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    _land(config, clock, "Friends S04E03")
    up["pushover"] = False
    _go_quiet(config, clock)
    assert pushes[-1][0] == "describarr: Friends, 2 more"
    # The hub took its part, so only Pushover's is tried again.
    assert hub == ["Friends S04E01", "Friends: 2 episodes, S04E02 to E03"]

    up["pushover"] = True
    clock.advance(0.5)
    srv._send_held_notifications(config)
    clock.advance(0.5)
    srv._send_held_notifications(config)
    assert [title for title, _ in pushes] == [
        "describarr: Friends S04E01", "describarr: Friends, 2 more", "describarr: Friends, 2 more"]
    assert hub == ["Friends S04E01", "Friends: 2 episodes, S04E02 to E03"]


def test_a_summary_the_hub_did_not_take_is_tried_again_on_its_own(tmp_path, monkeypatch):
    up = {}
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path, up=up)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    up["hub"] = False
    _go_quiet(config, clock)
    up["hub"] = True
    clock.advance(0.5)
    srv._send_held_notifications(config)

    assert hub == ["Friends S04E01", "Friends S04E02", "Friends S04E02"]
    assert [title for title, _ in pushes] == [
        "describarr: Friends S04E01", "describarr: Friends S04E02"]


def test_a_summary_is_given_up_after_an_hour_of_tries(tmp_path, monkeypatch):
    from describarr.held_notifications import RETRY_LIMIT
    up = {}
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path, up=up)

    _land(config, clock, "Friends S04E01")
    _land(config, clock, "Friends S04E02")
    _land(config, clock, "Friends S04E03")
    up["pushover"] = False
    clock.advance(30)
    for _ in range(RETRY_LIMIT + 5):
        clock.advance(0.5)
        srv._send_held_notifications(config)

    assert len(pushes) == 1 + RETRY_LIMIT
    assert not (config.cache_dir / HELD_FILENAME).read_text().strip("{}\n ")


def test_a_first_notification_that_did_not_go_is_tried_at_the_next_check(tmp_path, monkeypatch):
    up = {"pushover": False, "hub": False}
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path, up=up)

    clock.advance(2)
    srv._notify_outcome(config, "Friends S04E10", "described")
    up.update(pushover=True, hub=True)
    clock.advance(0.5)
    srv._send_held_notifications(config)
    clock.advance(0.5)
    srv._send_held_notifications(config)

    assert pushes == [("describarr: Friends S04E10", _DESCRIBED)] * 2
    assert hub == ["Friends S04E10"] * 2


def test_a_summary_put_back_into_a_new_window_goes_at_the_next_check(tmp_path):
    """The show was heard from again while its summary was being sent: the
    summary that failed does not wait for the new window to go quiet."""
    held = HeldNotifications(tmp_path / HELD_FILENAME, quiet_seconds=1800, max_wait_seconds=7200)
    assert held.offer(FILMS, "Heat (1995)", "described", _DESCRIBED, now=0)
    assert not held.offer(FILMS, "Ronin (1998)", "described", _DESCRIBED, now=60)
    [taken] = held.due(now=2000)
    assert held.offer(FILMS, "Cars (2006)", "described", _DESCRIBED, now=2001)
    held.put_back(taken)

    [again] = held.due(now=2030)
    assert [e["label"] for e in again.operator] == ["Ronin (1998)"]
    assert again.everyone == ["Ronin (1998)"]


def test_a_summary_put_back_after_the_show_came_back_is_not_lost(tmp_path):
    """Taken, then the show is heard from again while the send fails: the
    new window holds the old summary too."""
    held = HeldNotifications(tmp_path / HELD_FILENAME, quiet_seconds=1800, max_wait_seconds=7200)
    assert held.offer(FILMS, "Heat (1995)", "described", _DESCRIBED, now=0)
    assert not held.offer(FILMS, "Ronin (1998)", "described", _DESCRIBED, now=60)
    [taken] = held.due(now=2000)
    assert held.offer(FILMS, "Cars (2006)", "described", _DESCRIBED, now=2001)
    held.put_back(taken)
    assert not held.offer(FILMS, "Up (2009)", "described", _DESCRIBED, now=2002)

    [again] = held.due(now=4000)
    assert [e["label"] for e in again.operator] == ["Ronin (1998)", "Up (2009)"]
    assert again.everyone == ["Ronin (1998)", "Up (2009)"]


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
        _land(config, clock, f"Friends S01E{episode:02d}", "error",
              f"AD is {episode % 50} min vs 22 min video, likely wrong/truncated episode")
    _go_quiet(config, clock)

    title, body = pushes[-1]
    assert title == "describarr: Friends, 198 more"
    assert len(body) <= notify.MESSAGE_LIMIT
    assert body.startswith("198 failed.\n")
    assert body.endswith(" …")


# A whole season refused: the refusal's own words count the recordings tried,
# so each was a line of its own and the summary was cut after four (Friends
# Season 1, 2026-10-04).
_REFUSED = ("Tried 3 audio descriptions (3 from AudioVault) and {n} more filed near this "
            "episode, in case the catalogue mislabelled it; none lined up with this copy, "
            "so the file was left alone. LivingAudio had the same recording. ({cause})")


def test_refusals_share_a_line_per_cause(tmp_path, monkeypatch):
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_notify_message", lambda outcome, reason: reason)

    _land(config, clock, "Friends S01E01", "no_match", _REFUSED.format(n=12, cause="the sound did not match"))
    for episode in range(2, 12):
        _land(config, clock, f"Friends S01E{episode:02d}", "no_match",
              _REFUSED.format(n=10 + episode, cause="the sound did not match"))
    _land(config, clock, "Friends S01E12", "no_match", _REFUSED.format(n=9, cause="match score 2%"))
    _land(config, clock, "Friends S01E13", "no_match", _REFUSED.format(n=9, cause="match score 6%"))
    _go_quiet(config, clock)

    assert pushes[-1] == ("describarr: Friends, 12 more", "\n".join([
        "12 no match.",
        "S01E02 to E11: No recording lined up with this copy, so the file was left alone "
        "(the sound did not match).",
        "S01E12 to E13: No recording lined up with this copy, so the file was left alone "
        "(match score too low).",
    ]))


def test_files_numbered_one_behind_share_a_line(tmp_path, monkeypatch):
    config, clock, pushes, _ = _wire(monkeypatch, tmp_path)
    note = ("Described. (matched the recording filed as {q}{name}{q}, so the catalogue, "
            "or this file, names a different episode)")
    monkeypatch.setattr(srv, "_notify_message", lambda outcome, reason: reason)

    _land(config, clock, "Friends S06E02", "described", note.format(q="'", name="Friends S06E01 The One After Vegas"))
    _land(config, clock, "Friends S06E03", "described", note.format(q="'", name="Friends S06E02 The One Where Ross Hugs Rachel"))
    _land(config, clock, "Friends S06E04", "described", note.format(q='"', name="Friends S06E03 The One with Ross' Denial"))
    _land(config, clock, "Friends S06E05", "described", note.format(q="'", name="6.04 The One Where Joey Loses His Insurance"))
    _land(config, clock, "Friends S06E09", "described", note.format(q="'", name="Friends S06E11 The One with the Apothecary Table"))
    _land(config, clock, "Friends S06E10", "described", note.format(q="'", name="E23E24"))
    _go_quiet(config, clock)

    assert pushes[-1][1] == "\n".join([
        "5 described.",
        "S06E03 to E05: Described, each with the recording filed one episode earlier: "
        "these files may be numbered one behind.",
        "S06E09: Described, each with the recording filed 2 episodes later: "
        "these files may be numbered 2 ahead.",
        "S06E10: " + note.format(q="'", name="E23E24"),
    ])


def test_drained_descriptions_reach_the_hub_a_message_a_show(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)
    srv._tell_everyone_drained(config, {"described_labels": [
        "Friends S04E01", "Friends S04E02", "Heat (1995)", "Friends S04E03"]})
    assert sorted(hub) == sorted(["Friends: 3 episodes, S04E01 to E03", "Heat (1995)"])

    hub.clear()
    srv._tell_everyone_drained(config, None)
    srv._tell_everyone_drained(config, {"described": 0, "described_labels": []})
    assert hub == []


def test_a_drained_description_the_hub_did_not_take_is_tried_again(tmp_path, monkeypatch):
    up = {"hub": False}
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path, up=up)
    srv._tell_everyone_drained(config, {"described_labels": ["Friends S04E01", "Friends S04E02"]})
    up["hub"] = True
    clock.advance(0.5)
    srv._send_held_notifications(config)
    assert hub == ["Friends: 2 episodes, S04E01 to E02"] * 2


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


def test_only_what_could_go_later_is_worth_trying_again(monkeypatch):
    from urllib.error import HTTPError, URLError
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER", "usr")
    monkeypatch.setenv("NOTIFY_HUB_URL", "http://notify:8000")
    monkeypatch.setenv("NOTIFY_HUB_TOKEN", "tok")
    answer = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        if "error" in answer:
            raise answer["error"]
        return _Response()

    monkeypatch.setattr(notify, "urlopen", fake_urlopen)

    def refused(code):
        return HTTPError("https://api.pushover.net", code, "no", {}, None)

    for error, worth_another_go in (
            (None, False), (URLError("no route"), True), (TimeoutError(), True),
            (refused(503), True), (refused(429), True), (refused(400), False)):
        answer.clear()
        if error is not None:
            answer["error"] = error
        assert notify.send("t", "m") is not worth_another_go
        assert notify.send_hub("described", "t", "m") is not worth_another_go

    monkeypatch.delenv("PUSHOVER_TOKEN")
    monkeypatch.delenv("NOTIFY_HUB_URL")
    answer["error"] = URLError("never asked")
    assert notify.send("t", "m") is True
    assert notify.send_hub("described", "t", "m") is True


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


def test_the_overnight_drain_tells_the_hub(tmp_path, monkeypatch):
    config, clock, pushes, hub = _wire(monkeypatch, tmp_path)

    class _Queue:
        def load(self):
            return [{"type": "episode"}]

    monkeypatch.setattr(srv, "_get_retry_queue", lambda config: _Queue())
    monkeypatch.setattr(srv, "_get_client", lambda config: object())
    monkeypatch.setattr(srv, "drain_retry_queue", lambda queue, client, config: {
        "described": 2, "described_labels": ["Friends S04E01", "Friends S04E02"],
        "abandoned": 0, "remaining": 0})
    srv._worker_handle_drain({"type": "drain"}, config, None)

    assert hub == ["Friends: 2 episodes, S04E01 to E02"]
    assert pushes[-1][0] == "describarr: described 2 queued title(s)"
