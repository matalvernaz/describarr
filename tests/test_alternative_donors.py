"""When the number's pick is wrong, the walk tries what else the pack holds.

Family Guy, 2026-10-01: ten episodes were refused although describarr had
already downloaded the right recording, every time inside the same season pack:

- AudioVault's season 12 numbers by production order from E13 on, so the file
  called E13 is "Fresh Heir", which is the library's E14. The library's
  TrollHD names carry no title, so nothing said which file was which, and
  E14, E15 and E20 were refused (the extra source, tried next, lacks those three).
- "Season 13 UK" swaps E02 and E04, and repeats the show's name in every
  file ("13 - 02  Family Guy - Baking Bad.mp3").
- S13E01 "The Simpsons Guy" is one 44-minute episode, catalogued as its two
  halves under one number. Either half alone left 22 minutes undescribed.
- Season 17 names its files "16 You Can't Handle the Booth.mp3", and the
  positional guard read "16" as part of the title, refusing every one.

Every test here goes through ``process_episode`` (or ``extract_episode``)
with the real file names, so each one fails on the code that refused them.
"""

import json
import zipfile

import pytest

import describarr.workflow as workflow
from describarr.aligner import AlignResult
from describarr.config import Config
from describarr.matcher import extract_episode
from describarr.workflow import process_episode

from test_coverage_2026_09_26 import FAMILY_GUY_S21E01

# AudioVault "Family Guy - Season 12 (2013)", as cached on 2026-10-01.
SEASON_12 = (
    "Family Guy - Season 12 (2013)",
    ["[S12.E01] Family Guy - Finders Keepers.mp3", "[S12.E02] Family Guy - Vestigial Peter.mp3",
     "[S12.E03] Family Guy - Quagmires Quagmire.mp3", "[S12.E04] A Fistful of Meg.mp3",
     "[S12.E05] Boopa-Dee Bappa-Dee.mp3", "[S12.E06] Family Guy - Life of Brian.mp3",
     "[S12.E07] Family Guy - Into Harmonys Way.mp3", "[S12.E08] Christmas Guy.mp3",
     "[S12.E09] Family Guy - Peter Problems.mp3", "[S12.E10] Family Guy - Grimm Job.mp3",
     "[S12.E11] Brians a Bad Father.mp3", "[S12.E12] Family Guy - Moms the Word.mp3",
     "[S12.E13] Fresh Heir.mp3", "[S12.E14] Secondhand Spoke.mp3",
     "[S12.E15] Harpe the Love Sore.mp3", "[S12.E16] The Most Interesting Man in the World.mp3",
     "[S12.E17] Baby Got Black.mp3", "[S12.E18] Meg Stinks!.mp3", "[S12.E19] Hes Bla-Ack!.mp3",
     "[S12.E20] Chap Stewie.mp3", "[S12.E21] 3 Acts of God.mp3"],
)

# AudioVault "Season 13 UK.zip".
SEASON_13 = (
    "Season 13 UK",
    ["13 - 01  Family Guy - The simpson guy part 2.mp3",
     "13 - 01  Family Guy - the Simpsons Guy part 1.mp3",
     "13 - 02  Family Guy - Baking Bad.mp3", "13 - 03  Family Guy - Brian the closer.mp3",
     "13 - 04  Family Guy - The book of Joe.mp3", "13 - 05  Family Guy - turkey Guys.mp3",
     "13 - 06  Family Guy - the 2000-year old virgin.mp3",
     "13 - 07  Family Guy - Stewie Chris and Steve's excellent adventure.mp3",
     "13 - 08  Family Guy - Our idiot Brian.mp3", "13 - 09  This little piggy.mp3"],
)

# AudioVault "Family Guy - Season 17 (2018)".
SEASON_17 = (
    "Family Guy - Season 17 (2018)",
    ["01 Married With Cancer.mp3", "02 Dead Dog Walking.mp3", "03 Pal Stewie.mp3",
     "04 Big Trouble in Little Quahog.mp3", "05 Regarding Carter.mp3", "06 Stand by Meg.mp3",
     "07 The Griffin Winter Games.mp3", "08 Con Heiress.mp3", "09 Pawtucket Pete.mp3",
     "10 Hefty Shades of Gray.mp3", "11 Trump Guy.mp3", "12 Bri, Robot.mp3", "13 Trans-Fat.mp3",
     "14 Lite.mp3", "15 No Giggity, No Doubt.mp3", "16 You Can't Handle the Booth.mp3",
     "17 Island Adventure.mp3", "18 Island AdventureThrow It Away.mp3",
     "19 Girl Internetted.mp3", "20 Adam West High.mp3"],
)

