"""Episode titles read from release filenames and from donor filenames.

The corroborated rescue and the positional-fallback guard both need to know
whether a donor names the episode being described. Every case below is a real
file name from the 2026-09-25/26 This Is Us and Family Guy runs.
"""

import pytest

from describarr.titles import donor_episode_title, episode_title_from_filename, titles_agree


@pytest.mark.parametrize("name, title", [
    ("Family.Guy.S21E01.Oscars.Guy.1080p.DSNP.WEB-DL.DD+5.1.H.264-NTb.mkv", "Oscars Guy"),
    ("Family Guy S20E09 The Fatman Always Rings Twice REPACK 1080p HULU WEB-DL DD 5 1 H 264-NTb.mkv",
     "The Fatman Always Rings Twice"),
    ("Family.Guy-S20E01-LASIK.Instinct.WEBDL-1080p.mkv", "LASIK Instinct"),
    ("Family.Guy.S21E14.White.Meg.Cant.Jump.1080p.DSNP.WEB-DL.DD+5.1.H.264-NTb.mkv", "White Meg Cant Jump"),
    ("This.Is.Us.S06E10.Every.Version.of.You.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb.mkv", "Every Version of You"),
    ("This.Is.Us.S02E06.The.20s.AAC.5.1.1080p.WEBRip.x265-SiQ.mkv", "The 20s"),
    ("Heartland.S08E08.The.Family.Tree.1080p.NF.WEB-DL.DDP5.1.SDR.AV1-OnlyWeb.mkv", "The Family Tree"),
    ("Show.S01E01E02.Pilot.Part.One.720p.HDTV.x264-GRP.mkv", "Pilot Part One"),
])
def test_title_is_read_from_a_release_name(name, title):
    assert episode_title_from_filename(name) == title


@pytest.mark.parametrize("name", [
    "Family.Guy.S21E11.REPACK.1080p.WEB.H264-CAKES.mkv",      # no title in the name
    "This.Is.Us.S01E01.1080p.AMZN.WEBRip.DD5.1.x264-KiNGS.mkv",
    "SGNiK-462.H.1.5PDD.LD-BEW.NZMA.p0801.etaD.eht.dna.renniD.ehT.70E40S.sU.si.sihT.mkv",  # reversed
    "Avatar (2005) - S02E12-E13.mkv",
])
def test_no_title_is_invented(name):
    assert episode_title_from_filename(name) == ""


@pytest.mark.parametrize("name, title", [
    ("[S21.E01] Oscars Guy.mp3", "Oscars Guy"),
    ("2.07 The Most Disappointed Man.mp3", "The Most Disappointed Man"),
    ("5.01,5.02 Forty (Parts 1,2).mp3", "Forty (Parts 1,2)"),
    ("[S01.E17] What Now_.mp3", "What Now_"),
    ("01 - 07 A Title.mp3", "A Title"),
    ("S08E02 Benderama.mp3", "Benderama"),
])
def test_title_is_read_from_a_donor_name(name, title):
    assert donor_episode_title(name) == title


@pytest.mark.parametrize("name", ["Track 07.mp3", "4.07.mp3", "Episode 3.mp3", "07.mp3"])
def test_a_generic_donor_name_has_no_title(name):
    # A zip of "Track 07.mp3" files must not read as naming some other episode,
    # or the positional fallback would refuse every file in it.
    assert donor_episode_title(name) == ""


@pytest.mark.parametrize("a, b", [
    ("Oscars Guy", "Oscars Guy"),
    ("White Meg Cant Jump", "White Meg Can't Jump"),
    ("Vat Man and Rob Em", "Vat Man and Rob 'Em"),
    ("Deja Vu", "Déjà Vu"),
    ("What Now", "What Now_"),
    ("that'll Be the Day", "That'll Be the Day"),
])
def test_spellings_of_one_title_agree(a, b):
    assert titles_agree(a, b) is True


@pytest.mark.parametrize("a, b", [
    ("The 20s", "The Most Disappointed Man"),      # S02E06 vs the positional pick
    ("Number One", "Number Two"),
    ("A Hell of a Week (1)", "A Hell of a Week (2)"),
])
def test_different_episodes_disagree(a, b):
    assert titles_agree(a, b) is False


def test_an_unknown_title_is_neither_agreement_nor_disagreement():
    assert titles_agree("", "Oscars Guy") is None
    assert titles_agree("Oscars Guy", "") is None


@pytest.mark.parametrize("name, title", [
    ("Show.S02E03.Mad.Max.1080p.WEB-DL.DDP5.1.H.264-GRP.mkv", "Mad Max"),
    ("Show.S01E05.It.Follows.720p.HDTV.x264-GRP.mkv", "It Follows"),
    ("Show.S01E13.PROPER.1080p.AMZN.WEBRip.DD5.1.x264-KiNGS.mkv", ""),
    ("Hoarders.S01E02.REAL.720p.HDTV.x264-MOMENTUM.mkv", ""),
    ("Show.S03E01.The.Real.Deal.1080p.NF.WEB-DL.mkv", "The Real Deal"),
])
def test_flag_words_end_the_title_only_in_capitals(name, title):
    assert episode_title_from_filename(name) == title


