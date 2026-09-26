"""Acceptance-gate matrix.

similarity is the sync gate; the drift rescue must never accept on structure
alone. The headline regression guard is
``test_high_coverage_low_similarity_is_rejected`` — the exact failure where a
smoothly-aligned but mis-synced (or wrong) track used to overwrite the
original via the old ``coverage_ok`` path.
"""

from describarr.workflow import _acceptance_decision

MIN_SCORE = 65.0


def _decide(**overrides):
    base = dict(
        score=0.0,
        content_coverage=0.0,
        stable_fraction=0.0,
        median_rate=0.0,
        total_runtime=6000.0,
        sync_ok=True,
        min_score=MIN_SCORE,
    )
    base.update(overrides)
    return _acceptance_decision(**base)


def test_high_similarity_accepts():
    accepted, path, _ = _decide(score=80.0)
    assert accepted
    assert path == "similarity"


def test_high_coverage_low_similarity_is_rejected():
    # Regression for the coverage_ok bug: a wrong-but-smooth alignment with
    # excellent coverage and no drift structure must NOT publish.
    accepted, path, _ = _decide(
        score=10.0, content_coverage=99.0, stable_fraction=20.0, median_rate=0.0
    )
    assert not accepted
    assert path == "reject"


def test_pal_ntsc_drift_rescue_accepts():
    accepted, path, _ = _decide(
        score=45.0, content_coverage=99.0, stable_fraction=99.0,
        median_rate=4.27, total_runtime=3000.0, sync_ok=True,
    )
    assert accepted
    assert path == "drift-rescue"


def test_native_rate_seam_rescue_accepts():
    accepted, path, _ = _decide(
        score=45.0, stable_fraction=98.0, median_rate=0.1, sync_ok=True
    )
    assert accepted
    assert path == "drift-rescue"


def test_wildly_wrong_rate_rejected_even_if_stable():
    # Upper-bound guard: a "stably wrong" 40% rate is not a real frame-rate
    # shift, so structural stability must not rescue it.
    accepted, _, _ = _decide(
        score=45.0, stable_fraction=99.0, median_rate=40.0, sync_ok=True
    )
    assert not accepted


def test_rescue_requires_sync_ok():
    accepted, _, _ = _decide(
        score=45.0, stable_fraction=99.0, median_rate=4.27, sync_ok=False
    )
    assert not accepted


def test_rescue_requires_min_score_floor():
    accepted, _, _ = _decide(
        score=15.0, stable_fraction=99.0, median_rate=4.27, sync_ok=True
    )
    assert not accepted


def test_rescue_requires_min_runtime():
    accepted, _, _ = _decide(
        score=45.0, stable_fraction=99.0, median_rate=4.27,
        total_runtime=120.0, sync_ok=True,
    )
    assert not accepted


def test_middle_rate_band_rejected():
    # 0.5 < |rate| < 2.0: neither native sync nor a known drift → reject.
    accepted, _, _ = _decide(
        score=45.0, stable_fraction=99.0, median_rate=1.2, sync_ok=True
    )
    assert not accepted


def test_low_stable_fraction_blocks_rescue():
    accepted, _, _ = _decide(
        score=45.0, stable_fraction=70.0, median_rate=4.27, sync_ok=True
    )
    assert not accepted


# ── mixed-rate rescue: per-act broadcast time compression (2026-09-26) ───────
# This Is Us S04-S06 and Brooklyn Nine-Nine S06-S08 (both NBC): each act maps
# as a straight line, but one to three acts of the off-air description run
# 0.3-1.9 % fast while the rest run native. The single-rate trunk saw 51-84 %
# and refused 20 episodes whose every act lined up.

def test_per_act_compression_is_accepted():
    # This Is Us S05E06 as measured: 46.3 % similarity, trunk 59.0 %.
    accepted, path, _ = _decide(
        score=46.3, content_coverage=99.9, stable_fraction=59.0, median_rate=0.0,
        total_runtime=2559.0, sync_ok=False, piecewise_fraction=98.9,
    )
    assert accepted
    assert path == "mixed-rate-rescue"


def test_uniform_compression_under_two_percent_is_accepted():
    # Brooklyn Nine-Nine S08E09: every act 1.4-1.8 % fast, so neither native
    # nor a PAL shift, yet one long straight segment per act.
    accepted, path, _ = _decide(
        score=46.4, content_coverage=99.83, stable_fraction=77.4, median_rate=1.6,
        sync_ok=False, piecewise_fraction=99.8,
    )
    assert accepted
    assert path == "mixed-rate-rescue"


def test_mixed_rate_still_needs_the_rescue_score_floor():
    # This Is Us S01E12 French-first scored 20.4 % over a straight map; the
    # per-act path must not become a way round the floor.
    accepted, _, _ = _decide(
        score=20.4, stable_fraction=99.9, median_rate=0.0, sync_ok=True,
        piecewise_fraction=99.9,
    )
    assert not accepted


def test_mixed_rate_needs_most_of_the_runtime_in_long_straight_segments():
    accepted, _, _ = _decide(
        score=46.3, stable_fraction=59.0, median_rate=0.0, sync_ok=False,
        piecewise_fraction=80.0,
    )
    assert not accepted


def test_mixed_rate_needs_near_complete_coverage():
    accepted, _, _ = _decide(
        score=46.3, content_coverage=97.0, stable_fraction=59.0, median_rate=0.0,
        sync_ok=False, piecewise_fraction=98.9,
    )
    assert not accepted


def test_mixed_rate_needs_min_runtime():
    accepted, _, _ = _decide(
        score=46.3, stable_fraction=59.0, median_rate=0.0, sync_ok=False,
        piecewise_fraction=99.0, total_runtime=120.0,
    )
    assert not accepted


# ── corroborated rescue: low score, but independent evidence (2026-09-26) ────
# Family Guy donors always score low on similarity (last season 26-70 %, mostly
# 30-47 %); S21E01/E03/E04 and S20E09 scored 20.8-28.7 % over one native-rate
# line. A straight line proves the timing is uniform, not that the content is
# right, and a French-first file looks the same (20.4 %). So below the rescue
# floor the gate needs two checks the score cannot give: the donor names this
# episode, and the track that was aligned is English.

def _corroborated(**overrides):
    base = dict(
        score=20.8, content_coverage=99.9, stable_fraction=99.5, median_rate=0.0,
        total_runtime=1259.0, sync_ok=True, title_corroborated=True,
        primary_audio_english=True,
    )
    base.update(overrides)
    return _decide(**base)


def test_low_score_with_title_and_language_corroboration_is_accepted():
    accepted, path, _ = _corroborated()
    assert accepted
    assert path == "corroborated-rescue"


def test_low_score_without_title_corroboration_is_refused():
    assert not _corroborated(title_corroborated=False)[0]


def test_low_score_on_a_foreign_first_track_is_refused():
    # This Is Us S01E12: straight, native, right episode by title, French audio.
    assert not _corroborated(score=20.4, stable_fraction=99.9, primary_audio_english=False)[0]


def test_corroboration_never_goes_below_the_engines_own_mismatch_line():
    assert not _corroborated(score=19.9)[0]


def test_corroboration_needs_an_almost_perfect_native_line():
    assert not _corroborated(stable_fraction=98.0)[0]
    assert not _corroborated(median_rate=1.0)[0]
    assert not _corroborated(sync_ok=False)[0]
    assert not _corroborated(content_coverage=95.0)[0]
    assert not _corroborated(total_runtime=120.0)[0]
