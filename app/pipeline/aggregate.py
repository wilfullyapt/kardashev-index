"""Weighted aggregation, computed only (no LLM 'overall').
- coverage   = sum of weights of categories that have a score
- index      = sum(w_i * s_i) / coverage            (renormalized over scored categories)
- confidence = sum(w_i * conf_i) over scored ones   (missing data lowers confidence, never adds a 5.0)
- ranked     = coverage >= RANK_MIN_COVERAGE and measured coverage >= RANK_MIN_MEASURED"""
from __future__ import annotations

from dataclasses import dataclass

from .methodology import MEASURED_KEYS, WEIGHTS


@dataclass
class Aggregate:
    index_score: float | None
    measured_score: float | None
    judged_score: float | None
    coverage: float
    measured_coverage: float
    confidence: float
    ranked: bool
    reason: str | None


def _wmean(items: list[tuple[float, float]]) -> float | None:
    w = sum(wi for wi, _ in items)
    return round(sum(wi * si for wi, si in items) / w, 2) if w > 0 else None


def aggregate(scores: dict[str, tuple[float | None, float]], *, min_coverage: float = 0.6,
              min_measured: float = 0.3) -> Aggregate:
    """scores: category -> (score or None, confidence)."""
    present = {k: v for k, v in scores.items() if k in WEIGHTS and v[0] is not None}
    coverage = round(sum(WEIGHTS[k] for k in present), 4)
    measured_cov = round(sum(WEIGHTS[k] for k in present if k in MEASURED_KEYS), 4)
    index = _wmean([(WEIGHTS[k], s) for k, (s, _) in present.items()])
    measured = _wmean([(WEIGHTS[k], s) for k, (s, _) in present.items() if k in MEASURED_KEYS])
    judged = _wmean([(WEIGHTS[k], s) for k, (s, _) in present.items() if k not in MEASURED_KEYS])
    confidence = round(sum(WEIGHTS[k] * max(0.0, min(1.0, c)) for k, (_, c) in present.items()), 3)
    reason = None
    if coverage < min_coverage:
        reason = f"insufficient data: {coverage:.0%} of weight scored (needs {min_coverage:.0%})"
    elif measured_cov < min_measured:
        reason = f"insufficient measured data: {measured_cov:.0%} of weight measured (needs {min_measured:.0%})"
    return Aggregate(index, measured, judged, coverage, measured_cov, confidence, reason is None, reason)
