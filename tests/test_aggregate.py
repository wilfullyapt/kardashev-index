import pytest

from app.pipeline import methodology as meth
from app.pipeline.aggregate import aggregate


def test_weights_are_65_measured_35_judged_and_sum_to_one():
    assert sum(meth.WEIGHTS.values()) == pytest.approx(1.0)
    assert sum(meth.WEIGHTS[k] for k in meth.MEASURED_KEYS) == pytest.approx(0.65)
    assert sum(meth.WEIGHTS[k] for k in meth.JUDGED_KEYS) == pytest.approx(0.35)
    assert "overall" not in meth.WEIGHTS


def test_full_coverage_is_plain_weighted_mean():
    scores = {k: (6.0, 1.0) for k in meth.CATEGORY_KEYS}
    scores["energy_throughput"] = (10.0, 1.0)
    agg = aggregate(scores)
    assert agg.index_score == pytest.approx(6.0 + 0.30 * 4.0)
    assert agg.coverage == pytest.approx(1.0) and agg.confidence == pytest.approx(1.0) and agg.ranked


def test_missing_categories_renormalize_and_lower_confidence():
    scores = {k: (8.0, 0.9) for k in meth.CATEGORY_KEYS}
    scores["compute_capacity"] = (None, 0.0)
    agg = aggregate(scores)
    assert agg.index_score == pytest.approx(8.0)       # not dragged toward a default
    assert agg.coverage == pytest.approx(0.80)
    assert agg.confidence == pytest.approx(0.9 * 0.80)
    assert agg.ranked


def test_llm_overall_is_ignored():
    scores = {k: (5.0, 1.0) for k in meth.CATEGORY_KEYS}
    scores["overall"] = (10.0, 1.0)
    assert aggregate(scores).index_score == pytest.approx(5.0)


def test_insufficient_coverage_not_ranked():
    agg = aggregate({"energy_throughput": (9.0, 0.9), "frontier_acceleration": (9.0, 0.8)})
    assert agg.coverage == pytest.approx(0.45) and not agg.ranked and "insufficient data" in agg.reason


def test_mostly_judged_entities_are_not_ranked():
    # all opinion + growth = 0.50 coverage; even with a lower coverage bar, measured share is too small
    scores = {k: (9.0, 0.8) for k in meth.JUDGED_KEYS}
    scores["growth"] = (9.0, 0.9)
    agg = aggregate(scores, min_coverage=0.5, min_measured=0.3)
    assert not agg.ranked and "measured" in agg.reason


def test_nothing_scored():
    agg = aggregate({k: (None, 0.0) for k in meth.CATEGORY_KEYS})
    assert agg.index_score is None and agg.coverage == 0 and not agg.ranked
