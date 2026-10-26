"""Hashed paid-API keys (ki_live_ / ki_test_). Hermes keys stay separate (HERMES_API_KEY)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .billing.settings import settings as billing_settings
from .billing.tiers import TIERS, tier_or_none
from .models import ApiCustomer, ApiEntitlement, ApiKey

log = logging.getLogger("kardashev.api_keys")

_KEY_RE = re.compile(r"^ki_(live|test)_[A-Za-z0-9_-]{24,}$")
PREFIX_VISIBLE = 12  # ki_live_abcd…


def hash_key(raw: str, pepper: str | None = None) -> str:
    pepper = pepper if pepper is not None else billing_settings().api_key_pepper
    if not pepper:
        raise RuntimeError("API_KEY_PEPPER is not set")
    return hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()


def mint_raw_key(*, mode: str | None = None) -> str:
    mode = mode or billing_settings().key_mode
    if mode not in ("live", "test"):
        mode = "test"
    return f"ki_{mode}_{secrets.token_urlsafe(32)}"


def key_prefix(raw: str) -> str:
    return raw[:PREFIX_VISIBLE]


def looks_like_key(raw: str | None) -> bool:
    return bool(raw and _KEY_RE.match(raw.strip()))


def extract_bearer(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return None


def mint_key(db: Session, customer: ApiCustomer, *, label: str | None = None,
             checkout_session_id: str | None = None, keep_plaintext: bool = True) -> tuple[ApiKey, str]:
    """Create an active key. Returns (row, plaintext). Plaintext is stored once for the success page
    when keep_plaintext is True, then cleared on reveal."""
    raw = mint_raw_key()
    row = ApiKey(
        customer_id=customer.id,
        key_prefix=key_prefix(raw),
        key_hash=hash_key(raw),
        label=label,
        status="active",
        plaintext_once=raw if keep_plaintext else None,
        stripe_checkout_session_id=checkout_session_id,
    )
    db.add(row)
    db.flush()
    return row, raw


def revoke_key(db: Session, key: ApiKey, *, when: datetime | None = None) -> None:
    key.status = "revoked"
    key.revoked_at = when or datetime.now(UTC)
    key.plaintext_once = None


def revoke_customer_keys(db: Session, customer_id: int) -> int:
    n = 0
    for k in db.query(ApiKey).filter(ApiKey.customer_id == customer_id, ApiKey.status == "active"):
        revoke_key(db, k)
        n += 1
    return n


def find_active_key(db: Session, raw: str) -> ApiKey | None:
    if not looks_like_key(raw):
        return None
    try:
        digest = hash_key(raw)
    except RuntimeError:
        return None
    return (db.query(ApiKey)
            .filter(ApiKey.key_hash == digest, ApiKey.status == "active")
            .first())


def reveal_once(db: Session, checkout_session_id: str) -> str | None:
    """Return plaintext for a Checkout session and clear it. None if already revealed / missing."""
    key = (db.query(ApiKey)
           .filter(ApiKey.stripe_checkout_session_id == checkout_session_id,
                   ApiKey.status == "active")
           .order_by(ApiKey.id.desc()).first())
    if not key or not key.plaintext_once:
        return None
    raw = key.plaintext_once
    key.plaintext_once = None
    db.commit()
    return raw


def ensure_customer(db: Session, *, email: str, stripe_customer_id: str | None = None) -> ApiCustomer:
    email = email.strip().lower()
    cust = None
    if stripe_customer_id:
        cust = db.query(ApiCustomer).filter(ApiCustomer.stripe_customer_id == stripe_customer_id).first()
    if cust is None:
        cust = db.query(ApiCustomer).filter(ApiCustomer.email == email).first()
    if cust is None:
        cust = ApiCustomer(email=email, stripe_customer_id=stripe_customer_id)
        db.add(cust)
        db.flush()
    else:
        if stripe_customer_id and not cust.stripe_customer_id:
            cust.stripe_customer_id = stripe_customer_id
        if email and cust.email != email:
            cust.email = email
        cust.updated_at = datetime.now(UTC)
    return cust


def upsert_entitlement(db: Session, customer: ApiCustomer, *, tier: str,
                       status: str = "active", stripe_price_id: str | None = None,
                       period_start: datetime | None = None,
                       period_end: datetime | None = None) -> ApiEntitlement:
    t = tier_or_none(tier) or TIERS["explorer"]
    ent = db.query(ApiEntitlement).filter(ApiEntitlement.customer_id == customer.id).first()
    if ent is None:
        ent = ApiEntitlement(customer_id=customer.id, tier=t.key,
                             reads_per_month=t.reads_per_month,
                             ghost_runs_per_month=t.ghost_runs_per_month,
                             reads_used=0, status=status)
        db.add(ent)
    else:
        # Tier change resets the counter only when the monthly allowance changes.
        if ent.reads_per_month != t.reads_per_month:
            ent.reads_used = 0
        ent.tier = t.key
        ent.reads_per_month = t.reads_per_month
        ent.ghost_runs_per_month = t.ghost_runs_per_month
        ent.status = status
    ent.stripe_price_id = stripe_price_id
    if period_start is not None and (ent.period_start is None or period_start > ent.period_start):
        # New billing period → reset usage.
        if ent.period_start is not None and period_start > ent.period_start:
            ent.reads_used = 0
        ent.period_start = period_start
    if period_end is not None:
        ent.period_end = period_end
    ent.updated_at = datetime.now(UTC)
    db.flush()
    return ent


def admin_mint(db: Session, *, email: str, tier: str = "enterprise",
               label: str | None = "admin-mint") -> tuple[ApiCustomer, ApiKey, str]:
    """Mint a key without Stripe (Enterprise / dogfood)."""
    t = tier_or_none(tier)
    if t is None and tier != "enterprise":
        raise ValueError(f"unknown tier {tier!r}")
    cust = ensure_customer(db, email=email)
    if t is None:
        # enterprise: custom — use Operator quotas as a safe default until Wil sets custom.
        reads, ghosts = 500_000, 20
        tier_key = "enterprise"
    else:
        reads, ghosts, tier_key = t.reads_per_month, t.ghost_runs_per_month, t.key
    ent = db.query(ApiEntitlement).filter(ApiEntitlement.customer_id == cust.id).first()
    if ent is None:
        ent = ApiEntitlement(customer_id=cust.id, tier=tier_key, status="active",
                             reads_per_month=reads, ghost_runs_per_month=ghosts, reads_used=0)
        db.add(ent)
    else:
        ent.tier = tier_key
        ent.reads_per_month = reads
        ent.ghost_runs_per_month = ghosts
        ent.status = "active"
    key, raw = mint_key(db, cust, label=label, keep_plaintext=False)
    db.commit()
    return cust, key, raw
