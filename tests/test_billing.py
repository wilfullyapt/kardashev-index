"""Billing pages, feature flags, webhook mint/revoke (no live Stripe)."""
from __future__ import annotations

import pytest

from app import api_keys
from app.billing import webhooks
from app.models import ApiCustomer, ApiEntitlement, ApiKey, StripeEvent


@pytest.fixture
def pepper(monkeypatch):
    monkeypatch.setenv("API_KEY_PEPPER", "test-pepper-not-for-prod")
    monkeypatch.setenv("API_V1_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENABLED", "0")
    yield


def test_pricing_page_placeholders(client, pepper):
    r = client.get("/pricing")
    assert r.status_code == 200
    assert b"PLACEHOLDER" in r.content
    assert b"Explorer" in r.content
    assert b"$49" in r.content
    assert b"Not live" in r.content or b"not enabled" in r.content.lower()


def test_terms_pages(client):
    assert client.get("/terms/api").status_code == 200
    assert client.get("/terms/refunds").status_code == 200
    assert b"Paying never changes" in client.get("/terms/api").content


def test_checkout_503_when_billing_off(client, pepper):
    r = client.post("/billing/checkout", data={"tier": "explorer", "email": "a@b.co"},
                    follow_redirects=False)
    assert r.status_code == 503


def test_webhook_503_without_secrets(client, pepper):
    r = client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 503


def _evt(eid: str, etype: str, obj: dict) -> dict:
    return {"id": eid, "type": etype, "data": {"object": obj}}


def test_checkout_completed_mints_key(db, pepper):
    session = {
        "id": "cs_test_1",
        "mode": "subscription",
        "customer": "cus_1",
        "subscription": "sub_1",
        "customer_email": "pay@example.com",
        "customer_details": {"email": "pay@example.com"},
        "metadata": {"tier": "research"},
    }
    result = webhooks.process_event(db, _evt("evt_1", "checkout.session.completed", session))
    assert result["status"] == "ok"
    assert result["minted"] is True
    cust = db.query(ApiCustomer).filter(ApiCustomer.email == "pay@example.com").one()
    ent = db.query(ApiEntitlement).filter(ApiEntitlement.customer_id == cust.id).one()
    assert ent.tier == "research"
    assert ent.reads_per_month == 100_000
    key = db.query(ApiKey).filter(ApiKey.customer_id == cust.id).one()
    assert key.status == "active"
    assert key.plaintext_once  # still available for success page
    assert key.stripe_checkout_session_id == "cs_test_1"
    # reveal once
    raw = api_keys.reveal_once(db, "cs_test_1")
    assert raw and raw.startswith("ki_")
    assert api_keys.reveal_once(db, "cs_test_1") is None


def test_webhook_idempotent(db, pepper):
    session = {
        "id": "cs_test_2", "mode": "subscription", "customer": "cus_2",
        "subscription": "sub_2", "customer_email": "idemp@example.com",
        "customer_details": {"email": "idemp@example.com"},
        "metadata": {"tier": "explorer"},
    }
    e = _evt("evt_dup", "checkout.session.completed", session)
    assert webhooks.process_event(db, e)["minted"] is True
    assert webhooks.process_event(db, e)["status"] == "duplicate"
    assert db.query(ApiKey).count() == 1
    assert db.query(StripeEvent).count() == 1


def test_subscription_deleted_revokes(db, pepper):
    cust, key, raw = api_keys.admin_mint(db, email="gone@example.com", tier="explorer")
    cust.stripe_customer_id = "cus_gone"
    cust.stripe_subscription_id = "sub_gone"
    db.commit()
    result = webhooks.process_event(db, _evt(
        "evt_del", "customer.subscription.deleted",
        {"id": "sub_gone", "customer": "cus_gone"},
    ))
    assert result["status"] == "ok"
    assert result["revoked"] >= 1
    db.refresh(key)
    assert key.status == "revoked"
    assert api_keys.find_active_key(db, raw) is None


def test_admin_mint_and_revoke(admin_client, db, pepper):
    r = admin_client.post("/admin/api-keys/mint",
                          data={"email": "ent@example.com", "tier": "enterprise", "label": "dog"},
                          headers={"Accept": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["api_key"].startswith("ki_")
    kid = body["key_id"]
    r2 = admin_client.post(f"/admin/api-keys/{kid}/revoke",
                           headers={"Accept": "application/json"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "revoked"


def test_privacy_mentions_stripe(client):
    r = client.get("/privacy")
    assert r.status_code == 200
    assert b"Stripe" in r.content


def test_about_links_pricing(client):
    r = client.get("/about")
    assert b"/pricing" in r.content
    assert b"Paying never changes" in r.content
