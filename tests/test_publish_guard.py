"""Publishing guard: a run below the ranking thresholds is recorded (admin-visible, a labelled note
publicly) but never replaces better data — a ranked run, an unranked run that scored more of the
weight, or real legacy v0 scores. Also the NVIDIA-report end-to-end regression and migration 0004,
which applies the guard to runs that were published before it existed."""
import re
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app import publication
from app import main
from app.db import SessionLocal
from app.models import CategoryScore, Company, Evidence, JudgmentStage, Score
from app.pipeline.measures import SECONDS_PER_YEAR, kardashev
from tests import fakes
from tests.test_nvidia_extraction import _long_report
from tests.test_pipeline import _company, _run_once
from app.worker import Worker


@pytest.fixture
def use_deps(monkeypatch):
    holder = {}

    def set_deps(deps):
        holder["deps"] = deps
        monkeypatch.setattr(main, "worker", Worker(SessionLocal, lambda: holder["deps"]))
        return main.worker
    return set_deps

EMPTY_WORLD = {"research": [{"sources": []}], "extract": [{"figures": [], "claims": []}]}


def _thin_llm():
    llm = fakes.world_llm(**EMPTY_WORLD)
    llm.citations = {}
    return llm


def _legacy(db, company):
    for cat, sc in (("ai_tech_acceleration", 9.7), ("energy_kardashev", 4.2), ("market_competition", 7.1),
                    ("regulatory_stance", 7.8), ("builder_culture", 9.0)):
        db.add(Score(company_id=company.id, category=cat, score=sc, justification="x", model="grok-4"))
    db.commit()


# ---------------- the rule ----------------

def R(id, ranked, coverage):
    return SimpleNamespace(id=id, ranked=ranked, coverage=coverage)


@pytest.mark.parametrize("ranked,coverage,prev,legacy,publish", [
    (True, 0.9, R(1, True, 0.95), True, True),      # ranked always publishes
    (False, 0.0, R(1, True, 0.9), False, False),    # never replaces a ranked run
    (False, 0.2, R(1, False, 0.4), False, False),   # never replaces an unranked run with more weight
    (False, 0.4, R(1, False, 0.4), False, True),    # same weight: the newer one wins
    (False, 0.5, R(1, False, 0.4), False, True),
    (False, 0.0, None, True, False),                # never replaces real legacy v0 scores
    (False, 0.0, None, False, True),                # nothing better exists: show what was verified
])
def test_rule(ranked, coverage, prev, legacy, publish):
    assert publication.decide(ranked, coverage, prev, legacy)[0] is publish


def test_placeholder_legacy_scores_are_not_better_data(db):
    c = _company(db, "tesla")
    db.add(Score(company_id=c.id, category="overall", score=5.0, justification="Placeholder", model="stub"))
    db.commit()
    assert not publication.has_legacy_scores(db, c.id)
    _legacy(db, c)
    assert publication.has_legacy_scores(db, c.id)


# ---------------- the NVIDIA case ----------------

def test_insufficient_run_never_replaces_legacy_v0(client, admin_client, db, use_deps):
    c = _company(db, "nvidia")
    _legacy(db, c)
    thin = _run_once(db, use_deps, c, fakes.make_deps(_thin_llm(), sec=False))
    assert thin.status == "succeeded" and not thin.ranked and thin.coverage == 0
    assert not thin.published and thin.summary["publish_note"] == publication.WITHHELD_LEGACY
    db.expire_all()
    assert db.get(Company, c.id).current_run_id is None
    # the run is still fully recorded
    assert db.query(CategoryScore).filter_by(run_id=thin.id).count() == 6
    page = client.get(f"/companies/{c.id}").text
    assert "Legacy v0 assessment (superseded method)" in page and "mean 7.6" in page
    assert "Not ranked: insufficient data" not in page
    assert "A later run" in page and "found too little verified data" in page and "not published" in page
    assert "legacy v0 assessment stays on display" in page
    assert not re.search(r"\$\d", page)
    assert client.get(f"/companies/{c.id}/runs/1").status_code == 404     # no N numbering for it
    assert client.get(f"/companies/{c.id}?run=N").status_code in (302, 404)
    home = client.get("/").text
    assert "legacy v0 7.6" in home and "insufficient data: 0%" not in home
    # admins see it, labelled
    lst = admin_client.get("/admin/runs?status=withheld").text
    assert f"/admin/runs/{thin.id}" in lst and "withheld" in lst
    assert f"/admin/runs/{thin.id}" not in admin_client.get("/admin/runs?status=failed").text
    det = admin_client.get(f"/admin/runs/{thin.id}").text
    assert "withheld" in det and "kept the legacy v0 scores public" in det


