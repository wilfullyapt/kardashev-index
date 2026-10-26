"""Stripe webhook handlers. Idempotent via stripe_events. Mint/revoke keys; never touch publication."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from .. import api_keys
from ..models import ApiCustomer, ApiEntitlement, ApiKey, StripeEvent
from .settings import tier_for_price_id

log = logging.getLogger("kardashev.billing.webhooks")

HANDLED = {
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
}


def _ts(seconds: int | None) -> datetime | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(int(seconds), tz=UTC)


def _already(db: Session, event_id: str) -> bool:
    return db.query(StripeEvent).filter(StripeEvent.id == event_id).first() is not None


def _record(db: Session, event: Any) -> StripeEvent:
    row = StripeEvent(id=event["id"], type=event["type"],
                      received_at=datetime.now(UTC),
                      payload={"id": event["id"], "type": event["type"]})
    db.add(row)
    db.flush()
    return row


def process_event(db: Session, event: Any) -> dict:
    """Process one verified Stripe event. Safe to call twice (second is a no-op)."""
    eid = event["id"]
    etype = event["type"]
    if _already(db, eid):
        return {"status": "duplicate", "id": eid, "type": etype}
    row = _record(db, event)
    result: dict = {"status": "ignored", "id": eid, "type": etype}
    try:
        if etype == "checkout.session.completed":
            result = _checkout_completed(db, event["data"]["object"])
        elif etype == "customer.subscription.updated":
            result = _subscription_updated(db, event["data"]["object"])
        elif etype == "customer.subscription.deleted":
            result = _subscription_deleted(db, event["data"]["object"])
        elif etype == "invoice.paid":
            result = _invoice_paid(db, event["data"]["object"])
        elif etype == "invoice.payment_failed":
            result = _invoice_failed(db, event["data"]["object"])
        row.processed_at = datetime.now(UTC)
        db.commit()
        result["status"] = result.get("status") or "ok"
        return result
    except Exception:
        db.rollback()
        log.exception("stripe webhook failed type=%s id=%s", etype, eid)
        raise


def _checkout_completed(db: Session, session: dict) -> dict:
    if session.get("mode") != "subscription":
        return {"status": "skipped", "reason": "not_subscription"}
    email = (session.get("customer_details") or {}).get("email") or session.get("customer_email")
    stripe_customer_id = session.get("customer")
    stripe_sub_id = session.get("subscription")
    meta = session.get("metadata") or {}
    tier = (meta.get("tier") or "").lower() or "explorer"
    if not email and not stripe_customer_id:
        return {"status": "skipped", "reason": "no_customer"}
    cust = api_keys.ensure_customer(
        db, email=email or f"{stripe_customer_id}@stripe.local",
        stripe_customer_id=stripe_customer_id)
    if stripe_sub_id:
        cust.stripe_subscription_id = stripe_sub_id
    api_keys.upsert_entitlement(db, cust, tier=tier, status="active",
                                period_start=datetime.now(UTC))
    existing = (db.query(ApiKey)
                .filter(ApiKey.stripe_checkout_session_id == session["id"]).first())
    if existing:
        return {"status": "ok", "customer_id": cust.id, "key_id": existing.id, "minted": False}
    key, _raw = api_keys.mint_key(db, cust, label=f"{tier}-checkout",
                                  checkout_session_id=session["id"], keep_plaintext=True)
    log.info("minted api key prefix=%s customer=%s tier=%s", key.key_prefix, cust.id, tier)
    return {"status": "ok", "customer_id": cust.id, "key_id": key.id, "minted": True, "tier": tier}


def _subscription_updated(db: Session, sub: dict) -> dict:
    cust = db.query(ApiCustomer).filter(
        ApiCustomer.stripe_subscription_id == sub["id"]).first()
    if cust is None:
        cust = db.query(ApiCustomer).filter(
            ApiCustomer.stripe_customer_id == sub.get("customer")).first()
    if cust is None:
        return {"status": "skipped", "reason": "unknown_customer"}
    cust.stripe_subscription_id = sub["id"]
    items = ((sub.get("items") or {}).get("data") or [])
    price_id = (items[0].get("price") or {}).get("id") if items else None
    tier = (sub.get("metadata") or {}).get("tier") or tier_for_price_id(price_id or "") or "explorer"
    status_map = {
        "active": "active", "trialing": "active", "past_due": "past_due",
        "canceled": "canceled", "unpaid": "past_due",
        "incomplete": "past_due", "incomplete_expired": "canceled",
    }
    st = status_map.get(sub.get("status"), "active")
    api_keys.upsert_entitlement(
        db, cust, tier=tier, status=st, stripe_price_id=price_id,
        period_start=_ts(sub.get("current_period_start")),
        period_end=_ts(sub.get("current_period_end")),
    )
    if st == "canceled":
        n = api_keys.revoke_customer_keys(db, cust.id)
        return {"status": "ok", "revoked": n, "tier": tier, "sub_status": st}
    return {"status": "ok", "tier": tier, "sub_status": st}


def _subscription_deleted(db: Session, sub: dict) -> dict:
    cust = db.query(ApiCustomer).filter(
        ApiCustomer.stripe_subscription_id == sub["id"]).first()
    if cust is None:
        cust = db.query(ApiCustomer).filter(
            ApiCustomer.stripe_customer_id == sub.get("customer")).first()
    if cust is None:
        return {"status": "skipped", "reason": "unknown_customer"}
    ent = api_keys.upsert_entitlement(db, cust, tier="explorer", status="canceled")
    ent.status = "canceled"
    n = api_keys.revoke_customer_keys(db, cust.id)
    log.info("subscription deleted customer=%s revoked_keys=%s", cust.id, n)
    return {"status": "ok", "revoked": n}


def _invoice_paid(db: Session, invoice: dict) -> dict:
    cust = db.query(ApiCustomer).filter(
        ApiCustomer.stripe_customer_id == invoice.get("customer")).first()
    if cust is None:
        return {"status": "skipped", "reason": "unknown_customer"}
    ent = db.query(ApiEntitlement).filter(ApiEntitlement.customer_id == cust.id).first()
    if ent and ent.status == "past_due":
        ent.status = "active"
    return {"status": "ok"}


def _invoice_failed(db: Session, invoice: dict) -> dict:
    cust = db.query(ApiCustomer).filter(
        ApiCustomer.stripe_customer_id == invoice.get("customer")).first()
    if cust is None:
        return {"status": "skipped", "reason": "unknown_customer"}
    ent = db.query(ApiEntitlement).filter(ApiEntitlement.customer_id == cust.id).first()
    if ent and ent.status == "active":
        ent.status = "past_due"
    return {"status": "ok", "marked": "past_due"}
