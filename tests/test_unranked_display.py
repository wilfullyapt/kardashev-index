"""Unranked runs never show a numeric Index publicly; Δ and the Index sparkline use ranked runs only."""
import re
from datetime import UTC, datetime, timedelta

from app.models import Company, JudgmentRun
from app.runs import history

T0 = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)


def _company(db, name):
    c = Company(canonical_name=name, official_name=name.title(), industry="Test")
    db.add(c)
    db.commit()
    return c


def _run(db, c, day, index, *, ranked, coverage=0.72, confidence=0.5, measured=0.45, reason=None):
    summary = {"measured_coverage": measured}
    if reason:
        summary["not_ranked_reason"] = reason
    r = JudgmentRun(company_id=c.id, status="succeeded", published=True, ranked=ranked, index_score=index,
                    k_equivalent=0.1, coverage=coverage, confidence=confidence, trigger="t", triggered_by="t",
                    attempt=1, queued_at=T0 + timedelta(days=day), finished_at=T0 + timedelta(days=day),
                    pipeline_version="pipeline-v2.3", weights_version="weights-v1", rubric_version="rubrics-v1",
                    summary=summary)
    db.add(r)
    db.commit()
    c.current_run_id = r.id
    db.commit()
    return r


def _hist_html(html):
    i = html.index('id="history"')
    return html[i:html.index("</section>", i)]


def test_single_unranked_run_shows_unranked_not_ten(client, db):
    c = _company(db, "anduril")
    _run(db, c, 0, 10.0, ranked=False, coverage=0.15, confidence=0.02, measured=0.15,
         reason="insufficient data: 15% of weight scored (needs 60%)")
    html = client.get(f"/companies/{c.id}").text
    assert "10.0" not in html
    assert "Unranked" in _hist_html(html) and "insufficient data: 15% of weight scored" in html
    assert "Unranked · insufficient data" in html          # header band
    assert 'class="gauge__k">Unranked' in html


def test_ranked_run_after_unranked_has_no_delta_against_it(client, db):
    """Tesla case: N ranked 4.6, N-1 unranked 1.2 -> no '+3.4' anywhere."""
    c = _company(db, "tesla")
    _run(db, c, 0, 1.2, ranked=False, coverage=0.30, reason="insufficient data")
    _run(db, c, 5, 4.6, ranked=True)
    html = client.get(f"/companies/{c.id}").text
    assert "+3.4" not in html and "1.2" not in _hist_html(html) and " vs N-1" not in html
    h = history(db, c)
    assert [e["delta"] for e in h["runs"]] == [None, None]
    assert h["spark_index"]["n"] == 1


def test_delta_skips_unranked_runs_between_ranked_ones(client, db):
    c = _company(db, "nvidia")
    _run(db, c, 0, 5.0, ranked=True)
    _run(db, c, 5, 9.9, ranked=False, coverage=0.2, reason="insufficient data")
    _run(db, c, 10, 5.6, ranked=True)
    h = history(db, c)
    assert [e["delta"] for e in h["runs"]] == [None, None, 0.6]
    assert h["runs"][2]["delta_vs"] == "N-2"
    assert h["spark_index"]["n"] == 2 and h["spark_index"]["latest"] == 5.6
    html = client.get(f"/companies/{c.id}").text
    assert "+0.6 vs N-2" in html and "9.9" not in html
    assert "Index across 2 ranked runs" in html


def test_stored_ranked_but_now_ineligible_run_is_unranked_in_history(client, db):
    c = _company(db, "tesla")
    _run(db, c, 0, 4.6, ranked=True, coverage=0.42, confidence=0.187, measured=0.15)   # fails the v2.3 gate
    html = client.get(f"/companies/{c.id}").text
    assert not re.search(r">\s*4\.6", html) and "Unranked" in _hist_html(html)


def test_leaderboard_never_shows_unranked_index(client, db):
    c = _company(db, "extropic")
    _run(db, c, 0, 8.0, ranked=False, coverage=0.15, confidence=0.12, measured=0.0, reason="insufficient data")
    html = client.get("/").text
    assert "Extropic" in html and "8.0" not in html
