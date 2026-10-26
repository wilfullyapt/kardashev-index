"""JSON shapes for /api/v1 — same facts the public HTML shows (published current runs)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import publication, runs as runs_svc
from ..eligibility import rank_status
from ..models import CategoryScore, Company, JudgmentRun
from ..pipeline import methodology as meth
from ..version import __version__


def _iso(dt):
    return dt.isoformat() if dt else None


def run_public(run: JudgmentRun | None) -> dict | None:
    if run is None:
        return None
    ranked, reason = rank_status(run)
    return {
        "run_id": run.id,
        "status": run.status,
        "finished_at": _iso(run.finished_at),
        "index_score": run.index_score,
        "measured_score": run.measured_score,
        "judged_score": run.judged_score,
        "k_equivalent": run.k_equivalent,
        "avg_power_w": run.avg_power_w,
        "confidence": run.confidence,
        "coverage": run.coverage,
        "ranked": ranked,
        "rank_reason": reason,
        "pipeline_version": run.pipeline_version,
        "prompt_version": run.prompt_version,
        "rubric_version": run.rubric_version,
        "weights_version": run.weights_version,
        "code_version": run.code_version,
        "model": run.model_returned or run.model,
        "retracted_at": _iso(run.retracted_at),
    }


def categories_public(db: Session, run: JudgmentRun) -> list[dict]:
    rows = db.query(CategoryScore).filter(CategoryScore.run_id == run.id).all()
    order = {k: i for i, k in enumerate(meth.CATEGORY_KEYS)}
    out = []
    for c in rows:
        meta = meth.BY_KEY.get(c.category, {})
        out.append({
            "category": c.category,
            "label": meta.get("label", c.category),
            "family": c.family,
            "score": c.score,
            "confidence": c.confidence,
            "weight": c.weight,
            "insufficient": c.insufficient,
            "rationale": c.rationale,
        })
    out.sort(key=lambda x: order.get(x["category"], 99))
    return out


def company_brief(company: Company, run: JudgmentRun | None) -> dict:
    return {
        "id": company.id,
        "canonical_name": company.canonical_name,
        "official_name": company.official_name,
        "domain": company.domain,
        "ticker": company.ticker,
        "industry": company.industry,
        "hq": company.hq,
        "current_run": run_public(run),
    }


def leaderboard_payload(db: Session, *, limit: int = 100, q: str | None = None,
                        include_unranked: bool = True) -> dict:
    ranked, awaiting = runs_svc.leaderboard(db, limit=limit, q=q)
    ranked_out = []
    for i, e in enumerate(ranked, start=1):
        row = company_brief(e["company"], e["run"])
        row["rank"] = i
        ranked_out.append(row)
    awaiting_out = []
    if include_unranked:
        for e in awaiting:
            row = company_brief(e["company"], e["run"])
            row["reason"] = e.get("reason")
            awaiting_out.append(row)
    return {
        "ranked": ranked_out,
        "unranked": awaiting_out,
        "count_ranked": len(ranked_out),
        "count_unranked": len(awaiting_out),
    }


def company_payload(db: Session, company: Company) -> dict | None:
    run = publication.current_run(db, company)
    body = company_brief(company, run)
    if run:
        body["categories"] = categories_public(db, run)
        summary = run.summary or {}
        body["headline"] = summary.get("headline")
        body["synthesis"] = summary.get("synthesis")
    else:
        body["categories"] = []
        body["headline"] = None
        body["synthesis"] = None
    return body


def history_payload(db: Session, company: Company) -> dict:
    hist = runs_svc.history(db, company)
    runs = []
    for e in hist.get("runs") or []:
        r = e["run"]
        runs.append({
            **run_public(r),
            "label": e.get("label"),
            "ordinal": e.get("ordinal"),
            "is_current": bool(e.get("is_current")),
        })
    return {"company_id": company.id, "canonical_name": company.canonical_name, "runs": runs}


def meta_payload() -> dict:
    from ..pipeline import prompts
    return {
        "service": "kardashev-index",
        "version": __version__,
        "pipeline_version": meth.PIPELINE_VERSION,
        "prompt_version": prompts.PROMPT_VERSION,
        "weights_version": meth.WEIGHTS_VERSION,
        "rubric_version": meth.RUBRIC_VERSION,
        "license": "Commercial API license (separate from CC BY-NC site scraping)",
        "docs": "/docs/API.md#public-paid-api-v1",
        "contact": "contact@kardashevindex.com",
    }
