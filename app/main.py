from fastapi import FastAPI, Request, Depends, HTTPException, Form
from starlette.responses import RedirectResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from contextlib import asynccontextmanager
import asyncio
import logging
import os
from dotenv import load_dotenv
from .db import get_db, SessionLocal
from .models import Company, Suggestion, Score, IngestLog, JudgmentRun, Source, CATEGORIES  # noqa: F401 (re-exported)
from . import alerts as alerts_svc
from . import runs as runs_svc
from .pipeline import methodology as meth
from .pipeline.config import code_version, settings as pipeline_settings, worker_settings
from .pipeline.fetch import HttpFetcher
from .pipeline.measures import METRIC_LABELS, fmt_num
from .pipeline.semantics import DEFINITIONS
from .pipeline.llm import XAIClient
from .pipeline.runner import Deps
from .worker import Worker
from datetime import datetime, UTC
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from sqlalchemy.exc import IntegrityError
from collections import defaultdict
import hmac
import rapidfuzz

load_dotenv()
log = logging.getLogger("kardashev")

# App versioning: single source of truth is app/version.py
from .version import __version__

# xAI key (existing env var names). The pipeline builds its own httpx client from it.
XAI_API_KEY = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")


def build_deps() -> Deps:
    """Production dependencies for a judgment run (fresh settings each run, so env changes apply)."""
    cfg = pipeline_settings()
    llm = XAIClient(XAI_API_KEY, base_url=cfg.xai_base_url, chat_timeout_s=cfg.chat_timeout_s,
                    search_timeout_s=cfg.search_timeout_s, max_retries=cfg.max_retries,
                    backoff_max_s=cfg.backoff_max_s) if XAI_API_KEY else None
    return Deps(llm=llm, fetcher=HttpFetcher(timeout_s=cfg.fetch_timeout_s, max_bytes=cfg.fetch_max_bytes,
                                             retries=cfg.fetch_retries, sec_user_agent=cfg.sec_user_agent),
                settings=cfg)


worker = Worker(SessionLocal, lambda: build_deps())


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if os.getenv("WORKER_ENABLED", "1").lower() not in ("0", "false", "no"):
        task = asyncio.create_task(worker.run_forever())
    try:
        yield
    finally:
        # Graceful drain: stop claiming; the current stage finishes and the run is re-queued at its
        # checkpoint (resumed by the next instance). If the stage outlives WORKER_DRAIN_S, the process
        # exits anyway and stale-run recovery resumes it from the last completed stage.
        worker.stop()
        if task:
            try:
                await asyncio.wait_for(task, timeout=worker_settings().drain_s)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # pragma: no cover
                log.exception("worker drain failed")


