"""Canonical host, robots.txt, sitemap.xml and /favicon.ico.

PUBLIC_BASE_URL (e.g. https://kardashevindex.com) is the one public origin. When it is unset
everything here is a no-op apart from robots/sitemap/favicon, which then use the request's origin.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session

from .db import get_db
from .models import Company, JudgmentRun

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Never redirected: Render's health check hits the service by its onrender.com name, and Hermes may
# call /internal on the onrender.com host (redirecting POSTs with API keys across hosts is fragile).
REDIRECT_EXEMPT_PREFIXES = ("/health", "/internal")


def public_base_url() -> str:
    """Canonical origin without trailing slash, or '' when not configured."""
    v = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if v and not v.startswith(("http://", "https://")):
        v = "https://" + v
    return v


def request_origin(request: Request) -> str:
    """PUBLIC_BASE_URL if set, else the request's origin (https assumed behind a proxy)."""
    base = public_base_url()
    if base:
        return base
    origin = str(request.base_url).rstrip("/")
    host = urlsplit(origin).hostname or ""
    if origin.startswith("http://") and host not in ("localhost", "127.0.0.1", "testserver"):
        origin = "https://" + origin[len("http://"):]
    return origin


class CanonicalHostMiddleware:
    """301 (GET/HEAD) or 308 (other methods) from any host other than PUBLIC_BASE_URL's to it,
    keeping path and query. No-op when PUBLIC_BASE_URL is unset; /health and /internal exempt."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        base = public_base_url()
        if scope["type"] != "http" or not base or scope.get("path", "").startswith(REDIRECT_EXEMPT_PREFIXES):
            return await self.app(scope, receive, send)
        want = (urlsplit(base).netloc or "").lower()
        host = ""
        for k, v in scope.get("headers", []):
            if k == b"host":
                host = v.decode("latin-1").lower()
                break
        if not host or host == want:
            return await self.app(scope, receive, send)
        raw = (scope.get("raw_path") or scope.get("path", "/").encode()).split(b"?", 1)[0]
        target = base + raw.decode("latin-1")
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("latin-1")
        status = 301 if scope.get("method") in ("GET", "HEAD") else 308
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"location", target.encode("latin-1")), (b"content-length", b"0"),
                                (b"cache-control", b"public, max-age=3600")]})
        await send({"type": "http.response.body", "body": b""})


router = APIRouter()


@router.get("/robots.txt", include_in_schema=False)
def robots_txt(request: Request):
    origin = request_origin(request)
    body = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        "Disallow: /internal",
        "Disallow: /companies/*/runs/",  # historical run views (also noindex)
        f"Sitemap: {origin}/sitemap.xml",
        "",
    ])
    return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=3600"})


def _page_paths(request: Request) -> list[str]:
    """Public pages that exist in this build (about/privacy appear once those routes ship)."""
    registered = {getattr(r, "path", None) for r in request.app.routes}
    return [p for p in ("/", "/methodology", "/about", "/privacy", "/suggest") if p in registered]


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(request: Request, db: Session = Depends(get_db)):
    origin = request_origin(request)
    urls: list[tuple[str, datetime | None]] = [(origin + p, None) for p in _page_paths(request)]
    companies = db.query(Company).order_by(Company.id).all()
    run_ids = [c.current_run_id for c in companies if c.current_run_id]
    finished = dict(db.query(JudgmentRun.id, JudgmentRun.finished_at).filter(JudgmentRun.id.in_(run_ids))) \
        if run_ids else {}
    for c in companies:
        urls.append((f"{origin}/companies/{c.id}", finished.get(c.current_run_id)))
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in urls:
        parts.append(f"  <url><loc>{escape(loc)}</loc>"
                     + (f"<lastmod>{lastmod.date().isoformat()}</lastmod>" if lastmod else "") + "</url>")
    parts.append("</urlset>")
    return Response("\n".join(parts) + "\n", media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    # Browsers and crawlers request /favicon.ico regardless of <link rel=icon>; a PNG is accepted.
    return FileResponse(STATIC_DIR / "img" / "favicon-32.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
