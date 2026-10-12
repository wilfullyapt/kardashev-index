"""1.2.0: /health observability (runs by outcome, failing hosts, stale retry claims, archive budget)."""
from datetime import UTC, datetime, timedelta

import pytest

from app import observability
from app.models import Company, JudgmentRun, Source


@pytest.fixture(autouse=True)
def _fresh():
    observability.reset_cache()
    yield
    observability.reset_cache()


def _run(db, c, **kw):
    now = datetime.now(UTC)
    r = JudgmentRun(company_id=c.id, status=kw.pop("status", "succeeded"), trigger="test", triggered_by="t",
                    finished_at=kw.pop("finished_at", now), pipeline_version="pipeline-v2.9", **kw)
    db.add(r)
    db.commit()
    return r


def _company(db, name):
    c = Company(canonical_name=name, official_name=name.title(), industry="X")
    db.add(c)
    db.commit()
    return c


def test_runs_by_outcome_and_window(db):
    c = _company(db, "acme")
    _run(db, c, published=True, ranked=True, coverage=0.8)
    _run(db, c, published=False, ranked=False, degraded=["extract: x"],
         summary={"energy_unreadable": [{"url": "u"}]})
    _run(db, c, status="failed", error_type="internal_error", published=False)
    _run(db, c, status="failed", error_type="old", finished_at=datetime.now(UTC) - timedelta(days=30))
    out = observability.collect(db)
    assert out["runs_7d"] == {"succeeded": 2, "degraded": 1, "energy_unreadable": 1, "withheld": 1,
                              "failed:internal_error": 1}


def test_failing_hosts_with_status_and_archive_outcomes(db):
    c = _company(db, "tsmc")
    r = _run(db, c, published=True)
    for url, status, http, att in [
        ("https://www.tsmc.com/a.pdf", "dead", 403, {"outcome": "rate_limited"}),
        ("https://www.tsmc.com/b", "dead", 403, {"outcome": "not_found"}),
        ("https://esg.tsmc.com/c", "thin", 200, None),
        ("https://ok.example/d", "ok", 200, None),
    ]:
        db.add(Source(run_id=r.id, url=url, origin="research", status=status, http_status=http, archive_attempt=att))
    db.commit()
    hosts = {h["host"]: h for h in observability.collect(db)["hosts_7d"]}
    assert hosts["tsmc.com"]["failed"] == 2 and hosts["tsmc.com"]["dead:403"] == 2
    assert hosts["tsmc.com"]["archive:rate_limited"] == 1 and hosts["esg.tsmc.com"]["thin"] == 1
    assert "ok.example" not in hosts


def test_retry_claim_without_queued_run_is_flagged(db):
    c = _company(db, "tesla")
    r = _run(db, c, published=True, ranked=False, coverage=0.0,
             summary={"not_ranked_reason": "energy source found but couldn't be read (4: HTTP 403); "
                                           "retry scheduled for Oct 13, 09:00 PT"})
    out = observability.collect(db)
    assert out["retry_claims_without_queued_run"] == [{"company_id": c.id, "name": "tesla", "run_id": r.id}]
    db.add(JudgmentRun(company_id=c.id, status="queued", trigger="energy_retry", triggered_by="system",
                       next_attempt_at=datetime.now(UTC) + timedelta(hours=20)))
    db.commit()
    assert observability.collect(db)["retry_claims_without_queued_run"] == []


def test_legacy_suffix_is_not_a_claim(db):
    c = _company(db, "legacy")
    _run(db, c, published=True, ranked=False, coverage=0.0,
         summary={"not_ranked_reason": "energy source found but couldn't be read (4: HTTP 403); retry scheduled"})
    assert observability.collect(db)["retry_claims_without_queued_run"] == []


def test_health_includes_observability_and_caches(client, monkeypatch):
    body = client.get("/health").json()
    obs = body["observability"]
    assert set(obs) >= {"runs_7d", "hosts_7d", "retry_claims_without_queued_run", "archive"}
    calls = []
    monkeypatch.setattr(observability, "collect", lambda db: calls.append(1) or {"x": 1})
    client.get("/health")
    assert calls == []            # served from the cache


def test_observability_errors_never_break_health(client, monkeypatch):
    def boom(db):
        raise RuntimeError("x")
    monkeypatch.setattr(observability, "collect", boom)
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["observability"] == {"error": "RuntimeError"}
