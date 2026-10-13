"""1.14.1: 'Run now' for a company whose active run is scheduled for later."""
from datetime import UTC, datetime, timedelta

import pytest

from app import main
from app.models import Company, IngestLog, JudgmentRun
from app.runs import enqueue_run, is_scheduled
from tests.conftest import HERMES


def _setup(db, trigger="rerun", error_type=None, status="queued", attempt=2, later=True):
    c = Company(canonical_name="intel", official_name="Intel Corporation", industry="Semis")
    db.add(c)
    db.commit()
    when = datetime.now(UTC) + (timedelta(hours=5) if later else timedelta(minutes=-1))
    r = JudgmentRun(company_id=c.id, status=status, trigger=trigger, triggered_by="system", attempt=attempt,
                    error_type=error_type, next_attempt_at=when,
                    checkpoint={"v": "x", "done": ["resolve", "research"]})
    db.add(r)
    db.commit()
    return c, r


@pytest.fixture(autouse=True)
def _no_wake(monkeypatch):
    monkeypatch.setattr(main.worker, "wake", lambda: None)


@pytest.mark.parametrize("trigger,etype", [
    ("energy_retry", None), ("rerun", "deferred"), ("rerun", "api_error"),
    ("sweep", "oom"), ("approve", "crashed"), ("rerun", "timeout"),
])
def test_manual_rerun_runs_any_scheduled_run_now(client, db, trigger, etype):
    c, r = _setup(db, trigger, etype)
    resp = client.post(f"/internal/rerun-judgment/{c.id}", headers=HERMES)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "run_now" and body["run_id"] == r.id and "will run now" in body["detail"]
    db.expire_all()
    run = db.get(JudgmentRun, r.id)
    assert run.next_attempt_at is None and run.attempt == 2 and run.status == "queued"
    assert run.checkpoint["done"] == ["resolve", "research"]
    log = db.query(IngestLog).filter_by(action="run_now").one()
    assert log.admin_id == "hermes" and log.details["run_id"] == r.id
    assert log.details["prior_next_attempt_at"] and log.details["attempt"] == 2
    assert log.details["run_trigger"] == trigger and log.details["error_type"] == etype
    assert db.query(JudgmentRun).count() == 1


@pytest.mark.parametrize("auto", ["energy_retry", "auto_sweep", "sweep"])
def test_automatic_triggers_never_pull_forward(db, auto):
    c, r = _setup(db)
    run, created = enqueue_run(db, c.id, trigger=auto, triggered_by="system")
    assert not created and run.run_now_from is None
    db.expire_all()
    assert db.get(JudgmentRun, r.id).next_attempt_at is not None
    assert db.query(IngestLog).filter_by(action="run_now").count() == 0


def test_running_run_untouched_and_409(client, db):
    c, r = _setup(db, status="running")
    resp = client.post(f"/internal/rerun-judgment/{c.id}", headers=HERMES)
    assert resp.status_code == 409
    db.expire_all()
    assert db.get(JudgmentRun, r.id).next_attempt_at is not None
    assert db.query(IngestLog).filter_by(action="run_now").count() == 0


def test_past_due_queued_run_is_just_already_active(client, db):
    c, r = _setup(db, later=False)
    assert not is_scheduled(r)
    assert client.post(f"/internal/rerun-judgment/{c.id}", headers=HERMES).status_code == 409


def test_admin_html_flow_flashes_and_redirects(admin_client, db):
    c, r = _setup(db, "energy_retry")
    resp = admin_client.post(f"/internal/rerun-judgment/{c.id}", headers={"accept": "text/html"},
                             follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/admin/runs/{r.id}"
    page = admin_client.get(resp.headers["location"]).text
    assert "will run now" in page
    log = db.query(IngestLog).filter_by(action="run_now").one()
    assert log.admin_id == "admin@example.com"


def test_auth_and_origin_still_required(client, admin_client, db):
    c, r = _setup(db)
    assert client.post(f"/internal/rerun-judgment/{c.id}").status_code in (401, 403)
    resp = admin_client.post(f"/internal/rerun-judgment/{c.id}", headers={"origin": "https://evil.example"},
                             follow_redirects=False)
    assert resp.status_code in (401, 403)
    db.expire_all()
    assert db.get(JudgmentRun, r.id).next_attempt_at is not None


def test_board_card_and_queue_show_run_now(admin_client, db):
    c, r = _setup(db, "energy_retry")
    board = admin_client.get("/admin").text
    top = admin_client.get("/admin/board/top").text
    for html in (board, top):
        assert "Run now" in html and "(scheduled " in html and (" PDT)" in html or " PST)" in html)
        assert f'action="/internal/rerun-judgment/{c.id}"' in html
    assert f"Run #{r.id} queued…" not in board


def test_unscheduled_queued_run_keeps_link_label(admin_client, db):
    _c, r = _setup(db, later=False)
    board = admin_client.get("/admin").text
    assert "Run now" not in board and f"Run #{r.id} queued…" in board
