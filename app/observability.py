"""Pipeline observability for /health (1.2.0): what keeps failing, and whether public text matches
the queue. Read-only, cheap, and cached for OBS_CACHE_S seconds because Render polls /health often.

* ``retry_claims_without_queued_run``: companies whose public reason says "retry scheduled" while no
  run is queued for them (should always be empty; 1.0.1 made the wording honest).
* ``runs_7d``: runs finished in the last OBS_WINDOW_DAYS days by outcome: ``succeeded``,
  ``degraded``, ``energy_unreadable``, ``withheld`` (not published) and ``failed:<error_type>``.
* ``hosts_7d``: the hosts whose sources failed most often in that window, with counts by status
  (``dead:403``, ``thin``, ``error``...) and Wayback outcomes (``archive:rate_limited``...).
* ``archive``: real archive.org requests used today against FETCH_WAYBACK_DAILY_MAX.
"""
from __future__ import annotations

import os
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from .models import ACTIVE_RUN_STATUSES, Company, JudgmentRun, Source

_CACHE: dict = {"at": 0.0, "body": None}
_LOCK = threading.Lock()
TOP_HOSTS = 10


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def host_of(url: str | None) -> str:
    h = (urlsplit(url or "").hostname or "").lower()
    return h.removeprefix("www.")


def collect(db: Session, *, now: datetime | None = None, days: float | None = None) -> dict:
    from . import publication
    from .pipeline.config import settings
    from .pipeline.fetch import ARCHIVE_BUDGET

    now = now or datetime.now(UTC)
    days = days if days is not None else _env_f("OBS_WINDOW_DAYS", 7)
    since = now - timedelta(days=days)

    runs = Counter()
    for r in db.query(JudgmentRun).filter(JudgmentRun.finished_at >= since):
        if r.status == "failed":
            runs[f"failed:{r.error_type or 'unknown'}"] += 1
            continue
        if r.status != "succeeded":
            continue
        runs["succeeded"] += 1
        if r.degraded:
            runs["degraded"] += 1
        if (r.summary or {}).get("energy_unreadable"):
            runs["energy_unreadable"] += 1
        if not r.published:
            runs["withheld"] += 1

    hosts: dict[str, Counter] = defaultdict(Counter)
    q = (db.query(Source.url, Source.status, Source.http_status, Source.archive_attempt)
         .join(JudgmentRun, JudgmentRun.id == Source.run_id)
         .filter(JudgmentRun.finished_at >= since, Source.origin != "edgar"))
    for url, status, http, att in q:
        h = host_of(url)
        c = hosts[h]
        c["total"] += 1
        if status != "ok":
            c["failed"] += 1
            c[f"{status}:{http}" if status == "dead" and http else status or "unknown"] += 1
        if isinstance(att, dict) and att.get("outcome"):
            c[f"archive:{att['outcome']}"] += 1
    top = sorted(((h, c) for h, c in hosts.items() if c["failed"]), key=lambda x: (-x[1]["failed"], x[0]))
    hosts_out = [{"host": h, **dict(c)} for h, c in top[:TOP_HOSTS]]

    companies = db.query(Company).all()
    queued = {cid for (cid,) in db.query(JudgmentRun.company_id)
              .filter(JudgmentRun.status.in_(ACTIVE_RUN_STATUSES))}
    current = publication.replay_companies(db, [c.id for c in companies])
    stale = []
    for c in companies:
        run = current[c.id].current
        reason = run.rank_reason if run is not None and not run.is_ranked else None
        if reason and "retry scheduled" in reason and c.id not in queued:
            stale.append({"company_id": c.id, "name": c.canonical_name, "run_id": run.id})

    return {"window_days": days, "runs_7d": dict(runs), "hosts_7d": hosts_out,
            "retry_claims_without_queued_run": stale,
            "archive": {"used_today": ARCHIVE_BUDGET.used, "daily_max": settings().wayback_daily_max},
            "computed_at": now.isoformat()}


def cached(session_factory, *, clock=time.monotonic) -> dict:
    ttl = _env_f("OBS_CACHE_S", 60)
    with _LOCK:
        if _CACHE["body"] is not None and clock() - _CACHE["at"] < ttl:
            return _CACHE["body"]
    try:
        with session_factory() as db:
            body = collect(db)
    except Exception as e:  # /health must never fail because of observability
        return {"error": type(e).__name__}
    with _LOCK:
        _CACHE.update(at=clock(), body=body)
    return body


def reset_cache() -> None:
    with _LOCK:
        _CACHE.update(at=0.0, body=None)
