"""pipeline-v2.3 quality gate: rank only if measured share > 40% of scored weight AND confidence > 20%.

Confidence is stored 0-1 (shown x100 as a percent), so "> 20" means stored confidence > 0.20.
The gate is applied by aggregate() for new runs and recomputed at display time for stored runs
(app.eligibility.rank_status); stored ``ranked`` values are never rewritten.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import eligibility, publication
from app.db import SessionLocal
from app.models import CategoryScore, Company, JudgmentRun
from app.pipeline import methodology as meth
from app.pipeline.aggregate import aggregate, quality_reason, share_reason
from app.pipeline.config import WorkerSettings, settings
from app.worker import Worker

GATE = {"min_measured_share": 0.40, "min_confidence": 0.20}


def test_defaults_and_scale():
    s = settings()
    assert s.rank_min_measured_share == 0.40 and s.rank_min_confidence == 0.20
    assert meth.PIPELINE_VERSION == "pipeline-v2.3"


@pytest.mark.parametrize("share,conf,ok", [
    (0.40, 0.50, False),     # strict: exactly 40% fails
    (0.4001, 0.50, True),
    (0.60, 0.20, False),     # strict: exactly 20% fails
    (0.60, 0.201, True),
    (0.36, 0.187, False),    # Tesla today
    (0.43, 0.26, True),      # X.AI today
    (1.0, 0.02, False),      # Anduril: all measured but no confidence
])
def test_share_reason_boundaries(share, conf, ok):
    assert (share_reason(share, conf, **GATE) is None) is ok


def test_reason_text_names_the_failed_rule():
    assert "too little measured data: 36%" in quality_reason(0.42, 0.15, 0.5, **GATE)
    assert "low confidence: 19%" in quality_reason(0.72, 0.45, 0.19, **GATE)
    assert quality_reason(0.42, 0.15, 0.1, min_measured_share=None, min_confidence=None) is None


def _scores(present: dict[str, float], conf=0.5):
    return {k: (present.get(k), conf if k in present else 0.0) for k in meth.WEIGHTS}


def test_energy_undisclosed_path_is_gated_tesla_case():
    # growth + judged: coverage 0.50, measured 0.15 -> share 30%; undisclosed path would rank it.
    sc = _scores({"growth": 5.0, "frontier_acceleration": 6.0, "builder_velocity": 5.0, "policy_stance": 4.0})
    old = aggregate(sc, energy_undisclosed=True)
    assert old.ranked and old.basis == "energy_undisclosed"
    new = aggregate(sc, energy_undisclosed=True, **GATE)
    assert not new.ranked and "too little measured data: 30%" in new.reason
    assert new.index_score == old.index_score and new.coverage == old.coverage          # scores unchanged


def test_undisclosed_gate_uses_reduced_confidence():
    sc = _scores({"compute_capacity": 6.0, "growth": 5.0, "frontier_acceleration": 6.0,
                  "builder_velocity": 5.0}, conf=0.32)   # conf 0.62*0.32=0.198 -> x0.8 = 0.159
    a = aggregate(sc, energy_undisclosed=True, **GATE)
    assert not a.ranked and a.reason.startswith("low confidence")


def test_standard_path_gated_and_passing():
    full = _scores({k: 5.0 for k in meth.WEIGHTS}, conf=0.6)
    a = aggregate(full, **GATE)
    assert a.ranked and a.reason is None
    low = aggregate(_scores({k: 5.0 for k in meth.WEIGHTS}, conf=0.2), **GATE)    # confidence 0.200
    assert not low.ranked and "low confidence: 20%" in low.reason
    # coverage rule still comes first
    thin = aggregate(_scores({"growth": 5.0}), **GATE)
    assert not thin.ranked and thin.reason.startswith("insufficient data")


# ------------------------------------------------------------- stored runs, display time
def _company(db, name):
    c = Company(canonical_name=name, official_name=name.title(), industry="Test")
    db.add(c)
    db.commit()
    return c


def _stored(db, name, *, coverage, confidence, measured=None, cats=None, ranked=True, when=None):
    c = _company(db, name)
    when = when or datetime.now(UTC)
    r = JudgmentRun(company_id=c.id, status="succeeded", published=True, ranked=ranked, index_score=4.6,
                    coverage=coverage, confidence=confidence, trigger="t", triggered_by="t", attempt=1,
                    queued_at=when, finished_at=when,
                    summary={"measured_coverage": measured} if measured is not None else {})
    db.add(r)
    db.commit()
    for k in cats or ():
        db.add(CategoryScore(run_id=r.id, category=k, family="measured" if k in meth.MEASURED_KEYS else "judged",
                             weight=meth.WEIGHTS[k], score=5.0, confidence=0.5))
    c.current_run_id = r.id
    db.commit()
    return c, r


def test_rank_status_from_summary(db):
    _, tesla = _stored(db, "tesla", coverage=0.42, confidence=0.187, measured=0.15)
    _, xai = _stored(db, "xai", coverage=0.47, confidence=0.26, measured=0.20)
    assert eligibility.rank_status(tesla)[0] is False
    assert "too little measured data: 36%" in tesla.rank_reason
    assert xai.is_ranked and tesla.ranked is True                      # stored value untouched


def test_rank_status_falls_back_to_category_scores(db):
    _, r = _stored(db, "acme", coverage=0.65, confidence=0.5,
                   cats=["energy_throughput", "compute_capacity", "frontier_acceleration"])
    assert r.is_ranked                                                  # 0.50 / 0.65 = 77%
    _, r2 = _stored(db, "beta", coverage=0.5, confidence=0.5)            # nothing to recompute from
    assert not r2.is_ranked and "unavailable" in r2.rank_reason


def test_stored_unranked_stays_unranked(db):
    _, r = _stored(db, "anduril", coverage=0.15, confidence=0.02, measured=0.15, ranked=False)
    r.summary = {"measured_coverage": 0.15, "not_ranked_reason": "insufficient data: 15% of weight scored"}
    db.commit()
    assert eligibility.rank_status(r) == (False, "insufficient data: 15% of weight scored")


def test_env_can_disable_gate(db, monkeypatch):
    _, r = _stored(db, "tesla", coverage=0.42, confidence=0.187, measured=0.15)
    monkeypatch.setenv("RANK_MIN_MEASURED_SHARE", "0")
    monkeypatch.setenv("RANK_MIN_CONFIDENCE", "0")
    assert r.is_ranked


def test_leaderboard_and_company_page_use_effective_ranking(client, db):
    tesla, _ = _stored(db, "tesla", coverage=0.42, confidence=0.187, measured=0.15)
    _stored(db, "nvidia", coverage=0.72, confidence=0.57, measured=0.45)
    home = client.get("/").text
    board, _, waiting = home.partition("Not ranked yet")
    assert "Nvidia" in board and "Tesla" in waiting and "too little measured data: 36%" in waiting
    page = client.get(f"/companies/{tesla.id}").text
    assert "Not ranked: too little measured data: 36%" in page and "/methodology#ranking" in page


def test_methodology_page_states_the_rule(client):
    page = client.get("/methodology").text
    assert 'id="ranking"' in page and "40%" in page and "20%" in page and "0–100" in page


def test_publication_unranked_run_can_replace_ineligible_stored_ranked_run(db):
    _, prev = _stored(db, "tesla", coverage=0.42, confidence=0.187, measured=0.15)
    assert publication.decide(False, 0.50, prev, False)[0] is True       # more weight than prev
    assert publication.decide(False, 0.30, prev, False)[0] is False      # less weight: keep prev
    eligible = SimpleNamespace(id=9, ranked=True, is_ranked=True, coverage=0.7)
    assert publication.decide(False, 0.9, eligible, False)[0] is False


def test_sweep_still_uses_stored_ranked_so_no_paid_reruns(db):
    old = datetime.now(UTC).replace(hour=11) - timedelta(days=8)
    _stored(db, "tesla", coverage=0.42, confidence=0.187, measured=0.15, when=old)
    s = WorkerSettings(sweep_enabled=True, sweep_hour_utc=0, sweep_min_age_days=7, sweep_daily_cost_usd=2.0,
                       sweep_est_run_usd=0.35)
    w = Worker(SessionLocal, lambda: None, settings=s, now=lambda: datetime.now(UTC).replace(hour=11))
    summary = w.maybe_sweep()
    assert summary is not None and summary["candidates"] == 0
