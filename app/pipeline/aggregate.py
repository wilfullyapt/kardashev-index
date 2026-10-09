"""Weighted aggregation, computed only (no LLM 'overall').
- coverage   = sum of weights of categories that have a score
- index      = sum(w_i * s_i) / coverage            (renormalized over scored categories)
- confidence = sum(w_i * conf_i) over scored ones   (missing data lowers confidence, never adds a 5.0)
- ranked     = coverage >= RANK_MIN_COVERAGE and measured coverage >= RANK_MIN_MEASURED
- energy undisclosed: many companies (most non-hyperscalers) publish no energy figure at all. When
  no verified energy figure exists after a run that did read sources, coverage is measured over the
  remaining 70% of the weight: ranked if that adjusted coverage >= RANK_MIN_COVERAGE and at least one
  other measured category (compute or growth) is scored. The run carries an "energy undisclosed"
  flag and its confidence is multiplied by 0.8. Missing data is still never filled in.
- quality gate (pipeline-v2.3), applied after either coverage rule passes: ranked only if the
  measured share of the *scored* weight (measured_coverage / coverage) is > RANK_MIN_MEASURED_SHARE
  (default 40%) AND confidence (0-1, after the x0.8 undisclosed-energy reduction) is
  > RANK_MIN_CONFIDENCE (default 0.20, i.e. 20% as displayed). See ``quality_reason``."""
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


def measured_share(coverage: float | None, measured_coverage: float | None) -> float:
    """Measured share of the scored weight (0-1)."""
    return round((measured_coverage or 0) / coverage, 4) if coverage else 0.0


def quality_reason(coverage: float | None, measured_coverage: float | None, confidence: float | None, *,
                   min_measured_share: float | None, min_confidence: float | None) -> str | None:
    """None if the run passes the quality gate, else the not-ranked reason. Strict ">" on both."""
    return share_reason(measured_share(coverage, measured_coverage), confidence,
                        min_measured_share=min_measured_share, min_confidence=min_confidence)


def share_reason(share: float, confidence: float | None, *,
                 min_measured_share: float | None, min_confidence: float | None) -> str | None:
    """The quality gate given the measured share of scored weight (0-1) and confidence (0-1)."""
    share = round(share or 0.0, 4)
    if min_measured_share is not None and not share > min_measured_share:
        return (f"too little measured data: {share:.0%} of the scored weight is measured "
                f"(needs more than {min_measured_share:.0%})")
    conf = confidence or 0.0
    if min_confidence is not None and not round(conf, 4) > min_confidence:
        return f"low confidence: {conf:.0%} (needs more than {min_confidence:.0%})"
    return None


def aggregate(scores: dict[str, tuple[float | None, float]], *, min_coverage: float = 0.6,
              min_measured: float = 0.3, energy_undisclosed: bool = False,
              min_measured_share: float | None = None, min_confidence: float | None = None) -> Aggregate:
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
            conf = round(confidence * UNDISCLOSED_CONFIDENCE, 3)
            q = quality_reason(coverage, measured_cov, conf, min_measured_share=min_measured_share,
                               min_confidence=min_confidence)
            return Aggregate(index, measured, judged, coverage, measured_cov, conf, q is None, q,
                             "energy_undisclosed", adj)
        reason = (f"insufficient data: energy undisclosed and {adj:.0%} of the remaining weight scored "
                  f"(needs {min_coverage:.0%}, including compute or growth)")
        return Aggregate(index, measured, judged, coverage, measured_cov, confidence, False, reason,
                         "energy_undisclosed", adj)
    if reason is None:
        reason = quality_reason(coverage, measured_cov, confidence, min_measured_share=min_measured_share,
                                min_confidence=min_confidence)
    return Aggregate(index, measured, judged, coverage, measured_cov, confidence, reason is None, reason)
