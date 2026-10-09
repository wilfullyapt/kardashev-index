"""End-to-end pipeline tests with a fake LLM and fake HTTP: queueing, every stage, publishing rules."""
import re
from datetime import UTC, datetime, timedelta

import pytest

from app import main
from app.models import (CategoryScore, Company, Evidence, IngestLog, JudgmentRun, JudgmentStage, Score, Source,
                        Suggestion)
from app.pipeline import methodology as meth
from app.pipeline.aggregate import aggregate
from app.pipeline.runner import STAGES
from app.worker import Worker
from app.db import SessionLocal
from tests.conftest import HERMES
from tests import fakes


@pytest.fixture
def use_deps(monkeypatch):
    """Install deps for the worker; returns a setter."""
    holder = {}

    def set_deps(deps):
        holder["deps"] = deps
        monkeypatch.setattr(main, "worker", Worker(SessionLocal, lambda: holder["deps"]))
        return main.worker
    return set_deps


def _company(db, name="nvidia", **kw):
    c = Company(canonical_name=name, industry="Unknown", **kw)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _suggest(db, name="NVIDIA", domain=None):
    s = Suggestion(name=name, domain=domain, status="pending")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s.id


def test_approve_returns_immediately_and_worker_runs_full_chain(client, db, use_deps):
    llm = fakes.world_llm()
    worker = use_deps(fakes.make_deps(llm))
    sid = _suggest(db, "NVIDIA", "nvidia.com")

    r = client.post(f"/internal/approve/{sid}", headers=HERMES)
    assert r.status_code == 202
    body = r.json()
    assert body["judgment"] == "queued" and body["run_url"] == f"/internal/runs/{body['run_id']}"
    assert llm.calls == []  # nothing ran inside the request

    run_id = worker.process_next()
    assert run_id == body["run_id"]
    db.expire_all()
    run = db.get(JudgmentRun, run_id)
    assert run.status == "succeeded", run.error_message
    assert run.published and run.ranked
    assert llm.stages_called() == ["resolve", "research", "extract", "judge"]
    assert [c["kind"] for c in llm.calls[:2]] == ["search", "search"]

    # stages recorded in order with timing, model and accounting
    stages = db.query(JudgmentStage).filter_by(run_id=run_id).order_by(JudgmentStage.id).all()
    assert [s.stage for s in stages] == list(STAGES)
    assert all(s.status == "succeeded" and s.duration_ms is not None and s.finished_at for s in stages)
    llm_stages = [s for s in stages if s.stage in ("resolve", "research", "extract", "judge")]
    assert all(s.model == fakes.FAKE_MODEL and s.input_tokens and s.output_tokens for s in llm_stages)
    assert run.cost_usd == pytest.approx(sum(s.cost_usd or 0 for s in stages))
    assert run.tool_calls == 6 and run.model_returned == fakes.FAKE_MODEL and run.model == "grok-4.3"
    assert run.prompt_hash and run.rubric_version == meth.RUBRIC_VERSION

    # entity resolution applied to the company (identity facts only)
    company = db.get(Company, body["company_id"])
    assert company.official_name == "NVIDIA Corporation" and company.ticker == "NVDA"
    assert company.cik == "0001045810" and company.is_public is True
    assert company.industry == "Semiconductors & AI computing" and company.current_run_id == run_id

    # sources: every candidate fetched by us; dead / soft-404s rejected
    srcs = {s.url: s for s in db.query(Source).filter_by(run_id=run_id)}
    assert srcs[fakes.DEAD_URL].status == "dead"
    assert srcs[fakes.SOFT_URL].status == "soft_404" and "random path" in srcs[fakes.SOFT_URL].reject_reason
    assert srcs[fakes.ROOT_REDIRECT_URL].status == "soft_404"
    assert srcs[fakes.SUSTAIN_URL].status == "ok" and srcs[fakes.SUSTAIN_URL].is_primary
    assert len(srcs[fakes.SUSTAIN_URL].sha256) == 64 and fakes.ENERGY_SENTENCE[:40] in srcs[fakes.SUSTAIN_URL].text
    assert srcs[fakes.NEWS_URL].is_primary is False
    assert fakes.POLICY_URL in srcs  # citation from search was fetched too
    assert any(s.origin == "edgar" for s in srcs.values())

    # evidence: only verbatim quotes survive
    ev = db.query(Evidence).filter_by(run_id=run_id).all()
    rejected = {e.rejected_reason for e in ev if not e.quote_verified}
    assert "quote not found in fetched source" in rejected
    assert "value does not appear in the quote" in rejected
    assert not any(e.metric_key == "energy_supplied" and e.quote_verified for e in ev)

    # measured math done in code
    cats = {c.category: c for c in db.query(CategoryScore).filter_by(run_id=run_id)}
    assert set(cats) == set(meth.CATEGORY_KEYS) and "overall" not in cats
    p = 612000 * 3.6e9 / (365.25 * 86400)
    assert run.avg_power_w == pytest.approx(p)
    assert run.k_equivalent == pytest.approx(0.18439, abs=1e-4)
    assert cats["energy_throughput"].score == pytest.approx(2.11, abs=0.01)
    assert cats["compute_capacity"].score == pytest.approx(4.20, abs=0.01)
    assert cats["growth"].inputs["subs"]["capex"]["cagr"] == pytest.approx(0.10, abs=1e-6)  # EDGAR series
    assert "energy" in cats["growth"].inputs["subs"]  # FY2023 -> FY2024 quoted series
    assert cats["policy_stance"].score is None and cats["policy_stance"].insufficient
    assert cats["frontier_acceleration"].score == 9.0

    agg = aggregate({k: (c.score, c.confidence) for k, c in cats.items()})
    assert run.index_score == agg.index_score and run.coverage == pytest.approx(0.92)
    assert run.confidence < run.coverage  # missing policy + imperfect confidences lower it

    # run detail API
    detail = client.get(f"/internal/runs/{run_id}", headers=HERMES).json()
    assert [s["stage"] for s in detail["stages"]] == list(STAGES)
    assert detail["sources"]["ok"] >= 3 and detail["sources"]["dead"] == 1
    recent = client.get("/internal/recent-judgments", headers=HERMES).json()
    assert recent["recent_runs"][0]["run_id"] == run_id and recent["recent_runs"][0]["status"] == "succeeded"
    assert client.get("/internal/stats", headers=HERMES).json()["runs"] == {"succeeded": 1}

    # public page: K headline, measured figures with units + sources, opinion labelled, no hallucinations
    html = client.get(f"/companies/{company.id}").text
    assert "K&nbsp;0.184" in html and "Kardashev-equivalent" in html
    assert "612,000 MWh" in html or "612000" in html
    assert "Opinion" in html and "/methodology#rubric-frontier_acceleration" in html
    assert fakes.FRONTIER_QUOTE in html
    assert "lobbied tirelessly" not in html and "9,000,000" not in html
    assert "Planned / under construction: 120 MW" in html
    assert "Run N ·" in html and "/internal/runs" not in html and "/admin" not in html
    assert not re.search(r"\$\d", html) and "Tokens" not in html  # no spend data on public pages

    # leaderboard
    home = client.get("/").text
    assert "NVIDIA Corporation" in home and "0.184" in home

    logs = db.query(IngestLog).filter_by(action="judgment_run").all()
    assert logs and logs[0].details["cost_usd"] == pytest.approx(run.cost_usd)


