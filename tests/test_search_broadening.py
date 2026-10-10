"""A season the literal search cannot see is found by searching the title's parts.

AudioVault's search is a literal substring match. "Star Trek: Deep Space Nine"
returned seasons 1-3, 6 and 7 and not "Star Trek - Deep Space Nine - Season 5
(1997)", so all 26 episodes of season 5 were recorded as having no source
(2026-10-10); "Deep Space Nine" returns all seven. The folder spelling a
directory retry searches, "Law & Order - Special Victims Unit", returned nothing
at all for 595 episodes, while "Special Victims Unit" returns 27 seasons.

A segment search widens the pool to every show sharing those words, so only a
result that carries every word of the whole title reaches the matcher.
"""

import pytest

from describarr import workflow
from describarr.config import Config
from describarr.matcher import names_whole_title, search_segments
from describarr.workflow import process_episode


@pytest.mark.parametrize("title, segments", [
    ("Star Trek: Deep Space Nine", ["Deep Space Nine", "Star Trek"]),
    ("Law & Order - Special Victims Unit", ["Special Victims Unit", "Law & Order"]),
    ("Law & Order: Special Victims Unit", ["Special Victims Unit", "Law & Order"]),
    ("Friends", []),
    ("The Office (US)", []),
    ("9-1-1: Lone Star", ["Lone Star"]),                 # "9-1-1" has no word to search on
    ("Spider-Man: Across the Spider-Verse", ["Across the Spider-Verse", "Spider-Man"]),
])
def test_search_segments(title, segments):
    assert search_segments(title) == segments


@pytest.mark.parametrize("title, name, whole", [
    ("Star Trek: Deep Space Nine", "Star Trek - Deep Space Nine - Season 5 (1997)", True),
    ("Law & Order - Special Victims Unit",
     "Law and Order: Special Victims Unit - Season 03 (2001)", True),
    ("Star Trek: The Next Generation", "Degrassi: The Next Generation - Season 1 (2001)", False),
    ("The Office (US)", "The Office - Season 2 (2005) [US]", True),
    ("Star Trek: Deep Space Nine", "Deep Space Nine - Season 5 (1997)", False),
])
def test_names_whole_title(title, name, whole):
    assert names_whole_title(title, name) is whole


DS9 = "Star Trek: Deep Space Nine"
CATALOGUE = [
    "Star Trek: Deep Space Nine - Season 1 (1993)", "Star Trek: Deep Space Nine - Season 2 (1994)",
    "Star Trek: Deep Space Nine - Season 3 (1995)", "Star Trek - Deep Space Nine - Season 4 (1996)",
    "Star Trek - Deep Space Nine - Season 5 (1997)", "Star Trek: Deep Space Nine - Season 6 (1998)",
    "Star Trek: Deep Space Nine - Season 7 (1999)", "Star Trek: Voyager - Season 5 (1998)",
    "Degrassi: The Next Generation - Season 5 (2005)",
    "Law and Order: Special Victims Unit - Season 03 (2001)", "Law and Order - Season 03 (1992) mixed",
]


class LiteralCatalogue:
    """AudioVault's search: the entries whose name contains the query, case aside."""

    def __init__(self):
        self.queries = []

    def search_shows(self, title):
        self.queries.append(title)
        return [{"name": n, "url": f"https://av/{n}"} for n in CATALOGUE
                if title.lower() in n.lower()]


def _lookup(monkeypatch, tmp_path, client, title, season, episode, series_year):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / f"Show.S{season:02d}E{episode:02d}.mkv"
    video.write_bytes(b"x")
    fetched = []

    def fake_get_cached(client, url, cache_dir, limiter):
        fetched.append(url)
        return tmp_path / "season.zip"

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "_get_cached", fake_get_cached)
    monkeypatch.setattr(workflow, "_episode_donors", lambda *a, **k: [tmp_path / "ad.mp3"])
    monkeypatch.setattr(workflow, "_align_and_keep", lambda *a, **k: (True, None))
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    described, _ = process_episode(
        client, config, video, title, season, episode, series_year=series_year,
    )
    return described, fetched


def test_a_season_the_literal_search_misses_is_found_by_its_own_name(monkeypatch, tmp_path):
    client = LiteralCatalogue()
    described, fetched = _lookup(monkeypatch, tmp_path, client, DS9, 5, 1, "1993")
    assert described
    assert fetched == ["https://av/Star Trek - Deep Space Nine - Season 5 (1997)"]
    assert client.queries == [DS9, "Deep Space Nine"]


def test_a_season_the_literal_search_finds_costs_no_second_search(monkeypatch, tmp_path):
    client = LiteralCatalogue()
    described, fetched = _lookup(monkeypatch, tmp_path, client, DS9, 6, 1, "1993")
    assert described
    assert fetched == ["https://av/Star Trek: Deep Space Nine - Season 6 (1998)"]
    assert client.queries == [DS9]


def test_the_folder_spelling_of_a_show_finds_the_season(monkeypatch, tmp_path):
    # A directory retry searches the folder's name: a dash where the catalogue has a colon.
    client = LiteralCatalogue()
    described, fetched = _lookup(
        monkeypatch, tmp_path, client, "Law & Order - Special Victims Unit", 3, 1, "1999",
    )
    assert described
    assert fetched == ["https://av/Law and Order: Special Victims Unit - Season 03 (2001)"]
    assert client.queries == ["Law & Order - Special Victims Unit", "Special Victims Unit"]


def test_a_segment_shared_with_another_show_does_not_bring_it_in(monkeypatch, tmp_path):
    client = LiteralCatalogue()
    described, fetched = _lookup(
        monkeypatch, tmp_path, client, "Star Trek: The Next Generation", 5, 1, "1987",
    )
    assert not described
    assert fetched == []
    assert client.queries == ["Star Trek: The Next Generation", "The Next Generation", "Star Trek"]


def test_a_title_without_segments_is_searched_once(monkeypatch, tmp_path):
    client = LiteralCatalogue()
    described, fetched = _lookup(monkeypatch, tmp_path, client, "Sesame Street", 1, 1, "1969")
    assert not described
    assert fetched == []
    assert client.queries == ["Sesame Street"]
