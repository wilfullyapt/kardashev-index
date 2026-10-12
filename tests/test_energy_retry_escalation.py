"""1.6.0 / pipeline-v2.12: up to ENERGY_RETRY_MAX (3) energy retries with escalating delays
(6, 24, 72 h), each fetching the unreadable documents a different way."""
from datetime import timedelta

from app.models import IngestLog, JudgmentRun, Source
from app.pipeline.fetch import USER_AGENT
from app.pipeline.runner import ENERGY_RETRY_STRATEGIES, ENERGY_RETRY_TRIGGER, execute_run
from tests import fakes
from tests.test_energy_unreadable import IMPACT_URL, _run, _world


def _retries(db):
    return db.query(JudgmentRun).filter(JudgmentRun.trigger == ENERGY_RETRY_TRIGGER).order_by(JudgmentRun.id).all()


def test_three_escalating_retries_with_different_strategies_then_stop(db):
    llm, routes = _world((403, "text/html", "denied"))
    _c, first = _run(db, llm, routes)
    seen = []
    prev = first
    for n, (hours, strategy) in enumerate(zip((6, 24, 72), ENERGY_RETRY_STRATEGIES, strict=True), start=1):
        retry = _retries(db)[-1]
        cp = retry.checkpoint
        assert cp["strategy"] == strategy and cp["retry_number"] == n and cp["retry_urls"] == [IMPACT_URL]
        delay = retry.next_attempt_at.replace(tzinfo=None) - prev.finished_at.replace(tzinfo=None)
        assert timedelta(hours=hours - 1) < delay < timedelta(hours=hours + 1)
        assert f"(automatic retry {n})" in prev.summary["not_ranked_reason"]
        seen.append(strategy)
        llm2, routes2 = _world((403, "text/html", "denied"))
        prev = execute_run(db, retry.id, fakes.make_deps(llm2, routes=routes2))
        assert prev.summary["rank_basis"] == "energy_unreadable"
    assert len(_retries(db)) == 3
    assert "no automatic retry queued" in prev.summary["not_ranked_reason"]
    assert db.query(IngestLog).filter(IngestLog.action == "energy_retry_skipped").count() == 1
    assert seen == ["archive_first", "browser_first", "standard"]


def test_archive_first_retry_uses_the_wayback_copy_before_the_origin(db):
    llm, routes = _world((403, "text/html", "denied"))
    _c, _first = _run(db, llm, routes)
    retry = _retries(db)[-1]
    copy = fakes.world_routes()[fakes.SUSTAIN_URL]
    llm2, _ = _world((403, "text/html", "denied"))
    routes2 = {**fakes.world_routes({IMPACT_URL: (403, "text/html", "denied")}),
               **fakes.wayback_routes(IMPACT_URL, copy)}
    f = fakes.FakeFetcher(routes2)
    execute_run(db, retry.id, fakes.make_deps(llm2, fetcher=f))
    src = db.query(Source).filter(Source.run_id == retry.id, Source.url == IMPACT_URL).one()
    assert src.archive_url and "archive first" in src.reject_reason
    assert IMPACT_URL not in f.calls            # origin never asked: the archived copy was used


def test_browser_first_retry_sends_browser_headers(db):
    llm, routes = _world((403, "text/html", "denied"))
    _c, _first = _run(db, llm, routes)
    retry = _retries(db)[-1]
    retry.checkpoint = {**retry.checkpoint, "strategy": "browser_first"}
    db.commit()
    captured = {}

    class Spy(fakes.FakeFetcher):
        def get(self, url, *, headers=None, max_bytes=None, **kw):
            if url == IMPACT_URL:
                captured.update(headers or {})
            return super().get(url, headers=headers, max_bytes=max_bytes)

    llm2, routes2 = _world((403, "text/html", "denied"))
    execute_run(db, retry.id, fakes.make_deps(llm2, fetcher=Spy(routes2)))
    assert captured.get("User-Agent") == USER_AGENT


def test_unreadable_url_is_retried_even_if_research_drops_it(db):
    llm, routes = _world((403, "text/html", "denied"))
    _c, _first = _run(db, llm, routes)
    retry = _retries(db)[-1]
    llm2 = fakes.world_llm(extract=[fakes.extract_ok])          # research no longer lists IMPACT_URL
    execute_run(db, retry.id, fakes.make_deps(llm2, routes=routes))
    src = db.query(Source).filter(Source.run_id == retry.id, Source.url == IMPACT_URL).one()
    assert src.origin == "retry"


def test_legacy_single_delay_setting_still_applies(db, monkeypatch):
    from app.pipeline.config import settings
    monkeypatch.delenv("ENERGY_RETRY_DELAYS_HOURS", raising=False)
    monkeypatch.setenv("ENERGY_RETRY_DELAY_HOURS", "12")
    assert settings().energy_retry_delays_hours == (12.0,)
    monkeypatch.delenv("ENERGY_RETRY_DELAY_HOURS")
    assert settings().energy_retry_delays_hours == (6.0, 24.0, 72.0) and settings().energy_retry_max == 3
