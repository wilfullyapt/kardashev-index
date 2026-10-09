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
        if getattr(prev, "is_ranked", prev.ranked):
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


# ---------------------------------------------------------------- current run, computed at read time
#
# Two rules on top of ``decide`` (0.10.0):
#  * Retraction: an admin can retract a published run that is known to be wrong. A retracted run is
#    never current or listed publicly (a dated correction note with the reason is shown instead).
#    Runs after a retraction are re-decided without it; if nothing qualifies, the most recent
#    non-retracted completed run becomes current, even if it is unranked (shown as Unranked).
#  * Newer pipeline wins: a completed run on a newer pipeline version than the current run replaces
#    it even when it is below the ranking thresholds (shown as Unranked with its reason) — the old
#    run was measured with rules since found wrong. Only a *clean* run qualifies: not degraded
#    (budget cap, judge fallback, ...) and not "energy source couldn't be read", so a transient
#    failure on the new version never pushes out a complete older run. Failed runs never replace
#    anything (only succeeded runs are considered at all). Same-version runs keep ``decide``.
# Stored ``published`` flags are respected for history before any retraction, so existing pages
# change only where one of these two rules applies.

def pipeline_key(version: str | None) -> tuple[int, ...]:
    """'pipeline-v2.10' -> (2, 10); unknown/legacy -> () (older than everything)."""
    import re
    m = re.search(r"v(\d+(?:\.\d+)*)", version or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def can_supersede(run, cur, *, degraded=None, basis: str | None = None) -> bool:
    """A clean run on a newer pipeline version than ``cur`` replaces it regardless of ranking."""
    if cur is None or pipeline_key(run.pipeline_version) <= pipeline_key(cur.pipeline_version):
        return False
    summary = getattr(run, "summary", None) or {}
    degraded = run.degraded if degraded is None else degraded
    basis = summary.get("rank_basis") if basis is None else basis
    if degraded or basis == "energy_unreadable" or summary.get("energy_unreadable"):
        return False
    return getattr(run, "status", "succeeded") in ("succeeded", "running")


SUPERSEDED_NOTE = "replaced run {prev}: newer pipeline ({new} > {old})"


class Replay:
    __slots__ = ("current", "notes", "public", "retracted")

    def __init__(self):
        self.current = None
        self.public: list = []        # runs shown publicly, oldest first
        self.retracted: list = []     # retracted runs, oldest first
        self.notes: dict[int, str] = {}


def replay(runs: list, has_legacy) -> Replay:
    """``runs``: a company's succeeded runs, oldest first. ``has_legacy``: bool or zero-arg callable
    (only evaluated when a run after a retraction is re-decided)."""
    out = Replay()
    retraction_seen = False
    for r in runs:
        if getattr(r, "retracted_at", None):
            out.retracted.append(r)
            retraction_seen = True
            continue
        cur = out.current
        if retraction_seen:
            legacy = has_legacy() if callable(has_legacy) else bool(has_legacy)
            pub = decide(bool(r.ranked), r.coverage, cur, legacy)[0]
        else:
            pub = bool(r.published)
        if not pub and can_supersede(r, cur):
            pub = True
            out.notes[r.id] = SUPERSEDED_NOTE.format(prev=cur.id, new=r.pipeline_version, old=cur.pipeline_version)
        if pub:
            out.current = r
            out.public.append(r)
    if out.current is None and retraction_seen:
        rest = [r for r in runs if not getattr(r, "retracted_at", None)]
        if rest:
            out.current = rest[-1]
            out.public.append(rest[-1])
            out.notes[rest[-1].id] = "current after a retraction: the most recent remaining run"
    return out


def replay_companies(db: Session, company_ids: list[int]) -> dict[int, Replay]:
    """Replay for many companies with one query (leaderboard, sitemap, worker sweep)."""
    from .models import JudgmentRun
    by_company: dict[int, list] = {cid: [] for cid in company_ids}
    if company_ids:
        q = (db.query(JudgmentRun)
             .filter(JudgmentRun.company_id.in_(company_ids), JudgmentRun.status == "succeeded")
             .order_by(JudgmentRun.finished_at, JudgmentRun.id))
        for r in q:
            by_company[r.company_id].append(r)
    return {cid: replay(rs, lambda cid=cid: has_legacy_scores(db, cid)) for cid, rs in by_company.items()}


def current_run(db: Session, company):
    return replay_companies(db, [company.id])[company.id].current


def sync_pointer(db: Session, company) -> None:
    """Keep ``companies.current_run_id`` (a cache; readers use the replay) in step after a change."""
    cur = current_run(db, company)
    company.current_run_id = cur.id if cur else None


RETRACT_REASON_MIN, RETRACT_REASON_MAX = 10, 500


class RetractError(ValueError):
    pass


def retract(db: Session, run, by: str, reason: str, now=None) -> None:
    """Retract a completed run (audit-logged). The company's current run is recomputed."""
    from datetime import UTC, datetime

    from .models import Company, IngestLog
    reason = " ".join((reason or "").split())
    if run.status != "succeeded":
        raise RetractError("only a completed run can be retracted")
    if run.retracted_at:
        raise RetractError(f"run {run.id} is already retracted")
    if len(reason) < RETRACT_REASON_MIN:
        raise RetractError(f"a reason of at least {RETRACT_REASON_MIN} characters is required (it is shown publicly)")
    company = db.get(Company, run.company_id)
    before = current_run(db, company)
    run.retracted_at = now or datetime.now(UTC)
    run.retracted_by = by
    run.retraction_reason = reason[:RETRACT_REASON_MAX]
    db.flush()
    sync_pointer(db, company)
    db.add(IngestLog(company_id=company.id, action="run_retracted", admin_id=by,
                     details={"run_id": run.id, "reason": run.retraction_reason,
                              "retracted_at": run.retracted_at.isoformat(),
                              "current_before": before.id if before else None,
                              "current_after": company.current_run_id}))
