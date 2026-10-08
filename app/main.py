from fastapi import FastAPI, Request, Depends, HTTPException, Form
from starlette.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from passlib.context import CryptContext
import os
from dotenv import load_dotenv
from .db import get_db
from .models import Company, Suggestion, Score, IngestLog, CATEGORIES
from datetime import datetime, UTC
from openai import OpenAI
from sqlalchemy import func
import rapidfuzz

load_dotenv()

# App versioning
__version__ = "v0.1"

app = FastAPI(title="Kardashev Index")
templates = Jinja2Templates(directory="templates")
# app.mount("/static", StaticFiles(directory="static"), name="static")

# Admin auth
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Grok / xAI client (OpenAI compatible)
XAI_API_KEY = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")
grok_client = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
) if XAI_API_KEY else None

# Hermes integration access
HERMES_API_KEY = os.getenv("HERMES_API_KEY")

def get_hermes_user(key: str = None) -> str:
    """Returns 'hermes' if valid key provided via header or query param."""
    if not HERMES_API_KEY:
        raise HTTPException(401, "Hermes access not configured")
    
    # This is a simplified version - in production we should use proper header injection
    # For now we rely on the key being passed or environment
    if key and key == HERMES_API_KEY:
        return "hermes"
    raise HTTPException(401, "Invalid Hermes API key")

# Simple in-memory rate limiter (per IP)
from collections import defaultdict
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

def verify_admin(email: str, password: str) -> bool:
    if email != ADMIN_EMAIL:
        return False
    return pwd_context.verify(password, ADMIN_PASSWORD_HASH) if ADMIN_PASSWORD_HASH else False

# Simple session stub (cookie for MVP)
from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "dev-secret"))

def get_current_admin(request: Request):
    if request.session.get("admin") != ADMIN_EMAIL:
        raise HTTPException(status_code=401, detail="Admin only")
    return ADMIN_EMAIL

# Tables are managed via Alembic migrations (see alembic/ and alembic.ini)
# No create_all in production code.


# ===== HELPER FUNCTIONS =====

def run_llm_judgment(company_id: int, db: Session):
    """Call Grok (xAI) to score the company on all CATEGORIES.
    Stores real Score rows. Falls back to placeholders on error or missing key.
    """
    company = db.query(Company).get(company_id)
    if not company:
        return

    if not grok_client:
        create_placeholder_scores(db, company_id)
        return

    prompt = f"""You are an e/acc alignment judge for the Kardashev Index.
Rate the entity "{company.canonical_name}" (industry: {company.industry or "unknown"}) on these categories (0.0-10.0 scale):
{", ".join(CATEGORIES)}

For each category give:
- score (float 0-10)
- short justification (1-2 sentences, reference specific actions, products, or statements)
- 0-3 high-quality evidence links (official announcements, earnings calls, credible reporting — prefer primary sources)

Be specific and evidence-based. Return ONLY valid JSON (no extra text):
{{
  "scores": [
    {{"category": "ai_tech_acceleration", "score": 8.5, "justification": "...", "evidence_links": []}},
    ...
  ]
}}
"""

    try:
        resp = grok_client.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=2000
        )
        content = resp.choices[0].message.content.strip()
        import json
        import re

        # Strip markdown code fences if present
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.DOTALL).strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Fallback: extract largest JSON object
            json_match = re.search(r"\{[\s\S]*\}", content)
            data = json.loads(json_match.group(0)) if json_match else {"scores": []}

        for item in data.get("scores", []):
            cat = item.get("category")
            if cat not in CATEGORIES:
                continue
            score = Score(
                company_id=company_id,
                category=cat,
                score=float(item.get("score", 5.0)),
                justification=item.get("justification", ""),
                evidence_links=item.get("evidence_links", []),
                model="grok-4",
                model_version="2026-10",
                judged_at=datetime.now(UTC)
            )
            db.add(score)
        db.commit()
    except Exception as e:  
        try:
            db.add(IngestLog(action="judge_error", details={"error": str(e), "company_id": company_id}))
            db.commit()
        except:
            pass
        create_placeholder_scores(db, company_id)


