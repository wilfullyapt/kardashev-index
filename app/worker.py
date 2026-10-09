"""In-process background worker backed by the judgment_runs table (no extra infrastructure).

- Runs inside the web process (Render: single uvicorn worker); enable/disable with WORKER_ENABLED.
- Claims queued runs one at a time with a conditional UPDATE (safe even if two processes poll, e.g.
  the old and new instance overlapping during a deploy). Runs scheduled for an automatic retry
  (next_attempt_at in the future) wait until then.
- A heartbeat is written every WORKER_HEARTBEAT_S while a run executes. Runs whose heartbeat is
  older than WORKER_STALE_S (process died / redeploy) are re-queued and resume from their last
  completed stage, up to RUN_MAX_ATTEMPTS attempts in total.
- Graceful shutdown: on SIGTERM the worker stops claiming, lets the current stage finish (the run
  is then re-queued at its checkpoint) for up to WORKER_DRAIN_S.
- Daily sweep (in-process scheduler, SWEEP_ENABLED, default on): once a day after SWEEP_HOUR_UTC,
  re-queues companies whose latest run failed or ended unranked/withheld more than
  SWEEP_MIN_AGE_DAYS ago, within SWEEP_DAILY_COST_USD (estimated at SWEEP_EST_RUN_USD per run).
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import alerts
from .models import ACTIVE_RUN_STATUSES, Company, IngestLog, JudgmentRun, JudgmentStage
from .pipeline.config import WorkerSettings, worker_settings
from .pipeline.runner import Deps, execute_run, first_pending_stage

log = logging.getLogger("kardashev.worker")
WORKER_ID = f"{os.getenv('RENDER_INSTANCE_ID', 'local')}-{uuid.uuid4().hex[:8]}"
SWEEP_TRIGGER = "sweep"


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _aware(dt):
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


class Worker:
    def __init__(self, session_factory: Callable[[], Session], deps_factory: Callable[[], Deps], *,
                 poll_s: float | None = None, heartbeat_s: float | None = None, stale_s: float | None = None,
                 max_attempts: int | None = None, settings: WorkerSettings | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.session_factory = session_factory
        self.deps_factory = deps_factory
        self.settings = settings or worker_settings()
        self.poll_s = poll_s if poll_s is not None else _env_f("WORKER_POLL_S", 15)
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else _env_f("WORKER_HEARTBEAT_S", 20)
        self.stale_s = stale_s if stale_s is not None else _env_f("WORKER_STALE_S", 180)
        self.max_attempts = max_attempts if max_attempts is not None else self.settings.max_attempts
        self.now = now
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self.current_run_id: int | None = None
        self.started_at: datetime | None = None
        self.last_tick: datetime | None = None
        self.last_sweep_day: str | None = None

    # ---------- sync primitives (also used directly by tests) ----------
    def reconcile_stale(self) -> list[int]:
        cutoff = self.now() - timedelta(seconds=self.stale_s)
        touched = []
        with self.session_factory() as db:
            stale = (db.query(JudgmentRun)
                     .filter(JudgmentRun.status == "running",
                             (JudgmentRun.heartbeat_at < cutoff) | (JudgmentRun.heartbeat_at.is_(None)))
                     .all())
            for run in stale:
                if run.id == self.current_run_id:
                    continue
                db.query(JudgmentStage).filter(JudgmentStage.run_id == run.id,
                                               JudgmentStage.status == "running").update(
                    {"status": "interrupted", "finished_at": self.now()}, synchronize_session=False)
                run.error_type, run.error_class = "interrupted", "transient"
                if (run.attempt or 0) < self.max_attempts:
                    run.status = "queued"
                    run.next_attempt_at = None
                    run.error_message = (f"interrupted (restart/redeploy) during attempt {run.attempt}; re-queued, "
                                         f"resumes from {first_pending_stage(run.checkpoint)}")
                else:
                    run.status = "failed"
                    run.error_message = "worker stopped mid-run (restart/redeploy); attempts exhausted"
                    run.finished_at = self.now()
                    db.add(IngestLog(company_id=run.company_id, action="judge_error", admin_id=run.triggered_by,
                                     details={"run_id": run.id, "error_type": "interrupted",
                                              "existing_scores_untouched": True}))
                    alerts.notify(alerts.run_event(run, "run_failed"))
                run.current_stage = None
                touched.append(run.id)
            db.commit()
        if touched:
            log.warning("reconciled stale runs %s", touched)
        return touched

    def claim_next(self) -> int | None:
        with self.session_factory() as db:
            now = self.now()
            candidates = (db.query(JudgmentRun.id)
                          .filter(JudgmentRun.status == "queued",
                                  (JudgmentRun.next_attempt_at.is_(None)) | (JudgmentRun.next_attempt_at <= now))
                          .order_by(JudgmentRun.queued_at, JudgmentRun.id).limit(5).all())
            for (run_id,) in candidates:
                now = self.now()
                n = (db.query(JudgmentRun)
                     .filter(JudgmentRun.id == run_id, JudgmentRun.status == "queued")
                     .update({"status": "running", "started_at": now,
                              "heartbeat_at": now, "worker_id": WORKER_ID, "attempt": JudgmentRun.attempt + 1},
                             synchronize_session=False))
                db.commit()
                if n:
                    return run_id
        return None

    def _deps(self) -> Deps:
        deps = self.deps_factory()
        if deps is not None:
            if deps.retry is None:
                deps.retry = self.settings
            if deps.should_stop is None:
                deps.should_stop = lambda: self._stopping
        return deps

    def run_one(self, run_id: int) -> str:
        with self.session_factory() as db:
            run = execute_run(db, run_id, self._deps())
            return run.status

    def beat(self, run_id: int):
        with self.session_factory() as db:
            db.query(JudgmentRun).filter(JudgmentRun.id == run_id, JudgmentRun.status == "running").update(
                {"heartbeat_at": self.now()}, synchronize_session=False)
            db.commit()

    def process_next(self) -> int | None:
        """Claim and execute one queued run synchronously. Returns the run id or None."""
        self.reconcile_stale()
        run_id = self.claim_next()
        if run_id is not None:
            self.current_run_id = run_id
            try:
                self.run_one(run_id)
            finally:
                self.current_run_id = None
        return run_id

    # ---------- daily sweep ----------
    def maybe_sweep(self) -> dict | None:
        s = self.settings
        now = self.now()
        day = now.date().isoformat()
        if not s.sweep_enabled or now.hour < s.sweep_hour_utc or self.last_sweep_day == day:
            return None
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.session_factory() as db:
            done = (db.query(IngestLog.id).filter(IngestLog.action == "auto_sweep", IngestLog.timestamp >= start)
                    .first())
        self.last_sweep_day = day
        if done:
            return None
        return self.sweep()

    def sweep(self) -> dict:
        """Re-queue failed / unranked / withheld companies whose latest run is older than
        SWEEP_MIN_AGE_DAYS, within the daily cost cap. Returns a summary (also logged)."""
        s = self.settings
        now = self.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = now - timedelta(days=s.sweep_min_age_days)
        queued: list[int] = []
        with self.session_factory() as db:
            spent = (db.query(func.coalesce(func.sum(JudgmentRun.cost_usd), 0.0))
                     .filter(JudgmentRun.trigger == SWEEP_TRIGGER, JudgmentRun.queued_at >= start).scalar()) or 0.0
            pending = (db.query(JudgmentRun).filter(JudgmentRun.trigger == SWEEP_TRIGGER,
                                                    JudgmentRun.status.in_(ACTIVE_RUN_STATUSES)).count())
            left = s.sweep_daily_cost_usd - float(spent) - pending * s.sweep_est_run_usd
            latest: dict[int, JudgmentRun] = {}
            for r in db.query(JudgmentRun).order_by(JudgmentRun.id):
                latest[r.company_id] = r
            current = {c.id: c.current_run_id for c in db.query(Company)}
            ranked_current = {r.id for r in db.query(JudgmentRun).filter(
                JudgmentRun.id.in_([x for x in current.values() if x]), JudgmentRun.ranked.is_(True))} \
                if any(current.values()) else set()
            candidates = []
            for cid, r in latest.items():
                if r.status in ACTIVE_RUN_STATUSES or cid not in current:
                    continue
                needs = (r.status == "failed" or (r.status == "succeeded" and (not r.ranked or not r.published))
                         ) and current.get(cid) not in ranked_current
                ended = _aware(r.finished_at or r.queued_at)
                if needs and ended and ended < cutoff:
                    candidates.append((ended, cid))
            from .runs import enqueue_run
            for _, cid in sorted(candidates):
                if left < s.sweep_est_run_usd:
                    break
                run, created = enqueue_run(db, cid, trigger=SWEEP_TRIGGER, triggered_by="auto-sweep")
                if created:
                    queued.append(run.id)
                    left -= s.sweep_est_run_usd
            summary = {"date": now.date().isoformat(), "candidates": len(candidates), "queued_runs": queued,
                       "spent_today_usd": round(float(spent), 4), "budget_usd": s.sweep_daily_cost_usd,
                       "min_age_days": s.sweep_min_age_days, "skipped_for_budget": max(0, len(candidates) - len(queued))}
            db.add(IngestLog(action="auto_sweep", admin_id="auto-sweep", details=summary))
            db.commit()
        if queued:
            log.info("daily sweep queued runs %s", queued)
            self.wake()
        return summary

    # ---------- status (for /health) ----------
    def status(self) -> dict:
        now = self.now()
        alive = bool(self.last_tick and (now - self.last_tick).total_seconds() < max(3 * self.poll_s, 90))
        out = {"enabled": self.started_at is not None, "alive": alive, "id": WORKER_ID,
               "started_at": self.started_at.isoformat() if self.started_at else None,
               "last_tick": self.last_tick.isoformat() if self.last_tick else None,
               "current_run_id": self.current_run_id, "stopping": self._stopping,
               "last_sweep_day": self.last_sweep_day}
        try:
            with self.session_factory() as db:
                out["queue_depth"] = db.query(JudgmentRun).filter(JudgmentRun.status == "queued").count()
                out["retries_scheduled"] = db.query(JudgmentRun).filter(
                    JudgmentRun.status == "queued", JudgmentRun.next_attempt_at.isnot(None)).count()
                last = (db.query(JudgmentRun).filter(JudgmentRun.status.in_(("succeeded", "failed")))
                        .order_by(JudgmentRun.finished_at.desc(), JudgmentRun.id.desc()).first())
                out["last_run"] = ({"run_id": last.id, "status": last.status, "ranked": last.ranked,
                                    "published": last.published, "degraded": bool(last.degraded),
                                    "error_type": last.error_type,
                                    "finished_at": last.finished_at.isoformat() if last.finished_at else None}
                                   if last else None)
        except Exception as e:  # pragma: no cover - /health must not crash
            out["db_error"] = f"{type(e).__name__}"
        return out

    # ---------- async loop ----------
    def wake(self):
        if self._wake is not None:
            self._wake.set()

    async def _heartbeat(self, run_id: int):
        while True:
            await asyncio.sleep(self.heartbeat_s)
            self.last_tick = self.now()      # the loop is busy with a run, not dead
            try:
                await asyncio.to_thread(self.beat, run_id)
            except Exception:
                log.exception("heartbeat failed")

    async def run_forever(self):
        self._wake = asyncio.Event()
        self.started_at = self.now()
        log.info("judgment worker %s started", WORKER_ID)
        while not self._stopping:
            self.last_tick = self.now()
            try:
                await asyncio.to_thread(self.reconcile_stale)
                try:
                    await asyncio.to_thread(self.maybe_sweep)
                except Exception:
                    log.exception("daily sweep failed")
                if self._stopping:
                    break
                run_id = await asyncio.to_thread(self.claim_next)
                if run_id is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self.poll_s)
                    except TimeoutError:
                        pass
                    continue
                self.current_run_id = run_id
                hb = asyncio.create_task(self._heartbeat(run_id))
                try:
                    status = await asyncio.to_thread(self.run_one, run_id)
                    log.info("run %s finished: %s", run_id, status)
                finally:
                    hb.cancel()
                    self.current_run_id = None
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker loop error")
                await asyncio.sleep(self.poll_s)
        log.info("judgment worker %s stopped", WORKER_ID)

    def stop(self):
        self._stopping = True
        self.wake()