def test_double_approve_and_existing_company_are_idempotent(client, db, use_deps):
    use_deps(fakes.make_deps(fakes.world_llm()))
    sid = _suggest(db)
    r1 = client.post(f"/internal/approve/{sid}", headers=HERMES)
    r2 = client.post(f"/internal/approve/{sid}", headers=HERMES)
    assert r1.status_code == 202 and r2.status_code == 404
    # same name suggested again while a run is queued -> no second run
    sid2 = _suggest(db, "nvidia")
    r3 = client.post(f"/internal/approve/{sid2}", headers=HERMES)
    assert r3.status_code == 202 and r3.json()["run_id"] == r1.json()["run_id"]
    assert r3.json()["judgment"] == "already_queued"
    assert db.query(JudgmentRun).count() == 1 and db.query(Company).count() == 1


def test_rerun_queues_and_conflicts_while_active(client, db, use_deps):
    worker = use_deps(fakes.make_deps(fakes.world_llm()))
    c = _company(db)
    r = client.post(f"/internal/rerun-judgment/{c.id}", headers=HERMES)
    assert r.status_code == 202 and r.json()["status"] == "rerun_queued"
    again = client.post(f"/internal/rerun-judgment/{c.id}", headers=HERMES)
    assert again.status_code == 409 and again.json()["run_id"] == r.json()["run_id"]
    worker.process_next()
    assert client.post(f"/internal/rerun-judgment/{c.id}", headers=HERMES).status_code == 202
    assert client.post("/internal/rerun-judgment/999", headers=HERMES).status_code == 404


