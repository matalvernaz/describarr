"""AudioVault numbers a split-segment show by half-hour slot, with a letter per segment.

Phineas and Ferb (2026-10-15): AudioVault's season pack holds every 11-minute
segment with its own title, "[S01.E02b] Phineas and Ferb - Lawn Gnome Beach
Party of Terror.mp3", while the library numbers each segment as an episode
(S01E02 is "Lawn Gnome Beach Party of Terror"). The bracketed marker with a
letter was not read, so no donor title was either, and every episode was
aligned against the number's pick, another segment: one described, fourteen
engine failures, one refusal. These pin reading the letter, and that the
number's pick and everything else stay as they were.
"""

import zipfile

import pytest

import describarr.workflow as workflow
from describarr.config import Config
from describarr.titles import donor_episode_title, donor_names_episode, donor_title_readings
from describarr.workflow import process_episode

SHOW = "Phineas and Ferb"

# AudioVault's "Phineas and Ferb - Season 1 (2007)", as listed on 2026-10-15.
SEASON_1 = ("Phineas and Ferb - Season 1 (2007)", [
    "[S01.E01a] Phineas and Ferb - Rollercoaster.mp3",
    "[S01.E01b] Phineas and Ferb - Candace Loses Her Head.mp3",
    "[S01.E02a] Phineas and Ferb - The Fast and the Phineas.mp3",
    "[S01.E02b] Phineas and Ferb - Lawn Gnome Beach Party of Terror.mp3",
    "[S01.E03a] Phineas and Ferb - The Magnificent Few.mp3",
    "[S01.E03b] Phineas and Ferb - S'Winter.mp3",
    "[S01.E04a] Phineas and Ferb - Are You My Mummy.mp3",
    "[S01.E04b] Phineas and Ferb - Flop Starz.mp3",
    "[S01.E05a] Phineas and Ferb - Raging Bully.mp3",
    "[S01.E05b] Phineas and Ferb - Lights, Candace, Action.mp3",
    "[S01.E06a] Phineas and Ferb - Get that Bigfoot Outta My Face.mp3",
    "[S01.E06b] Phineas and Ferb - Tree to Get Ready.mp3",
    "[S01.E07] Phineas and Ferb - It's About Time.mp3",
    "[S01.E08a] Phineas and Ferb - Jerk de Soleil.mp3",
    "[S01.E08b] Phineas and Ferb - Toy to the World.mp3",
    "[S01.E09] Phineas and Ferb - One Good Scare Ought to Do It.mp3",
    "[S01.E10a] Phineas and Ferb - A Hard Day's Knight.mp3",
    "[S01.E10b] Phineas and Ferb - I, Brobot.mp3",
    "[S01.E11a] Phineas and Ferb - Mom's Birthday.mp3",
    "[S01.E11b] Phineas and Ferb - Journey to the Center of Candace.mp3",
    "[S01.E12a] Phineas and Ferb - Run Away Runway.mp3",
    "[S01.E12b] Phineas and Ferb - I Scream, You Scream.mp3",
    "[S01.E13a] Phineas and Ferb - It's a Mud, Mud, Mud, Mud World.mp3",
    "[S01.E13b] Phineas and Ferb - The Ballad of Badbeard.mp3",
    "[S01.E14] Phineas and Ferb - Dude, We're Getting the Band Back Together.mp3",
    "[S01.E15a] Phineas and Ferb - Ready for the Bettys.mp3",
    "[S01.E15b] Phineas and Ferb - The Flying Fishmonger.mp3",
    "[S01.E16] Phineas and Ferb - At Last.mp3",
    "[S01.E17a] Phineas and Ferb - Greece Lightning.mp3",
    "[S01.E17b] Phineas and Ferb - Leave the Busting to Us.mp3",
    "[S01.E18a] Phineas and Ferb - Crack That Whip.mp3",
    "[S01.E18b] Phineas and Ferb - The Best Lazy Day Ever.mp3",
    "[S01.E19a] Phineas and Ferb - Boyfriend from 27,000 BC.mp3",
    "[S01.E19b] Phineas and Ferb - Voyage to the Bottom of Buford.mp3",
    "[S01.E20a] Phineas and Ferb - Put That Putter Away.mp3",
    "[S01.E20b] Phineas and Ferb - Does This Duckbill Make Me Look Fat.mp3",
    "[S01.E21a] Phineas and Ferb - Traffic Cam Caper.mp3",
    "[S01.E21b] Phineas and Ferb - Bowl-R-Ama Drama.mp3",
    "[S01.E22a] Phineas and Ferb - The Monster of Phineas-n-Ferbenstein.mp3",
    "[S01.E22b] Phineas and Ferb - Oil on Candace.mp3",
    "[S01.E23a] Phineas and Ferb - Unfair Science Fair.mp3",
    "[S01.E23b] Phineas and Ferb - Unfair Science Fair Redux (Another Story).mp3",
    "[S01.E25a] Phineas and Ferb - Got Game.mp3",
    "[S01.E25b] Phineas and Ferb - Comet Kermillian.mp3",
    "[S01.E26a] Phineas and Ferb - Out of Toon.mp3",
    "[S01.E26b] Phineas and Ferb - Hail Doofania.mp3",
])