def get_ranked_companies(db: Session, limit: int = 100, q: str = None):
    """Centralized leaderboard query logic."""
    base_query = (
        db.query(Company, func.coalesce(func.avg(Score.score), 0.0).label("overall_score"))
        .outerjoin(Score)
        .group_by(Company.id)
    )

    if q:
        base_query = base_query.filter(
            Company.canonical_name.ilike(f"%{q}%") |
            Company.industry.ilike(f"%{q}%")
        )

    base_query = base_query.order_by(func.coalesce(func.avg(Score.score), 0.0).desc())
    results = base_query.limit(limit).all()

    companies_data = []
    for company, overall in results:
        scores = db.query(Score).filter(Score.company_id == company.id).all()
        score_dict = {s.category: s for s in scores}
        companies_data.append({
            "company": company,
            "overall": round(overall, 1) if overall else 0.0,
            "scores": score_dict
        })
    return companies_data


def create_placeholder_scores(db: Session, company_id: int):
    """Centralized placeholder score creation."""
    for cat in CATEGORIES:
        score = Score(
            company_id=company_id,
            category=cat,
            score=5.0,
            justification="Placeholder — run full judge",
            model="stub",
            model_version="v0"
        )
        db.add(score)


# ===== PUBLIC ROUTES =====

@app.get("/health")
async def health():
    return {"status": "ok", "service": "kardashev-index", "version": __version__}


# ===== HERMES INTERNAL ENDPOINTS =====

@app.get("/internal/health")
async def internal_health(hermes_key: str = None, request: Request = None):
    if not get_hermes_user():
        raise HTTPException(401, "Hermes access denied")
    return {
        "status": "ok",
        "service": "kardashev-index",
        "version": __version__,
        "hermes_access": True
    }


@app.get("/internal/stats")
async def internal_stats(hermes_key: str = None, request: Request = None, db: Session = Depends(get_db)):
    if not get_hermes_user():
        raise HTTPException(401, "Hermes access denied")
    
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
async def internal_recent_judgments(hermes_key: str = None, limit: int = 10, db: Session = Depends(get_db)):
    if not get_hermes_user():
        raise HTTPException(401, "Hermes access denied")
    
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
    hermes_key: str = None, 
    limit: int = 30, 
    action: str = None,
    db: Session = Depends(get_db)
):
    if not get_hermes_user():
        raise HTTPException(401, "Hermes access denied")
    
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
    hermes_key: str = None,
    db: Session = Depends(get_db)
):
    """Hermes-triggered approval of a pending suggestion (for E2E testing)."""
    if not get_hermes_user():
        raise HTTPException(401, "Hermes access denied")
    
    sug = db.query(Suggestion).get(suggestion_id)
    if not sug or sug.status != "pending":
        raise HTTPException(404, "Pending suggestion not found")
    
    # Create company if needed
    canonical = sug.name.lower().replace(" ", "-")
    company = db.query(Company).filter(Company.canonical_name == canonical).first()
    if not company:
        company = Company(canonical_name=canonical, industry="Test")
        db.add(company)
        db.commit()
        db.refresh(company)
    
    # Create placeholder scores (real scoring happens via LLM in approve flow)
    create_placeholder_scores(db, company.id)
    
    sug.status = "approved"
    sug.admin_id = "hermes"
    db.add(IngestLog(company_id=company.id, suggestion_id=sug.id, action="hermes_approve", admin_id="hermes"))
    db.commit()
    
    return {
        "status": "approved",
        "suggestion_id": suggestion_id,
        "company_id": company.id,
        "canonical_name": canonical
    }


@app.post("/internal/test-suggestion")
async def internal_test_suggestion(
    name: str,
    hermes_key: str = None,
    db: Session = Depends(get_db)
):
    """Submit a test suggestion for end-to-end Hermes testing."""
    if not get_hermes_user():
        raise HTTPException(401, "Hermes access denied")
    
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
    limit: int = 20,
    db: Session = Depends(get_db)
):
    """Dedicated view of all actions taken by Hermes."""
    get_hermes_user()  # Will raise 401 if invalid
    
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




# ===== PUBLIC ROUTES =====

@app.get("/")
def public_leaderboard(request: Request, q: str = None, db: Session = Depends(get_db)):
    companies = get_ranked_companies(db, limit=100, q=q)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "companies": companies,
            "version": __version__,
            "q": q or ""
        }
    )


@app.get("/companies/{company_id}")
def company_detail(company_id: int, request: Request, db: Session = Depends(get_db)):
    company = db.query(Company).get(company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    scores = db.query(Score).filter(Score.company_id == company_id).all()
    score_dict = {s.category: s for s in scores}
    overall = round(sum(s.score for s in scores) / len(scores), 1) if scores else 0.0

    return templates.TemplateResponse(
        "company.html",
        {
            "request": request,
            "company": company,
            "scores": score_dict,
            "overall": overall,
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
    # from fastapi import Form  (moved to top)
    # Reuse existing suggestion logic (simplified for MVP)
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
