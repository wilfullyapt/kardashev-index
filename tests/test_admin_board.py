"""1.8.0: phone-first admin board. Ranked cards by Index; unranked in four groups with a documented
precedence; card actions (re-run with a cost estimate, retract, un-retract, details, history)."""
from datetime import UTC, datetime, timedelta

from app import board, publication
from app.models import Company, IngestLog, JudgmentRun

T0 = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)


def _company(db, name):
    c = Company(canonical_name=name, official_name=name.title(), industry="x")
    db.add(c)
    db.commit()
    return c


def _run(db, c, day=0, *, ranked=False, index=5.0, coverage=0.7, confidence=0.5, status="succeeded",
         degraded=None, summary=None, cost=0.3, error_type=None, published=True):
    r = JudgmentRun(company_id=c.id, status=status, published=published, ranked=ranked, index_score=index,
                    coverage=coverage, confidence=confidence, trigger="t", triggered_by="t", attempt=1,
                    queued_at=T0 + timedelta(days=day), finished_at=T0 + timedelta(days=day),
                    pipeline_version="pipeline-v2.13", cost_usd=cost, degraded=degraded, error_type=error_type,
                    summary={"measured_coverage": 0.6, **(summary or {})})
    db.add(r)
    db.commit()
    return r


def _world(db):
    hi = _company(db, "alpha")
    _run(db, hi, ranked=True, index=7.2)
    lo = _company(db, "beta")
    _run(db, lo, ranked=True, index=3.1)
    en = _company(db, "tesla")
    _run(db, en, summary={"rank_basis": "energy_unreadable", "energy_unreadable": [{"url": "u"}],
                          "not_ranked_reason": "energy source found but couldn't be read (4: HTTP 403)"},
         degraded=["fetch: x"])                     # energy wins over degraded
    dg = _company(db, "gamma")
    _run(db, dg, summary={"not_ranked_reason": "insufficient data"}, degraded=["extract: budget cap"])
    fl = _company(db, "delta")
    _run(db, fl, summary={"not_ranked_reason": "insufficient data: 53%"}, coverage=0.53)
    _run(db, fl, day=1, status="failed", error_type="internal_error", published=False)
    cf = _company(db, "epsilon")
    _run(db, cf, confidence=0.15, summary={"not_ranked_reason": "confidence 15% (needs more than 20%)"})
    cv = _company(db, "zeta")
    _run(db, cv, coverage=0.45, summary={"not_ranked_reason": "insufficient data: 45% of weight scored"})
    _company(db, "eta")                            # no run yet
    return locals()


def test_groups_and_precedence(db):
    w = _world(db)
    b = board.build(db)
    assert [x["company"].canonical_name for x in b["ranked"]] == ["alpha", "beta"]
    g = {k: [x["company"].canonical_name for x in cards] for k, _l, cards in b["groups"]}
    assert g == {"energy": ["tesla"], "degraded": ["delta", "gamma"], "confidence": ["epsilon"],
                 "coverage": ["eta", "zeta"]}
    assert w["fl"].id and b["unranked_count"] == 6


def test_classify_rules_directly():
    from types import SimpleNamespace as N
    cur = N(id=1, summary={}, degraded=None, confidence=0.5)
    assert board.classify(cur, N(id=2, status="failed"), 0.2) == "degraded"
    assert board.classify(cur, N(id=1, status="succeeded"), 0.2) == "coverage"
    assert board.classify(N(id=1, summary={}, degraded=None, confidence=0.2), None, 0.2) == "confidence"
    assert board.classify(None, None, 0.2) == "coverage"
    assert board.classify(None, N(id=3, status="failed"), 0.2) == "degraded"


def test_cost_estimate_uses_recorded_costs(db):
    assert "typically" in board.cost_estimate(db)
    c = _company(db, "c1")
    _run(db, c, cost=0.2)
    _run(db, c, day=1, cost=0.4)
    assert board.cost_estimate(db).startswith("about $0.30 (average of the last 2 runs)")


def test_board_page_cards_and_actions(admin_client, db):
    w = _world(db)
    html = admin_client.get("/admin").text
    assert 'id="group-energy"' in html and 'id="group-coverage"' in html and "Ranked · by Index" in html
    assert html.index("Alpha") < html.index("Beta")                     # sorted by Index
    tesla = w["en"]
    assert f'action="/internal/rerun-judgment/{tesla.id}"' in html and "about $0.30" in html
    assert f'href="/admin/companies/{tesla.id}/history"' in html and "/admin/runs/" in html
    assert "last run 2026-10-01" in html and "$0.30" in html
    assert "Classic" in html and admin_client.get("/admin/classic").status_code == 200


def test_retract_then_unretract_from_board(admin_client, db):
    c = _company(db, "omega")
    r = _run(db, c, ranked=True, index=6.0)
    resp = admin_client.post(f"/admin/runs/{r.id}/retract", data={"reason": "wrong energy figure used", "next": f"/admin#company-{c.id}"},
                             follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/admin#company-{c.id}"
    db.refresh(r)
    assert r.retracted_at is not None
    html = admin_client.get("/admin").text
    assert f'action="/admin/runs/{r.id}/unretract"' in html
    resp = admin_client.post(f"/admin/runs/{r.id}/unretract", data={"next": "/admin"}, follow_redirects=False)
    assert resp.status_code == 303
    db.refresh(r)
    db.refresh(c)
    assert r.retracted_at is None and r.retraction_reason is None and c.current_run_id == r.id
    log = db.query(IngestLog).filter(IngestLog.action == "run_unretracted").one()
    assert log.details["previous_retraction"]["reason"] == "wrong energy figure used"
    assert log.details["current_after"] == r.id


def test_unretract_errors_and_auth(admin_client, client, db):
    c = _company(db, "psi")
    r = _run(db, c)
    resp = admin_client.post(f"/admin/runs/{r.id}/unretract", follow_redirects=True)
    assert "Not un-retracted" in resp.text
    assert client.post(f"/admin/runs/{r.id}/unretract", follow_redirects=False).status_code in (303, 401, 403)
    cross = admin_client.post(f"/admin/runs/{r.id}/unretract", headers={"origin": "https://evil.example"},
                              follow_redirects=False)
    assert cross.status_code == 403


def test_unretract_follows_existing_current_run_rules(db):
    c = _company(db, "rules")
    old = _run(db, c, ranked=True, index=5.0)
    new = _run(db, c, day=1, ranked=True, index=6.0)
    publication.retract(db, new, "a", "the newer figure was wrong")
    db.commit()
    assert publication.current_run(db, c).id == old.id
    publication.unretract(db, new, "a")
    db.commit()
    assert publication.current_run(db, c).id == new.id


def test_history_link_redirects_to_the_company_runs(admin_client, db):
    c = _company(db, "hist")
    r = admin_client.get(f"/admin/companies/{c.id}/history", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/admin/runs?company_id={c.id}"
    assert admin_client.get("/admin/companies/99999/history").status_code == 404
