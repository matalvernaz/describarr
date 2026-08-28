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