def _run_once(db, use_deps, company, deps):
    worker = use_deps(deps)
    run, _ = main.runs_svc.enqueue_run(db, company.id, trigger="rerun", triggered_by="test")
    worker.process_next()
    db.expire_all()
    return db.get(JudgmentRun, run.id)


def test_failed_rerun_preserves_published_run(client, db, use_deps):
    c = _company(db)
    good = _run_once(db, use_deps, c, fakes.make_deps(fakes.world_llm()))
    assert good.status == "succeeded" and good.published
    bad = _run_once(db, use_deps, c, fakes.make_deps(fakes.world_llm(research=[fakes.api_error()])))
    assert bad.status == "failed" and bad.error_type == "api_error" and not bad.published
    assert bad.error_class == "transient"
    db.expire_all()
    assert db.get(Company, c.id).current_run_id == good.id
    # the failed run's stages say where it broke; later stages never ran
    st = [(s.stage, s.status) for s in db.query(JudgmentStage).filter_by(run_id=bad.id).order_by(JudgmentStage.id)]
    assert st == [("resolve", "succeeded"), ("research", "failed")]
    err = db.query(IngestLog).filter_by(action="judge_error").one()
    assert err.details["existing_scores_untouched"] is True and err.details["run_id"] == bad.id
    page = client.get(f"/companies/{c.id}").text
    assert "newer measurement attempt did not complete" in page and "api_error" not in page and "K&nbsp;0.184" in page


def test_budget_cap_degrades_instead_of_failing(db, use_deps):
    """Cap reached after resolve: research is skipped (degraded), EDGAR still runs, and the run
    finishes with what it has instead of failing."""
    c = _company(db)
    llm = fakes.world_llm(judge=[lambda u: {"categories": [], "synthesis": None}])
    run = _run_once(db, use_deps, c, fakes.make_deps(llm, max_cost_usd=0.05))
    assert run.status == "succeeded", run.error_message
    assert any(d.startswith("research: budget cap reached") for d in run.degraded)
    assert "JUDGE_MAX_COST_USD" in run.degraded[0]
    assert llm.stages_called()[:1] == ["resolve"] and "research" not in llm.stages_called()
    st = {s.stage: s.status for s in db.query(JudgmentStage).filter_by(run_id=run.id)}
    assert st["research"] == "degraded" and st["edgar"] == "succeeded" and st["aggregate"] == "succeeded"
    assert run.cost_usd <= 0.05 + 1e-9


def test_budget_cap_reached_at_resolve_fails_permanently(db, use_deps):
    c = _company(db)
    run = _run_once(db, use_deps, c, fakes.make_deps(fakes.world_llm(), max_cost_usd=0.03, retry=fakes.RETRY))
    assert run.status == "failed" and run.error_type == "budget_exceeded" and run.error_class == "permanent"


def test_invalid_judge_output_is_repaired_once_without_new_search(db, use_deps):
    c = _company(db)
    llm = fakes.world_llm(judge=[{"categories": [{"category": "overall", "score": 7}]}, fakes.judge_ok])
    run = _run_once(db, use_deps, c, fakes.make_deps(llm))
    assert run.status == "succeeded"
    assert llm.stages_called() == ["resolve", "research", "extract", "judge", "judge"]
    assert llm.calls[-1]["kind"] == "chat" and "previous reply was rejected" in llm.calls[-1]["user"]
    judge = db.query(JudgmentStage).filter_by(run_id=run.id, stage="judge").one()
    assert len(judge.detail["attempts"]) == 2 and "no usable category" in judge.detail["attempts"][0]["error"]


