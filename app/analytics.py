"""First-party visit counts (1.15.0). No cookies, no scripts, no raw IP or full user agent stored.

* ``AnalyticsMiddleware`` records successful HTML GET page views (not static files, /health, /admin,
  /internal, robots/sitemap/favicon, HTMX partials or redirects): path (no query), referrer host,
  utm_source/medium/campaign, country from ``CF-IPCountry`` when the edge sends it, a user-agent class
  (human / bot / monitor) and a daily visitor id.
* Visitor id = sha256(daily salt + client IP + user agent)[:16]. The salt is random, lives only in
  this process's memory and rotates at Pacific midnight, so ids can't be linked across days or
  reversed from the database. A restart mid-day makes a new salt (that day's uniques run slightly high).
* Requests never touch the database: views go into a bounded in-memory buffer that a background task
  writes in one batch every ANALYTICS_FLUSH_S seconds (and once more on shutdown).
* Once a day (after ROLLUP_HOUR_PT) finished days are rolled up into ``page_view_daily`` and raw
  rows older than ANALYTICS_RAW_DAYS are deleted.

Env (all optional): ANALYTICS_ENABLED (1), ANALYTICS_FLUSH_S (30), ANALYTICS_BUFFER_MAX (5000),
ANALYTICS_RAW_DAYS (90), ANALYTICS_EXCLUDE_PATHS (extra comma-separated path prefixes to skip).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .models import PageView, PageViewDaily

log = logging.getLogger("kardashev.analytics")
PT = ZoneInfo("America/Los_Angeles")
ROLLUP_HOUR_PT = 3
DIMS = ("total", "path", "ref", "country", "utm_source")
UA_CLASSES = ("human", "bot", "monitor")
SKIP_PREFIXES = ("/static", "/health", "/admin", "/internal", "/robots.txt", "/sitemap.xml",
                 "/favicon", "/apple-touch-icon")

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|embedly|preview|scrape|curl|wget|python-requests|"
    r"python-urllib|httpx|aiohttp|go-http-client|java/|okhttp|libwww|headless|phantom|axios|node-fetch",
    re.IGNORECASE)
_MONITOR_RE = re.compile(r"uptime|pingdom|statuscake|betteruptime|better stack|site24x7|checkly|"
                         r"render/|monitor|healthcheck|newrelic|datadog", re.IGNORECASE)


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    enabled: bool
    flush_s: float
    buffer_max: int
    raw_days: int
    exclude: tuple[str, ...]


def settings() -> Settings:
    extra = tuple(p.strip() for p in (os.getenv("ANALYTICS_EXCLUDE_PATHS") or "").split(",") if p.strip())
    return Settings(
        enabled=os.getenv("ANALYTICS_ENABLED", "1").lower() not in ("0", "false", "no"),
        flush_s=max(1.0, _env_f("ANALYTICS_FLUSH_S", 30)),
        buffer_max=max(10, int(_env_f("ANALYTICS_BUFFER_MAX", 5000))),
        raw_days=max(1, int(_env_f("ANALYTICS_RAW_DAYS", 90))),
        exclude=extra)


# ---------- classification ----------
def ua_class(user_agent: str, accept: str = "") -> str:
    ua = (user_agent or "").strip()
    if not ua:
        return "bot"
    if _MONITOR_RE.search(ua):
        return "monitor"
    if _BOT_RE.search(ua):
        return "bot"
    if accept and "text/html" not in accept and "*/*" not in accept:
        return "monitor"
    return "human"


def ref_host(referer: str, own_host: str) -> str:
    if not referer:
        return "direct"
    try:
        host = (urlsplit(referer).hostname or "").lower()
    except ValueError:
        return "direct"
    if not host:
        return "direct"
    host = host.removeprefix("www.")
    own = (own_host or "").split(":")[0].lower().removeprefix("www.")
    return "internal" if own and host == own else host[:120]


def _clean(v: str | None, n: int = 64) -> str | None:
    v = (v or "").strip().lower()[:n]
    return v or None


def country(header: str | None) -> str | None:
    c = (header or "").strip().upper()
    return c if re.fullmatch(r"[A-Z][A-Z0-9]", c) else None


def pt_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(PT).date().isoformat()


# ---------- daily salt + visitor id ----------
_SALT: dict[str, bytes] = {}
_SALT_LOCK = threading.Lock()


def _salt(day: str) -> bytes:
    with _SALT_LOCK:
        s = _SALT.get(day)
        if s is None:
            _SALT.clear()               # yesterday's salt is gone for good
            s = _SALT[day] = secrets.token_bytes(32)
        return s


def visitor_id(day: str, ip: str, user_agent: str) -> str:
    return hashlib.sha256(_salt(day) + (ip or "").encode() + b"\0" + (user_agent or "").encode()).hexdigest()[:16]


def client_ip(headers: dict[str, str], scope_client) -> str:
    xff = headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return scope_client[0] if scope_client else ""


# ---------- buffer ----------
class Buffer:
    def __init__(self):
        self._q: deque[dict] = deque()
        self._lock = threading.Lock()
        self.dropped = 0
        self.written = 0
        self.last_flush_at: datetime | None = None
        self.last_error: str | None = None
        self.country_seen_at: float | None = None

    def add(self, row: dict, cap: int):
        with self._lock:
            if len(self._q) >= cap:
                self._q.popleft()
                self.dropped += 1
            self._q.append(row)

    def drain(self) -> list[dict]:
        with self._lock:
            rows = list(self._q)
            self._q.clear()
            return rows

    def requeue(self, rows: list[dict], cap: int):
        with self._lock:
            for r in reversed(rows):
                if len(self._q) >= cap:
                    self.dropped += 1
                    continue
                self._q.appendleft(r)

    def __len__(self):
        return len(self._q)

    def reset(self):
        with self._lock:
            self._q.clear()
            self.dropped = self.written = 0
            self.last_flush_at = self.last_error = self.country_seen_at = None


BUFFER = Buffer()


def _skip(path: str, extra: tuple[str, ...]) -> bool:
    return path.startswith(SKIP_PREFIXES + extra)


def build_row(scope, status: int, now: datetime | None = None) -> dict:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    now = now or datetime.now(UTC)
    day = pt_day(now)
    qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    ua = headers.get("user-agent", "")
    cc = country(headers.get("cf-ipcountry"))
    if cc:
        BUFFER.country_seen_at = time.time()
    return {"ts": now, "day": day, "path": (scope.get("path") or "/")[:200],
            "ref_host": ref_host(headers.get("referer", ""), headers.get("host", "")),
            "utm_source": _clean((qs.get("utm_source") or [None])[0]),
            "utm_medium": _clean((qs.get("utm_medium") or [None])[0]),
            "utm_campaign": _clean((qs.get("utm_campaign") or [None])[0]),
            "country": cc, "ua_class": ua_class(ua, headers.get("accept", "")),
            "visitor": visitor_id(day, client_ip(headers, scope.get("client")), ua), "status": status}


class AnalyticsMiddleware:
    """Pure ASGI: watches the response start, records after the response is sent. Never raises."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET":
            return await self.app(scope, receive, send)
        s = settings()
        path = scope.get("path") or "/"
        if not s.enabled or _skip(path, s.exclude):
            return await self.app(scope, receive, send)
        seen: dict = {}

        async def _send(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
                for k, v in message.get("headers") or []:
                    if k.lower() == b"content-type":
                        seen["ctype"] = v.decode("latin-1")
            await send(message)

        await self.app(scope, receive, _send)
        try:
            hx = any(k.lower() == b"hx-request" for k, _ in scope.get("headers") or [])
            st = seen.get("status", 0)
            if not hx and 200 <= st < 300 and seen.get("ctype", "").startswith("text/html"):
                BUFFER.add(build_row(scope, st), s.buffer_max)
        except Exception:  # pragma: no cover - analytics must never break a page
            log.exception("analytics record failed")


# ---------- writes ----------
def flush(session_factory: Callable[[], Session]) -> int:
    rows = BUFFER.drain()
    if not rows:
        return 0
    try:
        with session_factory() as db:
            db.bulk_insert_mappings(PageView, rows)
            db.commit()
    except Exception as e:
        BUFFER.last_error = f"{type(e).__name__}: {e}"[:200]
        log.exception("analytics flush failed; keeping %d rows for one retry", len(rows))
        BUFFER.requeue(rows, settings().buffer_max)
        return 0
    BUFFER.written += len(rows)
    BUFFER.last_flush_at = datetime.now(UTC)
    BUFFER.last_error = None
    return len(rows)


def _aggregate(rows) -> list[dict]:
    """rows: iterables of (day, path, ref_host, country, utm_source, ua_class, visitor)."""
    views: Counter = Counter()
    uniq: dict = {}
    for day, path, ref, cc, utm, ua, vis in rows:
        for dim, key in (("total", ""), ("path", path), ("ref", ref or "direct"), ("country", cc or "??"),
                         ("utm_source", utm)):
            if dim == "utm_source" and not key:
                continue
            k = (day, dim, key[:200], ua)
            views[k] += 1
            uniq.setdefault(k, set()).add(vis)
    return [{"day": k[0], "dim": k[1], "key": k[2], "ua_class": k[3], "views": v, "visitors": len(uniq[k])}
            for k, v in views.items()]


_RAW_COLS = (PageView.day, PageView.path, PageView.ref_host, PageView.country, PageView.utm_source,
             PageView.ua_class, PageView.visitor)


def rollup(db: Session, now: datetime | None = None, raw_days: int | None = None) -> dict:
    """Roll up every finished Pacific day that has raw rows (idempotent: a day's rollup is replaced),
    then delete raw rows older than raw_days."""
    today = pt_day(now)
    raw_days = raw_days or settings().raw_days
    days = [d for (d,) in db.query(PageView.day).filter(PageView.day < today).distinct().all()]
    for d in days:
        agg = _aggregate(db.query(*_RAW_COLS).filter(PageView.day == d).all())
        db.query(PageViewDaily).filter(PageViewDaily.day == d).delete(synchronize_session=False)
        if agg:
            db.bulk_insert_mappings(PageViewDaily, agg)
    cutoff = (date.fromisoformat(today) - timedelta(days=raw_days)).isoformat()
    purged = db.query(PageView).filter(PageView.day < cutoff).delete(synchronize_session=False)
    db.commit()
    return {"rolled_up_days": len(days), "purged_raw": purged}


class Runner:
    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory
        self.last_rollup_day: str | None = None
        self._stop = asyncio.Event()

    def maybe_rollup(self, now: datetime | None = None) -> dict | None:
        now = now or datetime.now(UTC)
        local = now.astimezone(PT)
        day = local.date().isoformat()
        if local.hour < ROLLUP_HOUR_PT or self.last_rollup_day == day:
            return None
        self.last_rollup_day = day
        with self.session_factory() as db:
            out = rollup(db, now)
        log.info("analytics rollup %s", out)
        return out

    async def run_forever(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings().flush_s)
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(flush, self.session_factory)
                await asyncio.to_thread(self.maybe_rollup)
            except Exception:
                log.exception("analytics loop error")
        await asyncio.to_thread(flush, self.session_factory)   # final flush on shutdown

    def stop(self):
        self._stop.set()


def status() -> dict:
    seen = BUFFER.country_seen_at
    return {"enabled": settings().enabled, "buffered": len(BUFFER), "written": BUFFER.written,
            "dropped": BUFFER.dropped, "last_error": BUFFER.last_error,
            "last_flush_at": BUFFER.last_flush_at.isoformat() if BUFFER.last_flush_at else None,
            "country_header_seen": bool(seen and time.time() - seen < 86400)}


# ---------- admin panel ----------
_CACHE: dict = {}
CACHE_S = 60


def report(db: Session, *, days: int = 30, include_bots: bool = False, now: datetime | None = None,
           use_cache: bool = True) -> dict:
    key = (days, include_bots)
    hit = _CACHE.get(key)
    if use_cache and hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    today = pt_day(now)
    start = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
    rows = [(r.day, r.dim, r.key, r.ua_class, r.views, r.visitors) for r in
            db.query(PageViewDaily).filter(PageViewDaily.day >= start, PageViewDaily.day < today).all()]
    rolled = {r[0] for r in rows}
    # Today (and any finished day not rolled up yet) straight from the raw rows.
    raw = db.query(*_RAW_COLS).filter(PageView.day >= start).all()
    rows += [(a["day"], a["dim"], a["key"], a["ua_class"], a["views"], a["visitors"])
             for a in _aggregate(r for r in raw if r[0] not in rolled)]
    classes = set(UA_CLASSES) if include_bots else {"human"}
    by_day: dict[str, list[int]] = {}
    tops: dict[str, Counter] = {d: Counter() for d in ("path", "ref", "country", "utm_source")}
    ua_views: Counter = Counter()
    for day, dim, k, ua, v, vis in rows:
        if dim == "total":
            ua_views[ua] += v
        if ua not in classes:
            continue
        if dim == "total":
            d = by_day.setdefault(day, [0, 0])
            d[0] += vis
            d[1] += v
        elif dim in tops:
            tops[dim][k] += v
    series = []
    for i in range(days):
        d = (date.fromisoformat(start) + timedelta(days=i)).isoformat()
        vis, v = by_day.get(d, [0, 0])
        series.append({"day": d, "visitors": vis, "views": v})
    peak = max((s["visitors"] for s in series), default=0) or 1
    for s in series:
        s["pct"] = round(100 * s["visitors"] / peak)
    last7 = series[-8:-1]
    total_all = sum(ua_views.values())
    out = {"days": days, "include_bots": include_bots, "series": list(reversed(series)),
           "today": series[-1], "avg7": round(sum(s["visitors"] for s in last7) / max(1, len(last7)), 1),
           "pages": tops["path"].most_common(10), "refs": tops["ref"].most_common(10),
           "utm": tops["utm_source"].most_common(10),
           "countries": [(k, v) for k, v in tops["country"].most_common(11) if k != "??"][:10],
           "country_unknown": tops["country"].get("??", 0),
           "share": {c: round(100 * ua_views[c] / total_all, 1) if total_all else 0.0 for c in UA_CLASSES},
           "total_views": total_all, "status": status()}
    _CACHE[key] = (time.time(), out)
    return out


def clear_cache():
    _CACHE.clear()
