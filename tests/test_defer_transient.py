"""1.5.0 / pipeline-v2.11: when the company's energy disclosure (or every source) failed only for
reasons that pass (rate limit, outage, timeout, Wayback unable to run), the run is deferred through the
existing transient-retry path instead of finishing and publishing; the final attempt completes."""
from types import SimpleNamespace

import pytest

from app.models import Company, JudgmentRun
from app.pipeline.runner import execute_run, transient_reason
from app.runs import enqueue_run
from tests import fakes
from tests.test_energy_unreadable import IMPACT_URL, _world


def _src(status="dead", http=None, reason=None, att=None):
    return SimpleNamespace(status=status, http_status=http, reject_reason=reason, archive_attempt=att)


@pytest.mark.parametrize("src,expect", [
    (_src(http=429), "HTTP 429"),
    (_src(http=503), "HTTP 503"),
    (_src(reason="ReadTimeout: timed out"), "timeout"),
    (_src(http=403, att={"outcome": "rate_limited", "note": "Wayback skipped: archive.org rate-limited us"}),
     "HTTP 403, Wayback rate_limited"),
    (_src(http=403, att={"outcome": "capped", "note": "per-run cap"}), "HTTP 403, Wayback capped"),
    (_src(http=403, att={"outcome": "unavailable", "note": "Wayback lookup failed (CDX unavailable (HTTP 503))"}),
     "HTTP 403, Wayback unavailable"),
    (_src(http=403, att={"outcome": "unavailable", "note": "Wayback lookup failed (CDX HTTP 404; availability API HTTP 404)"}),
     None),
    (_src(http=403, att={"outcome": "not_found"}), None),
    (_src(http=403), None),
    (_src(http=404), None),
    (_src(status="thin", http=200), None),
    (_src(status="error", http=200, reason="unreadable PDF"), None),
])
def test_transient_reason(src, expect):
    assert transient_reason(src) == expect


def _company(db, name):
    c = Company(canonical_name=name, official_name=name.title(), industry="Autos")
    db.add(c)
    db.commit()
    return c


def test_energy_source_rate_limited_defers_the_run(db):
    llm, routes = _world((429, "text/html", "Too Many Requests"))
    c = _company(db, "deferco")
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run.attempt = 1
    db.commit()
    run = execute_run(db, run.id, fakes.make_deps(llm, routes=routes, retry=fakes.RETRY))
    assert run.status == "queued" and run.error_type == "fetch_transient" and run.error_class == "transient"
    assert run.next_attempt_at is not None and not run.published
    assert IMPACT_URL in run.error_message
    db.refresh(c)
    assert c.current_run_id is None                  # nothing published
    assert "extract" not in (run.checkpoint or {}).get("done", [])


def test_final_attempt_completes_as_before(db):
    llm, routes = _world((429, "text/html", "Too Many Requests"))
    c = _company(db, "finalco")
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run.attempt = fakes.RETRY.max_attempts
    db.commit()
    run = execute_run(db, run.id, fakes.make_deps(llm, routes=routes, retry=fakes.RETRY))
    assert run.status == "succeeded" and run.summary["rank_basis"] == "energy_unreadable"


def test_permanent_block_is_not_deferred(db):
    llm, routes = _world((403, "text/html", "denied"))        # Wayback: nothing archived (404s)
    c = _company(db, "blockco")
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run.attempt = 1
    db.commit()
    run = execute_run(db, run.id, fakes.make_deps(llm, routes=routes, retry=fakes.RETRY))
    assert run.status == "succeeded" and run.summary["rank_basis"] == "energy_unreadable"


def test_can_be_switched_off(db):
    llm, routes = _world((429, "text/html", "Too Many Requests"))
    c = _company(db, "offco")
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run.attempt = 1
    db.commit()
    run = execute_run(db, run.id, fakes.make_deps(llm, routes=routes, retry=fakes.RETRY,
                                                  defer_transient_fetch=False))
    assert run.status == "succeeded"
    assert db.query(JudgmentRun).filter(JudgmentRun.status == "queued", JudgmentRun.trigger == "test").count() == 0
