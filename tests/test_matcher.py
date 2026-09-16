"""find_season / find_movie candidate selection and ordering."""

from describarr.matcher import find_movie, find_season


def _names(candidates):
    return [c["name"] for c in candidates]


def _movies(names):
    return [{"name": n, "url": f"u{i}"} for i, n in enumerate(names)]


def test_human_variant_ranked_before_tts_for_same_season():
    # Regression: [TTS] is a shorter title suffix than [New Description], so a
    # raw-similarity sort tried the worst (synthetic) variant first, burning a
    # scarce daily download slot on the variant least likely to align. Stripping
    # the tag ties the variants on title score; quality then orders them.
    results = [
        {"name": "The Big Bang Theory - Season 01 (2007) [TTS]", "url": "u-tts"},
        {"name": "The Big Bang Theory - Season 01 (2007) [New Description]", "url": "u-new"},
        {"name": "The Big Bang Theory - Season 01 (2007) [Old Description]", "url": "u-old"},
    ]
    ordered = _names(find_season(results, "The Big Bang Theory", 1))
    assert ordered.index("The Big Bang Theory - Season 01 (2007) [New Description]") < \
           ordered.index("The Big Bang Theory - Season 01 (2007) [Old Description]") < \
           ordered.index("The Big Bang Theory - Season 01 (2007) [TTS]")


def test_quality_never_promotes_a_wrong_show_over_a_near_exact_match():
    # Quality is only a tiebreaker — it must not let a low-similarity human
    # variant of the wrong show outrank a near-exact TTS match of the right one.
    results = [
        {"name": "The Big Bang Theory - Season 01 [TTS]", "url": "u-right"},
        {"name": "The Big Brain Theory - Season 01 [New Description]", "url": "u-wrong"},
    ]
    ordered = _names(find_season(results, "The Big Bang Theory", 1))
    assert ordered[0] == "The Big Bang Theory - Season 01 [TTS]"


def test_sole_tts_variant_still_returned():
    results = [{"name": "Some Show - Season 02 [TTS]", "url": "u"}]
    assert _names(find_season(results, "Some Show", 2)) == ["Some Show - Season 02 [TTS]"]

# ------------------------------------------------------------------
# find_movie: bracket tags, release years, narration language
# ------------------------------------------------------------------

def test_region_tag_is_not_a_title_token():
    # Regression: the movie "Us" Jaccard-matched the [US] region tag, so the
    # whole [US] catalog ranked as candidates ('1917 (2019) [US]' scored 1.15,
    # above any real match) and the candidate walk burned the daily cap.
    results = _movies([
        "1917 (2019) [US]",
        "2012 (2009) [US]",
        "Abominable (2019) [US]",
        "65 (2023) [US]",
        "Us (2019) [US]",
    ])
    assert _names(find_movie(results, "us", "2019")) == ["Us (2019) [US]"]


def test_conflicting_release_year_rejected():
    results = _movies(["Secret Obsession (2019)", "Obsession (2026)"])
    assert _names(find_movie(results, "obsession", "2026")) == ["Obsession (2026)"]


def test_no_year_keeps_candidates_and_ranks_exact_title_first():
    results = _movies(["Wonder Woman 1984 (2020)", "Wonder Woman (2017)"])
    names = _names(find_movie(results, "wonder woman", ""))
    assert names[0] == "Wonder Woman (2017)"
    assert "Wonder Woman 1984 (2020)" in names


def test_bare_year_is_title_content():
    # Only the parenthesised year is metadata; a bare "1984" means the sequel.
    results = _movies(["Wonder Woman 1984 (2020)", "Wonder Woman (2017)"])
    assert _names(find_movie(results, "wonder woman 1984", "2020")) == \
        ["Wonder Woman 1984 (2020)"]


def test_pure_numeric_title_still_matches():
    results = _movies(["2012 (2009) [US]"])
    assert _names(find_movie(results, "2012", "2009")) == ["2012 (2009) [US]"]


def test_year_stripping_no_longer_defeats_sequel_titles():
    # Ledger bug: "Blade Runner 2049" collapsed to "Blade Runner" after
    # year-token stripping and false-matched the 1982 film's request.
    results = _movies(["Blade Runner 2049 (2017)"])
    assert find_movie(results, "blade runner", "1982") == []


def test_sequel_guard_survives_paren_year_removal():
    results = _movies(["Iron Man 2 (2010)"])
    assert find_movie(results, "iron man 3", "") == []


def test_foreign_narration_rejected():
    results = _movies([
        "Wonka (2023) [Persian Description]",
        "Wonka (2023) [UK]",
        "Wonka (2023) [US]",
    ])
    names = _names(find_movie(results, "wonka", "2023"))
    assert "Wonka (2023) [Persian Description]" not in names
    assert len(names) == 2


def test_movie_tts_ranked_after_human():
    results = _movies(["Year One (2009) [TTS]", "Year One (2009)"])
    assert _names(find_movie(results, "year one", "2009")) == \
        ["Year One (2009)", "Year One (2009) [TTS]"]