def test_acceptance_needs_the_exact_title():
    # "Pilot" and "Pilots" are 0.91 alike and can be two episodes; only the
    # fallback guard may treat a near spelling as agreement.
    assert titles_agree("Pilot", "Pilots") is False
    assert titles_agree("Pilot", "Pilots", fuzzy=True) is True


def test_fuzzy_agreement_still_needs_the_same_numbers():
    assert titles_agree("A Hell of a Week (1)", "A Hell of a Week (2)", fuzzy=True) is False
    assert titles_agree("Brother", "Brothers", fuzzy=True) is True


# ── a donor's name read past catalogue clutter (2026-10-01, Family Guy) ──────

from describarr.matcher import _split_recording, whole_recording_stem  # noqa: E402
from describarr.titles import (  # noqa: E402
    donor_names_episode,
    donor_title_readings,
    episode_title_from_nfo,
)


@pytest.mark.parametrize("donor, title", [
    ("16 You Can't Handle the Booth.mp3", "You Can't Handle the Booth"),   # track number
    ("13 - 02  Family Guy - Baking Bad.mp3", "Baking Bad"),                # the show's name
    ("[S12.E01] Family Guy - Finders Keepers.mp3", "Finders Keepers"),
    ("13 - 12  Family guy - Stewie is Enciente.mp3", "Stewie is Enciente"),
    ("01 Family Guy - Pilot.mp3", "Pilot"),                                # both
    ("[S12.E21] 3 Acts of God.mp3", "3 Acts of God"),                      # a number IN the title
])
def test_a_donor_names_its_episode_past_the_clutter(donor, title):
    assert donor_names_episode(title, donor, series_title="Family Guy") is True


def test_a_series_year_does_not_hide_the_show_name():
    assert donor_names_episode("Baking Bad", "13 - 02  Family Guy - Baking Bad.mp3",
                               series_title="Family Guy (1999)") is True


def test_reading_past_the_clutter_still_tells_episodes_apart():
    assert donor_names_episode("The Book of Joe", "13 - 02  Family Guy - Baking Bad.mp3",
                               series_title="Family Guy") is False
    # Exact stays exact once the clutter is gone: no near spelling vouches.
    assert donor_names_episode("Pilot", "01 Family Guy - Pilots.mp3", series_title="Family Guy") is False


def test_the_literal_reading_comes_first():
    assert donor_title_readings("16 You Can't Handle the Booth.mp3")[0] == "16 You Can't Handle the Booth"


@pytest.mark.parametrize("donor", ["Track 07.mp3", "07.mp3", "Family Guy - 07.mp3"])
def test_a_counter_left_after_the_clutter_names_nothing(donor):
    # "Family Guy - 07" is the show's name and a counter: no title at all.
    assert donor_names_episode("Pilot", donor, series_title="Family Guy") in (None, False)
    assert all(not r.isdigit() for r in donor_title_readings(donor, "Family Guy")[1:])


def test_a_release_may_drop_the_leading_article():
    assert titles_agree("Book of Joe", "The Book of Joe", fuzzy=True) is True
    assert titles_agree("Book of Joe", "The Book of Joe") is False        # never for acceptance


# ── the title in Jellyfin's .nfo ─────────────────────────────────────────────

def _nfo(tmp_path, body, name="Family Guy S12E14 1080p WEB-DL AAC2.0 AVC-TrollHD"):
    video = tmp_path / f"{name}.mp4"
    video.write_bytes(b"v")
    (tmp_path / f"{name}.nfo").write_bytes(body.encode("utf-8"))
    return video


def _episode(title, season=12, episode=14):
    return (f"﻿<?xml version=\"1.0\"?>\n<episodedetails>\n  <title>{title}</title>\n"
            f"  <episode>{episode}</episode>\n  <season>{season}</season>\n</episodedetails>\n")


def test_the_nfo_title_is_read(tmp_path):
    assert episode_title_from_nfo(_nfo(tmp_path, _episode("Fresh Heir")), 12, 14) == "Fresh Heir"


def test_the_nfo_title_is_unescaped(tmp_path):
    video = _nfo(tmp_path, _episode("Stewie, Chris &amp; Brian&apos;s Excellent Adventure"))
    assert episode_title_from_nfo(video, 12, 14) == "Stewie, Chris & Brian's Excellent Adventure"


def test_a_cdata_title_is_read(tmp_path):
    video = _nfo(tmp_path, _episode("<![CDATA[He's Bla-ack!]]>"))
    assert episode_title_from_nfo(video, 12, 14) == "He's Bla-ack!"


