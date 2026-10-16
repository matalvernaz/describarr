"""AudioVault spells a show "Phineas & Ferb" in its later packs; "&" reads as "and".

Phineas and Ferb seasons 2 and 3 (2026-10-15): AudioVault's Season 2 (2009)
and Season 3 (2011) packs name every file "[S02.E02a] Phineas & Ferb -
Interview with a Platypus.mp3", while Season 1's say "Phineas and Ferb - ".
Titles were folded with "&" dropped, so the show's name was never read past,
no title was read, and every episode was aligned against the number's pick,
another segment: 2 of 66 described in season 2, 1 of 25 in season 3. Read
over the real Season 2 and 3 listings, the recording named for the episode
came first for 0 of 128 library episodes before this, 106 after.
"""

import zipfile

import pytest

import describarr.workflow as workflow
from describarr.config import Config
from describarr.titles import donor_names_episode, donor_title_readings, titles_agree
from describarr.workflow import process_episode

SHOW = "Phineas and Ferb"
FOLDER = "Phineas and Ferb - Season 2 (2009)"

# AudioVault's "Phineas and Ferb - Season 2 (2009)", as listed on 2026-10-16.
SEASON_2_NAMES = [
    '[S02.E01] Phineas & Ferb - The Lake Nose Monster.mp3',
    '[S02.E02a] Phineas & Ferb - Interview with a Platypus.mp3',
    '[S02.E02b] Phineas & Ferb - Tip of the Day.mp3',
    '[S02.E03a] Phineas & Ferb - Attack of the 50 Foot Sister.mp3',
    '[S02.E03b] Phineas & Ferb - Backyard Aquarium.mp3',
    '[S02.E04a] Phineas & Ferb - Day of the Living Gelatin.mp3',
    '[S02.E04b] Phineas & Ferb - Elementary My Dear Stacy.mp3',
    "[S02.E05a] Phineas & Ferb - Don't Even Blink.mp3",
    '[S02.E05b] Phineas & Ferb - Chez Platypus.mp3',
    '[S02.E06a] Phineas & Ferb - Perry Lays an Egg.mp3',
    '[S02.E06b] Phineas & Ferb - Gaming the System.mp3',
    '[S02.E07] Phineas & Ferb - The Chronicles of Meap.mp3',
    '[S02.E08a] Phineas & Ferb - Thaddeus and Thor.mp3',
    '[S02.E08b] Phineas & Ferb - De Plane! De Plane!.mp3',
    "[S02.E09a] Phineas & Ferb - Let's Take a Quiz.mp3",
    '[S02.E09b] Phineas & Ferb - The Car Wash.mp3',
    '[S02.E10a] Phineas & Ferb - Oh There You Are Perry.mp3',
    '[S02.E10b] Phineas & Ferb - Swiss Family Phineas.mp3',
    "[S02.E11] Phineas & Ferb - Phineas and Ferb's Quantum Boogaloo.mp3",
    "[S02.E12] Phineas & Ferb - Phineas and Ferb's Musical Cliptastic Countdown.mp3",
    '[S02.E13a] Phineas & Ferb - Hide and Seek.mp3',
    '[S02.E13b] Phineas & Ferb - That Sinking Feeling.mp3',
    '[S02.E14a] Phineas & Ferb - The Baljeatles.mp3',
    '[S02.E14b] Phineas & Ferb - Vanessassary Roughness.mp3',
    '[S02.E15a] Phineas & Ferb - No More Bunny Business.mp3',
    '[S02.E15b] Phineas & Ferb - Spa Day.mp3',
    '[S02.E16a] Phineas & Ferb - Bubble Boys.mp3',
    '[S02.E16b] Phineas & Ferb - Isabella and the Temple of Sap.mp3',
    '[S02.E17a] Phineas & Ferb - Cheer Up Candace.mp3',
    '[S02.E17b] Phineas & Ferb - Fireside Girl Jamboree.mp3',
    '[S02.E18a] Phineas & Ferb - The Bully Pledge.mp3',
    '[S02.E18b] Phineas & Ferb - Finding Mary McGuffin.mp3',
    '[S02.E19a] Phineas & Ferb - What Do It Do.mp3',
    '[S02.E19b] Phineas & Ferb - Atlantis.mp3',
    '[S02.E20a] Phineas & Ferb - Picture This.mp3',
    "[S02.E20b] Phineas & Ferb - Nerdy Dancin'.mp3",
    '[S02.E21a] Phineas & Ferb - I Was a Middle Aged Robot.mp3',
    '[S02.E21b] Phineas & Ferb - Suddenly Suzy.mp3',
    '[S02.E23a] Phineas & Ferb - Undercover Carl.mp3',
    '[S02.E23b] Phineas & Ferb - Hip Hip Parade.mp3',
    '[S02.E24a] Phineas & Ferb - Just Passing Through.mp3',
    "[S02.E24b] Phineas & Ferb - Candace's Big Day.mp3",
    '[S02.E25a] Phineas & Ferb - Invasion of the Ferb Snatchers.mp3',
    "[S02.E25b] Phineas & Ferb - Ain't No Kiddie Ride.mp3",
    '[S02.E26] Phineas & Ferb - Wizard of Odd.mp3',
    '[S02.E27] Phineas & Ferb - The Beak.mp3',
    '[S02.E28a] Phineas & Ferb - Not Phineas and Ferb.mp3',
    '[S02.E28b] Phineas & Ferb - Phineas and Ferb-busters.mp3',
    '[S02.E29a] Phineas & Ferb - The Lizard Whisperer.mp3',
    '[S02.E29b] Phineas & Ferb - Robot Rodeo.mp3',
    '[S02.E30a] Phineas & Ferb - The Secret of Success.mp3',
    "[S02.E31a] Phineas & Ferb - She's the Mayor.mp3",
    '[S02.E31b] Phineas & Ferb - The Lemonade Stand.mp3',
    '[S02.E32a] Phineas & Ferb - We Call It Maze.mp3',
    '[S02.E32b] Phineas & Ferb - Ladies and Gentlemen, Meet Max Modem!.mp3',
    '[S02.E33a] Phineas & Ferb - Nerds of a Feather Part 1.mp3',
    "[S02.E34] Phineas & Ferb - Phineas and Ferb's Hawaiian Vacation.mp3",
    '[S02.E35a] Phineas & Ferb - Split Personality.mp3',
    '[S02.E35b] Phineas & Ferb - Brain Drain.mp3',
    '[S02.E36a] Phineas & Ferb - Make Play.mp3',
    '[S02.E36b] Phineas & Ferb - Candace Gets Busted.mp3',
    '[S02.E38] Phineas & Ferb - Summer Belongs to You Part 2.mp3',
    '[S02.E39] Phineas & Ferb - Rollercoaster the Musical!.mp3',
]


