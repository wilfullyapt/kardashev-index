"""1.10.1: pipeline version chip on every admin run surface; "not started" when a run has no version."""
from datetime import UTC, datetime, timedelta

from app.models import Company, JudgmentRun

V = dict(pipeline_version="pipeline-v2.13", prompt_version="prompts-v2.5", rubric_version="rubrics-v1",
         weights_version="weights-v1")
TIP = 'title="pipeline-v2.13 · prompts-v2.5 · rubrics-v1 · weights-v1"'


def _setup(db):
    now = datetime.now(UTC)
    c = Company(canonical_name="crusoe", official_name="Crusoe", industry="x")
    q = Company(canonical_name="oklo", official_name="Oklo", industry="x")
    db.add_all([c, q])
    db.commit()
    done = JudgmentRun(company_id=c.id, status="succeeded", published=True, ranked=True, index_score=5.0,
                       coverage=0.7, confidence=0.5, trigger="t", triggered_by="t", attempt=1, queued_at=now,
                       finished_at=now, cost_usd=0.2, summary={"measured_coverage": 0.6}, **V)
    queued = JudgmentRun(company_id=q.id, status="queued", trigger="t", triggered_by="t", attempt=0, queued_at=now)
    sched = JudgmentRun(company_id=c.id, status="queued", trigger="t", triggered_by="t", attempt=1, queued_at=now,
                        next_attempt_at=now + timedelta(minutes=30), **V)
    db.add_all([done, queued])
    db.commit()
    return c, q, done, queued, sched


def test_board_cards_and_queue(admin_client, db):
    c, q, done, queued, _ = _setup(db)
    html = admin_client.get("/admin").text
    card = html[html.index(f'id="company-{c.id}"'):]
    card = card[:card.index("</article>")]
    assert "pipeline v2.13" in card and TIP in card
    oklo = html[html.index(f'id="company-{q.id}"'):]
    oklo = oklo[:oklo.index("</article>")]
    assert "not started" in oklo and "pipeline v2" not in oklo
    top = admin_client.get("/admin/board/top").text
    row = top[top.index(f"run #{queued.id}") - 600:top.index(f"run #{queued.id}")]
    assert "not started" in row and "runstat--queued" in row


def test_scheduled_retry_keeps_its_recorded_version(admin_client, db):
    c, q, done, queued, sched = _setup(db)
    queued.status = "failed"
    db.add(sched)
    db.commit()
    top = admin_client.get("/admin/board/top").text
    assert "scheduled" in top and "pipeline v2.13" in top and "not started" not in top


def test_classic_runs_list_and_history(admin_client, db):
    c, q, done, queued, _ = _setup(db)
    classic = admin_client.get("/admin/classic").text
    assert classic.count("pipeline v2.13") >= 3          # latest runs + companies current + last
    assert "not started" in classic
    runs = admin_client.get("/admin/runs").text
    assert "pipeline v2.13" in runs and "prompts-v2.5 · rubrics-v1 · weights-v1" in runs and "not started" in runs
    hist = admin_client.get(f"/admin/companies/{c.id}/history").text
    assert "pipeline v2.13" in hist and "prompts-v2.5 · rubrics-v1 · weights-v1" in hist
    assert "Swipe sideways" in hist


def test_public_pages_unchanged(client, db):
    c, *_ = _setup(db)
    html = client.get(f"/companies/{c.id}").text
    assert "vchip" not in html and "not started" not in html
