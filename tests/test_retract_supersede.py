"""0.10.0: admin retraction of known-wrong runs, and "newer pipeline wins" — both computed at read
time (app/publication.py replay), so existing data follows the rules on deploy without a data migration."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import publication
from app.models import Company, IngestLog, JudgmentRun
from app.runs import company_view, history, leaderboard
from tests.conftest import HERMES

T0 = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)
REASON = "Counted 3 GW of planned capacity as operating (fixed in pipeline-v2.4)."


def _company(db, name="crusoe"):
    c = Company(canonical_name=name, official_name=name.title(), industry="AI data centers")
    db.add(c)
    db.commit()
    return c


def _run(db, c, day, *, version, ranked, published, index=4.5, coverage=0.62, confidence=0.4, measured=0.81,
         status="succeeded", degraded=None, reason=None, basis=None, synthesis=None, pointer=None):
    summary = {"measured_coverage": measured, "synthesis": synthesis}
    if reason:
        summary["not_ranked_reason"] = reason
    if basis:
        summary["rank_basis"] = basis
    r = JudgmentRun(company_id=c.id, status=status, published=published, ranked=ranked, index_score=index,
                    k_equivalent=0.128, coverage=coverage, confidence=confidence, trigger="t", triggered_by="t",
                    attempt=1, queued_at=T0 + timedelta(days=day), finished_at=T0 + timedelta(days=day),
                    pipeline_version=version, weights_version="weights-v1", rubric_version="rubrics-v1",
                    summary=summary, degraded=degraded)
    db.add(r)
    db.commit()
    if pointer if pointer is not None else published:
        c.current_run_id = r.id
        db.commit()
    return r


def _crusoe(db, **v24):
    """Crusoe in production: a ranked pipeline-v2.2 run (3 GW counted as operating), then a v2.4 run
    that correctly dropped it and fell to 50% of the weight, so the old guard withheld it."""
    c = _company(db)
    old = _run(db, c, 0, version="pipeline-v2.2", ranked=True, published=True,
               synthesis="Large-scale builds underway with 3 GW operating capacity.")
    new = _run(db, c, 1, version="pipeline-v2.4", ranked=False, published=False, coverage=0.5, index=3.1,
               reason="insufficient data: 50% of weight scored (needs 60%)",
               synthesis="Operates 18.9 MW on average; 3 GW is planned.", **v24)
    return c, old, new


# ---------------------------------------------------------------- the replay rules (pure)

def ns(i, version, *, ranked=False, published=True, coverage=0.5, degraded=None, summary=None, retracted=None):
    return SimpleNamespace(id=i, pipeline_version=version, ranked=ranked, is_ranked=ranked, published=published,
                           coverage=coverage, degraded=degraded, summary=summary or {}, retracted_at=retracted,
                           status="succeeded")


def test_pipeline_key_orders_numerically():
    k = publication.pipeline_key
    assert k("pipeline-v2.10") > k("pipeline-v2.9") > k("pipeline-v2.4") > k(None) == ()


def test_newer_pipeline_insufficient_run_replaces_ranked_run():
    rp = publication.replay([ns(1, "pipeline-v2.2", ranked=True), ns(2, "pipeline-v2.4", published=False)], False)
    assert rp.current.id == 2 and [r.id for r in rp.public] == [1, 2]
    assert "newer pipeline" in rp.notes[2]


def test_same_version_keeps_the_guard():
    rp = publication.replay([ns(1, "pipeline-v2.4", ranked=True), ns(2, "pipeline-v2.4", published=False)], False)
    assert rp.current.id == 1 and [r.id for r in rp.public] == [1]


@pytest.mark.parametrize("bad", [{"degraded": ["judge: budget cap"]},
                                 {"summary": {"rank_basis": "energy_unreadable"}},
                                 {"summary": {"energy_unreadable": [{"url": "x"}]}}])
def test_degraded_or_unreadable_newer_run_never_supersedes(bad):
    rp = publication.replay([ns(1, "pipeline-v2.2", ranked=True), ns(2, "pipeline-v2.5", published=False, **bad)],
                            False)
    assert rp.current.id == 1


def test_no_supersede_over_legacy_only():
    rp = publication.replay([ns(1, "pipeline-v2.4", published=False)], True)
    assert rp.current is None


def test_retraction_redecides_later_runs_and_falls_back_to_latest():
    t = datetime(2026, 10, 9, tzinfo=UTC)
    # A ranked, B ranked (wrong, retracted), C unranked same version withheld because of B
    runs = [ns(1, "pipeline-v2.4", ranked=True, coverage=0.7), ns(2, "pipeline-v2.4", ranked=True, retracted=t),
            ns(3, "pipeline-v2.4", published=False, coverage=0.5)]
    rp = publication.replay(runs, False)
    assert rp.current.id == 1 and [r.id for r in rp.retracted] == [2]     # guard: unranked never replaces ranked
    # only run retracted: the most recent remaining run becomes current even if unranked
    rp = publication.replay([ns(2, "pipeline-v2.4", ranked=True, retracted=t),
                             ns(3, "pipeline-v2.4", published=False)], True)
    assert rp.current.id == 3 and "after a retraction" in rp.notes[3]


# ---------------------------------------------------------------- Crusoe, at read time (no data migration)

def test_crusoe_v24_run_becomes_current_on_deploy(db, client):
    c, old, new = _crusoe(db)
    assert c.current_run_id == old.id                     # stored pointer unchanged…
    assert company_view(db, c)["run"].id == new.id        # …but the read-time current is the v2.4 run
    ranked, awaiting = leaderboard(db)
    assert c.id not in [e["company"].id for e in ranked]
    assert next(e["reason"] for e in awaiting if e["company"].id == c.id).startswith("insufficient data")
    h = history(db, c)
    assert [e["run"].id for e in h["runs"]] == [old.id, new.id] and h["current"]["run"].id == new.id
    assert h["withheld_total"] == 0
    page = client.get(f"/companies/{c.id}").text
    assert "Unranked" in page and "3 GW is planned" in page and "3 GW operating capacity" not in page


def test_crusoe_degraded_v24_run_does_not_supersede(db):
    c, old, _new = _crusoe(db, degraded=["fetch: transient failure"])
    assert company_view(db, c)["run"].id == old.id


def test_failed_newer_run_never_replaces(db):
    c = _company(db)
    old = _run(db, c, 0, version="pipeline-v2.2", ranked=True, published=True)
    _run(db, c, 1, version="pipeline-v2.5", ranked=False, published=False, status="failed")
    assert company_view(db, c)["run"].id == old.id


# ---------------------------------------------------------------- admin retraction

def test_admin_retract_requires_reason(db, admin_client):
    c = _company(db)
    r = _run(db, c, 0, version="pipeline-v2.4", ranked=True, published=True)
    resp = admin_client.post(f"/admin/runs/{r.id}/retract", data={"reason": "bad"}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(JudgmentRun, r.id).retracted_at is None


def test_admin_retract_audits_and_shows_correction(db, admin_client, client):
    c, old, new = _crusoe(db, degraded=["fetch: transient failure"])   # v2.4 would not supersede by itself
    assert company_view(db, c)["run"].id == old.id
    page = admin_client.get(f"/admin/runs/{old.id}").text
    assert f'action="/admin/runs/{old.id}/retract"' in page
    resp = admin_client.post(f"/admin/runs/{old.id}/retract", data={"reason": REASON}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    old, c = db.get(JudgmentRun, old.id), db.get(Company, c.id)
    assert old.retracted_at and old.retracted_by == "admin@example.com" and old.retraction_reason == REASON
    assert c.current_run_id == new.id                      # most recent remaining run, even though unranked
    log = db.query(IngestLog).filter_by(action="run_retracted").one()
    assert log.admin_id == "admin@example.com" and log.details["run_id"] == old.id
    assert log.details["reason"] == REASON and log.details["current_after"] == new.id
    h = history(db, c)
    assert [e["run"].id for e in h["runs"]] == [new.id] and h["corrections"][0]["reason"] == REASON
    pub = client.get(f"/companies/{c.id}").text
    assert "Correction (" in pub and "3 GW of planned capacity" in pub and "Unranked" in pub
    assert "3 GW operating capacity" not in pub
    assert "retracted" in admin_client.get(f"/admin/runs/{old.id}").text
    # retracting again is refused
    admin_client.post(f"/admin/runs/{old.id}/retract", data={"reason": REASON})
    assert db.query(IngestLog).filter_by(action="run_retracted").count() == 1


def test_retract_is_csrf_protected_and_admin_only(db, admin_client, client):
    c = _company(db)
    r = _run(db, c, 0, version="pipeline-v2.4", ranked=True, published=True)
    resp = admin_client.post(f"/admin/runs/{r.id}/retract", data={"reason": REASON},
                             headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    resp = client.post(f"/admin/runs/{r.id}/retract", data={"reason": REASON}, follow_redirects=False)
    assert resp.status_code in (302, 303, 401)
    db.expire_all()
    assert db.get(JudgmentRun, r.id).retracted_at is None


def test_internal_retract_with_hermes_key(db, client):
    c = _company(db)
    a = _run(db, c, 0, version="pipeline-v2.4", ranked=True, published=True)
    b = _run(db, c, 1, version="pipeline-v2.4", ranked=True, published=True)
    assert client.post(f"/internal/runs/{b.id}/retract", json={"reason": REASON}).status_code == 401
    assert client.post(f"/internal/runs/{b.id}/retract", json={"reason": "x"}, headers=HERMES).status_code == 422
    r = client.post(f"/internal/runs/{b.id}/retract", json={"reason": REASON}, headers=HERMES)
    assert r.status_code == 200 and r.json()["current_run_id"] == a.id and r.json()["retracted_by"] == "hermes"


def test_only_completed_runs_can_be_retracted(db):
    c = _company(db)
    r = _run(db, c, 0, version="pipeline-v2.4", ranked=False, published=False, status="failed")
    with pytest.raises(publication.RetractError):
        publication.retract(db, r, "a", REASON)


# ---------------------------------------------------------------- at run time (new runs)

def test_new_run_on_newer_pipeline_publishes_over_ranked_run(db, monkeypatch):
    from app import main
    from app.db import SessionLocal
    from app.pipeline import methodology as meth
    from app.worker import Worker
    from tests import fakes
    from tests.test_publish_guard import _thin_llm
    c = _company(db, "nvidia")
    _run(db, c, 0, version="pipeline-v2.1", ranked=True, published=True, index=6.0)
    deps = fakes.make_deps(_thin_llm(), sec=False)
    monkeypatch.setattr(main, "worker", Worker(SessionLocal, lambda: deps))
    run, _ = main.runs_svc.enqueue_run(db, c.id, trigger="rerun", triggered_by="test")
    main.worker.process_next()
    db.expire_all()
    run = db.get(JudgmentRun, run.id)
    assert run.status == "succeeded" and not run.ranked and not run.degraded
    assert run.published and db.get(Company, c.id).current_run_id == run.id
    assert "newer pipeline" in run.summary["publish_note"] and meth.PIPELINE_VERSION in run.summary["publish_note"]
