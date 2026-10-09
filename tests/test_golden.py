"""Golden-set smoke tests: the Tesla, SpaceXSI and NVIDIA failures seen in production on 2026-10-08,
replayed through the full pipeline with recorded-style fixtures (tests/fixtures/golden) and a fake
LLM that proposes exactly what the model proposed then. No network, no paid calls."""
import json
import re
from pathlib import Path
from urllib.parse import quote

import pytest

from app.models import CategoryScore, Company, Evidence, JudgmentRun, JudgmentStage, Source
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes

G = Path(__file__).parent / "fixtures" / "golden"


def page(name: str) -> str:
    return (G / name).read_text(encoding="utf-8")


def _sid(user: str) -> dict:
    return {m.group(2): m.group(1) for m in re.finditer(r'<source id="(S\d+)" url="([^"]+)"', user)}


def _run(db, deps, name, **company):
    c = Company(canonical_name=name, industry="Unknown", **company)
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="test")
    run = execute_run(db, run.id, deps)
    db.expire_all()
    return db.get(JudgmentRun, run.id), db.get(Company, c.id)


def _figs(db, run_id):
    return db.query(Evidence).filter_by(run_id=run_id, kind="figure").order_by(Evidence.id).all()


def _wayback(url: str, ts: str = "20251130120000") -> dict:
    return {
        fakes.WAYBACK_API_URL(url): (200, "application/json", json.dumps(
            {"archived_snapshots": {"closest": {"available": True, "status": "200", "timestamp": ts,
                                                 "url": f"http://web.archive.org/web/{ts}/{url}"}}})),
    }


# ---------------------------------------------------------------- Tesla
T_10K = "https://www.sec.gov/Archives/edgar/data/1318605/000162828026003063/tsla-20251231.htm"
T_Q4 = "https://ir.tesla.com/q4-2025-update"
T_RESOLVE = {"official_name": "Tesla, Inc.", "domain": "tesla.com", "ticker": "TSLA", "exchange": "NASDAQ",
             "is_public": True, "sec_filer": True, "industry": "Automotive & energy", "hq": "Austin, Texas",
             "description": "EVs and energy storage.", "confidence": 0.98, "ambiguity": None}
T_RESEARCH = {"sources": [{"url": T_10K, "title": "10-K", "covers": ["growth"], "why": "cash flow"},
                          {"url": T_Q4, "title": "Q4 update", "covers": ["energy"], "why": "storage"}]}


def tesla_routes():
    ts = "20260130090000"
    return fakes.world_routes({
        T_10K: (200, "text/html", page("tesla_10k.html")),
        T_Q4: (403, "text/html", "<html><title>Access Denied</title><body>Access Denied</body></html>"),
        **_wayback(T_Q4, ts),
        f"https://web.archive.org/web/{ts}id_/{T_Q4}": (200, "text/html", page("tesla_q4.html")),
    })


def tesla_extract(user):
    s = _sid(user)
    k, q = s[T_10K], s[T_Q4]
    return {"figures": [
        # v2.1 bug: storage deployed read as energy supplied
        {"source_id": q, "metric_key": "energy_supplied", "value": 46.7, "unit": "GWh", "period": "2025",
         "scope": "company", "quote": "In 2025, we deployed 46.7 GWh of energy storage products"},
        # v2.1 bug: parenthesized cash-flow cell in an "(in millions)" table was rejected
        {"source_id": k, "metric_key": "capex", "value": 11339000000, "unit": "USD", "period": "FY2024",
         "scope": "company", "quote": "Purchases of property and equipment excluding finance leases, net of sales ( 8,527 ) ( 11,339 )"},
        {"source_id": k, "metric_key": "capex", "value": -8527, "unit": "USD", "period": "FY2025",
         "scope": "company", "quote": "Purchases of property and equipment excluding finance leases, net of sales ( 8,527 )"},
        # planned capacity stays planned
        {"source_id": q, "metric_key": "datacenter_capacity_operating", "value": 500, "unit": "MW",
         "period": "2026", "scope": "company", "quote": "We plan to bring 500 MW of additional training compute online"},
        # v2.1 bug: quote attributed to the wrong source was reported "not found"
        {"source_id": q, "metric_key": "revenue", "value": 94827, "unit": "USD_millions", "period": "FY2025",
         "scope": "company", "quote": "Total revenues 94,827"},
    ], "claims": []}


