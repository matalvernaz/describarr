"""The punctuation-stripped retry when a literal AudioVault search misses."""

from describarr.audiovault import AudioVaultClient, _normalize_search_query


def _client_with_canned_search(monkeypatch, responses):
    """Build a client (skipping login) whose _search_once pops from *responses*."""
    client = AudioVaultClient.__new__(AudioVaultClient)
    queries = []

    def fake_search_once(path, query):
        queries.append(query)
        return responses.pop(0)

    monkeypatch.setattr(client, "_search_once", fake_search_once)
    return client, queries


def test_normalize_collapses_dashes_and_colons():
    assert _normalize_search_query("The 40 Year-Old Virgin") == "The 40 Year Old Virgin"
    assert _normalize_search_query("Austin Powers: The Spy Who Shagged Me") == (
        "Austin Powers The Spy Who Shagged Me"
    )
    assert _normalize_search_query("Blade Runner 2049") == "Blade Runner 2049"


def test_zero_results_retries_normalized(monkeypatch):
    hit = [{"name": "The 40 Year Old Virgin (2005) [US]", "url": "u"}]
    client, queries = _client_with_canned_search(monkeypatch, [[], hit])
    assert client._search("/movies", "The 40 Year-Old Virgin") == hit
    assert queries == ["The 40 Year-Old Virgin", "The 40 Year Old Virgin"]


def test_literal_hit_never_retries(monkeypatch):
    hit = [{"name": "Up (2009) [US]", "url": "u"}]
    client, queries = _client_with_canned_search(monkeypatch, [hit])
    assert client._search("/movies", "Up") == hit
    assert queries == ["Up"]


def test_no_punctuation_no_second_query(monkeypatch):
    client, queries = _client_with_canned_search(monkeypatch, [[]])
    assert client._search("/movies", "Up") == []
    assert queries == ["Up"]


# "&" and "and" are two spellings of one title. Sonarr's "Law & Order: Special
# Victims Unit" is the catalogue's "Law and Order: Special Victims Unit"; the
# literal search found nothing and 595 episodes were recorded as having no
# source (2026-10-10).

def test_an_ampersand_is_retried_as_and(monkeypatch):
    hit = [{"name": "Law and Order: Special Victims Unit - Season 01 (1999)", "url": "u"}]
    client, queries = _client_with_canned_search(monkeypatch, [[], hit, [], []])
    assert client._search("/shows", "Law & Order: Special Victims Unit") == hit
    assert queries == [
        "Law & Order: Special Victims Unit", "Law and Order: Special Victims Unit",
        "Law & Order Special Victims Unit", "Law and Order Special Victims Unit",
    ]


# One spelling's hit must not hide another's: "Tom & Jerry: The Movie (2021)"
# answers the swapped spelling, and the film asked for, "Tom and Jerry The
# Movie (1992)", answers only the collapsed one. The matcher chooses by year,
# so it must see both.
def test_every_spellings_results_are_pooled(monkeypatch):
    remake = {"name": "Tom & Jerry: The Movie (2021)", "url": "u2021"}
    film = {"name": "Tom and Jerry The Movie (1992)", "url": "u1992"}
    client, queries = _client_with_canned_search(monkeypatch, [[], [remake], [film], []])
    assert client._search("/movies", "Tom and Jerry: The Movie") == [remake, film]
    assert queries == [
        "Tom and Jerry: The Movie", "Tom & Jerry: The Movie",
        "Tom and Jerry The Movie", "Tom & Jerry The Movie",
    ]


def test_the_same_entry_from_two_spellings_is_listed_once(monkeypatch):
    a = {"name": "Law and Order - Season 01 (1990)", "url": "ua"}
    b = {"name": "Law and Order: Organized Crime - Season 1 (2021)", "url": "ub"}
    client, queries = _client_with_canned_search(monkeypatch, [[a], [a, b], [], []])
    assert client._search("/shows", "Law and Order") == [a, b]


def test_and_is_retried_as_an_ampersand(monkeypatch):
    hit = [{"name": "Harold & Kumar Go to White Castle (2004)", "url": "u"}]
    client, queries = _client_with_canned_search(monkeypatch, [[], hit])
    assert client._search("/movies", "Harold and Kumar Go to White Castle") == hit
    assert queries == [
        "Harold and Kumar Go to White Castle", "Harold & Kumar Go to White Castle",
    ]


def test_every_spelling_is_tried_before_giving_up(monkeypatch):
    client, queries = _client_with_canned_search(monkeypatch, [[], [], [], []])
    assert client._search("/shows", "Law & Order: SVU") == []
    assert queries == [
        "Law & Order: SVU", "Law and Order: SVU", "Law & Order SVU", "Law and Order SVU",
    ]


def test_and_inside_a_word_is_left_alone(monkeypatch):
    client, queries = _client_with_canned_search(monkeypatch, [[]])
    assert client._search("/shows", "Grand Designs") == []
    assert queries == ["Grand Designs"]
