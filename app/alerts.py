"""Operational alerts: nothing fails silently.

- ``config_warnings``: missing configuration that silently weakens runs (SEC user agent, xAI key).
- ``collect``: failed / degraded / withheld runs and scheduled retries in a window — rendered as the
  admin banner and served to Hermes at /internal/alerts.
- ``notify``: optional POST of a JSON event to ALERT_WEBHOOK_URL (best effort, background thread).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from .models import Company, JudgmentRun

log = logging.getLogger("kardashev.alerts")


def config_warnings(settings=None) -> list[dict]:
    from .pipeline.config import settings as pipeline_settings
    s = settings or pipeline_settings()
    out = []
    if not s.sec_user_agent:
        out.append({"key": "sec_user_agent", "level": "error",
                    "message": "SEC_EDGAR_USER_AGENT is not set: EDGAR (XBRL revenue/capex, ticker checks) is "
                               "skipped for every company and sec.gov documents may refuse our fetcher. Set it in "
                               "Render to e.g. 'Kardashev Index admin@yourdomain.com'."})
    if not (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")):
        out.append({"key": "xai_api_key", "level": "error",
                    "message": "XAI_API_KEY is not set: every judgment run will fail with config_error."})
    return out


def _aware(dt):
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def collect(db: Session, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    rows = (db.query(JudgmentRun)
            .filter((JudgmentRun.finished_at >= since) | (JudgmentRun.status.in_(("queued", "running"))))
            .order_by(JudgmentRun.id.desc()).limit(200).all())
    names = {c.id: c.official_name or c.canonical_name for c in
             db.query(Company).filter(Company.id.in_({r.company_id for r in rows}))} if rows else {}

    def item(r: JudgmentRun, **extra) -> dict:
        return {"run_id": r.id, "company_id": r.company_id, "company": names.get(r.company_id),
                "status": r.status, "attempt": r.attempt, "error_type": r.error_type, "error_class": r.error_class,
                "error": (r.error_message or "")[:300] or None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "url": f"/admin/runs/{r.id}", **extra}

    failed = [item(r) for r in rows if r.status == "failed"]
    retrying = [item(r, next_attempt_at=r.next_attempt_at.isoformat() if r.next_attempt_at else None)
                for r in rows if r.status == "queued" and r.error_type]
    degraded = [item(r, degraded=r.degraded) for r in rows if r.status == "succeeded" and r.degraded]
    withheld = [item(r, reason=(r.summary or {}).get("publish_note"))
                for r in rows if r.status == "succeeded" and not r.published]
    stuck = [item(r) for r in rows if r.status == "running" and r.heartbeat_at
             and _aware(r.heartbeat_at) < now - timedelta(minutes=10)]
    cfg = config_warnings()
    return {"generated_at": now.isoformat(), "window_days": days, "config": cfg, "failed": failed,
            "retrying": retrying, "degraded": degraded, "withheld": withheld, "stuck": stuck,
            "count": len(cfg) + len(failed) + len(retrying) + len(degraded) + len(withheld) + len(stuck)}


def notify(event: dict, *, url: str | None = None, sync: bool = False) -> bool:
    """POST ``event`` as JSON to ALERT_WEBHOOK_URL if configured. Never raises."""
    url = url or os.getenv("ALERT_WEBHOOK_URL")
    if not url:
        return False
    payload = {"service": "kardashev-index", "sent_at": datetime.now(UTC).isoformat(), **event}

    def _send():
        try:
            httpx.post(url, json=payload, timeout=8)
        except Exception as e:  # pragma: no cover - best effort
            log.warning("alert webhook failed: %s", e)

    if sync:
        _send()
    else:
        threading.Thread(target=_send, daemon=True).start()
    return True


def run_event(run: JudgmentRun, kind: str) -> dict:
    return {"event": kind, "run_id": run.id, "company_id": run.company_id, "status": run.status,
            "attempt": run.attempt, "error_type": run.error_type, "error_class": run.error_class,
            "error": (run.error_message or "")[:500] or None, "degraded": run.degraded,
            "published": run.published, "ranked": run.ranked, "coverage": run.coverage,
            "next_attempt_at": run.next_attempt_at.isoformat() if run.next_attempt_at else None,
            "admin_url": f"/admin/runs/{run.id}",
            "text": (f"Kardashev Index: {kind.replace('_', ' ')}: run #{run.id} (company #{run.company_id}), "
                     f"attempt {run.attempt}" + (f", {run.error_type}" if run.error_type else "")
                     + (f", degraded: {'; '.join(run.degraded)[:200]}" if run.degraded else ""))}
