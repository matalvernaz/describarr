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
