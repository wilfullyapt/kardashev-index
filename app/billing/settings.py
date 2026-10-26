"""Billing / paid-API feature flags and Stripe env. Defaults keep endpoints inert until go-live."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class Settings:
    api_v1_enabled: bool
    billing_enabled: bool
    api_key_pepper: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    price_explorer: str
    price_research: str
    price_operator: str
    public_base_url: str
    key_mode: str                     # live | test → ki_live_ / ki_test_


def settings() -> Settings:
    sk = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    mode = "test" if sk.startswith("sk_test_") or not sk else "live"
    if (os.getenv("API_KEY_MODE") or "").strip().lower() in ("live", "test"):
        mode = os.getenv("API_KEY_MODE").strip().lower()
    return Settings(
        api_v1_enabled=_truthy("API_V1_ENABLED", "0"),
        billing_enabled=_truthy("BILLING_ENABLED", "0"),
        api_key_pepper=(os.getenv("API_KEY_PEPPER") or "").strip(),
        stripe_secret_key=sk,
        stripe_webhook_secret=(os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip(),
        price_explorer=(os.getenv("STRIPE_PRICE_EXPLORER") or "").strip(),
        price_research=(os.getenv("STRIPE_PRICE_RESEARCH") or "").strip(),
        price_operator=(os.getenv("STRIPE_PRICE_OPERATOR") or "").strip(),
        public_base_url=(os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/"),
        key_mode=mode,
    )


def billing_configured(s: Settings | None = None) -> bool:
    """Secrets + at least one price id present (still needs BILLING_ENABLED to serve Checkout)."""
    s = s or settings()
    prices = (s.price_explorer, s.price_research, s.price_operator)
    return bool(s.stripe_secret_key and s.stripe_webhook_secret and s.api_key_pepper and any(prices))


def billing_enabled(s: Settings | None = None) -> bool:
    s = s or settings()
    return s.billing_enabled and billing_configured(s)


def api_v1_ready(s: Settings | None = None) -> bool:
    """Reads need the pepper (to verify keys) and the flag. Admin-minted keys work without Stripe."""
    s = s or settings()
    return s.api_v1_enabled and bool(s.api_key_pepper)


def price_id_for_tier(tier: str, s: Settings | None = None) -> str | None:
    s = s or settings()
    return {"explorer": s.price_explorer, "research": s.price_research,
            "operator": s.price_operator}.get((tier or "").lower()) or None


def tier_for_price_id(price_id: str, s: Settings | None = None) -> str | None:
    s = s or settings()
    mapping = {s.price_explorer: "explorer", s.price_research: "research",
               s.price_operator: "operator"}
    return mapping.get(price_id) if price_id else None


def status() -> dict:
    s = settings()
    return {
        "api_v1_enabled": s.api_v1_enabled,
        "api_v1_ready": api_v1_ready(s),
        "billing_enabled": s.billing_enabled,
        "billing_configured": billing_configured(s),
        "billing_ready": billing_enabled(s),
        "key_mode": s.key_mode,
        "prices_set": {
            "explorer": bool(s.price_explorer),
            "research": bool(s.price_research),
            "operator": bool(s.price_operator),
        },
    }