LAWN_GNOME = "[S01.E02b] Phineas and Ferb - Lawn Gnome Beach Party of Terror.mp3"


# ── reading the name ──────────────────────────────────────────────────────────

def test_a_segment_lettered_name_reads_as_its_title():
    assert donor_episode_title(LAWN_GNOME) == "Phineas and Ferb - Lawn Gnome Beach Party of Terror"
    assert "lawn gnome beach party of terror" in donor_title_readings(LAWN_GNOME, SHOW)
    assert donor_names_episode("Lawn Gnome Beach Party of Terror", LAWN_GNOME, series_title=SHOW) is True


def test_an_uppercase_segment_letter_reads_the_same():
    upper = "[S01.E02B] Phineas and Ferb - Lawn Gnome Beach Party of Terror.mp3"
    assert donor_title_readings(upper, SHOW) == donor_title_readings(LAWN_GNOME, SHOW)


def test_more_than_one_letter_is_not_a_segment():
    # "[S01.E02Pilot]" is not a marker the catalogue writes; it stays as it is.
    assert donor_episode_title("[S01.E02Pilot].mp3") == "[S01.E02Pilot]"


@pytest.mark.parametrize("name, title", [
    ("[S21.E01] Oscars Guy.mp3", "Oscars Guy"),
    ("[S12.E21] 3 Acts of God.mp3", "3 Acts of God"),
    ("[S07.E25] What You Leave Behind, Part 1.mp3", "What You Leave Behind, Part 1"),
    ("[S01.E07] Phineas and Ferb - It's About Time.mp3", "Phineas and Ferb - It's About Time"),
])
def test_unlettered_markers_read_as_before(name, title):
    assert donor_episode_title(name) == title


# ── the walk: the title's pick first, the number's pick still after it ───────

def _walk(monkeypatch, tmp_path, episode, video_name, title=""):
    """process_episode over the real Season 1 listing with a gate that refuses
    everything; the donor names in the order they were aligned."""
    config = Config(email="e", password="p", cache_dir=tmp_path / "cache")
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in SEASON_1[1]:
            zf.writestr(f"{SEASON_1[0]}/{name}", name.encode())
    video = tmp_path / SHOW / "Season 1" / video_name
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")

    class Client:
        def search_shows(self, query):
            return [{"name": SEASON_1[0], "url": "https://av/pack"}]

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
    process_episode(Client(), config, video, SHOW, 1, episode, series_year="2007", episode_title=title)
    return aligned


def test_the_segment_titled_as_the_episode_is_aligned_first(monkeypatch, tmp_path):
    aligned = _walk(monkeypatch, tmp_path, 2,
                    "Phineas.and.Ferb.S01E02.Lawn.Gnome.Beach.Party.of.Terror.1080p.DSNP.WEBRip.DDP.2.0.H.265-iVy.mkv",
                    "Lawn Gnome Beach Party of Terror")
    assert aligned[0] == LAWN_GNOME
    # The number's pick is still tried after it, as before.
    assert aligned[1] == "[S01.E02a] Phineas and Ferb - The Fast and the Phineas.mp3"


@pytest.mark.parametrize("episode, title, first", [
    (3, "Flop Starz", "[S01.E04b] Phineas and Ferb - Flop Starz.mp3"),
    (4, "The Fast and The Phineas", "[S01.E02a] Phineas and Ferb - The Fast and the Phineas.mp3"),
    (10, "The Magnificent Few", "[S01.E03a] Phineas and Ferb - The Magnificent Few.mp3"),
])
def test_a_segment_numbered_elsewhere_is_found_by_its_title(monkeypatch, tmp_path, episode, title, first):
    aligned = _walk(monkeypatch, tmp_path, episode, f"Phineas.and.Ferb.S01E{episode:02d}.mkv", title)
    assert aligned[0] == first


# What master aligned for these two walks (recorded by running this module's
# _walk against the tree before the fix, 2026-10-15): with no title, or one no
# file carries, the fix must change nothing.
MASTER_NO_TITLE_E05 = [
    "[S01.E05a] Phineas and Ferb - Raging Bully.mp3",
    "[S01.E07] Phineas and Ferb - It's About Time.mp3",
]
MASTER_UNRELATED_TITLE_E05 = MASTER_NO_TITLE_E05


def test_with_no_title_the_walk_is_what_it_was(monkeypatch, tmp_path):
    aligned = _walk(monkeypatch, tmp_path, 5, "Phineas.and.Ferb.S01E05.1080p.DSNP.WEBRip.mkv")
    assert aligned == MASTER_NO_TITLE_E05


def test_with_a_title_no_file_carries_the_walk_is_what_it_was(monkeypatch, tmp_path):
    aligned = _walk(monkeypatch, tmp_path, 5, "Phineas.and.Ferb.S01E05.mkv", "Quantum Zebra Accountancy")
    assert aligned == MASTER_UNRELATED_TITLE_E05
