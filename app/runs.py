"""Run queueing and read models (leaderboard rows, company dossier, admin/run JSON)."""
from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .judgment import summarize_scores
from .models import (ACTIVE_RUN_STATUSES, CategoryScore, Company, Evidence, JudgmentRun, JudgmentStage, Metric,
                     Score, Source)
from .pipeline import methodology as meth
from .pipeline.measures import METRIC_LABELS, fmt_power


def active_run(db: Session, company_id: int) -> JudgmentRun | None:
    return (db.query(JudgmentRun)
            .filter(JudgmentRun.company_id == company_id, JudgmentRun.status.in_(ACTIVE_RUN_STATUSES))
            .order_by(JudgmentRun.id.desc()).first())


def enqueue_run(db: Session, company_id: int, *, trigger: str, triggered_by: str,
                suggestion_id: int | None = None) -> tuple[JudgmentRun, bool]:
    """Returns (run, created). At most one active run per company (unique partial index)."""
    existing = active_run(db, company_id)
    if existing:
        return existing, False
    run = JudgmentRun(company_id=company_id, status="queued", trigger=trigger, triggered_by=triggered_by,
                      suggestion_id=suggestion_id, attempt=0)
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = active_run(db, company_id)
        if existing:
            return existing, False
        raise
    db.refresh(run)
    return run, True


# ---------------- serialization ----------------

def _iso(dt):
    return dt.isoformat() if dt else None


def stage_json(s: JudgmentStage) -> dict:
    return {"stage": s.stage, "seq": s.seq, "attempt": s.attempt, "status": s.status,
            "started_at": _iso(s.started_at), "finished_at": _iso(s.finished_at), "duration_ms": s.duration_ms,
            "model": s.model, "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
            "reasoning_tokens": s.reasoning_tokens, "tool_calls": s.tool_calls, "cost_usd": s.cost_usd,
            "error": s.error, "detail": s.detail}


def run_json(db: Session, run: JudgmentRun, *, stages: bool = True, scores: bool = True) -> dict:
    out = {
        "run_id": run.id, "company_id": run.company_id, "status": run.status, "trigger": run.trigger,
        "triggered_by": run.triggered_by, "attempt": run.attempt, "current_stage": run.current_stage,
        "queued_at": _iso(run.queued_at), "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "duration_ms": run.duration_ms, "model": run.model, "model_returned": run.model_returned,
        "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
        "reasoning_tokens": run.reasoning_tokens, "tool_calls": run.tool_calls, "cost_usd": run.cost_usd,
        "budget_usd": run.budget_usd, "index_score": run.index_score, "measured_score": run.measured_score,
        "judged_score": run.judged_score, "k_equivalent": run.k_equivalent, "avg_power_w": run.avg_power_w,
        "confidence": run.confidence, "coverage": run.coverage, "ranked": run.ranked, "published": run.published,
        "error_type": run.error_type, "error": run.error_message, "pipeline_version": run.pipeline_version,
        "prompt_version": run.prompt_version, "rubric_version": run.rubric_version,
        "weights_version": run.weights_version, "prompt_hash": run.prompt_hash, "summary": run.summary,
    }
    if stages:
        out["stages"] = [stage_json(s) for s in
                         db.query(JudgmentStage).filter(JudgmentStage.run_id == run.id).order_by(JudgmentStage.id)]
    if scores:
        out["categories"] = [{"category": c.category, "family": c.family, "score": c.score,
                              "confidence": c.confidence, "weight": c.weight, "rationale": c.rationale,
                              "evidence_ids": c.evidence_ids, "insufficient": c.insufficient}
                             for c in db.query(CategoryScore).filter(CategoryScore.run_id == run.id)]
        counts: dict[str, int] = defaultdict(int)
        for (status,) in db.query(Source.status).filter(Source.run_id == run.id):
            counts[status or "unknown"] += 1
        out["sources"] = dict(counts)
    return out


# ---------------- leaderboard ----------------

def leaderboard(db: Session, limit: int = 100, q: str | None = None) -> tuple[list[dict], list[dict]]:
    """(ranked, awaiting). Ranked = published v2 run that met the coverage rules, by index score."""
    query = db.query(Company)
    if q:
        query = query.filter(Company.canonical_name.ilike(f"%{q}%") | Company.industry.ilike(f"%{q}%")
                             | Company.official_name.ilike(f"%{q}%"))
    companies = query.all()
    run_ids = [c.current_run_id for c in companies if c.current_run_id]
    runs = {r.id: r for r in db.query(JudgmentRun).filter(JudgmentRun.id.in_(run_ids))} if run_ids else {}
    cats: dict[int, dict[str, CategoryScore]] = defaultdict(dict)
    if run_ids:
        for cs in db.query(CategoryScore).filter(CategoryScore.run_id.in_(run_ids)):
            cats[cs.run_id][cs.category] = cs
    ids = [c.id for c in companies]
    active = {}
    legacy: dict[int, list] = defaultdict(list)
    if ids:
        for r in db.query(JudgmentRun).filter(JudgmentRun.company_id.in_(ids),
                                              JudgmentRun.status.in_(ACTIVE_RUN_STATUSES)):
            active[r.company_id] = r
        for s in db.query(Score).filter(Score.company_id.in_(ids)):
            legacy[s.company_id].append(s)
    ranked, awaiting = [], []
    for c in companies:
        run = runs.get(c.current_run_id)
        entry = {"company": c, "run": run, "cats": cats.get(run.id, {}) if run else {},
                 "active": active.get(c.id), "legacy": summarize_scores(legacy.get(c.id, []))}
        if run and run.ranked:
            ranked.append(entry)
        else:
            if run:
                entry["reason"] = (run.summary or {}).get("not_ranked_reason") or "insufficient data"
            elif entry["active"]:
                entry["reason"] = f"measurement run {entry['active'].status}"
            else:
                entry["reason"] = "awaiting a measured run"
            awaiting.append(entry)
    ranked.sort(key=lambda e: (-(e["run"].index_score or 0), e["company"].canonical_name or ""))
    awaiting.sort(key=lambda e: e["company"].canonical_name or "")
    return ranked[:limit], awaiting[:limit]