INTERVIEW = "[S02.E02a] Phineas & Ferb - Interview with a Platypus.mp3"


# ── the fold ──────────────────────────────────────────────────────────────────

def test_the_show_name_with_an_ampersand_is_read_past():
    assert "interview with a platypus" in donor_title_readings(INTERVIEW, SHOW)
    assert donor_names_episode("Interview With a Platypus", INTERVIEW, series_title=SHOW) is True


@pytest.mark.parametrize("a, b", [
    ("Rock & Roll", "Rock and Roll"),
    ("Rock&Roll", "Rock and Roll"),
    ("Tom & Jerry", "Tom and Jerry"),
    ("Q&A", "Q and A"),
    ("Phineas & Ferb's Quantum Boogaloo", "Phineas and Ferb's Quantum Boogaloo"),
])
def test_an_ampersand_and_the_word_agree(a, b):
    assert titles_agree(a, b, fuzzy=False) is True


def test_an_ampersand_no_longer_vanishes():
    # Before, "&" folded to nothing, so these two agreed exactly. They are not
    # the same words, and the exact comparison now says so.
    assert titles_agree("Tom & Jerry", "Tom Jerry", fuzzy=False) is False


def test_a_show_whose_own_name_has_an_ampersand_is_read_past_either_way():
    assert "payback" in donor_title_readings("[S01.E01] Law and Order SVU - Payback.mp3", "Law & Order: SVU")
    assert "payback" in donor_title_readings("[S01.E01] Law & Order: SVU - Payback.mp3", "Law and Order: SVU")


def test_season_one_names_read_as_before():
    readings = donor_title_readings("[S01.E02b] Phineas and Ferb - Lawn Gnome Beach Party of Terror.mp3", SHOW)
    assert "lawn gnome beach party of terror" in readings


# ── the walk: the title's pick first, the number's pick still after it ───────

def _walk(monkeypatch, tmp_path, episode, title):
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in SEASON_2_NAMES:
            zf.writestr(f"{FOLDER}/{name}", name.encode())
    video = tmp_path / SHOW / "Season 2" / f"Phineas.and.Ferb.S02E{episode:02d}.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")

    class Client:
        def search_shows(self, query):
            return [{"name": FOLDER, "url": "https://av/pack"}]

    aligned = []

    def gate(config, video_path, audio_path, label=None, episode_title="", **_):
        aligned.append(audio_path.name)
        return False, "No obvious cause from energy analysis."

    monkeypatch.setattr(workflow, "source_has_ad_track", lambda p: False)
    monkeypatch.setattr(workflow, "foreign_only_audio", lambda p: None)
    monkeypatch.setattr(workflow, "find_season", lambda results, *a, **k: results)
    monkeypatch.setattr(workflow, "_get_cached", lambda *a, **k: zip_path)
    monkeypatch.setattr(workflow, "_mark_episode_done", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "load_extra_sources", lambda: [])
    monkeypatch.setattr(workflow, "_align_and_keep", gate)
    process_episode(Client(), config, video, SHOW, 2, episode, series_year="2009", episode_title=title)
    return aligned


def test_the_recording_named_for_the_episode_is_aligned_first(monkeypatch, tmp_path):
    aligned = _walk(monkeypatch, tmp_path, 4, "Attack of the 50 Foot Sister")
    assert aligned[0] == "[S02.E03a] Phineas & Ferb - Attack of the 50 Foot Sister.mp3"
    assert aligned[1] == "[S02.E04a] Phineas & Ferb - Day of the Living Gelatin.mp3"   # the number's pick, as before


def test_a_title_that_repeats_the_show_name_is_found_too(monkeypatch, tmp_path):
    aligned = _walk(monkeypatch, tmp_path, 25, "Phineas and Ferb's Quantum Boogaloo")
    assert aligned[0] == "[S02.E11] Phineas & Ferb - Phineas and Ferb's Quantum Boogaloo.mp3"
