"""Legacy v0 scores (the original six-category LLM judgment), read side only.

New judgments are produced by the v2 pipeline (``app.pipeline``) and stored as judgment runs.
This module only summarizes the old ``scores`` rows so existing entries keep displaying,
clearly labelled as the superseded v0 method, until a v2 run is published for them.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .models import CATEGORIES

JUDGE_SYNTHESIS_CATEGORY = "overall"
DIMENSIONS = [c for c in CATEGORIES if c != JUDGE_SYNTHESIS_CATEGORY]
PLACEHOLDER_MODEL = "stub"


def is_placeholder(score: Any) -> bool:
    return (getattr(score, "model", None) == PLACEHOLDER_MODEL
            or (getattr(score, "justification", None) or "").startswith("Placeholder"))


def _sort_key(score: Any):
    judged = getattr(score, "judged_at", None)
    ts = judged.timestamp() if judged else 0.0
    return (ts, getattr(score, "id", 0) or 0)


def latest_by_category(scores: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for s in sorted(scores, key=_sort_key):
        out[s.category] = s  # later judged_at / higher id wins
    return out


def summarize_scores(scores: Iterable[Any]) -> dict[str, Any]:
    """Collapse raw Score rows into what the site shows.

    - placeholder rows are ignored for ranking;
    - duplicates collapse to the latest row per category;
    - Overall = mean of the five real dimensions only (the judge's own "overall" is excluded);
    - an entity is ranked only when all five dimensions have real scores.
    """
    scores = list(scores)
    real = latest_by_category(s for s in scores if not is_placeholder(s))
    if all(d in real for d in DIMENSIONS):
        overall = round(sum(float(real[d].score) for d in DIMENSIONS) / len(DIMENSIONS), 1)
        return {"status": "ranked", "scores": real, "overall": overall}
    shown = real or latest_by_category(scores)
    return {"status": "awaiting", "scores": shown, "overall": None}