# The shape Jellyfin writes, byte-order mark and entity escapes included.
_NFO = (
    '﻿<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<episodedetails>\n'
    "  <plot><![CDATA[A plot.]]></plot>\n  <title>{title}</title>\n"
    "  <episode>{episode}</episode>\n  <season>{season}</season>\n</episodedetails>\n"
)


def _pack(tmp_path, pack):
    """A season zip holding *pack*'s file names, each file's bytes its own name
    (so no two donors are byte-identical, as two different recordings are not)."""
    name, files = pack
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in files:
            zf.writestr(f"{name}/{f}", f.encode())
    return zip_path


def _video(tmp_path, season, name, nfo=None):
    video = tmp_path / "Family Guy" / f"Season {season}" / name
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    if nfo is not None:
        title, nfo_season, nfo_episode = nfo
        video.with_suffix(".nfo").write_bytes(
            _NFO.format(title=title, season=nfo_season, episode=nfo_episode).encode("utf-8")
        )
    return video


def _walk(monkeypatch, tmp_path, pack, video, season, episode, accept=(), **kwargs):
    """Run ``process_episode`` over *pack*; the gate passes only donors named in *accept*.

    Returns ``(described, aligned)``, *aligned* being ``(donor name, episode
    title)`` for every alignment, in order.
    """
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    zip_path = _pack(tmp_path, pack)

    class Client:
        def search_shows(self, title):
            return [{"name": pack[0], "url": "https://av/pack"}]

    aligned = []

    def gate(config, video_path, audio_path, label=None, episode_title="", **_):
        aligned.append((audio_path.name, episode_title))
        if audio_path.name in accept:
            return True, None
        return False, "No obvious cause from energy analysis."

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_align_and_keep", gate)
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    described, _ = process_episode(
        Client(), config, video, "Family Guy", season, episode, series_year="1999", **kwargs,
    )
    return described, aligned


# ── a renumbered season: the file titled as the episode goes first ───────────

TROLLHD_S12E14 = "Family Guy S12E14 1080p WEB-DL AAC2.0 AVC-TrollHD.mp4"


def test_a_renumbered_season_aligns_the_file_titled_as_the_episode(monkeypatch, tmp_path):
    video = _video(tmp_path, 12, TROLLHD_S12E14, nfo=("Fresh Heir", 12, 14))
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_12, video, 12, 14,
                               accept={"[S12.E13] Fresh Heir.mp3"})
    assert described
    assert aligned == [("[S12.E13] Fresh Heir.mp3", "Fresh Heir")]


def test_the_number_pick_is_still_tried_after_the_title_match(monkeypatch, tmp_path):
    video = _video(tmp_path, 12, TROLLHD_S12E14, nfo=("Fresh Heir", 12, 14))
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_12, video, 12, 14)
    assert not described
    assert [name for name, _ in aligned][:2] == ["[S12.E13] Fresh Heir.mp3",
                                                 "[S12.E14] Secondhand Spoke.mp3"]


def test_a_near_spelling_still_finds_the_renumbered_file(monkeypatch, tmp_path):
    # TVDB "Herpe, the Love Sore" (library E16) is AudioVault's "[S12.E15] Harpe the Love Sore".
    video = _video(tmp_path, 12, "Family Guy S12E16 1080p WEB-DL AAC2.0 AVC-TrollHD.mp4",
                   nfo=("Herpe, the Love Sore", 12, 16))
    _, aligned = _walk(monkeypatch, tmp_path, SEASON_12, video, 12, 16)
    assert aligned[0][0] == "[S12.E15] Harpe the Love Sore.mp3"


def test_sonarrs_title_wins_over_the_nfo(monkeypatch, tmp_path):
    video = _video(tmp_path, 12, TROLLHD_S12E14, nfo=("Secondhand Spoke", 12, 14))
    _, aligned = _walk(monkeypatch, tmp_path, SEASON_12, video, 12, 14,
                       accept={"[S12.E13] Fresh Heir.mp3"}, episode_title="Fresh Heir")
    assert aligned == [("[S12.E13] Fresh Heir.mp3", "Fresh Heir")]


