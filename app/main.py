from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from sqlalchemy.orm import Session
from passlib.context import CryptContext
import os
from dotenv import load_dotenv
from .db import get_db
from .models import Company, Suggestion, Score, ScoreHistory, IngestLog, CATEGORIES  # noqa: F401 (re-exported)
from . import judgment as jm
from .judgment import JudgmentError, JudgmentResult, summarize_scores, is_placeholder
from datetime import datetime, UTC
from openai import OpenAI
from sqlalchemy.exc import IntegrityError
from collections import defaultdict
import hmac
import threading
import rapidfuzz

load_dotenv()

# App versioning
__version__ = "v0.1"

app = FastAPI(title="Kardashev Index")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static"), name="static")

# Admin auth
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Grok / xAI client (OpenAI compatible). Explicit timeout + bounded transport retries.
XAI_API_KEY = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")
XAI_MODEL = jm.XAI_MODEL  # pinned via env XAI_MODEL (default "grok-4", the previous hard-coded value)
grok_client = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1",
    timeout=jm.XAI_TIMEOUT_S,
    max_retries=jm.XAI_MAX_RETRIES,
) if XAI_API_KEY else None

# Hermes integration access (key via `X-Hermes-Key` header or `?hermes_key=` query param)
HERMES_API_KEY = os.getenv("HERMES_API_KEY")


def _hermes_key_from(request: Request | None, hermes_key: str | None) -> str | None:
    header_key = request.headers.get("x-hermes-key") if request is not None else None
    return header_key or hermes_key


def is_valid_hermes_key(key: str | None) -> bool:
    if not HERMES_API_KEY or not key:
        return False
    return hmac.compare_digest(key.encode(), HERMES_API_KEY.encode())


def get_hermes_user(key: str = None) -> str:
    """Returns 'hermes' if the key is valid, else raises 401."""
    if not HERMES_API_KEY:
        raise HTTPException(401, "Hermes access not configured")
    if is_valid_hermes_key(key):
        return "hermes"
    raise HTTPException(401, "Invalid Hermes API key")


# Simple in-memory rate limiter (per IP)
import time
RATE_LIMIT = defaultdict(list)  # ip -> list of timestamps

def check_rate_limit(ip: str, max_requests: int = 5, window_seconds: int = 3600) -> bool:
    """Returns True if request is allowed."""
    now = time.time()
    RATE_LIMIT[ip] = [t for t in RATE_LIMIT[ip] if now - t < window_seconds]
    if len(RATE_LIMIT[ip]) >= max_requests:
        return False
    RATE_LIMIT[ip].append(now)
    return True


def client_ip(request: Request) -> str:
    # Render terminates TLS at its proxy, so request.client is the proxy; use the first
    # X-Forwarded-For hop (best effort — this is a soft abuse limit, not security).
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd.strip():
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def verify_admin(email: str, password: str) -> bool:
    if not ADMIN_EMAIL or email != ADMIN_EMAIL:
        return False
    return pwd_context.verify(password, ADMIN_PASSWORD_HASH) if ADMIN_PASSWORD_HASH else False

# Simple session stub (cookie for MVP)
from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "dev-secret"))


def admin_from_session(request: Request | None) -> str | None:
    if request is None or not ADMIN_EMAIL:
        return None
    return ADMIN_EMAIL if request.session.get("admin") == ADMIN_EMAIL else None


