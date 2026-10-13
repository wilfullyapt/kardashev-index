"""1.10.0: per-company run history, side by side."""
from datetime import UTC, datetime, timedelta

from app import board, publication
from app.models import Company, JudgmentRun, Source

T0 = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)


def _setup(db):
    c = Company(canonical_name="crusoe", official_name="Crusoe", industry="x")
    db.add(c)
    db.commit()
    runs = []
    for i, kw in enumerate([
        {"status": "succeeded", "ranked": True, "published": True, "index_score": 4.5, "coverage": 0.62, "confidence": 0.4,
             "cost_usd": 0.21, "pipeline_version": "pipeline-v2.2"},
        {"status": "succeeded", "ranked": False, "published": False, "index_score": 3.1, "coverage": 0.5, "confidence": 0.3,
             "cost_usd": None, "pipeline_version": "pipeline-v2.4",
             "summary": {"not_ranked_reason": "insufficient data: 50% of weight scored", "measured_coverage": 0.44}},
        {"status": "failed", "error_type": "timeout", "published": False, "cost_usd": 0.05, "pipeline_version": "pipeline-v2.13"},
    ]):
        r = JudgmentRun(company_id=c.id, trigger="t", triggered_by="t", attempt=1, queued_at=T0 + timedelta(days=i),
                        finished_at=T0 + timedelta(days=i), **kw)
        db.add(r)
        db.commit()
        runs.append(r)
    for n in range(3):
        db.add(Source(run_id=runs[0].id, url=f"https://e.com/{n}", status="ok"))
    db.commit()
    return c, runs


def test_history_rows(db):
    c, runs = _setup(db)
    h = board.history(db, c)
    assert [x["run"].id for x in h["rows"]] == [r.id for r in reversed(runs)]
    by = {x["run"].id: x for x in h["rows"]}
    assert by[runs[0].id]["sources"] == 3 and by[runs[0].id]["status"] == "published"
    assert by[runs[1].id]["status"] == "withheld" and by[runs[1].id]["measured"] == 0.44
    assert by[runs[2].id]["status"] == "failed (timeout)" and h["total"] == 3
    cur = publication.current_run(db, c)
    assert [x["run"].id for x in h["rows"] if x["current"]] == ([cur.id] if cur else [])


def test_retracted_status_and_current_moves(db):
    c, runs = _setup(db)
    publication.retract(db, runs[0], "a", "counted planned capacity as operating")
    db.commit()
    by = {x["run"].id: x for x in board.history(db, c)["rows"]}
    assert by[runs[0].id]["status"] == "retracted" and not by[runs[0].id]["current"]
    assert by[runs[0].id]["sources"] == 3


def test_history_page(admin_client, db):
    c, runs = _setup(db)
    html = admin_client.get(f"/admin/companies/{c.id}/history").text
    for s in ("v2.2", "v2.4", "v2.13", "62%", "40%", "4.50", "$0.21", "cost unknown", "failed (timeout)",
              "withheld", "measured 44%", "insufficient data: 50% of weight scored",
              f'href="/admin/runs/{runs[0].id}"', "2026-10-01 10:00 PDT"):
        assert s in html, s
    assert html.index(f"#{runs[2].id}") < html.index(f"#{runs[0].id}")


def test_history_requires_admin_and_handles_empty(client, admin_client, db):
    c = Company(canonical_name="new", industry="x")
    db.add(c)
    db.commit()
    assert "No runs yet" in admin_client.get(f"/admin/companies/{c.id}/history").text
    assert client.get(f"/admin/companies/{c.id}/history", follow_redirects=False).status_code in (302, 303, 401)