def test_an_nfo_for_another_episode_lends_no_title(monkeypatch, tmp_path):
    # A sidecar whose numbers disagree is not this file's: no title, so the
    # number's pick alone, exactly as before titles were read from it.
    video = _video(tmp_path, 12, TROLLHD_S12E14, nfo=("Fresh Heir", 12, 13))
    _, aligned = _walk(monkeypatch, tmp_path, SEASON_12, video, 12, 14)
    assert aligned[0] == ("[S12.E14] Secondhand Spoke.mp3", "")


# ── a scrambled season, with the show's name in every donor ──────────────────

def test_a_scrambled_season_follows_the_titles(monkeypatch, tmp_path):
    video = _video(tmp_path, 13, "Family.Guy.S13E02.Book.of.Joe.1080p.WEB-DL.DD5.1.H.264-CtrlHD.mkv",
                   nfo=("The Book of Joe", 13, 2))
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_13, video, 13, 2,
                               accept={"13 - 04  Family Guy - The book of Joe.mp3"})
    assert described
    assert aligned == [("13 - 04  Family Guy - The book of Joe.mp3", "The Book of Joe")]


def test_a_release_name_without_the_article_still_finds_its_file(monkeypatch, tmp_path):
    # No .nfo yet (a fresh import): the release name's "Book of Joe" has to
    # find "The book of Joe" on its own.
    video = _video(tmp_path, 13, "Family.Guy.S13E02.Book.of.Joe.1080p.WEB-DL.DD5.1.H.264-CtrlHD.mkv")
    _, aligned = _walk(monkeypatch, tmp_path, SEASON_13, video, 13, 2)
    assert aligned[0] == ("13 - 04  Family Guy - The book of Joe.mp3", "Book of Joe")


def test_a_misspelt_catalogue_title_keeps_the_number_pick(monkeypatch, tmp_path):
    # Published at 63.8 % on 2026-10-01: a catalogue's title can be wrong,
    # so a disagreeing number pick is demoted, never dropped.
    video = _video(tmp_path, 13,
                   "Family.Guy.S13E07.Stewie.Chris.and.Brians.Excellent.Adventure.1080p.WEB-DL.DD5.1.mkv",
                   nfo=("Stewie, Chris &amp; Brian's Excellent Adventure", 13, 7))
    right = "13 - 07  Family Guy - Stewie Chris and Steve's excellent adventure.mp3"
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_13, video, 13, 7, accept={right})
    assert described
    assert aligned == [(right, "Stewie, Chris & Brian's Excellent Adventure")]


# ── one episode catalogued as two halves ─────────────────────────────────────

SIMPSONS_GUY = "Family.Guy.S13E01.The.Simpsons.Guy.1080p.WEB-DL.DD5.1.H.264-CtrlHD.mkv"
PART_1 = "13 - 01  Family Guy - the Simpsons Guy part 1.mp3"
PART_2 = "13 - 01  Family Guy - The simpson guy part 2.mp3"


def _measured(monkeypatch, video_seconds):
    """ffprobe stand-in: the two halves as measured on 2026-10-01, and the video."""
    lengths = {PART_1: 1222.872, PART_2: 1217.76}

    def duration(path):
        return lengths.get(path.name, video_seconds if path.suffix == ".mkv" else 0.0)

    joins = []

    def concat(parts, out):
        joins.append([p.name for p in parts])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"+".join(p.read_bytes() for p in parts))
        return out

    monkeypatch.setattr(workflow, "_audio_duration", duration)
    monkeypatch.setattr(workflow, "_concat_audio", concat)
    return joins


def test_a_double_episode_recorded_in_halves_is_joined(monkeypatch, tmp_path):
    joins = _measured(monkeypatch, video_seconds=2623.04)
    video = _video(tmp_path, 13, SIMPSONS_GUY, nfo=("The Simpsons Guy", 13, 1))
    joined = "13 - 01  Family Guy - the Simpsons Guy.mp3"
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_13, video, 13, 1, accept={joined})
    assert described
    assert joins == [[PART_1, PART_2]]                      # in part order, not pack order
    assert aligned == [(joined, "The Simpsons Guy")]


def test_one_half_of_a_split_episode_is_not_joined(monkeypatch, tmp_path):
    # A library that splits the episode in two holds one half per file.
    joins = _measured(monkeypatch, video_seconds=1225.0)
    video = _video(tmp_path, 13, SIMPSONS_GUY, nfo=("The Simpsons Guy", 13, 1))
    _, aligned = _walk(monkeypatch, tmp_path, SEASON_13, video, 13, 1)
    assert joins == []
    assert aligned[0][0] == PART_1