@pytest.mark.parametrize("bad,degraded", [
    ("{\"categories\": [", True),                                                   # truncated JSON
    ({"categories": [{"category": "frontier_acceleration", "score": 11, "evidence_ids": []}]}, False),
    ({"categories": [{"category": "frontier_acceleration", "score": 5, "evidence_ids": [99999]},
                     {"category": "builder_velocity", "score": None},
                     {"category": "policy_stance", "score": None}]}, False),        # invented evidence id
])
def test_invalid_judge_output_never_discards_measured_work(db, use_deps, bad, degraded):
    c = _company(db)
    run = _run_once(db, use_deps, c, fakes.make_deps(fakes.world_llm(judge=[bad])))
    assert run.status == "succeeded", run.error_message
    cats = {x.category: x for x in db.query(CategoryScore).filter_by(run_id=run.id)}
    assert cats["energy_throughput"].score is not None and cats["compute_capacity"].score is not None
    assert all(cats[k].score is None for k in meth.JUDGED_KEYS)
    assert bool(run.degraded) == degraded
    if degraded:
        assert "judge" in run.degraded[0]


def test_insufficient_data_is_published_but_not_ranked_and_never_replaces_ranked(client, db, use_deps):
    empty_world = {"research": [{"sources": []}], "extract": [{"figures": [], "claims": []}]}
    # no sources, no EDGAR -> nothing measurable
    c = _company(db, "obscure-co")
    llm = fakes.world_llm(resolve=[{**fakes.RESOLVE_OK, "official_name": "Obscure Co", "ticker": None,
                                     "is_public": False, "sec_filer": False, "domain": "obscure.example"}],
                          **empty_world)
    llm.citations = {}
    run = _run_once(db, use_deps, c, fakes.make_deps(llm))
    assert run.status == "succeeded" and run.published and not run.ranked
    assert run.index_score is None and run.coverage == 0 and run.confidence == 0
    cats = db.query(CategoryScore).filter_by(run_id=run.id).all()
    assert all(x.score is None for x in cats)  # never a default 5.0
    assert "judge" in [s.stage for s in db.query(JudgmentStage).filter_by(run_id=run.id, status="skipped")]
    home = client.get("/").text
    assert "Not ranked yet" in home and "insufficient data" in home
    page = client.get(f"/companies/{c.id}").text
    assert "Not ranked: insufficient data" in page

    # a ranked company keeps its ranked run when a later run comes back thin
    n = _company(db, "nvidia")
    good = _run_once(db, use_deps, n, fakes.make_deps(fakes.world_llm()))
    thin_llm = fakes.world_llm(**empty_world)
    thin_llm.citations = {}
    thin = _run_once(db, use_deps, n, fakes.make_deps(thin_llm, sec=False))
    assert thin.status == "succeeded" and not thin.published
    assert "kept run" in thin.summary["publish_note"]
    db.expire_all()
    assert db.get(Company, n.id).current_run_id == good.id


def test_missing_api_key_fails_cleanly(db, use_deps):
    c = _company(db)
    run = _run_once(db, use_deps, c, fakes.make_deps(None))
    assert run.status == "failed" and run.error_type == "config_error"


def test_edgar_skipped_without_contact_string(db, use_deps):
    c = _company(db)
    deps = fakes.make_deps(fakes.world_llm(), sec=False)
    run = _run_once(db, use_deps, c, deps)
    st = db.query(JudgmentStage).filter_by(run_id=run.id, stage="edgar").one()
    assert st.status == "skipped" and "SEC_EDGAR_USER_AGENT" in st.detail["skipped"]
    assert not any("sec.gov" in u for u in deps.fetcher.calls)


def test_edgar_registrant_mismatch_is_not_used(db, use_deps):
    c = _company(db)
    llm = fakes.world_llm(resolve=[{**fakes.RESOLVE_OK, "ticker": "TSLA"}])
    run = _run_once(db, use_deps, c, fakes.make_deps(llm))
    st = db.query(JudgmentStage).filter_by(run_id=run.id, stage="edgar").one()
    assert st.status == "skipped" and "belongs to SEC registrant 'Tesla, Inc.'" in st.detail["skipped"]
    assert run.summary["entity"]["ticker"] is None and run.summary["identity_check"]["status"] == "mismatch"