def test_tesla_storage_is_not_energy_and_cash_flow_rows_verify(db):
    llm = fakes.FakeLLM({"resolve": [T_RESOLVE], "research": [T_RESEARCH], "extract": [tesla_extract],
                         "judge": [lambda u: {"categories": [], "synthesis": ""}]})
    run, company = _run(db, fakes.make_deps(llm, routes=tesla_routes()), "tesla")
    assert run.status == "succeeded", run.error_message
    figs = {(f.metric_key, f.period): f for f in _figs(db, run.id)}

    storage = figs[("energy_storage_deployed", "2025")]
    assert storage.quote_verified and "reclassified from energy_supplied" in storage.note
    energy = db.query(CategoryScore).filter_by(run_id=run.id, category="energy_throughput").one()
    assert energy.score is None                                      # storage never scores energy

    c24 = figs[("capex", "FY2024")]
    assert c24.quote_verified, c24.rejected_reason
    assert (c24.value, c24.unit) == (11339, "USD_millions") and "table" in c24.note
    c25 = figs[("capex", "FY2025")]
    assert c25.quote_verified and (c25.value, c25.unit) == (8527, "USD_millions")

    planned = [f for f in _figs(db, run.id) if f.metric_key == "datacenter_capacity_planned"]
    assert planned and planned[0].quote_verified

    rev = figs[("revenue", "FY2025")]
    assert rev.quote_verified and "model cited" in rev.note        # found in the 10-K, not the Q4 page

    # tesla.com refused us (403) -> the Wayback copy was used and labelled as archived
    q4 = db.query(Source).filter_by(run_id=run.id, url=T_Q4).one()
    assert q4.status == "ok" and q4.archive_url.startswith("https://web.archive.org/web/20260130090000id_/")
    assert q4.archive_timestamp == "20260130090000" and "HTTP 403" in q4.reject_reason
    assert company.identity_status == "auto" and company.ticker == "TSLA" and company.cik == "0001318605"


# ---------------------------------------------------------------- SpaceXSI
S_FWP = "https://www.sec.gov/Archives/edgar/data/1181412/000119312526000001/fwp.htm"
S_BOND = "https://www.capacityglobal.com/news/spacex-bond-deal"
S_WIRED = "https://www.wired.com/story/xai-colossus-gas-turbines/"
S_RESOLVE = {"official_name": "SpaceXSI", "domain": "spacexsi.com", "ticker": "SPCX", "exchange": "NASDAQ",
             "is_public": False, "sec_filer": False, "industry": "AI infrastructure", "hq": None,
             "description": "SpaceX AI division.", "confidence": 0.8,
             "ambiguity": "SpaceX AI division / X.AI LLC"}
S_RESEARCH = {"sources": [{"url": u, "title": None, "covers": ["growth"], "why": "x"} for u in (S_FWP, S_BOND, S_WIRED)]}


def spacexsi_routes():
    return fakes.world_routes({S_FWP: (200, "text/html", page("spacexsi_fwp.html")),
                               S_BOND: (200, "text/html", page("spacexsi_bond.html")),
                               S_WIRED: (200, "text/html", page("spacexsi_turbines.html"))})


def spacexsi_extract(user):
    s = _sid(user)
    return {"figures": [
        {"source_id": s[S_BOND], "metric_key": "capex", "value": 20000000000, "unit": "USD", "period": "2026",
         "scope": "company", "quote": "SpaceX priced a $20 billion bond deal on Tuesday to fund the expansion of its AI data centers"},
        {"source_id": s[S_WIRED], "metric_key": "capex", "value": 2800000000, "unit": "USD", "period": "2025",
         "scope": "company", "quote": "agreed to buy natural-gas turbines worth $2.8 billion"},
        # v2.1 bug: the dot-leader row failed verbatim matching
        {"source_id": s[S_FWP], "metric_key": "capex", "value": 12734, "unit": "USD_millions", "period": "FY2025",
         "scope": "company", "quote": "Purchases of property and equipment ... (12,734)"},
        {"source_id": s[S_FWP], "metric_key": "datacenter_capacity_operating", "value": 1.0, "unit": "GW",
         "period": "2025", "scope": "company",
         "quote": "Our COLOSSUS and COLOSSUS II supercomputer clusters in Memphis, Tennessee, have a combined power capacity of approximately 1.0 gigawatt"},
    ], "claims": []}


def test_spacexsi_bond_and_turbine_deals_are_not_capex(db):
    llm = fakes.FakeLLM({"resolve": [S_RESOLVE], "research": [S_RESEARCH], "extract": [spacexsi_extract],
                         "judge": [lambda u: {"categories": [], "synthesis": ""}]})
    run, company = _run(db, fakes.make_deps(llm, routes=spacexsi_routes()), "spacexsi")
    assert run.status == "succeeded", run.error_message
    figs = _figs(db, run.id)
    capex = {f.value: f for f in figs if f.metric_key == "capex"}
    assert not capex[20000000000].quote_verified and "financing" in capex[20000000000].rejected_reason
    assert not capex[2800000000].quote_verified
    assert re.search(r"deal|primary", capex[2800000000].rejected_reason)
    fwp = capex[12734]
    assert fwp.quote_verified, fwp.rejected_reason                     # dot leaders handled
    assert fwp.unit == "USD_millions" and "...." in fwp.quote          # stored as the source prints it
    dc = next(f for f in figs if f.metric_key == "datacenter_capacity_operating")
    assert dc.quote_verified

    # entity: "NASDAQ: SPCX" is not in SEC's registry -> ticker dropped, flagged, identity not saved
    resolve = db.query(JudgmentStage).filter_by(run_id=run.id, stage="resolve").one()
    chk = resolve.detail["entity"]["identity_check"]
    assert chk["status"] == "not_listed" and chk["consistent"] is False
    assert resolve.detail["identity_saved"] is False
    assert company.ticker is None and company.identity_status is None
    assert any("identity" in f for f in (run.summary or {}).get("flags", []) or (run.checkpoint or {}).get("flags", []))