# ── a pack numbered like a CD ────────────────────────────────────────────────

@pytest.mark.parametrize("episode, title, expected", [
    (16, "You Cant Handle the Booth", "16 You Can't Handle the Booth.mp3"),
    (1, "Married with Cancer", "01 Married With Cancer.mp3"),
    (14, "Family Guy Lite", "14 Lite.mp3"),           # the catalogue shortened the title
    (18, "Throw It Away", "18 Island AdventureThrow It Away.mp3"),
])
def test_a_track_numbered_pack_is_matched_by_its_numbers(tmp_path, episode, title, expected):
    got = extract_episode(_pack(tmp_path, SEASON_17), tmp_path / "x", episode, episode_title=title)
    assert got is not None and got.name == expected


def test_a_track_numbered_pack_is_described(monkeypatch, tmp_path):
    video = _video(tmp_path, 17,
                   "Family.Guy.S17E16.You.Cant.Handle.the.Booth.1080p.DSNP.WEB-DL.DDP5.1.H.264-FLUX.mkv")
    right = "16 You Can't Handle the Booth.mp3"
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_17, video, 17, 16, accept={right})
    assert described
    assert aligned == [(right, "You Cant Handle the Booth")]


def test_a_title_that_begins_with_a_number_is_not_a_track_number(tmp_path):
    # Only a pack named wholly that way is read so. Here "3" begins a title,
    # and E03 "Third" must not be handed "3 Acts of God" as if numbered 3.
    pack = ("Show - Season 1", ["[S01.E01] Pilot.mp3", "[S01.E02] Second.mp3", "3 Acts of God.mp3"])
    assert extract_episode(_pack(tmp_path, pack), tmp_path / "x", 3, episode_title="Third") is None


# ── through the real gate: a low score corroborated past the show's name ─────

