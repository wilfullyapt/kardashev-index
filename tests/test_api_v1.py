"""Paid /api/v1: feature flags, auth, Hermes rejection, quotas, public JSON shape."""
from __future__ import annotations

import os

import pytest

from app import api_keys, main
from app.models import Company, JudgmentRun


@pytest.fixture
def api_on(monkeypatch):
    monkeypatch.setenv("API_V1_ENABLED", "1")
    monkeypatch.setenv("API_KEY_PEPPER", "test-pepper-not-for-prod")
    monkeypatch.setenv("BILLING_ENABLED", "0")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    yield


@pytest.fixture
def minted(api_on, db):
    cust, key, raw = api_keys.admin_mint(db, email="buyer@example.com", tier="explorer", label="t")
    return cust, key, raw


def test_api_disabled_without_flag(client, monkeypatch):
    monkeypatch.setenv("API_V1_ENABLED", "0")
    monkeypatch.setenv("API_KEY_PEPPER", "test-pepper-not-for-prod")
    r = client.get("/api/v1/meta", headers={"X-Api-Key": "ki_test_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"})
    assert r.status_code == 503


def test_hermes_key_rejected(api_on, client):
    r = client.get("/api/v1/meta", headers={"X-Api-Key": os.environ["HERMES_API_KEY"]})
    assert r.status_code == 401


def test_missing_key_401(api_on, client):
    assert client.get("/api/v1/meta").status_code == 401


def test_meta_and_quota_headers(api_on, client, minted):
    _, _, raw = minted
    r = client.get("/api/v1/meta", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "kardashev-index"
    assert body["version"] == main.__version__
    assert "X-RateLimit-Limit" in r.headers
    assert r.headers["X-RateLimit-Tier"] == "explorer"
    assert int(r.headers["X-RateLimit-Remaining"]) == 9999  # 10000 - 1


def test_leaderboard_and_company(api_on, client, db, minted):
    _, _, raw = minted
    co = Company(canonical_name="acme-ai", official_name="Acme AI", domain="acme.example",
                 industry="AI", suggestion_attempts=0)
    db.add(co)
    db.flush()
    run = JudgmentRun(
        company_id=co.id, status="succeeded", published=True, ranked=True,
        index_score=4.2, measured_score=3.0, judged_score=5.0, confidence=0.8,
        coverage=0.9, pipeline_version="v2.16", prompt_version="v2.x",
        rubric_version="v1", weights_version="v1", model="test", attempt=1,
        trigger="test", triggered_by="t",
    )
    db.add(run)
    db.flush()
    co.current_run_id = run.id
    db.commit()

    h = {"X-Api-Key": raw}
    lb = client.get("/api/v1/leaderboard", headers=h)
    assert lb.status_code == 200
    data = lb.json()
    assert data["count_ranked"] + data["count_unranked"] >= 1

    detail = client.get(f"/api/v1/companies/{co.id}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["canonical_name"] == "acme-ai"
    assert detail.json()["current_run"]["run_id"] == run.id

    hist = client.get(f"/api/v1/companies/{co.id}/history", headers=h)
    assert hist.status_code == 200
    assert hist.json()["company_id"] == co.id


def test_quota_429(api_on, client, db, minted):
    cust, _key, raw = minted
    from app.models import ApiEntitlement
    ent = db.query(ApiEntitlement).filter(ApiEntitlement.customer_id == cust.id).one()
    ent.reads_used = ent.reads_per_month
    db.commit()
    r = client.get("/api/v1/meta", headers={"X-Api-Key": raw})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "quota_exceeded"


def test_revoked_key_401(api_on, client, db, minted):
    _, key, raw = minted
    api_keys.revoke_key(db, key)
    db.commit()
    assert client.get("/api/v1/meta", headers={"X-Api-Key": raw}).status_code == 401


def test_health_billing_block(client):
    r = client.get("/health")
    assert r.status_code == 200
    b = r.json()["billing"]
    assert b["api_v1_enabled"] is False
    assert b["billing_ready"] is False
    assert "prices_set" in b
