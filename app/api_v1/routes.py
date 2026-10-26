"""/api/v1 JSON reads of already-public published data."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from .. import api_quota
from ..billing.settings import api_v1_ready, status as billing_status
from ..db import get_db
from ..models import Company
from .auth import ApiPrincipal, require_api_key
from . import serialize

router = APIRouter(prefix="/api/v1", tags=["api-v1"])


def _gate():
    if not api_v1_ready():
        raise HTTPException(503, "Paid API is not enabled (set API_V1_ENABLED=1 and API_KEY_PEPPER)")


@router.get("/meta")
def meta(response: Response, principal: ApiPrincipal = Depends(require_api_key),
         db: Session = Depends(get_db)):
    _gate()
    api_quota.record_read(db, principal.key, principal.entitlement)
    for k, v in api_quota.quota_headers(principal.entitlement).items():
        response.headers[k] = v
    body = serialize.meta_payload()
    body["billing"] = billing_status()
    return body


@router.get("/leaderboard")
def leaderboard(response: Response,
                q: str | None = None,
                limit: int = Query(100, ge=1, le=500),
                include_unranked: bool = True,
                principal: ApiPrincipal = Depends(require_api_key),
                db: Session = Depends(get_db)):
    _gate()
    api_quota.record_read(db, principal.key, principal.entitlement)
    for k, v in api_quota.quota_headers(principal.entitlement).items():
        response.headers[k] = v
    return serialize.leaderboard_payload(db, limit=limit, q=q, include_unranked=include_unranked)


@router.get("/companies/{company_id}")
def company(company_id: int, response: Response,
            principal: ApiPrincipal = Depends(require_api_key),
            db: Session = Depends(get_db)):
    _gate()
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    api_quota.record_read(db, principal.key, principal.entitlement)
    for k, v in api_quota.quota_headers(principal.entitlement).items():
        response.headers[k] = v
    return serialize.company_payload(db, company)


@router.get("/companies/{company_id}/history")
def company_history(company_id: int, response: Response,
                    principal: ApiPrincipal = Depends(require_api_key),
                    db: Session = Depends(get_db)):
    _gate()
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    api_quota.record_read(db, principal.key, principal.entitlement)
    for k, v in api_quota.quota_headers(principal.entitlement).items():
        response.headers[k] = v
    return serialize.history_payload(db, company)
