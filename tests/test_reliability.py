"""Fault injection: xAI 429/500/timeouts, fetch retries and SEC headers, worker kill mid-stage with
resume from checkpoint, automatic retry schedule, graceful pause, daily sweep cap, /health and
/internal/alerts. No network, no paid calls."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import alerts, main
from app.db import SessionLocal
from app.models import Company, IngestLog, JudgmentRun, JudgmentStage, Source
from app.pipeline import methodology as meth
from app.pipeline.config import WorkerSettings
from app.pipeline.fetch import HttpFetcher, RateLimiter
from app.pipeline.llm import LLMError, XAIClient
from app.runs import enqueue_run
from app.worker import Worker
from tests import fakes
from tests.conftest import HERMES

CHAT = {"model": "grok-4.3", "choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
PUBLIC_IP = lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]


def _utc(dt):
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 8, 17, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def _company(db, name="nvidia"):
    c = Company(canonical_name=name, industry="Unknown")
    db.add(c)
    db.commit()
    return c


# ------------------------------------------------------------------ xAI client
def test_xai_honours_retry_after_and_retries_5xx_then_succeeds():
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": "rate limited"})
        if len(seen) == 2:
            return httpx.Response(500, json={"error": "internal"})
        return httpx.Response(200, json=CHAT)
    slept = []
    c = XAIClient("k", transport=httpx.MockTransport(handler), sleep=slept.append, rand=lambda: 0.0)
    res = c.chat(system="s", user="u", model="grok-4.3", max_tokens=10)
    assert res.text == "{\"ok\": true}" and len(seen) == 3
    assert slept[0] == 7 and slept[1] == pytest.approx(2.0)        # Retry-After, then backoff 4/2 + 0


def test_xai_timeouts_exhaust_retries_as_retryable_error():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    slept = []
    c = XAIClient("k", transport=httpx.MockTransport(handler), sleep=slept.append, max_retries=3, rand=lambda: 1.0)
    with pytest.raises(LLMError) as e:
        c.chat(system="s", user="u", model="grok-4.3", max_tokens=10)
    assert e.value.retryable and "timeout" in str(e.value)
    assert slept == [2.0, 4.0, 8.0]


def test_xai_400_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": "bad request"})
    with pytest.raises(LLMError) as e:
        XAIClient("k", transport=httpx.MockTransport(handler), sleep=lambda s: None).chat(
            system="s", user="u", model="m", max_tokens=1)
    assert not e.value.retryable and len(calls) == 1


# ------------------------------------------------------------------ fetch
def test_fetch_retries_429_with_retry_after_and_uses_browser_headers():
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": "3"})
        if len(seen) == 2:
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>")
    slept = []
    f = HttpFetcher(transport=httpx.MockTransport(handler), resolve=PUBLIC_IP, sleep=slept.append)
    res = f.get("https://www.example.com/report")
    assert res.status == 200 and res.attempts == 3 and slept[0] == 3
    ua = seen[0].headers["user-agent"]
    assert "Mozilla/5.0" in ua and "bot" not in ua.lower() and "accept-language" in seen[0].headers


def test_fetch_does_not_retry_403():
    calls = []
    f = HttpFetcher(transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(403)),
                    resolve=PUBLIC_IP, sleep=lambda s: None)
    assert f.get("https://www.example.com/x").status == 403 and len(calls) == 1


def test_sec_requests_carry_the_declared_user_agent_and_are_rate_limited():
    seen = []
    waits = []

    class Limiter:
        def wait(self):
            waits.append(1)
    f = HttpFetcher(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={})),
                    resolve=PUBLIC_IP, sleep=lambda s: None, sec_user_agent=fakes.SEC_UA, sec_limiter=Limiter())
    f.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json")
    f.get("https://www.example.com/")
    assert seen[0].headers["user-agent"] == fakes.SEC_UA and len(waits) == 1
    assert seen[1].headers["user-agent"] != fakes.SEC_UA


def test_rate_limiter_spaces_requests():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(round(s, 3))
        t[0] += s
    rl = RateLimiter(8, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert slept == [0.125, 0.125]


def test_fetch_403_falls_back_to_wayback_and_is_labelled(db):
    llm = fakes.world_llm()
    ts = "20250101000000"
    routes = fakes.world_routes({
        fakes.SUSTAIN_URL: (403, "text/html", "<html><title>Forbidden</title></html>"),
        **fakes.wayback_routes(fakes.SUSTAIN_URL, fakes.world_routes()[fakes.SUSTAIN_URL], ts),
    })
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    from app.pipeline.runner import execute_run
    run = execute_run(db, run.id, fakes.make_deps(llm, routes=routes))
    assert run.status == "succeeded" and run.ranked
    src = db.query(Source).filter_by(run_id=run.id, url=fakes.SUSTAIN_URL).one()
    assert src.status == "ok" and src.is_primary and src.archive_timestamp == ts
    assert src.archive_attempt["outcome"] == "used" and src.archive_attempt["lookup"] == "cdx"
    from fastapi.testclient import TestClient
    page = TestClient(main.app).get(f"/companies/{c.id}").text
    assert "archived copy (Wayback Machine, 2025-01-01)" in page


# ------------------------------------------------------------------ worker: kill, resume, retries
class Killed(BaseException):
    """Simulates the process dying mid-stage (not caught by the pipeline's error handling)."""


def die(_user):
    raise Killed()


def test_worker_killed_mid_stage_resumes_from_checkpoint_without_repeating_paid_calls(db):
    clock = Clock()
    llm = fakes.world_llm(extract=[die, fakes.extract_ok])
    deps = fakes.make_deps(llm, retry=fakes.RETRY, now=clock)
    w1 = Worker(SessionLocal, lambda: deps, settings=fakes.RETRY, now=clock, stale_s=180)
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    with pytest.raises(Killed):
        w1.process_next()
    db.expire_all()
    r = db.get(JudgmentRun, run.id)
    assert r.status == "running" and set(r.checkpoint["done"]) >= {"resolve", "research", "edgar", "fetch"}
    n_sources = db.query(Source).filter_by(run_id=run.id).count()

    clock.advance(minutes=5)                     # heartbeat is now stale; a new instance starts
    w2 = Worker(SessionLocal, lambda: deps, settings=fakes.RETRY, now=clock, stale_s=180)
    assert w2.reconcile_stale() == [run.id]
    db.expire_all()
    r = db.get(JudgmentRun, run.id)
    assert r.status == "queued" and r.error_type == "interrupted" and "resumes from extract" in r.error_message
    assert w2.process_next() == run.id
    db.expire_all()
    r = db.get(JudgmentRun, run.id)
    assert r.status == "succeeded" and r.ranked and r.attempt == 2, r.error_message
    assert llm.stages_called() == ["resolve", "research", "extract", "extract", "judge"]
    assert db.query(Source).filter_by(run_id=run.id).count() == n_sources   # fetch not repeated


def test_transient_failures_retry_at_2_10_60_minutes_then_degrade_on_final_attempt(db):
    clock = Clock()
    llm = fakes.world_llm(judge=[fakes.api_error()])
    deps = fakes.make_deps(llm, retry=fakes.RETRY, now=clock)
    w = Worker(SessionLocal, lambda: deps, settings=fakes.RETRY, now=clock)
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    gaps = []
    for _ in range(3):
        assert w.process_next() == run.id
        db.expire_all()
        r = db.get(JudgmentRun, run.id)
        assert r.status == "queued" and r.error_class == "transient" and r.published is False
        nxt = _utc(r.next_attempt_at)
        gaps.append(round((nxt - clock()).total_seconds() / 60))
        assert w.process_next() is None           # not before next_attempt_at
        clock.t = nxt
    assert gaps == [2, 10, 60]
    assert w.process_next() == run.id             # attempt 4 = final: judge failure degrades, not fails
    db.expire_all()
    r = db.get(JudgmentRun, run.id)
    assert r.status == "succeeded" and r.attempt == 4 and r.ranked
    assert any(d.startswith("judge") for d in r.degraded)
    # gathering stages ran once; only the judge was retried
    assert llm.stages_called().count("resolve") == 1 and llm.stages_called().count("extract") == 1
    assert llm.stages_called().count("judge") == 4
    logs = db.query(IngestLog).filter_by(action="run_retry_scheduled").count()
    assert logs == 3


def test_permanent_errors_are_not_retried(db):
    clock = Clock()
    w = Worker(SessionLocal, lambda: fakes.make_deps(None, retry=fakes.RETRY, now=clock), settings=fakes.RETRY,
               now=clock)
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    w.process_next()
    db.expire_all()
    r = db.get(JudgmentRun, run.id)
    assert r.status == "failed" and r.error_type == "config_error" and r.error_class == "permanent"


def test_shutdown_pauses_between_stages_without_using_an_attempt(db):
    llm = fakes.world_llm()
    deps = fakes.make_deps(llm, retry=fakes.RETRY)
    deps.should_stop = lambda: len(llm.calls) >= 1      # SIGTERM arrives during resolve
    w = Worker(SessionLocal, lambda: deps, settings=fakes.RETRY)
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    w.process_next()
    db.expire_all()
    r = db.get(JudgmentRun, run.id)
    assert r.status == "queued" and r.attempt == 0 and r.checkpoint["done"] == ["resolve"]
    assert "paused for shutdown" in r.error_message
    deps.should_stop = None
    w.process_next()
    db.expire_all()
    assert db.get(JudgmentRun, run.id).status == "succeeded"
    assert llm.stages_called().count("resolve") == 1


def test_rerun_while_retry_scheduled_runs_it_now(db, admin_client, monkeypatch):
    clock = Clock()
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run.next_attempt_at, run.error_type = clock() + timedelta(minutes=60), "api_error"
    db.commit()
    woke = []
    monkeypatch.setattr(main.worker, "wake", lambda: woke.append(1))
    r = admin_client.post(f"/internal/rerun-judgment/{c.id}", follow_redirects=False, headers={"accept": "text/html"})
    assert r.status_code in (202, 303)
    db.expire_all()
    assert db.get(JudgmentRun, run.id).next_attempt_at is None and woke


# ------------------------------------------------------------------ extraction / judge degradation
def test_empty_extraction_is_retried_once_on_other_excerpts(db):
    llm = fakes.world_llm(extract=[{"figures": [], "claims": []}, fakes.extract_ok])
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    from app.pipeline.runner import execute_run
    run = execute_run(db, run.id, fakes.make_deps(llm))
    assert run.status == "succeeded" and run.ranked
    st = db.query(JudgmentStage).filter_by(run_id=run.id, stage="extract").one()
    assert st.detail["retried_with_other_excerpts"] and "input_retry" in st.detail and st.detail["figures_verified"]
    assert llm.stages_called().count("extract") == 2


def test_judge_garbage_on_final_attempt_keeps_measured_work(db):
    llm = fakes.world_llm(judge=["this is not json"])
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    from app.pipeline.runner import execute_run
    run = execute_run(db, run.id, fakes.make_deps(llm))
    assert run.status == "succeeded" and run.ranked and run.published
    assert any(d.startswith("judge") for d in run.degraded)


# ------------------------------------------------------------------ daily sweep
def test_daily_sweep_requeues_old_unranked_runs_within_the_cost_cap(db):
    clock = Clock()
    clock.t = clock.t.replace(hour=11)
    old = clock() - timedelta(days=8)
    for i in range(8):
        c = _company(db, f"co{i}")
        r = JudgmentRun(company_id=c.id, status="succeeded", ranked=False, published=True, trigger="test",
                        triggered_by="t", queued_at=old, finished_at=old, attempt=1)
        db.add(r)
        db.commit()
        c.current_run_id = r.id
        db.commit()
    fresh = _company(db, "fresh")             # too recent
    db.add(JudgmentRun(company_id=fresh.id, status="succeeded", ranked=False, trigger="test", triggered_by="t",
                       queued_at=clock(), finished_at=clock(), attempt=1))
    db.commit()
    s = WorkerSettings(sweep_enabled=True, sweep_hour_utc=10, sweep_min_age_days=7, sweep_daily_cost_usd=2.0,
                       sweep_est_run_usd=0.35)
    w = Worker(SessionLocal, lambda: None, settings=s, now=clock)
    summary = w.maybe_sweep()
    assert summary["candidates"] == 8 and len(summary["queued_runs"]) == 5     # 5 × $0.35 ≤ $2
    assert summary["skipped_for_budget"] == 3
    assert w.maybe_sweep() is None                                             # once a day
    w2 = Worker(SessionLocal, lambda: None, settings=s, now=clock)
    assert w2.maybe_sweep() is None                                            # logged, survives restart
    clock.t = clock.t.replace(hour=9) + timedelta(days=1)
    assert w2.maybe_sweep() is None                                            # before SWEEP_HOUR_UTC


def test_sweep_disabled(db):
    w = Worker(SessionLocal, lambda: None, settings=WorkerSettings(sweep_enabled=False))
    assert w.maybe_sweep() is None


# ------------------------------------------------------------------ /health and alerts
def test_health_splits_queued_now_from_scheduled_retries(client, db, monkeypatch):
    clock = Clock()
    w = Worker(SessionLocal, lambda: None, now=clock)
    w.started_at = w.last_tick = clock()
    monkeypatch.setattr(main, "worker", w)
    tomorrow = clock() + timedelta(hours=23, minutes=44)
    for name, nxt in (("a", None), ("b", clock() - timedelta(minutes=1)), ("c", tomorrow)):
        db.add(JudgmentRun(company_id=_company(db, name).id, status="queued", trigger="t", triggered_by="t",
                           next_attempt_at=nxt))
    db.commit()
    wk = client.get("/health").json()["worker"]
    assert wk["queued_now"] == 2 and wk["queue_depth"] == 2           # the due retry counts as queued now
    assert wk["scheduled"] == 1 and wk["retries_scheduled"] == 1
    assert wk["next_scheduled_at"].startswith(tomorrow.replace(tzinfo=None).isoformat()[:16])


def test_health_reports_worker_queue_and_last_run(client, db, monkeypatch):
    clock = Clock()
    w = Worker(SessionLocal, lambda: None, now=clock)
    w.started_at = w.last_tick = clock()
    monkeypatch.setattr(main, "worker", w)
    c = _company(db)
    enqueue_run(db, c.id, trigger="test", triggered_by="t")
    db.add(JudgmentRun(company_id=_company(db, "x").id, status="failed", error_type="api_error", trigger="t",
                       triggered_by="t", finished_at=clock(), attempt=4))
    db.commit()
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["pipeline_version"] == meth.PIPELINE_VERSION
    wk = body["worker"]
    assert wk["alive"] and wk["queue_depth"] == 1 and wk["last_run"]["status"] == "failed"
    assert body["config"]["sec_edgar_user_agent"] is False
    clock.advance(minutes=10)
    assert client.get("/health").json()["status"] == "degraded"               # loop stopped ticking


def test_internal_alerts_and_admin_banner(client, admin_client, db):
    c = _company(db)
    now = datetime.now(UTC)
    db.add(JudgmentRun(company_id=c.id, status="failed", error_type="api_error", error_message="HTTP 503",
                       trigger="t", triggered_by="t", finished_at=now, attempt=4))
    db.add(JudgmentRun(company_id=_company(db, "d").id, status="succeeded", published=True, ranked=True,
                       degraded=["judge: budget cap reached"], trigger="t", triggered_by="t", finished_at=now))
    db.commit()
    assert client.get("/internal/alerts").status_code in (401, 403)
    a = client.get("/internal/alerts", headers=HERMES).json()
    assert len(a["failed"]) == 1 and len(a["degraded"]) == 1
    assert any(w["key"] == "sec_user_agent" for w in a["config"])
    page = admin_client.get("/admin").text
    assert "SEC_EDGAR_USER_AGENT is not set" in page and "Failed · 1" in page and "Degraded · 1" in page


def test_alert_webhook_payload(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts.httpx, "post", lambda url, json, timeout: sent.append((url, json)))
    run = JudgmentRun(id=7, company_id=3, status="failed", attempt=4, error_type="api_error", error_class="transient")
    assert alerts.notify(alerts.run_event(run, "run_failed"), url="https://hooks.example/x", sync=True)
    assert sent[0][0] == "https://hooks.example/x"
    body = sent[0][1]
    assert body["event"] == "run_failed" and body["run_id"] == 7 and "run #7" in body["text"]
    assert alerts.notify({"event": "x"}) is False                             # no URL configured