def test_numbering_stays_sane_when_a_thin_run_follows_published_ones(client, db, use_deps):
    c = _company(db, "nvidia")
    good = _run_once(db, use_deps, c, fakes.make_deps(fakes.world_llm()))
    thin = _run_once(db, use_deps, c, fakes.make_deps(_thin_llm(), sec=False))
    good2 = _run_once(db, use_deps, c, fakes.make_deps(fakes.world_llm()))
    assert good.published and not thin.published and good2.published
    page = client.get(f"/companies/{c.id}").text
    assert client.get(f"/companies/{c.id}/runs/1").status_code == 200       # good = run 1 = N-1
    assert client.get(f"/companies/{c.id}/runs/2", follow_redirects=False).status_code == 302  # good2 = N
    assert client.get(f"/companies/{c.id}/runs/3").status_code == 404
    assert ">N-1<" in page and ">N-2<" not in page
    assert "A later run" not in page            # the thin run is older than the current one


def test_unranked_run_with_less_weight_does_not_replace_unranked(db, use_deps):
    c = _company(db, "obscure-co")
    only_claims = {"research": [fakes.RESEARCH_OK],
                   "extract": [lambda u: {**fakes.extract_ok(u), "figures": []}]}
    first = _run_once(db, use_deps, c, fakes.make_deps(fakes.world_llm(**only_claims), sec=False))
    assert first.published and not first.ranked and first.coverage > 0
    thin = _run_once(db, use_deps, c, fakes.make_deps(_thin_llm(), sec=False))
    assert not thin.published and "scored more of the weight" in thin.summary["publish_note"]
    db.expire_all()
    assert db.get(Company, c.id).current_run_id == first.id


# ---------------- extraction → judge on a real report excerpt ----------------

REPORT_URL = "https://images.nvidia.com/aem-dam/Solutions/documents/NVIDIA-Sustainability-Report-Fiscal-Year-2026.pdf"
ROW = "Energy (MWh)* Energy consumption 1,053,479 815,864 593,953"


def _report_extract(user: str) -> dict:
    """What a model returns when shown the report: whole table rows, one figure per year column,
    hyphenation 'repaired', footnote markers dropped, an elided quote, and two inventions."""
    m = re.search(r'<source id="(S\d+)" url="' + re.escape(REPORT_URL) + r'">\n(.*?)\n</source>', user, re.DOTALL)
    sid, shown = m.group(1), m.group(2)
    assert "Energy consumption 1,053,479 815,864 593,953" in shown      # the table reached the model
    fig = lambda v, p, q, unit="MWh": {"source_id": sid, "metric_key": "energy_consumption", "value": v,
                                       "unit": unit, "period": p, "scope": "company-wide", "quote": q}
    claim = lambda cat, q: {"source_id": sid, "category": cat, "claim": "x", "quote": q}
    return {"figures": [fig(1053479, "FY2026", ROW), fig(815864, "FY2025", ROW),
                        fig(593953, "FY2024", "Energy consumption 1,053,479 815,864 593,953"),
                        fig(1053479, "FY2026", "Energy consumption 1,053,479", "megawatt hours"),
                        fig(1150000, "FY2026", "Energy consumption 1,053,479")],          # wrong value
            "claims": [
                claim("frontier_acceleration", "for real-time trillion-parameter inference and training"),
                claim("frontier_acceleration", "has demonstrated up to 10x energy efficiency over the previous "
                                               "architecture, software updates continue"),
                claim("policy_stance", "In FY26, we continued to match 100% of our global electricity usage "
                                       "with clean electricity, reducing Scope 2 market-based emissions."),
                claim("builder_velocity", "In 2025, we launched and scaled the NVIDIA Blackwell Ultra platform"),
                claim("policy_stance", "NVIDIA has lobbied for faster permitting of new power plants."),  # invented
            ]}