def test_a_low_score_is_corroborated_by_a_donor_that_repeats_the_show_name(monkeypatch, tmp_path):
    """S13E02 at a Family Guy-typical 20.8 %: the donor that names the episode
    is found first, and its name vouches for it once "Family Guy - " is read past."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    config.min_score = 60.0
    zip_path = _pack(tmp_path, SEASON_13)
    video = _video(tmp_path, 13, "Family.Guy.S13E02.Book.of.Joe.1080p.WEB-DL.DD5.1.H.264-CtrlHD.mkv",
                   nfo=("The Book of Joe", 13, 2))
    report = tmp_path / "alignments" / "ep.txt"
    report.parent.mkdir(parents=True)
    report.write_text("Input file similarity: 20.8%\n")
    report.with_suffix(".json").write_text(json.dumps({
        "similarity_pct": 20.8, "median_rate_pct": 0.0, "segments": FAMILY_GUY_S21E01,
    }))
    combined = tmp_path / "out" / "ad_ep.mkv"
    combined.parent.mkdir()
    combined.write_bytes(b"z")

    class Client:
        def search_shows(self, title):
            return [{"name": SEASON_13[0], "url": "https://av/pack"}]

    aligned, published = [], []

    def align(video_path, audio_path, *a, **k):
        aligned.append(audio_path.name)
        return AlignResult(combined, report, None, returncode=0)

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "align", align)
    monkeypatch.setattr(workflow, "_publish_in_place", lambda *a, **k: published.append(a))
    monkeypatch.setattr(workflow, "_cleanup_combined", lambda c: None)
    monkeypatch.setattr(workflow, "primary_audio_is_english", lambda p: True)
    described, _ = process_episode(Client(), config, video, "Family Guy", 13, 2, series_year="1999")
    assert described and len(published) == 1
    assert aligned == ["13 - 04  Family Guy - The book of Joe.mp3"]
    decision = json.loads((config.cache_dir / "decisions.json").read_text())[-1]
    assert decision["path"] == "corroborated-rescue"


def test_a_pack_numbered_across_the_show_is_not_read_as_track_numbers(tmp_path):
    # Season 2 of an absolutely numbered show opens at "27": its 27 is this
    # season's first episode, so E27 must not be handed "27 Pilot".
    pack = ("Show - Season 2", [f"{n} Title {n}.mp3" for n in range(27, 57)])
    got = extract_episode(_pack(tmp_path, pack), tmp_path / "x", 27)
    assert got.name == "53 Title 53.mp3"                    # the 27th file, as before


# ── a mislabelled catalogue: the right recording is filed near the episode ───

# AudioVault "Season 7" (UK), as cached on 2026-10-01. Its 07-05 holds the
# library's E06 and its 07-07 the library's E05 (checked by transcript).
SEASON_7 = (
    "Season 7",
    ["07- 01  Family Guy - Love Blactually.mp3", "07 - 02  Family Guy - I dream of Jesus.mp3",
     "07 - 03  Family Guy - Road to Germany.mp3", "07 - 04  Family Guy - Baby not on board.mp3",
     "07 - 05  Family Guy - The man with two Brian's.mp3",
     "07 - 06  Family Guy - Oceans three and a half.mp3",
     "07 - 07  Family Guy - Tales of the third grade nothing.mp3",
     "07 - 08  Family Guy - Family Gay.mp3", "07 - 09  Family Guy - The juice is loose.mp3"],
)
HOLDS_E05 = "07 - 07  Family Guy - Tales of the third grade nothing.mp3"
HOLDS_E06 = "07 - 05  Family Guy - The man with two Brian's.mp3"


def test_a_recording_filed_under_a_neighbour_is_found(monkeypatch, tmp_path):
    video = _video(tmp_path, 7,
                   "Family.Guy.S07E05.The.Man.with.Two.Brians.1080p.DSNP.WEB-DL.AAC2.0.H.264-PHOENiX.mkv",
                   nfo=("The Man With Two Brians", 7, 5))
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_7, video, 7, 5, accept={HOLDS_E05})
    assert described
    names = [name for name, _ in aligned]
    assert names[0] == HOLDS_E06                      # its own number and title: refused
    assert names[-1] == HOLDS_E05                     # found among the neighbours
    assert names[1:-1] == ["07 - 04  Family Guy - Baby not on board.mp3",
                           "07 - 06  Family Guy - Oceans three and a half.mp3",
                           "07 - 03  Family Guy - Road to Germany.mp3"]   # nearest first


def test_the_swap_is_found_from_the_other_side_too(monkeypatch, tmp_path):
    video = _video(tmp_path, 7,
                   "Family.Guy.S07E06.Tales.of.a.Third.Grade.Nothing.1080p.DSNP.WEB-DL.AAC2.0.H.264-PHOENiX.mkv",
                   nfo=("Tales of a Third Grade Nothing", 7, 6))
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_7, video, 7, 6, accept={HOLDS_E06})
    assert described
    # the title's pick, the number's pick, then the nearest neighbour; the
    # title's pick (07-07) is not aligned a second time as a neighbour
    assert [name for name, _ in aligned] == [HOLDS_E05,
                                             "07 - 06  Family Guy - Oceans three and a half.mp3",
                                             HOLDS_E06]


def test_a_neighbour_match_says_which_recording_it_was(monkeypatch, tmp_path):
    video = _video(tmp_path, 7,
                   "Family.Guy.S07E05.The.Man.with.Two.Brians.1080p.DSNP.WEB-DL.AAC2.0.H.264-PHOENiX.mkv",
                   nfo=("The Man With Two Brians", 7, 5))
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    zip_path = _pack(tmp_path, SEASON_7)

    class Client:
        def search_shows(self, title):
            return [{"name": SEASON_7[0], "url": "https://av/pack"}]

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "_align_and_keep",
                        lambda c, v, audio_path, **k: (audio_path.name == HOLDS_E05,
                                                       None if audio_path.name == HOLDS_E05
                                                       else "similarity 2.3% — refused"))
    described, note = process_episode(Client(), config, video, "Family Guy", 7, 5, series_year="1999")
    assert described
    assert "07 - 07  Family Guy - Tales of the third grade nothing" in note
    assert "names a different episode" in note


def test_nothing_nearby_is_tried_when_no_recording_was_filed_for_the_episode(monkeypatch, tmp_path):
    # This Is Us S02E06 "The 20's" is missing from its pack: nothing filed for
    # it was ever aligned, so there is no sign of a mislabel to chase.
    pack = ("This Is Us - Season 2 (2017)",
            ["2.01 A Father's Advice.mp3", "2.02 A Manny-Splendored Thing.mp3", "2.03 Deja Vu.mp3",
             "2.04 Still There.mp3", "2.05 Brothers.mp3", "2.07 The Most Disappointed Man.mp3",
             "2.08 Number One.mp3"])
    video = _video(tmp_path, 2, "This.Is.Us.S02E06.The.20s.AAC.5.1.1080p.WEBRip.x265-SiQ.mkv")
    described, aligned = _walk(monkeypatch, tmp_path, pack, video, 2, 6)
    assert not described
    assert aligned == []


def test_nothing_nearby_is_tried_after_an_engine_failure(monkeypatch, tmp_path):
    from describarr.aligner import EngineFailure
    video = _video(tmp_path, 7,
                   "Family.Guy.S07E05.The.Man.with.Two.Brians.1080p.DSNP.WEB-DL.AAC2.0.H.264-PHOENiX.mkv",
                   nfo=("The Man With Two Brians", 7, 5))
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    zip_path = _pack(tmp_path, SEASON_7)
    aligned = []

    class Client:
        def search_shows(self, title):
            return [{"name": SEASON_7[0], "url": "https://av/pack"}]

    def crash(c, v, audio_path, **k):
        aligned.append(audio_path.name)
        return False, EngineFailure("alignment failed (describealaign exit 1)")

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "_align_and_keep", crash)
    process_episode(Client(), config, video, "Family Guy", 7, 5, series_year="1999")
    assert aligned == [HOLDS_E06]


# ── a recording that says it is not described ────────────────────────────────

# AudioVault "Season 6" (UK): two gaps are filled with the plain soundtrack.
SEASON_6 = (
    "Season 6",
    ["06 - 05  Family Guy - Lois Kills Stewie.mp3", "06 - 06 -family Guy - Padre De Familia.mp3",
     "06 - 07  Family Guy - Peter's Daughter not described.mp3", "06 - 08  Family Guy - Mcstroke.mp3",
     "06 - 09  Family Guy - Back to the Woods not described.mp3",
     "06 - 10  Family Guy - Play it again Brian.mp3", "06 - 11  Family Guy - the former life of Brian.mp3"],
)


def test_a_recording_that_says_it_is_not_described_is_never_aligned(monkeypatch, tmp_path):
    # Published at 98.2 % on 2026-10-01: nothing narrated, so it matched the
    # soundtrack almost exactly. Skipping it is not a refusal, so nothing
    # nearby is searched either.
    video = _video(tmp_path, 6, "Family.Guy.S06E09.Back.to.the.Woods.1080p.WEB-DL.10bit.x265.HEVC-PHOCiS.mkv",
                   nfo=("Back to the Woods", 6, 9))
    described, aligned = _walk(monkeypatch, tmp_path, SEASON_6, video, 6, 9,
                               accept={"06 - 09  Family Guy - Back to the Woods not described.mp3"})
    assert not described
    assert aligned == []


def test_a_not_described_neighbour_is_skipped_too(monkeypatch, tmp_path):
    video = _video(tmp_path, 6, "Family.Guy.S06E08.McStroke.1080p.WEB-DL.10bit.x265.HEVC-PHOCiS.mkv",
                   nfo=("McStroke", 6, 8))
    _, aligned = _walk(monkeypatch, tmp_path, SEASON_6, video, 6, 8)
    names = [name for name, _ in aligned]
    assert names[0] == "06 - 08  Family Guy - Mcstroke.mp3"
    assert not any("not described" in n for n in names)


def test_a_failed_search_nearby_reports_the_episodes_own_refusal(monkeypatch, tmp_path):
    video = _video(tmp_path, 7,
                   "Family.Guy.S07E05.The.Man.with.Two.Brians.1080p.DSNP.WEB-DL.AAC2.0.H.264-PHOENiX.mkv",
                   nfo=("The Man With Two Brians", 7, 5))
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    zip_path = _pack(tmp_path, SEASON_7)

    class Client:
        def search_shows(self, title):
            return [{"name": SEASON_7[0], "url": "https://av/pack"}]

    def gate(c, v, audio_path, **k):
        if audio_path.name == HOLDS_E06:
            return False, "similarity 6.4% (coverage 100.0%) — the episode's own"
        return False, "similarity 2.0% (coverage 100.0%) — a neighbour's"

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "_align_and_keep", gate)
    described, reason = process_episode(Client(), config, video, "Family Guy", 7, 5, series_year="1999")
    assert not described
    assert reason.endswith("the episode's own")
