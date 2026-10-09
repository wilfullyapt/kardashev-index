"""About / privacy pages, disclosures, CONTACT_EMAIL, and the measured-share display (display only)."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import disclosure
from app.models import CategoryScore, Company, JudgmentRun
from app.pipeline import methodology as meth


def _ranked(db, scored: dict[str, float]):
    c = Company(canonical_name="acme", industry="Unknown")
    db.add(c)
    db.commit()
    run = JudgmentRun(company_id=c.id, status="succeeded", published=True, ranked=True, index_score=6.0,
                      confidence=0.7, coverage=0.9, trigger="t", triggered_by="t", finished_at=datetime.now(UTC))
    db.add(run)
    db.commit()
    for cat in meth.CATEGORIES:
        db.add(CategoryScore(run_id=run.id, category=cat["key"], family=cat["family"], weight=cat["weight"],
                             score=scored.get(cat["key"]), confidence=0.8 if cat["key"] in scored else 0.0,
                             insufficient=cat["key"] not in scored))
    c.current_run_id = run.id
    db.commit()
    return c


@pytest.mark.parametrize("path", ["/about", "/privacy"])
def test_pages_render_and_are_linked_from_footer(client, path):
    r = client.get(path)
    assert r.status_code == 200
    home = client.get("/").text
    assert f'href="{path}"' in home and "Not investment advice" in home


def test_about_has_ai_and_conflict_disclosure(client):
    t = client.get("/about").text
    assert "AI-assisted" in t and "Grok" in t and disclosure.scoring_model() in t
    assert "Conflict-of-interest disclosure" in t and "xAI is itself one of the entities ranked" in t
    assert "Not investment advice" in t and 'id="corrections"' in t


def test_methodology_has_disclosures(client):
    t = client.get("/methodology").text
    assert 'id="disclosures"' in t and "Conflict of interest" in t and "Measured vs opinion share" in t
    assert "65% measured / 35% opinion" in t


def test_privacy_reflects_practices(client):
    t = client.get("/privacy").text
    assert "no analytics" in t and "only for site administrators" in t and "Google Fonts" in t
    assert "set-cookie" not in {k.lower() for k in client.get("/privacy").headers}   # visitors get no cookie


def test_contact_placeholder_when_unset(client, monkeypatch):
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    t = client.get("/about").text
    assert disclosure.CONTACT_PLACEHOLDER in t and "mailto:" not in t


def test_contact_email_when_set(client, monkeypatch):
    monkeypatch.setenv("CONTACT_EMAIL", "corrections@example.org")
    for path in ("/about", "/privacy", "/"):
        assert 'href="mailto:corrections@example.org"' in client.get(path).text


@pytest.mark.parametrize("bad", ["not-an-email", "a@b.c\"><script>", "x y@z.io", ""])
def test_invalid_contact_email_falls_back(monkeypatch, bad):
    monkeypatch.setenv("CONTACT_EMAIL", bad)
    assert disclosure.contact_email() is None


def test_measured_share_math():
    assert disclosure.measured_share(None) is None
    assert disclosure.measured_share({}) is None
    full = [SimpleNamespace(category=c["key"], family=c["family"], weight=c["weight"], score=5.0)
            for c in meth.CATEGORIES]
    assert disclosure.measured_share(full) == pytest.approx(0.65)
    # energy (0.30) unscored: measured 0.35 of 0.70 scored
    part = [x for x in full if x.category != "energy_throughput"] + [
        SimpleNamespace(category="energy_throughput", family="measured", weight=0.30, score=None)]
    assert disclosure.measured_share(part) == pytest.approx(0.5)
    # dicts, mapping input and missing weight/family fall back to the published weights
    assert disclosure.measured_share({"growth": {"category": "growth", "score": 3.0},
                                      "policy_stance": {"category": "policy_stance", "score": 3.0}}) \
        == pytest.approx(0.15 / 0.23)


def test_measured_share_shown_on_leaderboard_and_company(client, db):
    scored = {"compute_capacity": 6.0, "growth": 5.0, "frontier_acceleration": 7.0,
              "builder_velocity": 6.0, "policy_stance": 5.0}             # energy undisclosed
    c = _ranked(db, scored)
    assert "meas 50%" in client.get("/").text
    page = client.get(f"/companies/{c.id}").text
    assert "measured share 50% of the scored weight (opinion 50%)" in page


def test_display_does_not_change_scores_or_ranking(client, db):
    c = _ranked(db, {"growth": 5.0})
    db.refresh(c)
    run = db.get(JudgmentRun, c.current_run_id)
    client.get("/")
    client.get(f"/companies/{c.id}")
    db.refresh(run)
    assert run.index_score == 6.0 and run.ranked is True
