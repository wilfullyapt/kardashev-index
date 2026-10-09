"""Effective ranking eligibility of a stored run, recomputed at display time.

``JudgmentRun.ranked`` is what the run decided when it finished (coverage rule, and from
pipeline-v2.3 the quality gate too). Rule changes must not rewrite history, so the stored value is
never updated. Public views use ``rank_status(run)`` instead: the stored coverage verdict AND the
*current* quality gate (measured share of scored weight > RANK_MIN_MEASURED_SHARE, confidence >
RANK_MIN_CONFIDENCE), recomputed from the run's stored components (coverage, confidence and
measured coverage). No migration, no paid re-run.
"""
from __future__ import annotations

from .disclosure import measured_share as disclosure_share
from .pipeline.aggregate import measured_share, share_reason
from .pipeline.config import settings


def stored_measured_share(run) -> float | None:
    """Measured share of the scored weight (0-1) from the run's stored components.

    Prefers ``summary.measured_coverage / coverage`` (written by aggregate for every v2 run); falls back
    to the run's category scores, the same computation as the public "measured share" label.
    """
    summary = run.summary if isinstance(run.summary, dict) else {}
    mc = summary.get("measured_coverage")
    if mc is not None and run.coverage:
        return measured_share(run.coverage, float(mc))
    try:
        rows = list(run.category_scores or [])
    except Exception:  # detached instance without loaded scores
        return None
    return disclosure_share(rows)


def rank_status(run) -> tuple[bool, str | None]:
    """(ranked now?, reason if not)."""
    if run is None:
        return False, None
    if not run.ranked:
        return False, (run.summary or {}).get("not_ranked_reason") or "insufficient data"
    s = settings()
    share = stored_measured_share(run)
    if share is None:
        return False, "ranking components unavailable for this run"
    reason = share_reason(share, run.confidence, min_measured_share=s.rank_min_measured_share,
                          min_confidence=s.rank_min_confidence)
    return reason is None, reason
