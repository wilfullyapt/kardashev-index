"""Monthly read quotas for paid /api/v1. Hard 429 at the cap (no overage billing)."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import ApiCustomer, ApiEntitlement, ApiKey


def entitlement_for(db: Session, customer_id: int) -> ApiEntitlement | None:
    return db.query(ApiEntitlement).filter(ApiEntitlement.customer_id == customer_id).first()


def assert_may_read(db: Session, key: ApiKey) -> ApiEntitlement:
    ent = entitlement_for(db, key.customer_id)
    if ent is None or ent.status not in ("active", "past_due"):
        raise HTTPException(401, "Subscription inactive")
    if ent.status == "past_due":
        # Allow reads while Stripe retries payment, still under the monthly cap.
        pass
    if ent.reads_used >= ent.reads_per_month:
        raise HTTPException(
            429,
            detail={"error": "quota_exceeded",
                    "reads_used": ent.reads_used,
                    "reads_per_month": ent.reads_per_month,
                    "tier": ent.tier},
            headers={"Retry-After": "3600"},
        )
    return ent


def record_read(db: Session, key: ApiKey, ent: ApiEntitlement) -> None:
    ent.reads_used = (ent.reads_used or 0) + 1
    key.last_used_at = datetime.now(UTC)
    db.commit()


def quota_headers(ent: ApiEntitlement) -> dict[str, str]:
    remaining = max(0, (ent.reads_per_month or 0) - (ent.reads_used or 0))
    return {
        "X-RateLimit-Limit": str(ent.reads_per_month),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Tier": ent.tier,
    }


def customer_summary(db: Session, customer: ApiCustomer) -> dict:
    ent = entitlement_for(db, customer.id)
    keys = db.query(ApiKey).filter(ApiKey.customer_id == customer.id).all()
    return {
        "customer_id": customer.id,
        "email": customer.email,
        "tier": ent.tier if ent else None,
        "status": ent.status if ent else None,
        "reads_per_month": ent.reads_per_month if ent else None,
        "reads_used": ent.reads_used if ent else None,
        "keys": [{"id": k.id, "prefix": k.key_prefix, "status": k.status,
                  "label": k.label, "created_at": k.created_at.isoformat() if k.created_at else None}
                 for k in keys],
    }