def _report_routes():
    return fakes.world_routes({REPORT_URL: (200, "text/plain", _long_report())})


def test_nvidia_report_figures_and_claims_verify_end_to_end(client, db, use_deps):
    research = {"sources": [{"url": REPORT_URL, "title": "FY26 report", "covers": ["energy"], "why": "table"},
                            {"url": fakes.NEWS_URL, "title": "news", "covers": ["frontier_acceleration"], "why": "x"}]}
    llm = fakes.world_llm(research=[research], extract=[_report_extract])
    llm.citations = {}
    c = _company(db, "nvidia", domain="nvidia.com")
    run = _run_once(db, use_deps, c, fakes.make_deps(llm, routes=_report_routes(), sec=False))
    assert run.status == "succeeded", run.error_message
    ev = {(e.kind, e.period, e.value, e.unit): e for e in db.query(Evidence).filter_by(run_id=run.id)}
    for period, v in (("FY2026", 1053479), ("FY2025", 815864), ("FY2024", 593953)):
        assert ev[("figure", period, v, "MWh")].quote_verified, period
    assert not ev[("figure", "FY2026", 1150000, "MWh")].quote_verified
    claims = db.query(Evidence).filter_by(run_id=run.id, kind="claim").all()
    ok = [e for e in claims if e.quote_verified]
    assert len(ok) == 4 and all("lobbied" not in e.quote for e in ok)
    assert any(e.quote == "for real-time trillion- parameter inference and training" for e in ok)  # source text
    # measured from the report, in code
    p = 1053479 * 3.6e9 / SECONDS_PER_YEAR
    assert run.avg_power_w == pytest.approx(p) and run.k_equivalent == pytest.approx(kardashev(p))
    cats = {x.category: x for x in db.query(CategoryScore).filter_by(run_id=run.id)}
    assert cats["energy_throughput"].score is not None and cats["growth"].score is not None
    judge = db.query(JudgmentStage).filter_by(run_id=run.id, stage="judge").one()
    assert judge.status == "succeeded" and judge.detail["evidence_items"]["claims"] >= 4
    extract = db.query(JudgmentStage).filter_by(run_id=run.id, stage="extract").one()
    sent = next(v for v in extract.detail["input"].values() if v["chars"] > 100_000)
    assert sent["sent"] < sent["chars"]
    assert extract.detail["match_methods"].get("footnote") and extract.detail["figures_verified"] == 4


def test_judge_runs_on_verified_figures_even_without_claims(db, use_deps):
    def judge(user):
        fig = int(re.search(r"\[E(\d+)\] \(measured figure;", user).group(1))
        return {"categories": [
            {"category": "builder_velocity", "score": 6.0, "confidence": 0.5, "insufficient_evidence": False,
             "evidence_ids": [fig], "rationale": "Capacity planned [E]."},
            {"category": "frontier_acceleration", "score": None, "confidence": 0, "insufficient_evidence": True,
             "evidence_ids": [], "rationale": "No evidence."},
            {"category": "policy_stance", "score": None, "confidence": 0, "insufficient_evidence": True,
             "evidence_ids": [], "rationale": "No evidence."}], "synthesis": "x"}
    llm = fakes.world_llm(extract=[lambda u: {**fakes.extract_ok(u), "claims": []}], judge=[judge])
    c = _company(db)
    run = _run_once(db, use_deps, c, fakes.make_deps(llm))
    assert "judge" in llm.stages_called()
    judge_user = next(x["user"] for x in llm.calls if x["stage"] == "judge")
    assert "(measured figure;" in judge_user
    st = db.query(JudgmentStage).filter_by(run_id=run.id, stage="judge").one()
    assert st.status == "succeeded" and st.detail["evidence_items"]["claims"] == 0 and st.detail["evidence_items"]["figures"] >= 4
    assert db.query(CategoryScore).filter_by(run_id=run.id, category="builder_velocity").one().score == 6.0


