"""pipeline-v2.2 rules: metric semantics, energy-undisclosed ranking, public display fixes and the
0005 migration's data fix."""
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.models import Company, JudgmentRun
from app.pipeline.aggregate import aggregate
from app.pipeline.semantics import check_figure, table_scale
from app.runs import dedupe_evidence, history

Y = 2026


@pytest.mark.parametrize("key,quote,expect_key", [
    ("energy_consumption", "In 2025, we deployed 46.7 GWh of energy storage products", "energy_storage_deployed"),
    ("energy_supplied", "Megapack deployments reached 31.4 GWh", "energy_storage_deployed"),
    ("energy_consumption", "We sold 120 TWh of electricity to retail customers", "energy_sold"),
    ("energy_supplied", "Our plants generated 45 TWh of electricity", "energy_generated"),
    ("energy_consumption", "Total energy consumption across our operations was 612,000 MWh", "energy_consumption"),
    ("datacenter_capacity_operating", "1 GW of capacity under construction", "datacenter_capacity_planned"),
])
def test_energy_and_capacity_semantics(key, quote, expect_key):
    v = check_figure(key, quote=quote, period="2025", is_primary=True, current_year=Y)
    assert v.ok and v.metric_key == expect_key


@pytest.mark.parametrize("quote,period,primary,why", [
    ("SpaceX priced a $20 billion bond deal", "2026", True, "financing"),
    ("raised $6 billion in a Series C funding round", "2025", True, "financing"),
    ("agreed to buy natural-gas turbines worth $2.8 billion", "2025", True, "deal"),
    ("We expect capital expenditures of $60 billion in 2026", "2026", True, "planned"),
    ("Capital expenditures were $11.3 billion", "2024", False, "primary"),
    ("Capital expenditures were $11.3 billion", None, True, "fiscal period"),
    ("Capital expenditures were $11.3 billion", "2028", True, "future"),
])
def test_capex_rejections_have_clear_reasons(quote, period, primary, why):
    v = check_figure("capex", quote=quote, period=period, is_primary=primary, current_year=Y)
    assert not v.ok and why in v.reason


def test_capex_accepts_cash_flow_line():
    v = check_figure("capex", quote="Purchases of property and equipment ( 11,339 )", period="FY2024",
                     is_primary=True, current_year=Y)
    assert v.ok


def test_revenue_rejects_run_rate_and_forecast():
    assert not check_figure("revenue", quote="annualized revenue run-rate of $1 billion", period="2025",
                            is_primary=True, current_year=Y).ok
    assert check_figure("revenue", quote="Total revenues 97,690", period="FY2024", is_primary=True,
                        current_year=Y).ok


def test_table_scale_lookback():
    text = "Consolidated Statements of Cash Flows (in millions) ... " + "x " * 500 + "Purchases ( 11,339 )"
    assert table_scale(text, text.index("Purchases")) == ("USD_millions", 1e6)
    assert table_scale("no header here Purchases 5", 15) is None


# ---------------------------------------------------------------- energy undisclosed
def test_energy_undisclosed_rule_ranks_on_remaining_weight_with_reduced_confidence():
    scores = {"compute_capacity": (7.5, 0.9), "growth": (5.0, 0.9), "frontier_acceleration": (8.0, 0.7)}
    plain = aggregate(scores)
    assert not plain.ranked                                          # 50% < 60%
    a = aggregate(scores, energy_undisclosed=True)
    assert a.ranked and a.basis == "energy_undisclosed"
    assert a.adjusted_coverage == pytest.approx(0.5 / 0.7, abs=1e-3)
    assert a.confidence == pytest.approx(plain.confidence * 0.8, abs=1e-3)
    thin = aggregate({"frontier_acceleration": (8.0, 0.7), "builder_velocity": (7, 0.7), "policy_stance": (5, 0.9)},
                     energy_undisclosed=True)                         # 35% / 70% = 50%, nothing measured
    assert thin.ranked is False and "energy undisclosed" in thin.reason
    with_energy = aggregate({**scores, "energy_throughput": (3.0, 0.9)}, energy_undisclosed=True)
    assert with_energy.basis is None or with_energy.basis != "energy_undisclosed"


# ---------------------------------------------------------------- display
def test_evidence_quotes_are_deduplicated():
    items = [{"quote": "Energy  consumption 1,053,479", "domain": "nvidia.com"},
             {"quote": "Energy consumption 1,053,479", "domain": "nvidia.com"},
             {"quote": "Energy consumption 1,053,479", "domain": "sec.gov"}]
    assert len(dedupe_evidence(items)) == 2


def test_empty_legacy_runs_are_hidden_from_public_history(db, client, admin_client):
    c = Company(canonical_name="tesla", industry="Unknown")
    db.add(c)
    db.commit()
    t = datetime(2026, 10, 8, 22, tzinfo=UTC)
    empty = JudgmentRun(company_id=c.id, status="succeeded", published=True, ranked=False, coverage=0.0,
                        trigger="t", triggered_by="t", finished_at=t, attempt=1)
    real = JudgmentRun(company_id=c.id, status="succeeded", published=True, ranked=False, coverage=0.45,
                       index_score=1.2, trigger="t", triggered_by="t", finished_at=t + timedelta(minutes=40), attempt=1)
    db.add_all([empty, real])
    db.commit()
    c.current_run_id = real.id
    db.commit()
    h = history(db, c)
    assert [e["run"].id for e in h["runs"]] == [real.id]
    assert client.get(f"/companies/{c.id}/runs/2").status_code == 404
    assert f"/admin/runs/{empty.id}" in admin_client.get("/admin/runs").text   # admins still see it


# ---------------------------------------------------------------- migration 0005 data fix
def test_0005_aligns_published_flag_in_stage_detail_and_log(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/mig5.db"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0004")
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO companies (id, canonical_name, suggestion_attempts) VALUES (1, 'nvidia', 0)"))
        c.execute(sa.text("INSERT INTO judgment_runs (id, company_id, status, attempt, published) "
                          "VALUES (2, 1, 'succeeded', 1, 0)"))
        c.execute(sa.text("INSERT INTO judgment_stages (run_id, stage, attempt, status, detail) VALUES "
                          "(2, 'aggregate', 1, 'succeeded', '{\"published\": true, \"coverage\": 0.4}')"))
        c.execute(sa.text("INSERT INTO ingest_logs (action, details) VALUES "
                          "('judgment_run', '{\"run_id\": 2, \"published\": true}')"))
    command.upgrade(cfg, "head")
    import json
    with eng.connect() as c:
        d = json.loads(c.execute(sa.text("SELECT detail FROM judgment_stages")).scalar())
        lg = json.loads(c.execute(sa.text("SELECT details FROM ingest_logs")).scalar())
        assert d == {"published": False, "coverage": 0.4, "published_at_run_time": True}
        assert lg["published"] is False and lg["published_at_run_time"] is True
        assert c.execute(sa.text("SELECT checkpoint, degraded FROM judgment_runs")).one() == (None, None)
    command.downgrade(cfg, "0004")
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM judgment_runs")).scalar() == 1