def test_entity_flip_prevented_by_persisted_identity(db):
    """Run 1 resolves confidently and saves the identity; run 2 reuses it even though the model
    would now resolve the name to a different entity (no resolve call is made)."""
    flip = {**T_RESOLVE, "official_name": "Tesla Energy Ventures Ltd", "ticker": "TEV", "exchange": "LSE"}
    llm = fakes.FakeLLM({"resolve": [T_RESOLVE, flip], "research": [T_RESEARCH], "extract": [tesla_extract],
                         "judge": [lambda u: {"categories": [], "synthesis": ""}]})
    deps = fakes.make_deps(llm, routes=tesla_routes())
    _run1, company = _run(db, deps, "tesla")
    assert company.identity_status == "auto"
    run2, _ = enqueue_run(db, company.id, trigger="test", triggered_by="test")
    run2 = execute_run(db, run2.id, deps)
    assert llm.stages_called().count("resolve") == 1
    db.expire_all()
    company = db.get(Company, company.id)
    assert company.official_name == "Tesla, Inc." and company.ticker == "TSLA"
    resolve = db.query(JudgmentStage).filter_by(run_id=run2.id, stage="resolve").one()
    assert resolve.detail["identity"] == "reused"


def test_ticker_belonging_to_another_registrant_is_dropped(db):
    wrong = {**S_RESOLVE, "ticker": "TSLA", "is_public": True, "sec_filer": True, "confidence": 0.95,
             "ambiguity": None}
    llm = fakes.FakeLLM({"resolve": [wrong], "research": [{"sources": []}]})
    run, company = _run(db, fakes.make_deps(llm, routes=spacexsi_routes()), "spacexsi")
    resolve = db.query(JudgmentStage).filter_by(run_id=run.id, stage="resolve").one()
    chk = resolve.detail["entity"]["identity_check"]
    assert chk["status"] == "mismatch" and "Tesla" in chk["note"]
    assert company.ticker is None and company.cik is None and company.identity_status is None


def test_pinned_identity_is_always_used(db, admin_client):
    c = Company(canonical_name="spacexsi", industry="Unknown")
    db.add(c)
    db.commit()
    r = admin_client.post(f"/admin/companies/{c.id}/identity", follow_redirects=False,
                          data={"official_name": "Space Exploration Technologies Corp.", "domain": "https://www.spacex.com/",
                                "ticker": "", "exchange": "", "cik": "1181412", "is_public": "0", "action": "pin"})
    assert r.status_code == 303
    db.expire_all()
    c = db.get(Company, c.id)
    assert c.identity_status == "pinned" and c.identity["cik"] == "0001181412" and c.domain == "spacex.com"
    llm = fakes.FakeLLM({"resolve": [S_RESOLVE], "research": [{"sources": []}]})
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="test")
    execute_run(db, run.id, fakes.make_deps(llm, routes=spacexsi_routes()))
    assert "resolve" not in llm.stages_called()
    st = db.query(JudgmentStage).filter_by(run_id=run.id, stage="resolve").one()
    assert st.detail["identity"] == "pinned" and st.detail["entity"]["official_name"].startswith("Space Exploration")
    # clear -> next run resolves again
    admin_client.post(f"/admin/companies/{c.id}/identity", data={"action": "clear"}, follow_redirects=False)
    db.expire_all()
    assert db.get(Company, c.id).identity_status is None


# ---------------------------------------------------------------- NVIDIA display
def test_large_figures_never_render_in_scientific_notation(db, client):
    from app.runs import fix_sci
    from app.pipeline.measures import fmt_num
    assert fmt_num(1053479) == "1,053,479" and "e+" not in fmt_num(1.05348e6)
    assert fix_sci("Energy consumed: 1.05348e+06 MWh", [1053479]) == "Energy consumed: 1,053,479 MWh"
    llm = fakes.world_llm()
    run, company = _run(db, fakes.make_deps(llm), "nvidia")
    assert run.published
    html = client.get(f"/companies/{company.id}").text
    assert "612,000" in html and not re.search(r"\d\.\d+e\+\d", html)


@pytest.mark.parametrize("quote,expected", [
    ("Purchases of property and equipment ............ (12,734)", 12734),
    ("Purchases of property and equipment ( 11,339 )", 11339),
    ("Capital expenditures …… $ 4,213", 4213),
])
def test_table_number_forms(quote, expected):
    from app.pipeline import evidence as ev
    assert ev.number_matches(expected, quote)


def test_wayback_url_helper_matches_fetch_module():
    from app.pipeline.fetch import WAYBACK_API
    url = "https://ir.tesla.com/x?y=1"
    assert fakes.WAYBACK_API_URL(url) == WAYBACK_API.format(url=quote(url, safe=""))