def test_duplicate_sources_are_read_once(db, use_deps):
    dup = fakes.SUSTAIN_URL + "?utm=1"
    routes = fakes.world_routes({dup: fakes.world_routes()[fakes.SUSTAIN_URL]})
    research = {"sources": fakes.RESEARCH_OK["sources"] + [{"url": dup, "title": "dup", "covers": ["energy"],
                                                            "why": "x"}]}
    c = _company(db)
    run = _run_once(db, use_deps, c, fakes.make_deps(fakes.world_llm(research=[research]), routes=routes))
    from app.models import Source
    srcs = {s.url: s for s in db.query(Source).filter_by(run_id=run.id)}
    assert srcs[dup].status == "duplicate" and "same content as source" in srcs[dup].reject_reason


# ---------------- migration 0004 (retroactive) ----------------

def test_migration_withholds_existing_insufficient_runs(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/m.db"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0003")
    eng = sa.create_engine(url)
    t = "2026-10-08 22:49:00"
    with eng.begin() as c:
        for i, name in ((1, "tesla"), (2, "nvidia"), (3, "acme"), (4, "beta")):
            c.execute(sa.text("INSERT INTO companies (id, canonical_name, suggestion_attempts) VALUES (:i, :n, 0)"),
                      {"i": i, "n": name})
        c.execute(sa.text("INSERT INTO scores (company_id, category, score, model, justification) VALUES "
                          "(1, 'overall', 5, 'stub', 'Placeholder'), (2, 'energy_kardashev', 4.2, 'grok-4', 'x')"))
        rows = [  # id, company, ranked, coverage, published, finished
            (1, 1, False, 0.0, True, t),                       # tesla: placeholders only -> stays
            (2, 2, False, 0.0, True, t),                       # nvidia: real legacy -> withheld
            (3, 3, True, 0.8, True, "2026-09-01 00:00:00"),    # acme: ranked
            (4, 3, False, 0.0, False, t),                      # acme: thin, already kept out
            (5, 4, False, 0.4, True, "2026-09-01 00:00:00"),   # beta: unranked 40%
            (6, 4, False, 0.0, True, t),                       # beta: thin 0% published by the old rule
        ]
        for rid, cid, ranked, cov, pub, fin in rows:
            c.execute(sa.text("INSERT INTO judgment_runs (id, company_id, status, ranked, coverage, published, "
                              "finished_at, summary) VALUES (:id, :c, 'succeeded', :r, :cov, :p, :f, :s)"),
                      {"id": rid, "c": cid, "r": ranked, "cov": cov, "p": pub, "f": fin,
                       "s": '{"not_ranked_reason": "insufficient data: 0% of weight scored (needs 60%)"}'})
        c.execute(sa.text("INSERT INTO judgment_runs (id, company_id, status) VALUES (7, 2, 'failed')"))
        for cid, cur in ((1, 1), (2, 2), (3, 3), (4, 6)):
            c.execute(sa.text("UPDATE companies SET current_run_id = :r WHERE id = :c"), {"r": cur, "c": cid})
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        cur = dict(c.execute(sa.text("SELECT id, current_run_id FROM companies")).all())
        pub = dict(c.execute(sa.text("SELECT id, published FROM judgment_runs")).all())
        note = c.execute(sa.text("SELECT summary FROM judgment_runs WHERE id = 2")).scalar()
    assert cur == {1: 1, 2: None, 3: 3, 4: 5}
    assert pub[1] and not pub[2] and pub[3] and not pub[4] and pub[5] and not pub[6] and not pub[7]
    assert "legacy v0" in note and "not_ranked_reason" in note
    command.upgrade(cfg, "head")       # idempotent
    command.downgrade(cfg, "0003")     # data-only: downgrade is a no-op
