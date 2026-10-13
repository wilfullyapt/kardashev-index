"""1.9.0: board top — live queue, health (from /health observability), spend, add-company, and the
10 s HTMX refresh that runs only while something is active."""
from datetime import UTC, datetime, timedelta

import pytest

from app import board, observability
from app.models import Company, IngestLog, JudgmentRun, Suggestion


@pytest.fixture(autouse=True)
def _fresh_obs():
    observability.reset_cache()
    yield
    observability.reset_cache()


def _company(db, name, ticker=None):
    c = Company(canonical_name=name, official_name=name.title(), industry="x", ticker=ticker)
    db.add(c)
    db.commit()
    return c


def _run(db, c, status, *, cost=None, finished=None, queued=None, next_at=None, started=None):
    r = JudgmentRun(company_id=c.id, status=status, trigger="t", triggered_by="t", attempt=1, cost_usd=cost,
                    queued_at=queued or datetime.now(UTC), finished_at=finished, next_attempt_at=next_at,
                    started_at=started, pipeline_version="pipeline-v2.13")
    db.add(r)
    db.commit()
    return r


def test_queue_buckets(db):
    now = datetime.now(UTC)
    _run(db, _company(db, "a"), "running", started=now)
    _run(db, _company(db, "b"), "queued")
    late = _run(db, _company(db, "c"), "queued", next_at=now + timedelta(hours=2))
    early = _run(db, _company(db, "d"), "queued", next_at=now + timedelta(minutes=5))
    q = board.queue(db)
    assert [len(q[k]) for k in ("running", "queued", "scheduled")] == [1, 1, 2]
    assert [x["run"].id for x in q["scheduled"]] == [early.id, late.id] and q["active"] and q["total"] == 4


def test_spend_today_and_month_in_pacific(db):
    c = _company(db, "s")
    now = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)          # 11:00 PT on Oct 12
    _run(db, c, "succeeded", cost=0.25, finished=datetime(2026, 10, 12, 8, 0, tzinfo=UTC))   # 01:00 PT today
    _run(db, c, "succeeded", cost=0.10, finished=datetime(2026, 10, 12, 6, 0, tzinfo=UTC))   # 23:00 PT Oct 11
    _run(db, c, "failed", cost=None, finished=datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    _run(db, c, "succeeded", cost=0.40, finished=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))   # last month
    s = board.spend(db, now=now)
    assert s["today"] == {"usd": 0.25, "runs": 2, "unknown": 1}
    assert s["month"] == {"usd": 0.35, "runs": 3, "unknown": 1}


def test_spend_unknown_when_no_costs(db):
    assert board.spend(db)["today"]["usd"] is None


def test_health_reshapes_observability():
    h = board.health({"window_days": 7, "runs_7d": {"succeeded": 5, "failed:timeout": 2, "failed:internal_error": 1},
                      "hosts_7d": [{"host": "x.com", "failed": 3, "total": 4}],
                      "archive": {"used_today": 12, "daily_max": 200}, "retry_claims_without_queued_run": []})
    assert h["failed_total"] == 3 and h["failures"][0] == ("timeout", 2) and h["archive"]["used_today"] == 12
    assert board.health({"error": "OperationalError"}) == {"error": "OperationalError"}


def test_board_top_polls_only_while_active(admin_client, db):
    html = admin_client.get("/admin").text
    assert 'id="board-top"' in html and "every 10s" not in html and "Nothing running" in html
    assert "Spend" in html and "unknown" in html and "System health" in html and "archive.org today" in html
    c = _company(db, "busy")
    r = _run(db, c, "queued", next_at=datetime.now(UTC) + timedelta(hours=1))
    part = admin_client.get("/admin/board/top").text
    assert 'hx-trigger="every 10s"' in part and "Scheduled retries (1)" in part and f"run #{r.id}" in part
    assert " PDT" in part or " PST" in part
    r.status = "failed"
    db.commit()
    assert "every 10s" not in admin_client.get("/admin/board/top").text


def test_add_company_creates_and_queues(admin_client, db):
    resp = admin_client.post("/admin/companies/add", data={"name": "Nebula Compute", "ticker": "nbx"},
                             follow_redirects=False)
    assert resp.status_code == 303
    c = db.query(Company).filter(Company.canonical_name == "nebula-compute").one()
    assert c.ticker == "NBX"
    run = db.query(JudgmentRun).filter(JudgmentRun.company_id == c.id).one()
    assert run.status in ("queued", "running") and run.triggered_by == "admin@example.com"
    log = db.query(IngestLog).filter(IngestLog.action == "admin_add").one()
    assert log.details["existing"] is False and log.details["run_id"] == run.id
    assert db.query(Suggestion).filter(Suggestion.id == log.suggestion_id).one().status == "approved"


def test_add_existing_by_ticker_reruns_instead_of_duplicating(admin_client, db):
    c = _company(db, "tesla", ticker="TSLA")
    admin_client.post("/admin/companies/add", data={"name": "", "ticker": "tsla"})
    assert db.query(Company).count() == 1
    assert db.query(JudgmentRun).filter(JudgmentRun.company_id == c.id).count() == 1
    flashed = admin_client.post("/admin/companies/add", data={"name": "tesla"}, follow_redirects=True).text
    assert "already exists" in flashed and db.query(JudgmentRun).filter(JudgmentRun.company_id == c.id).count() == 1


def test_add_company_needs_input_auth_and_origin(admin_client, client, db):
    assert "Enter a company name" in admin_client.post("/admin/companies/add", data={}, follow_redirects=True).text
    assert client.post("/admin/companies/add", data={"name": "x"}, follow_redirects=False).status_code in (303, 401, 403)
    assert admin_client.post("/admin/companies/add", data={"name": "x"}, headers={"origin": "https://evil.example"},
                             follow_redirects=False).status_code == 403
    assert db.query(Company).count() == 0
