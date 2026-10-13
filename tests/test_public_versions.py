"""1.11.0: versions on the public site (display only)."""
from datetime import UTC, datetime, timedelta

from app.models import Company, JudgmentRun

V = {"prompt_version": "prompts-v2.5", "rubric_version": "rubrics-v1", "weights_version": "weights-v1"}
T0 = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)


def _setup(db):
    c = Company(canonical_name="crusoe", official_name="Crusoe", industry="AI data centers")
    db.add(c)
    db.commit()
    runs = []
    for i, pv in enumerate(("pipeline-v2.12", "pipeline-v2.13")):
        r = JudgmentRun(company_id=c.id, status="succeeded", published=True, ranked=True, index_score=5.0 + i,
                        k_equivalent=0.12, coverage=0.8, confidence=0.6, trigger="t", triggered_by="t", attempt=1,
                        queued_at=T0 + timedelta(days=i), finished_at=T0 + timedelta(days=i),
                        summary={"measured_coverage": 0.7}, pipeline_version=pv, **V)
        db.add(r)
        db.commit()
        runs.append(r)
    c.current_run_id = runs[-1].id
    db.commit()
    return c, runs


def test_leaderboard_row_has_pipeline_chip(client, db):
    _setup(db)
    html = client.get("/").text
    assert 'class="pvchip mono"' in html and ">v2.13</span>" in html
    assert 'title="Measured with pipeline-v2.13 · prompts-v2.5 · rubrics-v1 · weights-v1"' in html


def test_run_history_shows_prompts_version(client, db):
    c, _runs = _setup(db)
    html = client.get(f"/companies/{c.id}").text
    assert "pipeline-v2.13 · prompts-v2.5 · rubrics-v1 · weights-v1" in html     # history row + runlog
    assert "pipeline-v2.12 · prompts-v2.5 · rubrics-v1 · weights-v1" in html


def test_past_run_page_shows_all_versions(client, db):
    c, _runs = _setup(db)
    resp = client.get(f"/companies/{c.id}/runs/1")
    assert resp.status_code == 200
    assert resp.text.count("pipeline-v2.12 · prompts-v2.5 · rubrics-v1 · weights-v1") >= 2


def test_no_admin_chip_leaks_to_public(client, db):
    c, _ = _setup(db)
    for url in ("/", f"/companies/{c.id}"):
        html = client.get(url).text
        assert "class=\"vchip" not in html and "not started" not in html
