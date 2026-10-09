"""Public run history (N-x labels, published-only, no spend data) and the admin runs views."""
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import main
from app.models import CategoryScore, Company, JudgmentRun, JudgmentStage, Score
from app.runs import parse_rel, rel_label, spark
from app.db import SessionLocal
from app.worker import Worker
from tests import fakes

T0 = datetime(2026, 7, 1, 17, 0, tzinfo=UTC)


def _company(db, name="nvidia"):
    c = Company(canonical_name=name, official_name=name.title(), industry="Chips")
    db.add(c)
    db.commit()
    return c


def _run(db, c, day, index, k=0.18, status="succeeded", published=True, cats=None, cost=0.21, **kw):
    r = JudgmentRun(company_id=c.id, status=status, trigger="rerun", triggered_by="t", attempt=1,
                    queued_at=T0 + timedelta(days=day), started_at=T0 + timedelta(days=day, seconds=5),
                    finished_at=T0 + timedelta(days=day, seconds=95), duration_ms=90_000,
                    index_score=index, k_equivalent=k, confidence=0.7, coverage=0.9, ranked=index is not None,
                    published=published, cost_usd=cost, input_tokens=1000, output_tokens=200,
                    pipeline_version="pipeline-v2.0", prompt_version="prompts-v2.0", rubric_version="rubrics-v1",
                    weights_version="weights-v1", model="grok-4.3", error_type=None if status != "failed" else "api_error",
                    error_message=None if status != "failed" else "research: boom", **kw)
    db.add(r)
    db.commit()
    for key, sc in (cats or {}).items():
        db.add(CategoryScore(run_id=r.id, category=key, family="measured" if key in (
            "energy_throughput", "compute_capacity", "growth") else "judged", score=sc, confidence=0.8,
            weight=0.3, rationale=f"{key} rationale"))
    db.add(JudgmentStage(run_id=r.id, seq=1, stage="resolve", attempt=1, status="succeeded", duration_ms=4000,
                         started_at=r.started_at, finished_at=r.started_at, cost_usd=0.03, model="grok-4.3"))
    if published and status == "succeeded":
        c.current_run_id = r.id
    db.commit()
    return r


@pytest.fixture
def three_runs(db):
    """published 5.0 → failed → published 6.0 → succeeded-but-unpublished 3.0 → published 6.5 (current)."""
    c = _company(db)
    r1 = _run(db, c, 0, 5.0, 0.170, cats={"energy_throughput": 2.0, "frontier_acceleration": 8.0})
    rf = _run(db, c, 3, None, None, status="failed", published=False, cost=0.11)
    r2 = _run(db, c, 10, 6.0, 0.180, cats={"energy_throughput": 2.5, "frontier_acceleration": 8.0})
    unpub = _run(db, c, 15, 3.0, 0.1, published=False)  # succeeded but kept unpublished (not ranked)
    r3 = _run(db, c, 20, 6.5, 0.184, cats={"energy_throughput": 2.1, "frontier_acceleration": 9.0,
                                            "builder_velocity": 7.0})
    db.add(Score(company_id=c.id, category="energy_kardashev", score=7.0, justification="old", model="grok-4",
                 judged_at=T0 - timedelta(days=30)))
    db.commit()
    return SimpleNamespace(c=c, r1=r1, rf=rf, r2=r2, unpub=unpub, r3=r3)


def test_labels_and_parsing():
    assert [rel_label(o) for o in (0, -1, -2)] == ["N", "N-1", "N-2"]
    assert parse_rel("N") == 0 and parse_rel("n-2") == -2 and parse_rel("N+1") is None and parse_rel("3") is None
    sp = spark([5.0, None, 6.5], 0, 10)
    assert sp["n"] == 2 and sp["latest"] == 6.5 and len(sp["points"].split()) == 2
    assert spark([None]) is None and spark([0.18])["n"] == 1


