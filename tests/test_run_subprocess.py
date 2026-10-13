"""1.14.0: each run in its own process, supervised by the in-process worker; memory caps."""
import json
import os
import sys
from dataclasses import replace

import httpx
import pytest

from app import main, memstats, runproc
from app.db import SessionLocal
from app.models import Company, IngestLog, JudgmentRun, JudgmentStage
from app.pipeline import fetch as F
from app.pipeline.config import WorkerSettings
from app.pipeline.fetch import FetchResult, HostProfiles, HttpFetcher, SourceChecker
from app.runs import enqueue_run
from app.worker import Worker

SUB = WorkerSettings(max_attempts=4, retry_delays_min=(2.0,), auto_retry=True, subprocess=True, child_term_s=1)


@pytest.fixture(autouse=True)
def _child_db(monkeypatch):
    """The run child must open the same database as this test process (other tests may change env)."""
    from app.db import engine
    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))


def _run(db, name="nvidia"):
    c = Company(canonical_name=name, industry="Unknown")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="admin", triggered_by="admin@example.com")
    return run.id


def _script_worker(code: str, settings=SUB, **kw) -> Worker:
    w = Worker(SessionLocal, lambda: None, settings=settings, **kw)
    w.child_poll_s = 0.05
    w.child_argv = lambda run_id: [sys.executable, "-c", code]
    return w


def _claimed(db, w):
    rid = _run(db)
    assert w.claim_next() == rid
    return rid


# ---------- the real child: python -m app.runproc ----------

def test_real_child_runs_the_whole_pipeline_in_its_own_process(db, monkeypatch):
    monkeypatch.setenv("WORKER_CHILD_DEPS", "tests.child_deps:deps")
    w = Worker(SessionLocal, lambda: None, settings=SUB)
    rid = _run(db)
    assert w.process_next() == rid
    db.expire_all()
    run = db.get(JudgmentRun, rid)
    assert run.status == "succeeded", (run.error_type, run.error_message)
    mem = run.summary["memory"]
    assert mem["mode"] == "subprocess" and mem["pid"] != os.getpid() and mem["pid"] == w.last_child["pid"]
    assert set(mem["stages_rss_mb"]) >= {"start", "fetch", "extract", "judge"} and mem["peak_rss_mb"] > 0
    assert w.last_child["exit_code"] == 0 and w.last_child["peak_rss_mb"] > 0
    assert w.last_release is not None


def test_supervisor_rss_is_not_grown_by_a_run(db, monkeypatch):
    """The owner's Render graph stair-stepped up per run and stayed there. In subprocess mode all of a
    run's allocations live in the child: the supervisor's RSS must come back to baseline."""
    monkeypatch.setenv("WORKER_CHILD_DEPS", "tests.child_deps:deps")
    w = Worker(SessionLocal, lambda: None, settings=SUB)
    _run(db, "warmup")
    w.process_next()                       # warm imports / connection pool once
    memstats.release()
    base = memstats.rss_mb()
    for i in range(3):
        _run(db, f"co{i}")
        assert w.process_next() is not None
    after = w.last_release["rss_after_mb"]
    assert base is not None and after is not None
    assert after - base < 15, (base, after)


def test_in_process_fallback_still_works(db):
    from tests import fakes
    w = Worker(SessionLocal, lambda: fakes.make_deps(fakes.world_llm()), settings=replace(SUB, subprocess=False))
    rid = _run(db)
    w.process_next()
    db.expire_all()
    run = db.get(JudgmentRun, rid)
    assert run.status == "succeeded" and run.summary["memory"]["mode"] == "in-process"
    assert w.status()["mode"] == "in-process"


# ---------- a child that dies ----------

