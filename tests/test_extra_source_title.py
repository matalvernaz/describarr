"""An extra source is told Sonarr's episode title; an older source still works.

A catalogue can file a show's recordings one folder down, in a folder named
for how every episode title begins: "Spoiler: Gary Dies" arrived as
``Stuart Fails to Save the Universe/Spoiler/1.01 Gary Dies.mp3``, so nine
recordings that existed were reported as missing (2026-09-30). Only the
episode's own title tells a folder like that apart from a different work's,
so the source has to be given it.
"""

import describarr.workflow as workflow
from describarr.config import Config
from describarr.sources import episode_candidates_from
from describarr.workflow import process_episode

SHOW = "Stuart Fails to Save the Universe"
VIDEO = "Stuart.Fails.to.Save.the.Universe.S01E01.1080p.10bit.WEBRip.6CH.x265.HEVC-PSA.mkv"
TITLE = "Spoiler: Gary Dies"


class _TitledSource:
    """A source that takes the title, and records what it was given."""

    def __init__(self):
        self.titles = []

    def episode_candidates(self, cache_dir, series_title, season, episode, episode_title=""):
        self.titles.append(episode_title)
        return []

    def close(self):
        pass


class _OlderSource:
    """A source written before the title parameter existed."""

    def __init__(self):
        self.calls = 0

    def episode_candidates(self, cache_dir, series_title, season, episode):
        self.calls += 1
        return []

    def close(self):
        pass


def _run(monkeypatch, tmp_path, source, episode_title):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    video = tmp_path / VIDEO
    video.write_bytes(b"v")

    class AudioVaultWithoutTheShow:
        def search_shows(self, title):
            return []

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [source])
    return process_episode(
        AudioVaultWithoutTheShow(), config, video, SHOW, 1, 1, episode_title=episode_title,
    )


def test_sonarrs_title_reaches_a_source_that_takes_it(monkeypatch, tmp_path):
    source = _TitledSource()
    _run(monkeypatch, tmp_path, source, TITLE)
    assert source.titles == [TITLE]


def test_a_source_without_the_parameter_is_asked_as_before(monkeypatch, tmp_path):
    source = _OlderSource()
    described, _ = _run(monkeypatch, tmp_path, source, TITLE)
    assert source.calls == 1 and not described


def test_an_unknown_title_is_not_passed_at_all():
    source = _TitledSource()
    episode_candidates_from(source, None, SHOW, 1, 1, episode_title="")
    assert source.titles == [""]


def test_a_source_taking_any_keyword_is_given_the_title():
    class Catchall:
        def __init__(self):
            self.kwargs = None

        def episode_candidates(self, *args, **kwargs):
            self.kwargs = kwargs
            return []

    source = Catchall()
    episode_candidates_from(source, None, SHOW, 1, 1, episode_title=TITLE)
    assert source.kwargs == {"episode_title": TITLE}