def test_history_maps_published_runs_to_n_labels(db, three_runs):
    t = three_runs
    c = t.c
    h = main.runs_svc.history(db, db.get(Company, c.id))
    assert [(e["label"], e["ordinal"], e["run"].id) for e in h["runs"]] == [
        ("N-2", 1, t.r1.id), ("N-1", 2, t.r2.id), ("N", 3, t.r3.id)]
    assert h["runs"][1]["delta"] == 1.0 and h["runs"][2]["delta"] == 0.5
    assert h["runs"][0]["url"] == f"/companies/{c.id}/runs/1" and h["runs"][2]["url"] == f"/companies/{c.id}"
    assert h["legacy"] and h["legacy"]["judged_at"] is not None
    assert h["spark_index"]["n"] == 3


def test_public_history_excludes_failed_runs_and_spend(client, db, three_runs):
    t = three_runs
    c = t.c
    html = client.get(f"/companies/{c.id}").text
    hist = html[html.index('id="history"'):html.index("</section>", html.index('id="history"'))]
    for lbl in ("N-2", "N-1", ">N<", "v0"):
        assert lbl in hist
    assert hist.count('class="runlbl') == 4  # 3 published runs + legacy v0; failed/unpublished absent
    assert 'data-label="Index" class="mono num">3.0' not in hist and "api_error" not in html and "boom" not in html
    assert not re.search(r"\$\d", html) and "cost" not in hist.lower() and "/internal/" not in html
    assert "<polyline" in html and "Index across 3 runs" in html
    # per-category delta vs N-1: energy 2.5 -> 2.1, frontier 8 -> 9, builder velocity is new
    assert "-0.4" in html and "+1.0" in html and ">new<" in html and "+0.5 vs N-1" in html