def test_sigkilled_child_is_oom_transient_and_requeued(db):
    w = _script_worker("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    rid = _claimed(db, w)
    db.add(JudgmentStage(run_id=rid, stage="fetch", status="running"))
    db.commit()
    assert w.run_one(rid) == "queued"
    db.expire_all()
    run = db.get(JudgmentRun, rid)
    assert (run.error_type, run.error_class) == ("oom", "transient")
    assert "out of memory" in run.error_message and run.next_attempt_at is not None
    assert db.query(JudgmentStage).filter_by(run_id=rid).one().status == "interrupted"
    assert db.query(IngestLog).filter_by(action="run_retry_scheduled").count() == 1
    assert w.last_child["exit_code"] == -9


def test_killed_child_fails_when_attempts_are_used_up(db):
    w = _script_worker("import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
                       settings=replace(SUB, max_attempts=1))
    rid = _claimed(db, w)
    assert w.run_one(rid) == "failed"
    db.expire_all()
    run = db.get(JudgmentRun, rid)
    assert run.error_type == "oom" and run.finished_at is not None
    assert db.query(IngestLog).filter_by(action="judge_error").count() == 1


@pytest.mark.parametrize("code,etype", [
    ("import os, signal; os.kill(os.getpid(), signal.SIGSEGV)", "killed"),
    ("raise SystemExit(3)", "crashed"),
])
def test_other_child_deaths(db, code, etype):
    w = _script_worker(code)
    rid = _claimed(db, w)
    assert w.run_one(rid) == "queued"
    db.expire_all()
    assert db.get(JudgmentRun, rid).error_type == etype


def test_child_that_finished_the_run_is_left_alone(db):
    w = _script_worker("pass")       # exit 0 but the run is still "running": treated as a crash
    rid = _claimed(db, w)
    with SessionLocal() as s:
        s.get(JudgmentRun, rid).status = "succeeded"
        s.commit()
    assert w.run_one(rid) == "succeeded"
    db.expire_all()
    assert db.get(JudgmentRun, rid).error_type is None


def test_run_timeout_stops_the_child(db):
    w = _script_worker("import time; time.sleep(30)")
    w.run_timeout_s = 0.3
    rid = _claimed(db, w)
    assert w.run_one(rid) == "queued"
    db.expire_all()
    assert db.get(JudgmentRun, rid).error_type == "timeout"


def _stop_later(w, after_s=0.6):
    import threading
    threading.Timer(after_s, lambda: setattr(w, "_stopping", True)).start()


def test_shutdown_sends_sigterm_then_sigkill_and_requeues_at_checkpoint(db, tmp_path):
    flag = tmp_path / "term"
    code = (f"import signal, time\n"
            f"signal.signal(signal.SIGTERM, lambda *a: open({str(flag)!r}, 'w').write('x'))\n"
            f"time.sleep(30)\n")
    w = _script_worker(code)
    rid = _claimed(db, w)
    _stop_later(w)
    assert w.run_one(rid) == "queued"          # child gone: re-queued now, attempt not used up
    assert flag.exists() and w.last_child["exit_code"] == -9
    db.expire_all()
    run = db.get(JudgmentRun, rid)
    assert run.attempt == 0 and run.error_type is None and "shutdown" in run.error_message
    assert run.next_attempt_at is None


def test_real_child_pauses_on_sigterm(db, monkeypatch):
    """SIGTERM before the first stage: execute_run pauses, run re-queued without using an attempt."""
    monkeypatch.setenv("WORKER_CHILD_DEPS", "tests.child_deps:deps")
    w = Worker(SessionLocal, lambda: None, settings=SUB)
    rid = _claimed(db, w)
    w._stopping = True       # SIGTERM right at spawn, possibly before the child installs its handler
    assert w.run_one(rid) in ("queued", "succeeded")   # a quick run may finish before the signal lands
    db.expire_all()
    run = db.get(JudgmentRun, rid)
    if run.status == "queued":
        assert "shutdown" in run.error_message and run.attempt == 0


def test_health_reports_memory_and_mode(client, monkeypatch):
    w = Worker(SessionLocal, lambda: None, settings=SUB)
    w.started_at = w.last_tick = w.now()
    monkeypatch.setattr(main, "worker", w)
    body = client.get("/health").json()["worker"]
    assert body["mode"] == "subprocess" and body["rss_mb"] > 0 and body["peak_rss_mb"] > 0
    assert "last_child" in body and "last_release" in body


# ---------- memory caps ----------

def _fetcher(body: bytes, ctype: str, **kw):
    t = httpx.MockTransport(lambda r: httpx.Response(200, content=body, headers={"content-type": ctype}))
    return HttpFetcher(transport=t, resolve=lambda h, p: [(2, 1, 6, "", ("93.184.216.34", 0))], **kw)


def test_html_is_capped_at_html_max_bytes():
    res = _fetcher(b"<html>" + b"a" * 2_000_000, "text/html", html_max_bytes=1_000_000,
                   pdf_max_bytes=500_000).get("https://a.example/")
    assert res.truncated and len(res.body) <= 1_000_000


def test_pdf_over_inprocess_cap_goes_to_the_large_pdf_path():
    f = _fetcher(b"%PDF-1.7\n" + b"0" * 800_000, "application/pdf", html_max_bytes=2_000_000,
                 pdf_max_bytes=500_000)
    res = f.get("https://a.example/r.pdf")
    assert res.truncated and len(res.body) <= 500_000
    a = SourceChecker(f).assess(res)
    assert a.status == "error" and "in-process cap" in a.reason
    from app.pipeline.largepdf import Result, rescue_oversized
    seen = []
    out = rescue_oversized("https://a.example/r.pdf", res, a,
                           lambda url: seen.append(url) or Result(ok=True, text="energy " * 100, title="R"))
    assert seen == ["https://a.example/r.pdf"] and out.status == "ok" and res.body == b""


def test_without_kind_caps_only_max_bytes_applies():
    res = _fetcher(b"<html>" + b"a" * 300_000, "text/html", max_bytes=1_000_000).get("https://a.example/")
    assert not res.truncated


def test_big_html_is_cut_before_parsing(monkeypatch):
    monkeypatch.setattr(F, "HTML_PARSE_MAX_BYTES", 5_000)
    body = b"<html><head><title>T</title></head><body><p>" + b"word " * 10_000 + b"</p><p>TAILMARK</p></body></html>"
    title, text = F.extract_text("text/html", body)
    assert title == "T" and "TAILMARK" not in text and len(text) < 6_000


def test_drop_body_keeps_size():
    r = FetchResult(url="u", body=b"x" * 10)
    r.drop_body()
    assert r.body == b"" and r.size == 10


def test_fetch_stage_drops_bodies_but_records_byte_size(db):
    from tests import fakes
    from app.models import Source
    w = Worker(SessionLocal, lambda: fakes.make_deps(fakes.world_llm()), settings=replace(SUB, subprocess=False))
    rid = _run(db)
    w.process_next()
    db.expire_all()
    srcs = db.query(Source).filter_by(run_id=rid).all()
    fetched = [s for s in srcs if s.origin != "edgar"]     # EDGAR facts are not page fetches
    assert fetched and all(s.byte_size for s in fetched)    # incl. dead/soft-404 pages (size kept on drop)


def test_pdf_slots_and_child_cap_defaults():
    from app.pipeline.largepdf import Limits
    assert F._PDF_SLOTS._initial_value == 1
    assert Limits().memory_mb == 192


# ---------- header-profile memory survives the per-run process ----------

def test_host_profiles_round_trip(tmp_path):
    p = str(tmp_path / "prof.json")
    a = HostProfiles()
    a.remember("tesla.com", "browser")
    runproc.save_profiles(a, p)
    b = HostProfiles()
    assert runproc.load_profiles(b, p) == 1 and b.get("tesla.com") == "browser"
    with open(p) as fh:
        assert json.load(fh)["tesla.com"][0] == "browser"




def test_big_html_uses_lxml_and_matches_beautifulsoup_text(monkeypatch):
    body = (b"<html><head><title>Annual report</title><style>.x{}</style></head><body><table>"
            + b"".join(b"<tr><td>Item %d</td><td>%d MWh</td></tr>" % (i, i) for i in range(3000))
            + b"</table><script>var a=1;</script><p>END</p></body></html>")
    small = F.extract_text("text/html", body)
    monkeypatch.setattr(F, "HTML_LIGHT_PARSE_BYTES", 1_000)
    big = F.extract_text("text/html", body)
    assert big == small and "var a" not in big[1] and big[1].endswith("END")


def test_skeleton_offsets_are_compact():
    from array import array
    from app.pipeline.evidence import SourceText
    sk, pos = SourceText("Capital expenditures were $4.2 billion.").sk()
    assert isinstance(pos, array) and len(pos) == len(sk)
