"""A donor names its episode whatever the leading article or the part marker.

Law & Order: SVU (2026-10-17): three episodes the engine measured in sync, past
every measured-sync bar, were refused because acceptance compared titles word
for word. AudioVault and LivingAudio name the recordings "Misunderstanding" for
Sonarr's "A Misunderstanding", "Ballad of Dwight and Irena" for "The Ballad of
Dwight and Irena", and "ClosurePart2" or "Closure (Part 2)" for "Closure (2)".
A leading article and the way a closing part number is written are not a
different title; a near spelling and a different part number still are. These
pin both halves, in the title rules and through _align_and_keep.
"""

import pytest

from describarr.titles import donor_names_episode, donor_title_readings, titles_agree
from test_measured_sync import GOOD_CHECK, _keep, _wobbly

SVU = "Law & Order: Special Victims Unit"


# ── the title rules ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("title, donor", [
    ("A Misunderstanding", "12 Misunderstanding.mp3"),                     # AudioVault, S17E12
    ("A Misunderstanding", "17.12 Misunderstanding.mp3"),                  # LivingAudio
    ("The Ballad of Dwight and Irena", "22.02 Ballad of Dwight and Irena.mp3"),
    ("Closure (2)", "03 ClosurePart2.mp3"),                                # AudioVault, S02E03
    ("Closure (2)", "02.03 Closure (Part 2).mp3"),                         # LivingAudio
    ("Closure (2)", "02.03 Closure, Pt. 2.mp3"),
    ("Closure (2)", "02.03 Closure Part  2.mp3"),
])
def test_the_donor_names_the_episode(title, donor):
    assert donor_names_episode(title, donor, series_title=SVU) is True


def test_a_release_name_without_the_article_names_the_episode():
    # Family Guy S13E02's release is "Book.of.Joe"; the pack says "The book of Joe".
    assert donor_names_episode("Book of Joe", "13 - 04  Family Guy - The book of Joe.mp3",
                               series_title="Family Guy") is True


@pytest.mark.parametrize("title, donor", [
    ("Closure (2)", "02.02 Closure (Part 1).mp3"),                         # the other part
    ("Closure (1)", "03 ClosurePart2.mp3"),
    ("Closure (2)", "Closure.mp3"),                                        # the whole, for part 2
    ("Lost Traveller", "09 Lost Traveler.mp3"),                            # a near spelling
    ("Pilot", "01 Pilots.mp3"),
    ("A Misunderstanding", "17.13 41 Witnesses.mp3"),                      # another episode
])
def test_the_donor_does_not_name_the_episode(title, donor):
    assert donor_names_episode(title, donor, series_title=SVU) is False


@pytest.mark.parametrize("a, b", [
    ("Book of Joe", "Book of the Joe"),          # only a LEADING article goes
    ("The Fall", "A Rise"),
    ("Counterpart 2", "Counter 2"),              # "part" inside a word is not a marker
    ("Counterpart 2", "Counter2"),
    ("Crypt 2", "Cry2"),
    ("Spare Parts 2", "Spare 2"),
    ("Part 2: The Return", "2: The Return"),     # only a CLOSING part number is folded
    ("The", "A"),
])
def test_other_words_still_differ(a, b):
    assert titles_agree(a, b) is False


@pytest.mark.parametrize("a, b", [
    ("A Misunderstanding", "Misunderstanding"),
    ("An Officer", "Officer"),
    ("Closure (2)", "Closure Part 2"),
    ("Closure (2)", "Closure, Pt. 2"),
    ("The Ballad (2)", "Ballad Part 2"),         # both at once
])
def test_acceptance_reads_one_title(a, b):
    assert titles_agree(a, b) is True
    assert titles_agree(b, a) is True


def test_a_fuzzy_reading_still_needs_the_same_part_number():
    assert titles_agree("Closure (1)", "Closure Part 2", fuzzy=True) is False
    assert titles_agree("Lost Traveller", "Lost Traveler") is False
    assert titles_agree("Lost Traveller", "Lost Traveler", fuzzy=True) is True


def test_a_glued_part_number_is_read_spaced_out():
    readings = donor_title_readings("03 ClosurePart2.mp3", SVU)
    assert readings[0] == "03 ClosurePart2"                                 # the literal stays first
    assert "closure part 2" in readings                                     # and past the track number


@pytest.mark.parametrize("donor", ["03 Counterpart2.mp3", "03 Closurepart2.mp3", "03 ClosurePart.mp3"])
def test_only_a_capitalised_part_number_unglues(donor):
    assert not any("part 2" in reading for reading in donor_title_readings(donor, SVU))


# ── through _align_and_keep ─────────────────────────────────────────────────

# S17E12 against "17.12 Misunderstanding.mp3", as the engine measured it (2026-10-17).
MEASURED_MISUNDERSTANDING = dict(GOOD_CHECK, confirmed_pct=74.07, within_pct=99.81, over_pct=0.03, anchored_pct=76.2)
# S02E03 against "03 ClosurePart2.mp3".
MEASURED_CLOSURE = dict(GOOD_CHECK, confirmed_pct=66.65, within_pct=98.6, over_pct=0.23, anchored_pct=70.65)


@pytest.mark.parametrize("donor, title, measurement", [
    ("17.12 Misunderstanding.mp3", "A Misunderstanding", MEASURED_MISUNDERSTANDING),
    ("22.02 Ballad of Dwight and Irena.mp3", "The Ballad of Dwight and Irena",
     dict(GOOD_CHECK, confirmed_pct=57.65, within_pct=100.0, over_pct=0.0)),
    ("03 ClosurePart2.mp3", "Closure (2)", MEASURED_CLOSURE),
])
def test_the_refused_episodes_publish(monkeypatch, tmp_path, donor, title, measurement):
    ok, _, published, decision = _keep(monkeypatch, tmp_path, segments=_wobbly(), measurement=measurement,
                                       donor=donor, title=title, similarity=58.6)
    assert ok and len(published) == 1
    assert decision["path"] == "measured-sync"


@pytest.mark.parametrize("donor, title, measurement", [
    ("17.13 41 Witnesses.mp3", "A Misunderstanding", MEASURED_MISUNDERSTANDING),
    ("02.02 Closure (Part 1).mp3", "Closure (2)", MEASURED_CLOSURE),
    ("Closure.mp3", "Closure (2)", MEASURED_CLOSURE),
])
def test_a_donor_titled_for_something_else_stays_refused(monkeypatch, tmp_path, donor, title, measurement):
    ok, _, published, decision = _keep(monkeypatch, tmp_path, segments=_wobbly(), measurement=measurement,
                                       donor=donor, title=title, similarity=58.6)
    assert not ok and not published
    assert decision["path"] == "reject"
