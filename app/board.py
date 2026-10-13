"""Admin board (1.8.0): every company as a card, ranked first (by Index), then the unranked ones in
four groups derived from the company's current run and latest run. Read-only; no scoring or
publication rule is computed here that the public pages don't already use.

Group precedence (the first rule that applies wins; documented in docs/API.md):
  1. ``energy``     Energy couldn't be read: the current run's basis is ``energy_unreadable``.
  2. ``degraded``   Degraded / failed: the latest run failed (errors, timeouts, budget, deferred and
                    out of attempts), or the current run is degraded (budget cap, empty extraction,
                    judge fallback...), or there is no current run and the latest one failed.
  3. ``confidence`` Confidence below the ranking floor (current run confidence <= RANK_MIN_CONFIDENCE).
  4. ``coverage``   Low coverage / not enough measured: everything else that isn't ranked, including
                    companies still awaiting their first completed run.
Energy comes first because it has a specific fix (the energy retry); a degraded/failed run is next
because its numbers are not trustworthy enough to read the confidence or coverage reason from.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import publication
from .models import ACTIVE_RUN_STATUSES, Company, JudgmentRun
from .pipeline.config import settings

GROUPS = (
    ("energy", "Energy couldn’t be read"),
    ("degraded", "Degraded or failed"),
    ("confidence", "Confidence too low"),
    ("coverage", "Low coverage / not enough measured"),
)
DEFAULT_COST_TEXT = "typically $0.15–0.30"


def classify(current: JudgmentRun | None, latest: JudgmentRun | None, min_confidence: float) -> str:
    summary = (current.summary or {}) if current is not None else {}
    if current is not None and (summary.get("rank_basis") == "energy_unreadable" or summary.get("energy_unreadable")):
        return "energy"
    latest_failed = latest is not None and latest.status == "failed" and (
        current is None or latest.id > current.id)
    if latest_failed or (current is not None and current.degraded):
        return "degraded"
    if current is not None and current.confidence is not None and current.confidence <= min_confidence:
        return "confidence"
    return "coverage"


def card_reason(group: str, current: JudgmentRun | None, latest: JudgmentRun | None) -> str:
    if group == "degraded":
        if latest is not None and latest.status == "failed" and (current is None or latest.id > current.id):
            return f"latest run failed: {latest.error_type or 'error'}" + (
                f" — {latest.error_message[:160]}" if latest.error_message else "")
        if current is not None and current.degraded:
            return "degraded: " + "; ".join(current.degraded)[:200]
    if current is None:
        if latest is not None and latest.status in ACTIVE_RUN_STATUSES:
            return f"measurement run {latest.status}"
        return "awaiting a measured run"
    return current.rank_reason or "insufficient data"


def cost_estimate(db: Session, n: int = 10) -> str:
    """Re-run confirmation text: mean recorded cost of the last ``n`` completed runs, with the cap."""
    cap = settings().max_cost_usd
    costs = [c for (c,) in db.query(JudgmentRun.cost_usd).filter(JudgmentRun.status == "succeeded",
                                                                  JudgmentRun.cost_usd > 0)
             .order_by(JudgmentRun.id.desc()).limit(n)]
    if not costs:
        return f"{DEFAULT_COST_TEXT}, capped at ${cap:.2f}"
    return f"about ${sum(costs) / len(costs):.2f} (average of the last {len(costs)} runs), capped at ${cap:.2f}"


def build(db: Session) -> dict:
    s = settings()
    companies = db.query(Company).order_by(Company.canonical_name).all()
    ids = [c.id for c in companies]
    latest: dict[int, JudgmentRun] = {}
    active: dict[int, JudgmentRun] = {}
    retracted: dict[int, JudgmentRun] = {}
    runs_count: dict[int, int] = defaultdict(int)
    if ids:
        for r in db.query(JudgmentRun).filter(JudgmentRun.company_id.in_(ids)).order_by(JudgmentRun.id):
            runs_count[r.company_id] += 1
            if r.status in ACTIVE_RUN_STATUSES:
                active[r.company_id] = r
            else:
                latest[r.company_id] = r
            if r.retracted_at:
                retracted[r.company_id] = r
    rp = publication.replay_companies(db, ids)
    ranked, groups = [], {k: [] for k, _ in GROUPS}
    for c in companies:
        cur = rp[c.id].current
        last = latest.get(c.id)
        card = {"company": c, "current": cur, "last": last, "active": active.get(c.id),
                "retracted": retracted.get(c.id), "runs": runs_count.get(c.id, 0),
                "detail_run": cur or last}
        if cur is not None and cur.is_ranked:
            card["group"] = "ranked"
            ranked.append(card)
            continue
        g = classify(cur, last, s.rank_min_confidence)
        card["group"], card["reason"] = g, card_reason(g, cur, last)
        groups[g].append(card)
    ranked.sort(key=lambda x: (-(x["current"].index_score or 0), x["company"].canonical_name or ""))
    return {"ranked": ranked, "groups": [(k, label, groups[k]) for k, label in GROUPS],
            "unranked_count": sum(len(v) for v in groups.values()), "cost_estimate": cost_estimate(db),
            "total_runs": db.query(func.count(JudgmentRun.id)).scalar() or 0}


# ---------------------------------------------------------------- top of page (1.9.0)

PT = __import__("zoneinfo").ZoneInfo("America/Los_Angeles")


def _pt_bounds(now):
    """Start of today and of this month in Pacific time, as aware datetimes."""
    from datetime import datetime
    local = now.astimezone(PT)
    day = datetime(local.year, local.month, local.day, tzinfo=PT)
    month = datetime(local.year, local.month, 1, tzinfo=PT)
    from datetime import UTC
    return day.astimezone(UTC), month.astimezone(UTC)  # UTC: SQLite compares naive strings


def queue(db: Session) -> dict:
    """Running, queued (ready now) and scheduled automatic retries, oldest first."""
    rows = (db.query(JudgmentRun, Company).join(Company, Company.id == JudgmentRun.company_id)
            .filter(JudgmentRun.status.in_(ACTIVE_RUN_STATUSES)).order_by(JudgmentRun.id).all())
    out = {"running": [], "queued": [], "scheduled": []}
    for r, c in rows:
        key = ("running" if r.status == "running"
               else "scheduled" if r.next_attempt_at is not None else "queued")
        out[key].append({"run": r, "company": c})
    out["scheduled"].sort(key=lambda x: x["run"].next_attempt_at)
    out["active"] = bool(out["running"] or out["queued"] or out["scheduled"])
    out["total"] = sum(len(out[k]) for k in ("running", "queued", "scheduled"))
    return out


def spend(db: Session, now=None) -> dict:
    """Recorded run costs (JudgmentRun.cost_usd) for today and this month, Pacific time. Runs with no
    recorded cost are counted separately so the total is never presented as complete when it isn't."""
    from datetime import UTC, datetime
    now = now or datetime.now(UTC)
    day, month = _pt_bounds(now)
    out = {}
    for key, since in (("today", day), ("month", month)):
        q = db.query(JudgmentRun.cost_usd).filter(func.coalesce(JudgmentRun.finished_at, JudgmentRun.queued_at) >= since)
        costs = [c for (c,) in q]
        known = [c for c in costs if c is not None]
        out[key] = {"usd": round(sum(known), 4) if known else None, "runs": len(costs),
                    "unknown": len(costs) - len(known)}
    return out


def health(obs: dict) -> dict:
    """The /health observability block, reshaped for the board (no extra queries)."""
    if not obs or "error" in obs:
        return {"error": (obs or {}).get("error", "unavailable")}
    runs = obs.get("runs_7d", {})
    failures = sorted(((k.split(":", 1)[1], v) for k, v in runs.items() if k.startswith("failed:")),
                      key=lambda x: -x[1])
    return {"window_days": obs.get("window_days"), "succeeded": runs.get("succeeded", 0),
            "degraded": runs.get("degraded", 0), "energy_unreadable": runs.get("energy_unreadable", 0),
            "withheld": runs.get("withheld", 0), "failures": failures,
            "failed_total": sum(v for _k, v in failures), "hosts": obs.get("hosts_7d", [])[:5],
            "archive": obs.get("archive", {}), "stale": obs.get("retry_claims_without_queued_run", []),
            "computed_at": obs.get("computed_at")}
