"""Public pricing / Checkout / success / portal + Stripe webhook + admin key mint."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import api_keys
from ..db import get_db
from ..models import ApiCustomer, ApiKey
from ..version import __version__
from . import stripe_client, webhooks
from .settings import billing_enabled, settings as billing_settings, status as billing_status
from .tiers import tier_or_none

log = logging.getLogger("kardashev.billing.routes")
router = APIRouter(tags=["billing"])


def _templates():
    from ..main import templates
    return templates


def _base(request: Request) -> str:
    s = billing_settings()
    if s.public_base_url:
        return s.public_base_url
    return str(request.base_url).rstrip("/")


@router.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    tiers = stripe_client.public_pricing()
    return _templates().TemplateResponse(request, "billing/pricing.html", {
        "request": request, "tiers": tiers, "billing": billing_status(),
        "version": __version__,
    })


@router.post("/billing/checkout")
def start_checkout(request: Request, tier: str = Form(...), email: str = Form(""),
                   db: Session = Depends(get_db)):
    if not billing_enabled():
        raise HTTPException(503, "Billing is not enabled")
    t = tier_or_none(tier)
    if t is None:
        raise HTTPException(400, "Unknown tier")
    base = _base(request)
    try:
        session = stripe_client.create_checkout_session(
            tier=t.key,
            customer_email=email.strip() or None,
            success_url=base + "/pricing/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base + "/pricing?canceled=1",
        )
    except Exception as e:
        log.exception("checkout create failed")
        raise HTTPException(502, f"Could not start Checkout: {e}") from e
    return RedirectResponse(session.url, status_code=303)


@router.get("/pricing/success", response_class=HTMLResponse)
def pricing_success(request: Request, session_id: str = "", db: Session = Depends(get_db)):
    raw = api_keys.reveal_once(db, session_id) if session_id else None
    return _templates().TemplateResponse(request, "billing/success.html", {
        "request": request, "api_key": raw, "session_id": session_id,
        "version": __version__,
    })


@router.post("/billing/portal")
def customer_portal(request: Request, customer_id: str = Form(""), email: str = Form(""),
                    db: Session = Depends(get_db)):
    if not billing_enabled():
        raise HTTPException(503, "Billing is not enabled")
    cust = None
    if customer_id:
        cust = db.query(ApiCustomer).filter(ApiCustomer.stripe_customer_id == customer_id).first()
    if cust is None and email:
        cust = db.query(ApiCustomer).filter(ApiCustomer.email == email.strip().lower()).first()
    if cust is None or not cust.stripe_customer_id:
        raise HTTPException(404, "No billing customer found")
    try:
        session = stripe_client.create_portal_session(
            stripe_customer_id=cust.stripe_customer_id,
            return_url=_base(request) + "/pricing",
        )
    except Exception as e:
        log.exception("portal create failed")
        raise HTTPException(502, f"Could not open Customer Portal: {e}") from e
    return RedirectResponse(session.url, status_code=303)


@router.post("/billing/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe → us. Signature required. Feature flag does not block webhooks (so test mode works
    before BILLING_ENABLED flips); missing secret still 503s."""
    s = billing_settings()
    if not s.stripe_webhook_secret or not s.stripe_secret_key or not s.api_key_pepper:
        raise HTTPException(503, "Stripe webhook not configured")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe_client.construct_event(payload, sig, s)
    except Exception as e:
        log.warning("webhook signature failed: %s", e)
        raise HTTPException(400, "Invalid signature") from e
    result = webhooks.process_event(db, event)
    return JSONResponse(result)


# ---- Admin mint / revoke (session required; wired via main.require) ----

admin_router = APIRouter(prefix="/admin/api-keys", tags=["admin-api-keys"])


@admin_router.post("/mint")
def admin_mint_key(request: Request, email: str = Form(...), tier: str = Form("enterprise"),
                   label: str = Form("admin-mint"), db: Session = Depends(get_db)):
    from ..main import get_current_admin, flash
    get_current_admin(request)
    if not billing_settings().api_key_pepper:
        raise HTTPException(503, "API_KEY_PEPPER is not set")
    try:
        cust, key, raw = api_keys.admin_mint(db, email=email, tier=tier, label=label)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # Show once via flash (admin HTML). JSON clients get the body.
    if "text/html" in request.headers.get("accept", ""):
        flash(request, f"Minted {key.key_prefix}… for {cust.email}. Copy now: {raw}", "success")
        return RedirectResponse("/admin", status_code=303)
    return {"customer_id": cust.id, "key_id": key.id, "prefix": key.key_prefix,
            "api_key": raw, "tier": tier, "email": cust.email}


@admin_router.post("/{key_id}/revoke")
def admin_revoke_key(key_id: int, request: Request, db: Session = Depends(get_db)):
    from ..main import get_current_admin, flash
    get_current_admin(request)
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(404, "Key not found")
    api_keys.revoke_key(db, key)
    db.commit()
    if "text/html" in request.headers.get("accept", ""):
        flash(request, f"Revoked key {key.key_prefix}", "success")
        return RedirectResponse("/admin", status_code=303)
    return {"status": "revoked", "key_id": key.id, "prefix": key.key_prefix}
