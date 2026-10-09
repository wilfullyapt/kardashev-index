"""pipeline-v2.5: an energy source that was found but couldn't be read is not "no energy figure found".

The run stays unranked (never scored without energy), admins get an alert, and one automatic retry is
scheduled. Dead links / soft 404s are not "unreadable": those sources don't exist.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import alerts
from app.models import Company, IngestLog, JudgmentRun
from app.pipeline.fetch import FetchResult
from app.pipeline.runner import ENERGY_RETRY_TRIGGER, execute_run, source_unreadable
from app.runs import enqueue_run
from tests import fakes

IMPACT_URL = "https://www.nvidia.com/impact/2024-extended-impact-report.pdf"   # the company's own domain


@pytest.mark.parametrize("status,http,reason,expect", [
    ("error", 200, "PDF larger than 30 MB cap (FETCH_MAX_BYTES); not parsed", True),
    ("error", 200, "unreadable PDF: PdfReadError", True),
    ("thin", 200, "too little text (12 chars)", True),
    ("dead", 403, "HTTP 403", True),
    ("dead", 429, "HTTP 429", True),
    ("dead", 503, "HTTP 503", True),
    ("dead", None, "ReadTimeout: timed out", True),
    ("dead", 404, "HTTP 404", False),
    ("dead", 410, "HTTP 410", False),
    ("soft_404", 200, "page reads as 'not found'", False),
    ("blocked", None, "blocked: private address", False),
    ("ok", 200, None, False),
])
def test_source_unreadable(status, http, reason, expect):
    assert source_unreadable(SimpleNamespace(status=status, http_status=http, reject_reason=reason)) is expect


def _no_energy_extract(user):
    out = fakes.extract_ok(user)
    out["figures"] = [f for f in out["figures"] if not f["metric_key"].startswith(("energy", "electricity"))]
    return out


def _world(impact_route, covers=("energy",)):
    research = {"sources": [*fakes.RESEARCH_OK["sources"],
                            {"url": IMPACT_URL, "title": "2024 Impact Report", "covers": list(covers), "why": "energy"}]}
    llm = fakes.world_llm(research=[research], extract=[_no_energy_extract])
    routes = fakes.world_routes({IMPACT_URL: impact_route} if impact_route is not None else {})
    return llm, routes


def _run(db, llm, routes, name="examplemotors", company=None):
    c = company or Company(canonical_name=name, official_name=name.title(), industry="Autos")
    if company is None:
        db.add(c)
        db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    return c, execute_run(db, run.id, fakes.make_deps(llm, routes=routes))


TRUNCATED_PDF = FetchResult(url=IMPACT_URL, final_url=IMPACT_URL, status=200, content_type="application/pdf",
                            body=b"%PDF-1.7 " + b"x" * 1000, truncated=True)


@pytest.mark.parametrize("route,label", [
    ((403, "text/html", "<html>Access Denied</html>"), "403"),
    (TRUNCATED_PDF, "too large"),
])
def test_unreadable_energy_source_keeps_run_unranked_alerts_and_schedules_retry(db, route, label):
    llm, routes = _world(route)
    c, run = _run(db, llm, routes)
    assert run.status == "succeeded"
    s = run.summary
    assert s["rank_basis"] == "energy_unreadable" and not s["energy_undisclosed"]
    assert run.ranked is False and "couldn't be read" in s["not_ranked_reason"]
    assert s["energy_unreadable"][0]["url"] == IMPACT_URL
    retry = db.query(JudgmentRun).filter(JudgmentRun.company_id == c.id,
                                         JudgmentRun.trigger == ENERGY_RETRY_TRIGGER).one()
    assert retry.status == "queued" and retry.next_attempt_at is not None
    delay = retry.next_attempt_at.replace(tzinfo=None) - run.finished_at.replace(tzinfo=None)
    assert timedelta(hours=23) < delay < timedelta(hours=25)
    a = alerts.collect(db)
    assert [x["run_id"] for x in a["energy_unreadable"]] == [run.id]
    assert retry.id in [x["run_id"] for x in a["retrying"]]
    assert db.query(IngestLog).filter(IngestLog.action == "energy_retry_scheduled").count() == 1


def test_dead_energy_link_is_not_unreadable_and_keeps_undisclosed_logic(db):
    llm, routes = _world(None)          # IMPACT_URL -> 404: the document doesn't exist
    _c, run = _run(db, llm, routes)
    assert run.summary["energy_unreadable"] is None and run.summary["rank_basis"] != "energy_unreadable"
    assert db.query(JudgmentRun).filter(JudgmentRun.trigger == ENERGY_RETRY_TRIGGER).count() == 0


def test_unreadable_source_not_about_energy_is_ignored(db):
    llm, routes = _world((403, "text/html", "denied"), covers=("policy_stance",))
    research = llm.responses["research"][0]
    research["sources"][-1].update(url="https://www.nvidia.com/blog/policy", title="Policy blog")
    routes = fakes.world_routes({"https://www.nvidia.com/blog/policy": (403, "text/html", "denied")})
    _c, run = _run(db, llm, routes)
    assert run.summary["energy_unreadable"] is None


def test_retry_is_bounded_and_manual_rerun_pulls_it_forward(db):
    llm, routes = _world((403, "text/html", "denied"))
    c, _first = _run(db, llm, routes)
    retry = db.query(JudgmentRun).filter(JudgmentRun.trigger == ENERGY_RETRY_TRIGGER).one()
    # an admin asks for a re-run: the scheduled retry runs now instead of waiting a day
    same, created = enqueue_run(db, c.id, trigger="admin_rerun", triggered_by="admin@x")
    assert not created and same.id == retry.id and same.next_attempt_at is None
    llm2, routes2 = _world((403, "text/html", "denied"))
    second = execute_run(db, retry.id, fakes.make_deps(llm2, routes=routes2))
    assert second.summary["rank_basis"] == "energy_unreadable"
    # ENERGY_RETRY_MAX=1: the retry itself does not schedule another
    assert db.query(JudgmentRun).filter(JudgmentRun.trigger == ENERGY_RETRY_TRIGGER).count() == 1
    assert db.query(IngestLog).filter(IngestLog.action == "energy_retry_skipped").count() == 1


def test_retry_can_be_disabled(db):
    llm, routes = _world((403, "text/html", "denied"))
    c = Company(canonical_name="m", official_name="M", industry="Autos")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run = execute_run(db, run.id, fakes.make_deps(llm, routes=routes, energy_retry_enabled=False))
    assert run.ranked is False and "retry scheduled" not in run.summary["not_ranked_reason"]
    assert db.query(JudgmentRun).filter(JudgmentRun.trigger == ENERGY_RETRY_TRIGGER).count() == 0


def test_public_labels(client, db):
    llm, routes = _world((403, "text/html", "denied"))
    c, _r = _run(db, llm, routes)
    page = client.get(f"/companies/{c.id}").text
    assert "Energy source couldn’t be read" in page and "Energy undisclosed" not in page
    meth = client.get("/methodology").text
    assert "No energy figure found" in meth and 'id="energy-unreadable"' in meth
    assert "Energy undisclosed" not in meth
