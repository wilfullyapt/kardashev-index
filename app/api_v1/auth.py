"""Bearer / X-Api-Key auth for /api/v1. Hermes key is NOT accepted here."""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .. import api_keys, api_quota
from ..billing.settings import api_v1_ready
from ..db import get_db
from ..models import ApiEntitlement, ApiKey


@dataclass
class ApiPrincipal:
    key: ApiKey
    entitlement: ApiEntitlement


def _is_hermes_key(raw: str) -> bool:
    hermes = os.getenv("HERMES_API_KEY") or ""
    if not hermes or not raw:
        return False
    return hmac.compare_digest(raw.encode(), hermes.encode())


def require_api_key(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-Api-Key"),
) -> ApiPrincipal:
    if not api_v1_ready():
        raise HTTPException(503, "Paid API is not enabled")
    raw = api_keys.extract_bearer(authorization, x_api_key)
    if not raw:
        raise HTTPException(401, "Missing API key (Authorization: Bearer … or X-Api-Key)")
    # Never accept the Hermes operator key on the paid surface.
    if _is_hermes_key(raw):
        raise HTTPException(401, "Invalid API key")
    key = api_keys.find_active_key(db, raw)
    if key is None:
        raise HTTPException(401, "Invalid API key")
    ent = api_quota.assert_may_read(db, key)
    return ApiPrincipal(key=key, entitlement=ent)
