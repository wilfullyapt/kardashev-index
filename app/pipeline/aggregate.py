"""Weighted aggregation, computed only (no LLM 'overall').
- coverage   = sum of weights of categories that have a score
- index      = sum(w_i * s_i) / coverage            (renormalized over scored categories)
- confidence = sum(w_i * conf_i) over scored ones   (missing data lowers confidence, never adds a 5.0)
- ranked     = coverage >= RANK_MIN_COVERAGE and measured coverage >= RANK_MIN_MEASURED
- energy undisclosed: many companies (most non-hyperscalers) publish no energy figure at all. When
  no verified energy figure exists after a run that did read sources, coverage is measured over the
  remaining 70% of the weight: ranked if that adjusted coverage >= RANK_MIN_COVERAGE and at least one
  other measured category (compute or growth) is scored. The run carries an "energy undisclosed"
  flag and its confidence is multiplied by 0.8. Missing data is still never filled in."""
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
    basis: str = "standard"              # standard | energy_undisclosed
    adjusted_coverage: float | None = None


def _wmean(items: list[tuple[float, float]]) -> float | None:
    w = sum(wi for wi, _ in items)
    return round(sum(wi * si for wi, si in items) / w, 2) if w > 0 else None


ENERGY_KEY = "energy_throughput"
UNDISCLOSED_CONFIDENCE = 0.8


def aggregate(scores: dict[str, tuple[float | None, float]], *, min_coverage: float = 0.6,
              min_measured: float = 0.3, energy_undisclosed: bool = False) -> Aggregate:
    """scores: category -> (score or None, confidence). ``energy_undisclosed``: the run found no
    energy figure although it read sources (see module docstring)."""
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
    if reason and energy_undisclosed and ENERGY_KEY not in present:
        remaining = 1.0 - WEIGHTS[ENERGY_KEY]
        adj = round(coverage / remaining, 4)
        min_other = min(WEIGHTS[k] for k in MEASURED_KEYS if k != ENERGY_KEY)
        if adj >= min_coverage and measured_cov >= min_other - 1e-9:
            return Aggregate(index, measured, judged, coverage, measured_cov,
                             round(confidence * UNDISCLOSED_CONFIDENCE, 3), True, None,
                             "energy_undisclosed", adj)
        reason = (f"insufficient data: energy undisclosed and {adj:.0%} of the remaining weight scored "
                  f"(needs {min_coverage:.0%}, including compute or growth)")
        return Aggregate(index, measured, judged, coverage, measured_cov, confidence, False, reason,
                         "energy_undisclosed", adj)
    return Aggregate(index, measured, judged, coverage, measured_cov, confidence, reason is None, reason)
