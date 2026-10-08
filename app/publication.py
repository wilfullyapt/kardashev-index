"""Which successful run is a company's public (current) one.

A run below the ranking thresholds ("insufficient data") is still recorded and shown to admins,
but it never replaces better data:
  * a ranked run always publishes;
  * an unranked run never replaces a ranked one;
  * an unranked run replaces an unranked current run only if it scored at least as much weight;
  * an unranked run never replaces real legacy v0 scores (the page keeps the legacy view);
  * otherwise (nothing better exists) it publishes, so the page shows what was verified.
The same rule is replayed over history by migration 0004 so existing runs follow it too."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .judgment import PLACEHOLDER_MODEL
from .models import Score

WITHHELD_LEGACY = "kept the legacy v0 scores public: this run was below the ranking thresholds"


def decide(ranked: bool, coverage: float | None, prev, has_legacy: bool) -> tuple[bool, str | None]:
    """(publish?, note). ``prev`` is the current published run (or None)."""
    if ranked:
        return True, None
    if prev is not None:
        if prev.ranked:
            return False, f"kept run {prev.id} published: it was ranked and this run was not"
        if (prev.coverage or 0) > (coverage or 0):
            return False, (f"kept run {prev.id} published: it scored more of the weight "
                           f"({prev.coverage or 0:.0%} vs {coverage or 0:.0%})")
        return True, None
    if has_legacy:
        return False, WITHHELD_LEGACY
    return True, None


def has_legacy_scores(db: Session, company_id: int) -> bool:
    """Real (non-placeholder) v0 scores exist for this company."""
    return db.query(Score.id).filter(
        Score.company_id == company_id,
        (Score.model.is_(None)) | (Score.model != PLACEHOLDER_MODEL),
        (Score.justification.is_(None)) | (~Score.justification.like("Placeholder%")),
    ).first() is not None