def test_stale_running_run_is_requeued_then_failed(db):
    c = _company(db)
    old = datetime.now(UTC) - timedelta(minutes=30)
    run = JudgmentRun(company_id=c.id, status="running", attempt=1, heartbeat_at=old, started_at=old,
                      current_stage="research")
    db.add(run)
    db.commit()
    db.add(JudgmentStage(run_id=run.id, stage="research", status="running", attempt=1, started_at=old))
    db.commit()
    w = Worker(SessionLocal, lambda: None, stale_s=180, max_attempts=2)
    assert w.reconcile_stale() == [run.id]
    db.expire_all()
    assert db.get(JudgmentRun, run.id).status == "queued"
    assert db.query(JudgmentStage).filter_by(run_id=run.id).one().status == "interrupted"
    # second interruption exhausts attempts
    r = db.get(JudgmentRun, run.id)
    r.status, r.attempt, r.heartbeat_at = "running", 2, old
    db.commit()
    w.reconcile_stale()
    db.expire_all()
    r = db.get(JudgmentRun, run.id)
    assert r.status == "failed" and r.error_type == "interrupted"
    # fresh heartbeats are left alone
    fresh = JudgmentRun(company_id=_company(db, "x").id, status="running", attempt=1,
                        heartbeat_at=datetime.now(UTC))
    db.add(fresh)
    db.commit()
    assert w.reconcile_stale() == []


def test_claim_is_atomic_and_one_active_run_per_company(db):
    c = _company(db)
    run, created = main.runs_svc.enqueue_run(db, c.id, trigger="rerun", triggered_by="t")
    again, created2 = main.runs_svc.enqueue_run(db, c.id, trigger="rerun", triggered_by="t")
    assert created and not created2 and again.id == run.id
    w = Worker(SessionLocal, lambda: None)
    assert w.claim_next() == run.id
    assert w.claim_next() is None
    # the database itself refuses a second active run
    from sqlalchemy.exc import IntegrityError
    db.add(JudgmentRun(company_id=c.id, status="queued"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_legacy_scores_shown_as_superseded_and_not_ranked(client, db):
    c = _company(db, "legacy-co")
    for cat, sc in (("ai_tech_acceleration", 9.7), ("energy_kardashev", 4.2), ("market_competition", 7.1),
                    ("regulatory_stance", 7.8), ("builder_culture", 9.0), ("overall", 8.5)):
        db.add(Score(company_id=c.id, category=cat, score=sc, justification="x", model="grok-4"))
    db.commit()
    page = client.get(f"/companies/{c.id}").text
    assert "Legacy v0 assessment (superseded method)" in page and "mean 7.6" in page
    home = client.get("/").text
    assert "legacy v0 7.6" in home and "awaiting a measured run" in home
    assert 'class="board-table"' not in home


def test_admin_sees_runs_with_stage_timings(admin_client, db, use_deps):
    worker = use_deps(fakes.make_deps(fakes.world_llm()))
    c = _company(db)
    r = admin_client.post(f"/internal/rerun-judgment/{c.id}", headers={"accept": "text/html"},
                          follow_redirects=False)
    run_id = db.query(JudgmentRun).one().id
    assert r.status_code == 303 and r.headers["location"] == f"/admin/runs/{run_id}"
    detail = admin_client.get(r.headers["location"]).text
    assert f"Queued run #{run_id}" in detail  # flash shown once
    assert 'hx-trigger="every 3s"' in detail and "waiting for the worker" in detail
    page = admin_client.get("/admin").text
    assert "queued" in page and 'hx-trigger="every 4s"' in page and f"Queued run #{run_id}" not in page
    worker.process_next()
    part = admin_client.get("/admin/runs").text
    assert "succeeded" in part and "reso" in part and "hx-trigger" not in part
    detail = admin_client.get(f"/admin/runs/{run_id}").text
    assert "hx-trigger" not in detail and "pipeline-v2.6" in detail and "resolve" in detail
    # extracted evidence is listed with verification outcome and rejection reasons (debugging empty runs)
    assert "Extracted figures &amp; quotes" in detail and ">no<" in detail and ">yes<" in detail


def test_methodology_page_documents_weights_anchors_and_rubrics(client):
    html = client.get("/methodology").text
    assert "65%" in html and "35%" in html
    for c in meth.CATEGORIES:
        assert c["label"] in html
    assert "clamp(2.5 × (log₁₀P − 7), 0, 10)" in html
    assert 'id="rubric-policy_stance"' in html and meth.RUBRICS["builder_velocity"][8] in html
    assert "soft-404" in html


def test_public_copy_no_longer_overclaims(client):
    for path in ("/", "/suggest", "/methodology"):
        text = client.get(path).text.lower()
        assert "human-reviewed" not in text, path
        assert "five judged dimensions" not in text, path
