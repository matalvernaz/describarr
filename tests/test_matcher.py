"""find_season candidate ordering: description-variant preference."""

from describarr.matcher import find_season


def _names(candidates):
    return [c["name"] for c in candidates]


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