def test_historical_run_view_is_read_only_and_labelled(client, db, three_runs):
    t = three_runs
    c = t.c
    page = client.get(f"/companies/{c.id}/runs/2")
    assert page.status_code == 200
    html = page.text
    assert "Historical run, read-only" in html and 'name="robots" content="noindex"' in html
    assert "run N-1" in html and "+1.0 vs N-2" in html and "6.0" in html
    # ?run=N-1 resolves to the stable URL; current ordinal and N redirect to the live page
    r = client.get(f"/companies/{c.id}?run=N-1", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/companies/{c.id}/runs/2"
    r = client.get(f"/companies/{c.id}/runs/3", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/companies/{c.id}"
    assert client.get(f"/companies/{c.id}?run=N").status_code == 200
    # the failed and unpublished runs have no public ordinal; ids are never accepted
    for bad in ("4", "5", "0", "abc"):
        assert client.get(f"/companies/{c.id}/runs/{bad}").status_code == 404
    assert client.get(f"/companies/{c.id}?run=N-7").status_code == 404
    legacy = client.get(f"/companies/{c.id}/runs/v0").text
    assert "Legacy v0 scores — superseded method" in legacy and "Legacy v0 assessment" in legacy


def test_unknown_pages_render_html_404_for_browsers_json_for_api(client):
    r = client.get("/companies/999", headers={"accept": "text/html"})
    assert r.status_code == 404 and "Nothing at this address" in r.text
    assert client.get("/companies/999").json() == {"detail": "Company not found"}
    assert client.get("/internal/runs/1").status_code == 401


@pytest.mark.parametrize("path", ["/admin", "/admin/runs", "/admin/runs/1", "/admin/runs?status=failed"])
def test_admin_pages_require_login(client, path):
    assert client.get(path, follow_redirects=False).status_code == 401
    r = client.get(path, headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login?next=%2Fadmin")
    from urllib.parse import parse_qs, urlsplit
    assert parse_qs(urlsplit(r.headers["location"]).query)["next"] == [path]


def test_login_returns_to_requested_admin_page_only(client):
    r = client.post("/admin/login", data={"email": "admin@example.com", "password": "test-password",
                                          "next": "/admin/runs"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/runs"
    c2 = client.__class__(main.app)
    r = c2.post("/admin/login", data={"email": "admin@example.com", "password": "test-password",
                                      "next": "https://evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/admin"
    bad = c2.post("/admin/login", data={"email": "x@y.z", "password": "nope"})
    assert bad.status_code == 401 and "Invalid credentials" in bad.text


def test_public_pages_never_link_admin(client, db, three_runs):
    c = three_runs.c
    for path in ("/", f"/companies/{c.id}", f"/companies/{c.id}/runs/1", "/methodology", "/suggest"):
        html = client.get(path).text
        assert "/admin" not in html and "/internal" not in html, path


def test_admin_runs_list_filters_paginates_and_shows_versions(admin_client, db, three_runs):
    t = three_runs
    c = t.c
    other = _company(db, "tesla")
    for d in range(30):
        _run(db, other, d, 4.0, published=False)
    page = admin_client.get("/admin/runs").text
    assert "35 runs" in page and "Page 1 of 2" in page and "Older" in page
    p2 = admin_client.get("/admin/runs?page=2").text
    assert "Page 2 of 2" in p2 and "Newer" in p2
    only = admin_client.get(f"/admin/runs?company_id={c.id}").text
    assert "5 runs" in only and f"#{t.rf.id}" in only and "Re-run Nvidia" in only
    failed = admin_client.get("/admin/runs?status=failed").text
    assert "1 run<" in failed and f'href="/admin/runs/{t.rf.id}"' in failed
    assert "PDT" in page  # queued times shown in Pacific
    assert admin_client.get("/admin/runs?company_id=abc&status=bogus").status_code == 200


def test_admin_run_detail_shows_times_versions_cost_and_errors(admin_client, db, three_runs, monkeypatch):
    t = three_runs
    c = t.c
    html = admin_client.get(f"/admin/runs/{t.rf.id}").text
    assert "api_error" in html and "research: boom" in html
    # 2026-07-04 17:00 UTC -> 10:00 PDT
    assert "2026-07-04 10:00:00 PDT" in html and "1m 30s" in html and "waited 5.0 s" in html
    assert "prompts-v2.0" in html and "rubrics-v1" in html and "weights-v1" in html and "$0.1100" in html
    assert "unknown (set by RENDER_GIT_COMMIT)" in html
    ok = admin_client.get(f"/admin/runs/{t.r2.id}").text
    assert f'href="/companies/{c.id}/runs/2"' in ok  # link to its public view
    assert admin_client.get("/admin/runs/9999").status_code == 404


def test_rerun_from_admin_confirms_and_handles_conflict(admin_client, db, three_runs):
    c = three_runs.c
    html = admin_client.get("/admin").text
    assert f'action="/internal/rerun-judgment/{c.id}"' in html and "data-confirm=\"Re-run " in html
    r = admin_client.post(f"/internal/rerun-judgment/{c.id}", headers={"accept": "text/html"},
                          follow_redirects=False)
    new = db.query(JudgmentRun).filter_by(status="queued").one()
    assert r.headers["location"] == f"/admin/runs/{new.id}"
    again = admin_client.post(f"/internal/rerun-judgment/{c.id}", headers={"accept": "text/html"},
                              follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == f"/admin/runs/{new.id}"
    shown = admin_client.get(again.headers["location"]).text
    assert "already has run" in shown and f"Run #{new.id} queued" in shown
    # API clients still get the 409 contract
    assert admin_client.post(f"/internal/rerun-judgment/{c.id}").status_code == 409


def test_run_records_code_version(db, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")
    c = _company(db)
    worker = Worker(SessionLocal, lambda: fakes.make_deps(fakes.world_llm()))
    run, _ = main.runs_svc.enqueue_run(db, c.id, trigger="rerun", triggered_by="t")
    worker.process_next()
    db.expire_all()
    assert db.get(JudgmentRun, run.id).code_version == "abcdef1234567890"
    assert main.runs_svc.run_json(db, db.get(JudgmentRun, run.id))["code_version"] == "abcdef1234567890"
