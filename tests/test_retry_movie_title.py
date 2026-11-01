"""A film asked for again by its label is searched by its title, not "Title (Year) (Year)".

2026-11-01: the host monitor queued Forrest Gump again with title="Forrest
Gump (1994)", the label describarr itself had logged, and the folder's year
was added on top: "Looking up movie: Forrest Gump (1994) (1994)". AudioVault's
search stripped the qualifier and still found the film, but the title went on,
year and all, to the matcher and to every extra source.
"""

import types

import describarr.server as srv
from conftest import fake_config


class _FakePending:
    def __init__(self):
        self.pushed = []

    def push(self, item):
        self.pushed.append(item)


def _handler(monkeypatch, pending, tmp_path):
    monkeypatch.setattr(srv, "_get_pending_queue", lambda config: pending)
    monkeypatch.setattr(
        srv, "Config", types.SimpleNamespace(from_env=lambda: fake_config(tmp_path)),
    )
    handler = srv._HookHandler.__new__(srv._HookHandler)
    handler.responses = []
    handler._respond = lambda code, msg: handler.responses.append((code, msg))
    return handler


def _film(tmp_path):
    folder = tmp_path / "movies" / "Forrest Gump (1994)"
    folder.mkdir(parents=True)
    video = folder / "Forrest.Gump.1994.1080p.BluRay.Remux.AVC.Atmos-PmP-AsRequested.mkv"
    video.write_bytes(b"x")
    return video


def test_a_title_carrying_the_folders_year_loses_the_copy(tmp_path, monkeypatch):
    video = _film(tmp_path)
    pending = _FakePending()
    handler = _handler(monkeypatch, pending, tmp_path)
    handler._handle_retry({"path": str(video), "title": "Forrest Gump (1994)"})
    assert handler.responses[0][0] == 202
    item = pending.pushed[0]
    assert (item["type"], item["title"], item["year"]) == ("retry_movie", "Forrest Gump", "1994")
    assert "'Forrest Gump' (1994)" in handler.responses[0][1]


def test_a_title_carrying_its_year_needs_no_folder_year(tmp_path, monkeypatch):
    folder = tmp_path / "movies" / "Forrest Gump"
    folder.mkdir(parents=True)
    video = folder / "Forrest.Gump.mkv"
    video.write_bytes(b"x")
    pending = _FakePending()
    handler = _handler(monkeypatch, pending, tmp_path)
    handler._handle_retry({"path": str(video), "title": "Forrest Gump (1994)"})
    item = pending.pushed[0]
    assert (item["title"], item["year"]) == ("Forrest Gump", "1994")


def test_a_title_whose_year_disagrees_with_year_is_left_whole(tmp_path, monkeypatch):
    video = _film(tmp_path)
    pending = _FakePending()
    handler = _handler(monkeypatch, pending, tmp_path)
    handler._handle_retry({"path": str(video), "title": "Dune (2021)", "year": "1984"})
    item = pending.pushed[0]
    assert (item["title"], item["year"]) == ("Dune (2021)", "1984")


def test_a_plain_title_is_untouched(tmp_path, monkeypatch):
    video = _film(tmp_path)
    pending = _FakePending()
    handler = _handler(monkeypatch, pending, tmp_path)
    handler._handle_retry({"path": str(video), "title": "Forrest Gump"})
    item = pending.pushed[0]
    assert (item["title"], item["year"]) == ("Forrest Gump", "1994")