# ---------------- company dossier ----------------

def _domain(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").removeprefix("www.")


def company_view(db: Session, company: Company) -> dict:
    run = db.get(JudgmentRun, company.current_run_id) if company.current_run_id else None
    view: dict = {"run": run, "active": active_run(db, company.id), "categories": [], "measured": [], "judged": [],
                  "figures": [], "sources": [], "stages": [], "headline": None, "previous": None}
    last_failed = (db.query(JudgmentRun).filter(JudgmentRun.company_id == company.id, JudgmentRun.status == "failed")
                   .order_by(JudgmentRun.id.desc()).first())
    view["last_failed"] = last_failed if (last_failed and (not run or last_failed.id > run.id)) else None
    if run is None:
        view["legacy"] = summarize_scores(db.query(Score).filter(Score.company_id == company.id).all())
        return view
    sources = {s.id: s for s in db.query(Source).filter(Source.run_id == run.id)}
    evid = {e.id: e for e in db.query(Evidence).filter(Evidence.run_id == run.id, Evidence.quote_verified.is_(True))}

    def ev_json(eid):
        e = evid.get(eid)
        if not e:
            return None
        s = sources.get(e.source_id)
        url = (e.context if e.kind == "filing" else None) or (s.final_url or s.url if s else None)
        return {"id": e.id, "kind": e.kind, "claim": e.claim, "quote": e.quote, "url": url,
                "domain": _domain(url), "title": s.title if s else None,
                "fetched_at": s.fetched_at if s else None, "period": e.period}

    for cs in db.query(CategoryScore).filter(CategoryScore.run_id == run.id):
        meta = meth.BY_KEY.get(cs.category, {"label": cs.category, "short": cs.category, "kicker": ""})
        item = {"key": cs.category, "meta": meta, "family": cs.family, "score": cs.score,
                "confidence": cs.confidence, "weight": cs.weight, "rationale": cs.rationale, "inputs": cs.inputs or {},
                "evidence": [x for x in (ev_json(i) for i in (cs.evidence_ids or [])) if x]}
        view["categories"].append(item)
    order = {k: i for i, k in enumerate(meth.CATEGORY_KEYS)}
    view["categories"].sort(key=lambda c: order.get(c["key"], 99))
    view["measured"] = [c for c in view["categories"] if c["family"] == meth.MEASURED]
    view["judged"] = [c for c in view["categories"] if c["family"] == meth.JUDGED]
    for m in db.query(Metric).filter(Metric.run_id == run.id).order_by(Metric.id):
        if m.metric_key in METRIC_LABELS:
            evj = ev_json((m.evidence_ids or [None])[0]) if m.evidence_ids else None
            view["figures"].append({"label": METRIC_LABELS[m.metric_key], "key": m.metric_key, "value": m.value,
                                    "unit": m.unit, "period": m.period, "method": m.method, "evidence": evj,
                                    "power": fmt_power(m.value_si) if m.unit_si == "W" and m.value_si else None})
    view["sources"] = sorted((s for s in sources.values() if s.status == "ok"),
                             key=lambda s: (not s.is_primary, s.id))
    view["rejected_sources"] = sum(1 for s in sources.values() if s.status != "ok")
    view["stages"] = db.query(JudgmentStage).filter(JudgmentStage.run_id == run.id).order_by(JudgmentStage.id).all()
    summary = run.summary or {}
    view["headline"] = summary.get("headline")
    view["synthesis"] = summary.get("synthesis")
    if summary.get("previous_index") is not None and run.index_score is not None:
        view["previous"] = {"run_id": summary.get("previous_run_id"),
                            "delta": round(run.index_score - summary["previous_index"], 2)}
    return view


def admin_runs(db: Session, limit: int = 15) -> list[dict]:
    runs = db.query(JudgmentRun).order_by(JudgmentRun.id.desc()).limit(limit).all()
    names = {c.id: c.canonical_name for c in db.query(Company).filter(Company.id.in_([r.company_id for r in runs]))}
    stages: dict[int, list] = defaultdict(list)
    if runs:
        for s in db.query(JudgmentStage).filter(JudgmentStage.run_id.in_([r.id for r in runs])).order_by(JudgmentStage.id):
            stages[s.run_id].append(s)
    return [{"run": r, "name": names.get(r.company_id, f"#{r.company_id}"), "stages": stages.get(r.id, [])}
            for r in runs]
