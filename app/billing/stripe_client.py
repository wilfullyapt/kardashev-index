"""Thin Stripe wrappers. No Stripe.js — Checkout and Portal are server-created redirects."""
from __future__ import annotations

import logging
from typing import Any

import stripe

from .settings import Settings, price_id_for_tier, settings as get_settings
from .tiers import TIER_ORDER, TIERS

log = logging.getLogger("kardashev.billing.stripe")


def configure(s: Settings | None = None) -> Settings:
    s = s or get_settings()
    if not s.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set")
    stripe.api_key = s.stripe_secret_key
    return s


def create_checkout_session(*, tier: str, customer_email: str | None,
                            success_url: str, cancel_url: str,
                            s: Settings | None = None) -> Any:
    s = configure(s)
    price = price_id_for_tier(tier, s)
    if not price:
        raise ValueError(f"No Stripe price configured for tier {tier!r}")
    if tier not in TIERS:
        raise ValueError(f"Unknown tier {tier!r}")
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "allow_promotion_codes": True,
        "metadata": {"tier": tier, "product": "kardashev-api"},
        "subscription_data": {"metadata": {"tier": tier, "product": "kardashev-api"}},
    }
    if customer_email:
        params["customer_email"] = customer_email
    return stripe.checkout.Session.create(**params)


def create_portal_session(*, stripe_customer_id: str, return_url: str,
                          s: Settings | None = None) -> Any:
    configure(s)
    return stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url,
    )


def construct_event(payload: bytes, sig_header: str, s: Settings | None = None) -> Any:
    s = s or get_settings()
    if not s.stripe_webhook_secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not set")
    return stripe.Webhook.construct_event(payload, sig_header, s.stripe_webhook_secret)


def public_pricing(s: Settings | None = None) -> list[dict]:
    """Tiers for the /pricing page. price_id present only when env is set (never secret keys)."""
    s = s or get_settings()
    out = []
    for key in TIER_ORDER:
        t = TIERS[key]
        out.append({
            "key": t.key,
            "name": t.name,
            "price_usd_monthly": t.price_usd_monthly,
            "reads_per_month": t.reads_per_month,
            "ghost_runs_per_month": t.ghost_runs_per_month,
            "blurb": t.blurb,
            "checkout_available": bool(price_id_for_tier(key, s)),
            "placeholder": True,
        })
    return out