app = FastAPI(title="Kardashev Index", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
PT = ZoneInfo("America/Los_Angeles")


def _to_pt(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:  # sqlite returns naive UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(PT)


def fmt_pt(dt, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Admin/public timestamps in the operator's zone (PDT/PST)."""
    d = _to_pt(dt)
    return f"{d.strftime(fmt)} {d.strftime('%Z')}" if d else "—"


def fmt_date(dt) -> str:
    d = _to_pt(dt)
    return d.strftime("%Y-%m-%d") if d else "—"


def fmt_dur(ms) -> str:
    if ms is None:
        return "—"
    s_ = ms / 1000
    if s_ < 60:
        return f"{s_:.1f} s" if s_ < 10 else f"{s_:.0f} s"
    m, sec = divmod(round(s_), 60)
    return f"{m}m {sec:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


templates.env.filters["pt"] = fmt_pt
templates.env.filters["pt_date"] = fmt_date
templates.env.filters["dur"] = fmt_dur
templates.env.filters["num"] = fmt_num
templates.env.globals["code_version"] = code_version
templates.env.globals["version"] = __version__  # pages that don't pass it still show the real version
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static"), name="static")

# Admin auth
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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


class AdminLoginRequired(Exception):
    pass


@app.exception_handler(AdminLoginRequired)
async def _admin_login_required(request: Request, exc: AdminLoginRequired):
    # Browsers get the login page; API clients / tests get a plain 401.
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse("/admin/login?" + urlencode({"next": nxt}), status_code=303)
    return JSONResponse(status_code=401, content={"detail": "Admin only"})


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    # Browsers on public pages get a styled page; APIs (/internal, JSON clients) keep JSON.
    if (exc.status_code in (404, 405) and not request.url.path.startswith("/internal")
            and "text/html" in request.headers.get("accept", "")):
        detail = exc.detail if exc.status_code == 404 and exc.detail != "Not Found" else (
            "That page doesn't exist — it may have moved." if exc.status_code == 404 else "Method not allowed.")
        return templates.TemplateResponse("error.html", {"request": request, "code": exc.status_code,
                                                         "detail": detail, "version": __version__},
                                          status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


def get_current_admin(request: Request):
    admin = admin_from_session(request)
    if not admin:
        raise AdminLoginRequired()
    return admin


def flash(request: Request, msg: str, kind: str = "success"):
    request.session["flash"] = {"kind": kind, "msg": msg}


def pop_flash(request: Request) -> dict:
    f = request.session.pop("flash", None) or {}
    return {f["kind"]: f["msg"]} if f.get("kind") in ("success", "error") else {}


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


# ===== JUDGMENT RUNS (v2 pipeline; executed by the in-process worker) =====

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
    """Returns (ranked, awaiting). Ranked entities have a published v2 run that met the coverage
    rules; their Index is computed in code (65% measured / 35% judged)."""
    return runs_svc.leaderboard(db, limit=limit, q=q)


def get_ranked_companies(db: Session, limit: int = 100, q: str = None):
    """Centralized leaderboard query logic (ranked entities only)."""
    return get_leaderboard(db, limit=limit, q=q)[0]


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "") and admin_from_session(request) is not None


# ===== PUBLIC ROUTES =====

@app.get("/health")
def health():
    """Liveness plus worker status: alive, queue depth, scheduled retries, last run outcome."""
    from .pipeline import prompts
    w = worker.status()
    status = "ok" if (w["alive"] or not w["enabled"]) and "db_error" not in w else "degraded"
    return {"status": status, "service": "kardashev-index", "version": __version__,
            "pipeline_version": meth.PIPELINE_VERSION, "prompt_version": prompts.PROMPT_VERSION,
            "code_version": code_version(), "worker": w,
            "config": {"sec_edgar_user_agent": bool(pipeline_settings().sec_user_agent),
                       "alert_webhook": bool(os.getenv("ALERT_WEBHOOK_URL"))}}


@app.get("/internal/alerts")
def internal_alerts(request: Request, hermes_key: str = None, days: int = 7, db: Session = Depends(get_db)):
    """Failed / retrying / degraded / withheld / stuck runs and configuration warnings (Hermes)."""
    require_internal(request, hermes_key)
    return alerts_svc.collect(db, days=max(1, min(days, 90)))


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
    run_counts = defaultdict(int)
    for (st,) in db.query(JudgmentRun.status):
        run_counts[st] += 1

    return {
        "companies": companies,
        "pending_suggestions": pending,
        "total_scores": total_scores,
        "total_logs": recent_logs,
        "runs": dict(run_counts),
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

    runs = (db.query(JudgmentRun, Company).join(Company, Company.id == JudgmentRun.company_id)
            .filter(JudgmentRun.finished_at.isnot(None))
            .order_by(JudgmentRun.finished_at.desc()).limit(limit).all())
    recent_runs = [{
        "run_id": r.id, "company_id": c.id, "company": c.canonical_name, "status": r.status,
        "index_score": r.index_score, "k_equivalent": r.k_equivalent, "confidence": r.confidence,
        "ranked": r.ranked, "published": r.published, "cost_usd": r.cost_usd, "duration_ms": r.duration_ms,
        "error_type": r.error_type, "finished_at": r.finished_at.isoformat(),
    } for r, c in runs]

    # recent_judgments: legacy v0 per-category scores (no longer written); recent_runs: v2 runs.
    return {"recent_judgments": results, "recent_runs": recent_runs}


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
    """Approve a pending suggestion and queue a measurement run (returns immediately, 202).
    Idempotent: the suggestion is claimed atomically, approving a name that already exists
    queues a re-run for that company, and there is at most one active run per company."""
    approver = require_internal(request, hermes_key)

    claimed = (
        db.query(Suggestion)
        .filter(Suggestion.id == suggestion_id, Suggestion.status == "pending")
        .update({"status": "approved", "admin_id": approver}, synchronize_session=False)
    )
    db.commit()
    if not claimed:
        if wants_html(request):
            flash(request, f"Suggestion #{suggestion_id} is no longer pending.", "error")
            return RedirectResponse("/admin", status_code=303)
        raise HTTPException(404, "Pending suggestion not found")
    sug = db.get(Suggestion, suggestion_id)

    canonical = sug.name.lower().replace(" ", "-")
    company = get_or_create_company(db, canonical, sug.domain)

    source = "web" if approver != "hermes" else "hermes"
    run, created = runs_svc.enqueue_run(db, company.id, trigger="approve", triggered_by=approver,
                                        suggestion_id=sug.id)
    db.add(IngestLog(
        company_id=company.id,
        suggestion_id=sug.id,
        action="approve_ingest",
        admin_id=approver,
        details={"source": source, "run_id": run.id, "run_created": created}
    ))
    db.commit()
    worker.wake()

    if wants_html(request):
        flash(request, f"Approved {sug.name} — run #{run.id} "
                       f"{'queued' if created else 'already ' + run.status}.")
        return RedirectResponse(f"/admin/runs/{run.id}", status_code=303)
    return JSONResponse(status_code=202, content={
        "status": "approved",
        "suggestion_id": suggestion_id,
        "company_id": company.id,
        "canonical_name": canonical,
        "approved_by": approver,
        "judgment": "queued" if created else f"already_{run.status}",
        "run_id": run.id,
        "run_url": f"/internal/runs/{run.id}",
    })


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
        if wants_html(request):
            flash(request, "A denial reason is required.", "error")
            return RedirectResponse("/admin", status_code=303)
        raise HTTPException(422, "A denial reason is required")

    denied = (
        db.query(Suggestion)
        .filter(Suggestion.id == suggestion_id, Suggestion.status == "pending")
        .update({"status": "denied", "denial_reason": reason[:2000], "admin_id": approver},
                synchronize_session=False)
    )
    if not denied:
        db.rollback()
        if wants_html(request):
            flash(request, f"Suggestion #{suggestion_id} is no longer pending.", "error")
            return RedirectResponse("/admin", status_code=303)
        raise HTTPException(404, "Pending suggestion not found")
    db.add(IngestLog(
        suggestion_id=suggestion_id,
        action="deny",
        admin_id=approver,
        details={"source": "web" if approver != "hermes" else "hermes", "reason": reason[:500]}
    ))
    db.commit()
    if wants_html(request):
        flash(request, f"Denied suggestion #{suggestion_id}.")
        return RedirectResponse("/admin", status_code=303)
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
    published = [e["run"].id for e in companies]
    verified_sources = (db.query(Source).filter(Source.run_id.in_(published), Source.status == "ok").count()
                        if published else 0)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "companies": companies,
            "awaiting": awaiting,
            "verified_sources": verified_sources,
            "version": __version__,
            "q": q or ""
        }
    )


@app.get("/companies/{company_id}")
def company_detail(company_id: int, request: Request, run: str = None, db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    hist = runs_svc.history(db, company)
    if run:  # ?run=N-2 / ?run=v0 -> stable URL
        if run.lower() == "v0":
            return RedirectResponse(f"/companies/{company_id}/runs/v0", status_code=302)
        off = runs_svc.parse_rel(run)
        entry = next((e for e in hist["runs"] if e["offset"] == off), None) if off is not None else None
        if entry is None:
            raise HTTPException(404, "No such published run")
        if not entry["current"]:
            return RedirectResponse(entry["url"], status_code=302)
    return _render_company(request, db, company, hist, None)


@app.get("/companies/{company_id}/runs/{ref}")
def company_run(company_id: int, ref: str, request: Request, db: Session = Depends(get_db)):
    """Read-only dossier of a past published run. ``ref`` is the run's stable ordinal (1 = first
    published run) or ``v0`` for the legacy single-prompt scores. Failed runs are never public."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    hist = runs_svc.history(db, company)
    if ref.lower() == "v0":
        if not hist["legacy"]:
            raise HTTPException(404, "No legacy scores for this entity")
        return _render_company(request, db, company, hist, "v0")
    if not ref.isdigit():
        raise HTTPException(404, "No such published run")
    entry = next((e for e in hist["runs"] if e["ordinal"] == int(ref)), None)
    if entry is None:
        raise HTTPException(404, "No such published run")
    if entry["current"]:
        return RedirectResponse(f"/companies/{company_id}", status_code=302)
    return _render_company(request, db, company, hist, entry)


def _render_company(request: Request, db: Session, company: Company, hist: dict, entry):
    if entry == "v0":
        view = runs_svc.company_view(db, company, hist=hist)
        view.update(run=None, historical=True, active=None, last_failed=None, legacy=hist["legacy"],
                    categories=[], measured=[], judged=[], figures=[], sources=[], stages=[], headline=None,
                    synthesis=None, previous=None, entry={"label": "v0", "legacy": True})
    else:
        view = runs_svc.company_view(db, company, run=entry["run"] if entry else None, hist=hist)
    return templates.TemplateResponse(
        "company.html",
        {"request": request, "company": company, "v": view, "hist": hist, "meth": meth, "version": __version__,
         "cfg_cov": pipeline_settings().rank_min_coverage},
    )


@app.get("/methodology")
def methodology_page(request: Request):
    from .pipeline import prompts
    cfg = pipeline_settings()
    return templates.TemplateResponse(
        "methodology.html",
        {"request": request, "meth": meth, "prompt_version": prompts.PROMPT_VERSION, "cfg": cfg,
         "meth_defs": DEFINITIONS, "metric_labels": METRIC_LABELS, "version": __version__},
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

def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/admin") and "//" not in nxt else "/admin"


@app.get("/admin/login")
def admin_login_form(request: Request, next: str = None):
    if admin_from_session(request):
        return RedirectResponse(_safe_next(next), status_code=302)
    return templates.TemplateResponse("admin/login.html", {"request": request, "version": __version__,
                                                           "next": _safe_next(next)})


@app.post("/admin/login")
def admin_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(None),
):
    if verify_admin(email, password):
        request.session["admin"] = email
        return RedirectResponse(_safe_next(next), status_code=302)
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "error": "Invalid credentials", "version": __version__, "next": _safe_next(next)},
        status_code=401,
    )


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=302)


def _admin_alerts(db: Session) -> dict:
    a = alerts_svc.collect(db)
    a["webhook"] = bool(os.getenv("ALERT_WEBHOOK_URL"))
    return a


IDENTITY_TEXT = {"official_name": 200, "domain": 120, "ticker": 16, "exchange": 32, "cik": 10}


@app.post("/admin/companies/{company_id}/identity")
async def admin_company_identity(company_id: int, request: Request, db: Session = Depends(get_db),
                                 current_admin: str = Depends(get_current_admin)):
    """Pin a corrected identity (reused by every later run, never overwritten by resolution),
    or clear it so the next run resolves the company again."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    form = await request.form()
    action = form.get("action", "pin")
    if action == "clear":
        company.identity, company.identity_status, company.identity_resolved_at = None, None, None
        msg = "Identity cleared: the next run resolves this company again."
    else:
        ident = dict(company.identity or {})
        for key, cap in IDENTITY_TEXT.items():
            val = (form.get(key) or "").strip()[:cap]
            ident[key] = val or None
        if not ident.get("official_name"):
            flash(request, "Official name is required to pin an identity.", "error")
            return RedirectResponse("/admin#companies", status_code=303)
        if ident.get("domain"):
            ident["domain"] = ident["domain"].lower().removeprefix("https://").removeprefix("http://") \
                .removeprefix("www.").split("/")[0]
        if ident.get("ticker"):
            ident["ticker"] = ident["ticker"].upper()
        if ident.get("cik"):
            if not ident["cik"].isdigit():
                flash(request, "CIK must be digits.", "error")
                return RedirectResponse("/admin#companies", status_code=303)
            ident["cik"] = f"{int(ident['cik']):010d}"
        pub = form.get("is_public")
        ident["is_public"] = True if pub == "1" else (False if pub == "0" else None)
        ident["sec_filer"] = bool(ident.get("cik"))
        ident["confidence"] = 1.0
        ident["identity_check"] = {"status": "pinned", "note": f"pinned by {current_admin}", "consistent": True}
        company.identity, company.identity_status = ident, "pinned"
        company.identity_resolved_at = datetime.now(UTC)
        for attr in ("official_name", "ticker", "exchange", "cik", "is_public"):
            setattr(company, attr, ident.get(attr))
        if ident.get("domain"):
            company.domain = ident["domain"]
        msg = f"Identity pinned for {ident['official_name']}; later runs reuse it."
    db.add(IngestLog(company_id=company.id, action="identity_" + ("cleared" if action == "clear" else "pinned"),
                     admin_id=current_admin, details={"identity": company.identity}))
    db.commit()
    flash(request, msg)
    return RedirectResponse("/admin#companies", status_code=303)


@app.get("/admin")
def admin_dashboard(request: Request, db: Session = Depends(get_db), current_admin: str = Depends(get_current_admin)):
    pending = db.query(Suggestion).filter(Suggestion.status == "pending").order_by(Suggestion.id).all()
    logs = db.query(IngestLog).order_by(IngestLog.timestamp.desc()).limit(20).all()
    high_attempt = db.query(Company).filter(Company.suggestion_attempts > 1).order_by(
        Company.suggestion_attempts.desc()).limit(10).all()
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "pending": pending, "admin": current_admin, "version": __version__,
         "companies": runs_svc.admin_companies(db), "logs": logs, "high_attempt": high_attempt,
         "runs": runs_svc.admin_runs(db, limit=10), "alerts": _admin_alerts(db), **pop_flash(request)}
    )


@app.get("/admin/runs")
def admin_runs_list(request: Request, company_id: str = None, status: str = None, page: int = 1,
                    db: Session = Depends(get_db), current_admin: str = Depends(get_current_admin)):
    """Every run, newest first, filterable by company and status. Polls itself while runs are active."""
    cid = int(company_id) if company_id and company_id.isdigit() else None
    status = status if status in runs_svc.RUN_FILTERS else None
    data = runs_svc.admin_runs_page(db, company_id=cid, status=status, page=page)
    filters = {k: v for k, v in (("company_id", cid), ("status", status)) if v is not None}
    return templates.TemplateResponse(
        "admin_runs.html",
        {"request": request, "admin": current_admin, "version": __version__, "data": data, "runs": data["rows"],
         "company_id": cid, "status": status, "statuses": runs_svc.RUN_FILTERS,
         "companies": db.query(Company).order_by(Company.canonical_name).all(),
         "qs": urlencode(filters), "self_url": "/admin/runs?" + urlencode({**filters, "page": data["page"]}),
         "alerts": _admin_alerts(db), **pop_flash(request)},
    )


@app.get("/admin/runs/{run_id}")
def admin_run_detail(run_id: int, request: Request, db: Session = Depends(get_db),
                     current_admin: str = Depends(get_current_admin)):
    run = db.get(JudgmentRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return templates.TemplateResponse(
        "admin_run.html",
        {"request": request, "admin": current_admin, "version": __version__, "d": runs_svc.admin_run_detail(db, run),
         "meth": meth, "alerts": _admin_alerts(db), **pop_flash(request)},
    )


@app.get("/internal/runs/{run_id}")
def internal_run_detail(run_id: int, request: Request, hermes_key: str = None, db: Session = Depends(get_db)):
    require_internal(request, hermes_key)
    run = db.get(JudgmentRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return runs_svc.run_json(db, run)


@app.get("/internal/runs")
def internal_runs(request: Request, hermes_key: str = None, company_id: int = None, status: str = None,
                  limit: int = 20, db: Session = Depends(get_db)):
    require_internal(request, hermes_key)
    q = db.query(JudgmentRun).order_by(JudgmentRun.id.desc())
    if company_id is not None:
        q = q.filter(JudgmentRun.company_id == company_id)
    if status:
        q = q.filter(JudgmentRun.status == status)
    rows = q.limit(max(1, min(limit, 100))).all()
    return {"runs": [runs_svc.run_json(db, r, stages=False, scores=False) for r in rows], "count": len(rows)}


# ===== RERUN JUDGMENT (Admin + Hermes only) =====

@app.post("/internal/rerun-judgment/{company_id}")
async def rerun_judgment(
    company_id: int,
    request: Request,
    hermes_key: str = None,
    db: Session = Depends(get_db)
):
    """Queue a fresh measurement run (202). The published data changes only if the new run
    succeeds; failed runs never touch it. 409 if a run is already queued/running."""
    approver = require_internal(request, hermes_key)

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    run, created = runs_svc.enqueue_run(db, company_id, trigger="rerun", triggered_by=approver)
    body = {"company_id": company_id, "canonical_name": company.canonical_name, "rerun_by": approver,
            "run_id": run.id, "run_url": f"/internal/runs/{run.id}"}
    if not created and run.status == "queued" and run.next_attempt_at is not None:
        # an automatic retry is waiting: run it now (it resumes from its checkpoint)
        run.next_attempt_at = None
        db.add(IngestLog(company_id=company_id, action="retry_now", admin_id=approver, details={"run_id": run.id}))
        db.commit()
        worker.wake()
        if wants_html(request):
            flash(request, f"Run #{run.id} had an automatic retry scheduled; it will resume now.")
            return RedirectResponse(f"/admin/runs/{run.id}", status_code=303)
        return JSONResponse(status_code=202, content={**body, "status": "retry_now"})
    if not created:
        if wants_html(request):
            flash(request, f"{company.official_name or company.canonical_name} already has run #{run.id} "
                           f"{run.status} — showing it instead of queueing another.", "error")
            return RedirectResponse(f"/admin/runs/{run.id}", status_code=303)
        return JSONResponse(status_code=409, content={
            **body, "status": "already_active", "run_status": run.status,
            "detail": "A run for this company is already queued or running"})

    db.add(IngestLog(
        company_id=company_id,
        action="rerun_judgment",
        admin_id=approver,
        details={"source": "web" if approver != "hermes" else "hermes", "run_id": run.id}
    ))
    db.commit()
    worker.wake()
    if wants_html(request):
        flash(request, f"Queued run #{run.id} for {company.official_name or company.canonical_name} "
                       f"(current versions: {meth.PIPELINE_VERSION}, {meth.WEIGHTS_VERSION}).")
        return RedirectResponse(f"/admin/runs/{run.id}", status_code=303)
    return JSONResponse(status_code=202, content={**body, "status": "rerun_queued"})