def test_foreign_narration_rejected_for_seasons():
    results = [{"name": "Some Show - Season 02 [Spanish Description]", "url": "u"}]
    assert find_season(results, "Some Show", 2) == []


def test_spinoff_named_by_appending_to_the_parent_is_rejected():
    # Live 2026-09-16: "The Epic Tales of Captain Underpants" S01E01-E06 were
    # published with audio description lifted from "...in Space", a separate
    # 2020 series. It was the ONLY candidate — the parent show isn't in the
    # catalogue — so ranking could not save us, and symmetric Jaccard scored it
    # 0.67, comfortably over the 0.3 floor, because every word of the query is
    # present. Same runtime and the same voice cast then carried it past
    # alignment at 69-79% similarity. The matcher is the only guard for this.
    results = [
        {"name": "The Epic Tales of Captain Underpants in Space - Season 1 (2020)",
         "url": "u-space"},
    ]
    assert find_season(results, "The Epic Tales of Captain Underpants", 1, "2018") == []


def test_spinoff_rejected_without_a_series_year():
    # The year lock only engages when Sonarr supplies a series year; the
    # extra-words guard must not depend on one.
    results = [
        {"name": "The Fairly OddParents: A New Wish - Season 1 (2024)", "url": "u-wish"},
        {"name": "The Fairly OddParents- Fairly Odder - Season 1 (2022)", "url": "u-odder"},
    ]
    assert find_season(results, "The Fairly OddParents", 1) == []


def test_season_1_year_only_fallback_rejects_a_revival_subtitle():
    # "Gilmore Girls - A Year in the Life" (2016) is a revival miniseries, not
    # season 1 of the 2000 show. It carries no season marker, so it reached the
    # season-1 year-only fallback pool and cost a download slot.
    results = [{"name": "Gilmore Girls - A Year in the Life (2016)", "url": "u-yitl"}]
    assert find_season(results, "Gilmore Girls", 1, "2000") == []


def test_the_shows_own_entry_still_matches_after_the_guard():
    # The guard keys on the candidate carrying EXTRA title words. The season
    # number is not one: it is metadata the season filter has already matched,
    # and leaving it in the token set would make every correct entry a superset
    # of its own show title and reject the lot.
    results = [
        {"name": "The Epic Tales of Captain Underpants - Season 1 (2018)", "url": "u-real"},
        {"name": "The Epic Tales of Captain Underpants in Space - Season 1 (2020)",
         "url": "u-space"},
    ]
    assert _names(find_season(results, "The Epic Tales of Captain Underpants", 1, "2018")) == [
        "The Epic Tales of Captain Underpants - Season 1 (2018)",
    ]


def test_a_terser_catalogue_entry_is_not_rejected():
    # The asymmetry is the whole signal. A catalogue that drops a possessive
    # prefix the arr app carries ("Marvel's Daredevil" → "Daredevil") is
    # ordinary terseness, not a different work, and must still match.
    results = [{"name": "Daredevil - Season 1 (2015)", "url": "u-dd"}]
    assert _names(find_season(results, "Marvel's Daredevil", 1, "2015")) == [
        "Daredevil - Season 1 (2015)",
    ]


def test_country_qualifier_is_not_an_added_title_word():
    # Live catalogue: Sonarr calls the 2001 show "The Office"; AudioVault calls
    # it "The Office UK" and the remake "The Office US". A strict superset test
    # rejects both, losing the show its own audio description. The country tag
    # names a regional version of one format, not a different work — the year
    # lock is what separates them.
    results = [
        {"name": "The Office UK - Season 1 (2001)", "url": "u-uk"},
        {"name": "The Office US - Season 1 (2005) [TTS]", "url": "u-us"},
        {"name": "The Office: Superfan Episodes - Season 1 (2005)", "url": "u-sf"},
    ]
    assert _names(find_season(results, "The Office", 1, "2001")) == [
        "The Office UK - Season 1 (2001)",
    ]


def test_a_split_season_survives_the_guard():
    # A catalogue that splits one season across two uploads writes "Season 1
    # Part 1". That is release structure like the season marker itself, not an
    # added title word, and both parts must stay walkable.
    results = [
        {"name": "Gossip Girl - Season 1 Part 1 (2007)", "url": "u-p1"},
        {"name": "Gossip Girl - Season 1 Part 2 (2007)", "url": "u-p2"},
    ]
    assert sorted(_names(find_season(results, "Gossip Girl", 1, "2007"))) == [
        "Gossip Girl - Season 1 Part 1 (2007)",
        "Gossip Girl - Season 1 Part 2 (2007)",
    ]


def test_a_one_off_special_is_not_a_season():
    # "The Office UK - Christmas Special" clears the country carve-out but
    # still adds two real title words, and it is not season 1 of anything.
    results = [{"name": "The Office UK - Christmas Special (2003)", "url": "u-xmas"}]
    assert find_season(results, "The Office", 1, "2001") == []