def get_current_admin(request: Request):
    admin = admin_from_session(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Admin only")
    return admin


def require_internal(request: Request, hermes_key: str = None) -> str:
    """Internal routes: a logged-in admin session OR a valid Hermes key (header or query)."""
    admin = admin_from_session(request)
    if admin:
        return admin
    if is_valid_hermes_key(_hermes_key_from(request, hermes_key)):
        return "hermes"
    raise HTTPException(401, "Invalid Hermes API key or admin session")

# Tables are managed via Alembic migrations (see alembic/ and alembic.ini)
# No create_all in production code.


# ===== JUDGMENT PERSISTENCE =====

# One judgment per company at a time (per process; Render runs a single worker).
_judging: set[int] = set()
_judging_lock = threading.Lock()


def _begin_judging(company_id: int) -> bool:
    with _judging_lock:
        if company_id in _judging:
            return False
        _judging.add(company_id)
        return True


def _end_judging(company_id: int) -> None:
    with _judging_lock:
        _judging.discard(company_id)


def apply_judgment(db: Session, company_id: int, result: JudgmentResult, *, source: str,
                   actor: str, suggestion_id: int = None) -> int:
    """Atomically replace a company's scores with a validated judgment.
    Real (non-placeholder) old rows are archived to score_history first.
    Returns the number of archived rows. Rolls back on any error."""
    try:
        company = db.query(Company).filter(Company.id == company_id).with_for_update().one()
        old = db.query(Score).filter(Score.company_id == company_id).all()
        archived = 0
        for s in old:
            if is_placeholder(s) or s.score is None:
                continue
            db.add(ScoreHistory(
                company_id=s.company_id, category=s.category, score=s.score,
                justification=s.justification, evidence_links=s.evidence_links,
                model=s.model, model_version=s.model_version, judged_at=s.judged_at,
            ))
            archived += 1
        for s in old:
            db.delete(s)
        db.flush()
        now = datetime.now(UTC)
        model_id = result.meta.get("model") or XAI_MODEL
        for item in result.items:
            db.add(Score(
                company_id=company_id,
                category=item["category"],
                score=item["score"],
                justification=item["justification"],
                evidence_links=item["evidence_links"],
                model=model_id,
                model_version=jm.PROMPT_VERSION,
                judged_at=now,
            ))
        company.last_ingested_at = now
        db.add(IngestLog(
            company_id=company_id,
            suggestion_id=suggestion_id,
            action="judgment",
            admin_id=actor,
            details={**result.meta, "source": source, "scores_written": len(result.items),
                     "archived_scores": archived},
        ))
        db.commit()
        return archived
    except Exception:
        db.rollback()
        raise


def log_judge_error(db: Session, company_id: int, error: str, meta: dict, *, source: str,
                    actor: str, suggestion_id: int = None) -> None:
    try:
        db.add(IngestLog(
            company_id=company_id,
            suggestion_id=suggestion_id,
            action="judge_error",
            admin_id=actor,
            details={**(meta or {}), "error": str(error)[:1000], "company_id": company_id,
                     "source": source, "existing_scores_untouched": True},
        ))
        db.commit()
    except Exception:
        db.rollback()


async def judge_and_apply(db: Session, company: Company, *, source: str, actor: str,
                          suggestion_id: int = None) -> dict:
    """Run the blocking LLM judgment in a worker thread, then swap scores in on success.
    On any failure existing scores are left exactly as they were."""
    company_id, name, industry = company.id, company.canonical_name, company.industry
    db.commit()  # end any open read transaction so no DB connection is held during the LLM call
    if not _begin_judging(company_id):
        return {"status": "already_running"}
    try:
        try:
            result = await run_in_threadpool(jm.judge_company, grok_client, name, industry)
        except JudgmentError as e:
            log_judge_error(db, company_id, str(e), e.meta, source=source, actor=actor,
                            suggestion_id=suggestion_id)
            return {"status": "failed", "error": str(e)}
        try:
            archived = apply_judgment(db, company_id, result, source=source, actor=actor,
                                      suggestion_id=suggestion_id)
        except Exception as e:
            log_judge_error(db, company_id, f"persist failed: {type(e).__name__}: {e}", result.meta,
                            source=source, actor=actor, suggestion_id=suggestion_id)
            return {"status": "failed", "error": "could not save judgment"}
        return {
            "status": "succeeded",
            "model": result.meta.get("model"),
            "duration_ms": result.meta.get("duration_ms"),
            "archived_scores": archived,
        }
    finally:
        _end_judging(company_id)


def get_or_create_company(db: Session, canonical: str, domain: str = None) -> Company:
    company = db.query(Company).filter(Company.canonical_name == canonical).first()
    if company:
        return company
    company = Company(canonical_name=canonical, industry="Unknown", domain=domain or None)
    db.add(company)
    try:
        db.commit()
    except IntegrityError:  # concurrent creation of the same canonical name
        db.rollback()
        return db.query(Company).filter(Company.canonical_name == canonical).one()
    db.refresh(company)
    return company


# ===== LEADERBOARD =====

def get_leaderboard(db: Session, limit: int = 100, q: str = None):
    """Returns (ranked, awaiting). Overall is computed in code from the five real dimensions;
    placeholder rows and duplicates never affect the ranking."""
    query = db.query(Company)
    if q:
        query = query.filter(
            Company.canonical_name.ilike(f"%{q}%") |
            Company.industry.ilike(f"%{q}%")
        )
    companies = query.all()
    by_company = defaultdict(list)
    ids = [c.id for c in companies]
    if ids:
        for s in db.query(Score).filter(Score.company_id.in_(ids)).all():
            by_company[s.company_id].append(s)

    ranked, awaiting = [], []
    for c in companies:
        summary = summarize_scores(by_company.get(c.id, []))
        entry = {"company": c, "overall": summary["overall"], "scores": summary["scores"],
                 "status": summary["status"]}
        (ranked if summary["status"] == "ranked" else awaiting).append(entry)
    ranked.sort(key=lambda e: (-e["overall"], e["company"].canonical_name or ""))
    awaiting.sort(key=lambda e: e["company"].canonical_name or "")
    return ranked[:limit], awaiting[:limit]


def get_ranked_companies(db: Session, limit: int = 100, q: str = None):
    """Centralized leaderboard query logic (ranked entities only)."""
    return get_leaderboard(db, limit=limit, q=q)[0]


# ===== PUBLIC ROUTES =====

@app.get("/health")
async def health():
    return {"status": "ok", "service": "kardashev-index", "version": __version__}


# ===== HERMES INTERNAL ENDPOINTS (admin session or Hermes key) =====

@app.get("/internal/health")
async def internal_health(request: Request, hermes_key: str = None):
    require_internal(request, hermes_key)
    return {
        "status": "ok",
        "service": "kardashev-index",
        "version": __version__,
        "hermes_access": True
    }


@app.get("/internal/stats")
async def internal_stats(request: Request, hermes_key: str = None, db: Session = Depends(get_db)):
    require_internal(request, hermes_key)
    companies = db.query(Company).count()
    pending = db.query(Suggestion).filter(Suggestion.status == "pending").count()
    total_scores = db.query(Score).count()
    recent_logs = db.query(IngestLog).count()

    return {
        "companies": companies,
        "pending_suggestions": pending,
        "total_scores": total_scores,
        "total_logs": recent_logs,
        "version": __version__
    }


@app.get("/internal/recent-judgments")
async def internal_recent_judgments(request: Request, hermes_key: str = None, limit: int = 10,
                                    db: Session = Depends(get_db)):
    require_internal(request, hermes_key)
    recent = (
        db.query(Company, Score)
        .join(Score)
        .order_by(Score.judged_at.desc())
        .limit(limit)
        .all()
    )

    results = []
    for company, score in recent:
        results.append({
            "company": company.canonical_name,
            "category": score.category,
            "score": round(score.score, 1),
            "justification": score.justification[:200] if score.justification else None,
            "judged_at": score.judged_at.isoformat() if score.judged_at else None
        })

    return {"recent_judgments": results}


@app.get("/internal/logs")
async def internal_logs(
    request: Request,
    hermes_key: str = None,
    limit: int = 30,
    action: str = None,
    db: Session = Depends(get_db)
):
    require_internal(request, hermes_key)
    query = db.query(IngestLog).order_by(IngestLog.timestamp.desc())
    if action:
        query = query.filter(IngestLog.action == action)
    logs = query.limit(limit).all()

    results = []
    for log in logs:
        results.append({
            "id": log.id,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "action": log.action,
            "company_id": log.company_id,
            "suggestion_id": log.suggestion_id,
            "details": log.details
        })

    return {"logs": results, "count": len(results)}


@app.post("/internal/approve/{suggestion_id}")
async def internal_approve_suggestion(
    suggestion_id: int,
    request: Request,
    hermes_key: str = None,
    db: Session = Depends(get_db)
):
    """Approve + ingest a pending suggestion. Supports both web admin and Hermes.
    Idempotent: the suggestion is claimed atomically, so a double-submit can't judge twice,
    and approving a name that already exists re-judges that company instead of appending rows."""
    approver = require_internal(request, hermes_key)

    claimed = (
        db.query(Suggestion)
        .filter(Suggestion.id == suggestion_id, Suggestion.status == "pending")
        .update({"status": "approved", "admin_id": approver}, synchronize_session=False)
    )
    db.commit()
    if not claimed:
        raise HTTPException(404, "Pending suggestion not found")
    sug = db.get(Suggestion, suggestion_id)

    canonical = sug.name.lower().replace(" ", "-")
    company = get_or_create_company(db, canonical, sug.domain)

    source = "web" if approver != "hermes" else "hermes"
    outcome = await judge_and_apply(db, company, source=source, actor=approver,
                                    suggestion_id=sug.id)

    db.add(IngestLog(
        company_id=company.id,
        suggestion_id=sug.id,
        action="approve_ingest",
        admin_id=approver,
        details={"source": source, "judgment": outcome["status"]}
    ))
    db.commit()

    return {
        "status": "approved",
        "suggestion_id": suggestion_id,
        "company_id": company.id,
        "canonical_name": canonical,
        "approved_by": approver,
        "judgment": outcome["status"],
    }


@app.post("/internal/deny/{suggestion_id}")
async def internal_deny_suggestion(
    suggestion_id: int,
    request: Request,
    hermes_key: str = None,
    reason: str = Form(None),
    db: Session = Depends(get_db)
):
    """Deny a pending suggestion with a reason (form field `reason`, or `?reason=`)."""
    approver = require_internal(request, hermes_key)
    reason = (reason or request.query_params.get("reason") or "").strip()
    if not reason:
        raise HTTPException(422, "A denial reason is required")

    denied = (
        db.query(Suggestion)
        .filter(Suggestion.id == suggestion_id, Suggestion.status == "pending")
        .update({"status": "denied", "denial_reason": reason[:2000], "admin_id": approver},
                synchronize_session=False)
    )
    if not denied:
        db.rollback()
        raise HTTPException(404, "Pending suggestion not found")
    db.add(IngestLog(
        suggestion_id=suggestion_id,
        action="deny",
        admin_id=approver,
        details={"source": "web" if approver != "hermes" else "hermes", "reason": reason[:500]}
    ))
    db.commit()
    return {"status": "denied", "suggestion_id": suggestion_id, "denied_by": approver,
            "reason": reason}


@app.post("/internal/test-suggestion")
async def internal_test_suggestion(
    request: Request,
    name: str,
    hermes_key: str = None,
    db: Session = Depends(get_db)
):
    """Submit a test suggestion for end-to-end Hermes testing."""
    require_internal(request, hermes_key)
    sug = Suggestion(name=name, status="pending")
    db.add(sug)
    db.commit()
    db.refresh(sug)

    return {
        "status": "created",
        "suggestion_id": sug.id,
        "name": name,
        "message": "Use /internal/approve/{id} to approve via Hermes"
    }


@app.get("/internal/hermes-activity")
async def internal_hermes_activity(
    request: Request,
    hermes_key: str = None,
    limit: int = 20,
    db: Session = Depends(get_db)
):
    """Dedicated view of all actions taken by Hermes."""
    require_internal(request, hermes_key)

    logs = (
        db.query(IngestLog)
        .filter(
            (IngestLog.admin_id == "hermes") |
            (IngestLog.action.like("%hermes%"))
        )
        .order_by(IngestLog.timestamp.desc())
        .limit(limit)
        .all()
    )

    results = []
    for log in logs:
        results.append({
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "action": log.action,
            "suggestion_id": log.suggestion_id,
            "company_id": log.company_id,
            "details": log.details
        })

    return {"hermes_activity": results, "count": len(results)}


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, normalize whitespace for dedup."""
    import re
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def find_duplicate_company(db: Session, name: str, domain: str = None, threshold: int = 85, limit: int = 50) -> Company | None:
    """Efficient duplicate lookup: exact domain + SQL ilike candidates then limited fuzzy.
    Also checks pending Suggestions for richer matching (Phase 2)."""
    norm = normalize_name(name)
    if domain:
        existing = db.query(Company).filter(Company.domain == domain).first()
        if existing:
            return existing
        # Also check pending suggestions by domain
        pending = db.query(Suggestion).filter(
            Suggestion.status == "pending",
            Suggestion.domain == domain
        ).first()
        if pending:
            # Return None so it still creates a new Suggestion (but we could enhance later)
            pass

    # SQL-level candidate narrowing (case-insensitive)
    like_pattern = f"%{norm[:20]}%"
    candidates = (
        db.query(Company)
        .filter(Company.canonical_name.ilike(like_pattern))
        .limit(limit)
        .all()
    )

    for c in candidates:
        if c.canonical_name and rapidfuzz.fuzz.ratio(norm, normalize_name(c.canonical_name)) >= threshold:
            return c
    return None


# ===== PUBLIC PAGES =====

@app.get("/")
def public_leaderboard(request: Request, q: str = None, db: Session = Depends(get_db)):
    companies, awaiting = get_leaderboard(db, limit=100, q=q)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "companies": companies,
            "awaiting": awaiting,
            "version": __version__,
            "q": q or ""
        }
    )


@app.get("/companies/{company_id}")
def company_detail(company_id: int, request: Request, db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    scores = db.query(Score).filter(Score.company_id == company_id).all()
    summary = summarize_scores(scores)

    return templates.TemplateResponse(
        "company.html",
        {
            "request": request,
            "company": company,
            "scores": summary["scores"],
            "overall": summary["overall"],
            "judgment_status": summary["status"],
            "version": __version__
        }
    )


@app.get("/suggest")
def suggest_form(request: Request):
    return templates.TemplateResponse("suggest.html", {"request": request, "version": __version__})


@app.post("/suggest")
def submit_suggestion(
    request: Request,
    name: str = Form(...),
    domain: str = Form(None),
    reason: str = Form(None),
    db: Session = Depends(get_db)
):
    if not check_rate_limit(client_ip(request)):
        return templates.TemplateResponse(
            "suggest.html",
            {"request": request, "version": __version__,
             "error": "Too many suggestions from your network. Please try again in an hour."},
            status_code=429,
        )

    dup = find_duplicate_company(db, name, domain)
    if dup:
        return templates.TemplateResponse(
            "suggest.html",
            {"request": request, "error": f"Company already exists: {dup.canonical_name}", "version": __version__}
        )

    suggestion = Suggestion(
        name=name,
        domain=domain,
        status="pending",
        submitted_at=datetime.now(UTC)
    )
    db.add(suggestion)
    db.commit()

    return templates.TemplateResponse(
        "suggest.html",
        {"request": request, "success": "Thank you — your suggestion has been submitted for review.", "version": __version__}
    )


# ===== ADMIN AUTH ROUTES =====

@app.get("/admin/login")
def admin_login_form(request: Request):
    return templates.TemplateResponse("admin/login.html", {"request": request, "version": __version__})


@app.post("/admin/login")
def admin_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    if verify_admin(email, password):
        request.session["admin"] = email
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "error": "Invalid credentials", "version": __version__}
    )


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=302)


@app.get("/admin")
def admin_dashboard(request: Request, db: Session = Depends(get_db), current_admin: str = Depends(get_current_admin)):
    # Simple admin dashboard - list pending suggestions
    pending = db.query(Suggestion).filter(Suggestion.status == "pending").all()
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "pending": pending, "admin": current_admin, "version": __version__}
    )


# ===== RERUN JUDGMENT (Admin + Hermes only) =====

@app.post("/internal/rerun-judgment/{company_id}")
async def rerun_judgment(
    company_id: int,
    request: Request,
    hermes_key: str = None,
    db: Session = Depends(get_db)
):
    """Re-judge a company. The new judgment is produced first and swapped in atomically
    only if it validates; old real scores are archived to score_history. On failure the
    existing scores are left untouched and a judge_error is logged."""
    approver = require_internal(request, hermes_key)

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    source = "web" if approver != "hermes" else "hermes"
    outcome = await judge_and_apply(db, company, source=source, actor=approver)
    if outcome["status"] == "already_running":
        raise HTTPException(409, "A judgment for this company is already running")

    db.add(IngestLog(
        company_id=company_id,
        action="rerun_judgment",
        admin_id=approver,
        details={"source": source, "judgment": outcome["status"]}
    ))
    db.commit()

    body = {
        "company_id": company_id,
        "canonical_name": company.canonical_name,
        "rerun_by": approver,
    }
    if outcome["status"] != "succeeded":
        return JSONResponse(status_code=502, content={
            **body, "status": "rerun_failed", "error": outcome.get("error"),
            "detail": "Existing scores were left untouched.",
        })
    return {**body, "status": "rerun_complete", "model": outcome.get("model"),
            "duration_ms": outcome.get("duration_ms")}
