"""In-process background worker backed by the judgment_runs table (no extra infrastructure).

- Runs inside the web process (Render: single uvicorn worker); enable/disable with WORKER_ENABLED.
- Claims queued runs one at a time with a conditional UPDATE (safe even if two processes poll).
- A heartbeat is written every WORKER_HEARTBEAT_S while a run executes. Runs whose heartbeat is
  older than WORKER_STALE_S (process died / redeploy) are re-queued once, then failed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from .models import IngestLog, JudgmentRun, JudgmentStage
from .pipeline.runner import Deps, execute_run

log = logging.getLogger("kardashev.worker")
WORKER_ID = f"{os.getenv('RENDER_INSTANCE_ID', 'local')}-{uuid.uuid4().hex[:8]}"


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class Worker:
    def __init__(self, session_factory: Callable[[], Session], deps_factory: Callable[[], Deps], *,
                 poll_s: float | None = None, heartbeat_s: float | None = None, stale_s: float | None = None,
                 max_attempts: int | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.session_factory = session_factory
        self.deps_factory = deps_factory
        self.poll_s = poll_s if poll_s is not None else _env_f("WORKER_POLL_S", 15)
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else _env_f("WORKER_HEARTBEAT_S", 20)
        self.stale_s = stale_s if stale_s is not None else _env_f("WORKER_STALE_S", 180)
        self.max_attempts = max_attempts if max_attempts is not None else int(_env_f("WORKER_MAX_ATTEMPTS", 2))
        self.now = now
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self.current_run_id: int | None = None

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
                if (run.attempt or 0) < self.max_attempts:
                    run.status = "queued"
                    run.error_message = f"re-queued after interruption (attempt {run.attempt})"
                else:
                    run.status = "failed"
                    run.error_type = "interrupted"
                    run.error_message = "worker stopped mid-run (restart/redeploy); attempts exhausted"
                    run.finished_at = self.now()
                    db.add(IngestLog(company_id=run.company_id, action="judge_error", admin_id=run.triggered_by,
                                     details={"run_id": run.id, "error_type": "interrupted",
                                              "existing_scores_untouched": True}))
                run.current_stage = None
                touched.append(run.id)
            db.commit()
        if touched:
            log.warning("reconciled stale runs %s", touched)
        return touched

    def claim_next(self) -> int | None:
        with self.session_factory() as db:
            candidates = (db.query(JudgmentRun.id).filter(JudgmentRun.status == "queued")
                          .order_by(JudgmentRun.queued_at, JudgmentRun.id).limit(5).all())
            for (run_id,) in candidates:
                now = self.now()
                n = (db.query(JudgmentRun)
                     .filter(JudgmentRun.id == run_id, JudgmentRun.status == "queued")
                     .update({"status": "running", "started_at": now, "heartbeat_at": now,
                              "worker_id": WORKER_ID, "attempt": JudgmentRun.attempt + 1},
                             synchronize_session=False))
                db.commit()
                if n:
                    return run_id
        return None

    def run_one(self, run_id: int) -> str:
        with self.session_factory() as db:
            run = execute_run(db, run_id, self.deps_factory())
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

    # ---------- async loop ----------
    def wake(self):
        if self._wake is not None:
            self._wake.set()

    async def _heartbeat(self, run_id: int):
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                await asyncio.to_thread(self.beat, run_id)
            except Exception:
                log.exception("heartbeat failed")

    async def run_forever(self):
        self._wake = asyncio.Event()
        log.info("judgment worker %s started", WORKER_ID)
        while not self._stopping:
            try:
                await asyncio.to_thread(self.reconcile_stale)
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

    def stop(self):
        self._stopping = True
        self.wake()
