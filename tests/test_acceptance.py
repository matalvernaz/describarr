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
