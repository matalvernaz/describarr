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
