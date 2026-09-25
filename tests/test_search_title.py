"""The AudioVault query drops Sonarr's trailing year and country qualifiers.

`Heartland (2007) (CA)` (2026-09-24): the old strip removed only a trailing
`(YYYY)`, so the whole title went to AudioVault, which answered nothing, and
every episode fell through to the extra sources although AudioVault carries
all nineteen seasons as `Heartland - Season NN (YYYY)`.
"""

import pytest

from describarr import workflow
from describarr.config import Config
from describarr.workflow import _strip_title_qualifiers, _year_suffix, process_episode


@pytest.mark.parametrize("title, query, year", [
    ("Heartland (2007) (CA)", "Heartland", "2007"),
    ("Heartland (CA) (2007)", "Heartland", "2007"),
    ("The Office (US)", "The Office", ""),
    ("Archer (2009)", "Archer", "2009"),
    ("Archer", "Archer", ""),
    ("Blade Runner 2049", "Blade Runner 2049", ""),
    ("Law & Order (Special Victims Unit)", "Law & Order (Special Victims Unit)", ""),
    ("(2009)", "(2009)", "2009"),
])
def test_qualifiers(title, query, year):
    assert _strip_title_qualifiers(title) == query
    assert _year_suffix(title) == year


def test_country_qualified_show_reaches_audiovault(monkeypatch, tmp_path):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / "Heartland.CA.S12E07.1080p.WEBRip.x265-KONTRAST.mp4"
    video.write_bytes(b"x")

    queries, fetched = [], []

    class FakeClient:
        def search_shows(self, title):
            queries.append(title)
            if title != "Heartland":
                return []
            return [{"name": "Heartland - Season 12 (2018)", "url": "https://av/dl/12"}]

    def fake_get_cached(client, url, cache_dir, limiter):
        fetched.append(url)
        return tmp_path / "s12.zip"

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "_get_cached", fake_get_cached)
    monkeypatch.setattr(workflow, "_episode_donor", lambda *a, **k: tmp_path / "12.07.mp3")
    monkeypatch.setattr(workflow, "_align_and_keep", lambda *a, **k: (True, None))
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources",
                        lambda: pytest.fail("AudioVault had the episode; no extra source needed"))

    described, _ = process_episode(FakeClient(), config, video, "Heartland (2007) (CA)", 12, 7,
                                   series_year="2007")
    assert described
    assert queries == ["Heartland"]
    assert fetched == ["https://av/dl/12"]