def test_an_nfo_for_another_number_is_not_trusted(tmp_path):
    assert episode_title_from_nfo(_nfo(tmp_path, _episode("Fresh Heir", episode=13)), 12, 14) == ""
    assert episode_title_from_nfo(_nfo(tmp_path, _episode("Fresh Heir", season=11)), 12, 14) == ""


def test_a_multi_episode_nfo_is_not_trusted(tmp_path):
    # Little House S03E21-E22's sidecar holds both episodes' titles.
    body = _episode("Gold Country (1)", 3, 21) + _episode("Gold Country (2)", 3, 22).replace("﻿", "")
    assert episode_title_from_nfo(_nfo(tmp_path, body), 3, 21) == ""


def test_no_nfo_means_no_title(tmp_path):
    video = tmp_path / "Family Guy S12E14 1080p WEB-DL AAC2.0 AVC-TrollHD.mp4"
    video.write_bytes(b"v")
    assert episode_title_from_nfo(video, 12, 14) == ""


# ── one recording in numbered parts ──────────────────────────────────────────

def test_parts_are_put_in_part_order(tmp_path):
    p2 = tmp_path / "13 - 01  Family Guy - The simpson guy part 2.mp3"
    p1 = tmp_path / "13 - 01  Family Guy - the Simpsons Guy part 1.mp3"
    assert _split_recording([p2, p1]) == (p1, p2)
    assert whole_recording_stem((p1, p2)) == "13 - 01  Family Guy - the Simpsons Guy"


@pytest.mark.parametrize("names", [
    ["Title part 1.mp3", "Title part 3.mp3"],               # a part missing
    ["Title part 1.mp3", "Title.mp3"],                      # one is not a part
    ["Title part 1.mp3", "Other part 1.mp3"],               # two first parts
    ["Title part 1.mp3"],                                   # one file is just a file
])
def test_files_that_are_not_one_recording_are_not_joined(tmp_path, names):
    assert _split_recording([tmp_path / n for n in names]) == ()


def test_bracketed_part_numbers_count(tmp_path):
    p1, p2 = tmp_path / "13.01 the Simpsons Guy (part 1).mp3", tmp_path / "13.01 the Simpsons Guy (part 2).mp3"
    assert _split_recording([p1, p2]) == (p1, p2)
    assert whole_recording_stem((p1, p2)) == "13.01 the Simpsons Guy"


@pytest.mark.parametrize("name", [
    "06 - 09  Family Guy - Back to the Woods not described.mp3",
    "07 - 11  Family Guy - Not All Dogs Go to Heaven not described.mp3",
    "Some Film (2001) (no audio description).mp3",
    "1.04 Title - undescribed.mp3",
    "Title non-described.mp3",
])
def test_a_donor_that_says_it_has_no_description(name):
    from describarr.titles import donor_says_undescribed
    assert donor_says_undescribed(name)


@pytest.mark.parametrize("name", [
    "[S01.E01] Pilot [New Description].mp3",
    "Gladiator (2000) [Old Description].mp3",
    "The Undescribable Thing.mp3",
    "07 - 06  Family Guy - Oceans three and a half.mp3",
])
def test_an_ordinary_donor_says_nothing_of_the_sort(name):
    from describarr.titles import donor_says_undescribed
    assert not donor_says_undescribed(name)


@pytest.mark.parametrize("text", [
    "Season 9 not described",
    "family_guy_-_season_09_2010_not_described",
    "family_guy_-_season_09_2010_not_described_parts",
    "Family Guy - Season 09 (2010) [Not Described]",
])
def test_a_pack_or_folder_that_says_it_is_not_described(text):
    from describarr.titles import says_undescribed
    assert says_undescribed(text)


@pytest.mark.parametrize("text", ["Mr. Robot - Season 1", "Described and Captioned", "The Undescribable Thing"])
def test_an_ordinary_pack_name(text):
    from describarr.titles import says_undescribed
    assert not says_undescribed(text)


# A release that writes "Title- GROUP" (every Deep Space Nine file: "DS9 s07e25
# What You Leave Behind- AIU 1080p+ H265.mkv") read as "What You Leave Behind
# AIU 1080p+": nothing stopped at the group, and "1080p+" is a resolution with
# a plus (2026-10-10). A word that ends in a hyphen ends the title.
@pytest.mark.parametrize("name, title", [
    ("DS9 s07e25 What You Leave Behind- AIU 1080p+ H265.mkv", "What You Leave Behind"),
    ("DS9 s04e25 Broken Link- AIU 1080p+ H265.mkv", "Broken Link"),
    ("Ds9 S07e06 Treachery, Faith, And The Great River- AIU 1080p+ H265.mkv",
     "Treachery, Faith, And The Great River"),
    ("Show S01E01 Pilot 1080p+ WEB.mkv", "Pilot"),
    ("Show - S01E01 - Spider-Man Returns - 1080p.mkv", "Spider Man Returns"),
])
def test_a_title_ends_at_a_trailing_hyphen_or_a_plus_resolution(name, title):
    assert episode_title_from_filename(name) == title
