"""Regression tests for the reboot-fallback found live on 2026-09-04.

`Gossip Girl (2007)` S02 listed three AudioVault candidates that all scored
0.67 on title alone — the real `Season 2 (2008)` plus the 2021 reboot's
`Season 2 (2022) [US]` and `(2023) [UK]`. When the correct one failed to
align, the candidate walk moved onto the reboot and began a 665 MB download.

A season entry is dated by its own air year, not the series', so these cannot
be filtered the way `find_movie` filters a film's year. The series year is
used to rank instead, and the walk is then confined to the winner's year.
"""
import pytest

from describarr.matcher import find_season


def _results(*names):
    return [{"name": n, "url": f"https://audiovault.net/{i}"} for i, n in enumerate(names)]


GOSSIP_GIRL = _results(
    "Gossip Girl - Season 2 (2008)",
    "Gossip Girl - Season 2 (2022) [US]",
    "Gossip Girl - Season 2 (2023) [UK]",
)


def test_reboot_candidates_are_dropped_when_series_year_known():
    got = find_season(GOSSIP_GIRL, "Gossip Girl", 2, series_year="2007")
    assert [r["name"] for r in got] == ["Gossip Girl - Season 2 (2008)"]


def test_correct_season_wins_even_if_listed_last():
    shuffled = _results(
        "Gossip Girl - Season 2 (2023) [UK]",
        "Gossip Girl - Season 2 (2022) [US]",
        "Gossip Girl - Season 2 (2008)",
    )
    got = find_season(shuffled, "Gossip Girl", 2, series_year="2007")
    assert [r["name"] for r in got] == ["Gossip Girl - Season 2 (2008)"]


def test_without_a_series_year_behaviour_is_unchanged():
    """No year plumbed (old webhooks, /retry by hand) must not start filtering."""
    got = find_season(GOSSIP_GIRL, "Gossip Girl", 2)
    assert len(got) == 3


def test_later_season_of_the_same_series_is_kept():
    """The season's year is its own air year, well after the series began —
    an equality check against the series year would reject this."""
    results = _results("Gossip Girl - Season 5 (2011)")
    got = find_season(results, "Gossip Girl", 5, series_year="2007")
    assert [r["name"] for r in got] == ["Gossip Girl - Season 5 (2011)"]


def test_revival_season_long_after_the_series_began_is_kept():
    """Futurama began in 1999 and its season 11 aired in 2023. A year window
    would false-reject this; ranking plus the lock must not."""
    results = _results("Futurama - Season 11 (2023)")
    got = find_season(results, "Futurama", 11, series_year="1999")
    assert [r["name"] for r in got] == ["Futurama - Season 11 (2023)"]


def test_tts_variant_of_the_same_season_survives_the_lock():
    """Human and TTS variants share a year, so the walk must keep both — the
    TTS fallback is the whole point of walking past a failed candidate."""
    results = _results(
        "Gossip Girl - Season 2 (2008) [TTS]",
        "Gossip Girl - Season 2 (2008)",
    )
    got = find_season(results, "Gossip Girl", 2, series_year="2007")
    assert len(got) == 2
    assert got[0]["name"] == "Gossip Girl - Season 2 (2008)", "human AD first"


def test_catalogue_year_wobble_is_tolerated():
    """A catalogue dating the same season a year either side must not split
    the walk; only a gap wider than _SEASON_YEAR_LOCK_GAP does."""
    results = _results(
        "Some Show - Season 3 (2015)",
        "Some Show - Season 3 (2016) [TTS]",
    )
    got = find_season(results, "Some Show", 3, series_year="2013")
    assert len(got) == 2


def test_candidate_predating_the_series_is_dropped():
    results = _results(
        "Some Show - Season 1 (2020)",
        "Some Show - Season 1 (1998)",
    )
    got = find_season(results, "Some Show", 1, series_year="2020")
    assert [r["name"] for r in got] == ["Some Show - Season 1 (2020)"]


def test_yearless_candidates_are_never_dropped():
    """Absent metadata is not evidence of a different work."""
    results = _results(
        "Some Show - Season 2 (2011)",
        "Some Show - Season 2",
    )
    got = find_season(results, "Some Show", 2, series_year="2010")
    assert len(got) == 2


@pytest.mark.parametrize("year", ["", "   ", "unknown", "0"])
def test_unparseable_series_year_falls_back_to_old_behaviour(year):
    got = find_season(GOSSIP_GIRL, "Gossip Girl", 2, series_year=year)
    assert len(got) >= 1
