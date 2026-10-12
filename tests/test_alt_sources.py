"""1.7.0 / pipeline-v2.13: alternative sources. The latest 10-K / 20-F from SEC EDGAR is fetched as a
candidate; when no energy source could be read, ResponsibilityReports.com supplies the company's
newest report (fixtures recorded from responsibilityreports.com on 2026-10-12)."""
import json
from pathlib import Path

from app.models import Company, JudgmentStage, Source
from app.pipeline.edgar import SUBMISSIONS_URL, latest_annual_report
from app.pipeline.registries import RR_SEARCH, responsibility_reports, rr_company_path, rr_report_link
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes
from tests.test_energy_unreadable import IMPACT_URL, _world

FIX = Path(__file__).parent / "fixtures" / "registries"
SEARCH = (FIX / "rr_search_tesla.html").read_text()
LOCKED = (FIX / "rr_company_tesla_locked.html").read_text()
NVIDIA = (FIX / "rr_company_nvidia.html").read_text()

SUBMISSIONS = {"filings": {"recent": {
    "form": ["8-K", "10-Q", "10-K/A", "10-K", "10-K"],
    "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-26-000003", "0001045810-26-000023",
                        "0001045810-25-000023"],
    "primaryDocument": ["a.htm", "b.htm", "c.htm", "nvda-20260125.htm", "nvda-20250126.htm"],
    "filingDate": ["2026-05-01", "2026-04-01", "2026-03-15", "2026-02-25", "2025-02-26"]}}}
DOC_URL = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000023/nvda-20260125.htm"


# ---------------------------------------------------------------- EDGAR annual report
def test_latest_original_annual_report_is_picked():
    doc = latest_annual_report(SUBMISSIONS, "0001045810")
    assert doc == {"url": DOC_URL, "form": "10-K", "filed": "2026-02-25", "accn": "0001045810-26-000023"}
    assert latest_annual_report({"filings": {"recent": {"form": ["8-K"], "accessionNumber": ["x"],
                                                        "primaryDocument": ["y"]}}}, "1") is None
    assert latest_annual_report({}, "1") is None


def test_twenty_f_counts():
    sub = {"filings": {"recent": {"form": ["6-K", "20-F"], "accessionNumber": ["a", "0001046179-26-000010"],
                                  "primaryDocument": ["x.htm", "tsm-20251231.htm"], "filingDate": ["", "2026-04-10"]}}}
    assert latest_annual_report(sub, "0001046179")["form"] == "20-F"


def _company(db, name="nvidia"):
    c = Company(canonical_name=name, official_name=name.title(), industry="Semiconductors")
    db.add(c)
    db.commit()
    return c


def test_run_fetches_the_annual_report_as_a_primary_source(db):
    text = fakes.html("NVIDIA 10-K", "Our data centers consumed 1,234,000 MWh of electricity in fiscal 2026. " * 5)
    routes = fakes.world_routes({SUBMISSIONS_URL.format(cik="0001045810"): (200, "application/json", json.dumps(SUBMISSIONS)),
                                 DOC_URL: (200, "text/html", text)})
    c = _company(db)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run = execute_run(db, run.id, fakes.make_deps(fakes.world_llm(), routes=routes))
    src = db.query(Source).filter(Source.run_id == run.id, Source.url == DOC_URL).one()
    assert src.origin == "filing" and src.status == "ok" and src.is_primary
    edgar = db.query(JudgmentStage).filter(JudgmentStage.run_id == run.id, JudgmentStage.stage == "edgar").one()
    assert edgar.detail["annual_report"]["form"] == "10-K"


def test_missing_submissions_never_fails_the_run(db):
    c = _company(db, "nvidia2")
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run = execute_run(db, run.id, fakes.make_deps(fakes.world_llm(), routes=fakes.world_routes()))
    assert run.status == "succeeded"


def test_annual_report_can_be_switched_off(db):
    routes = fakes.world_routes({SUBMISSIONS_URL.format(cik="0001045810"): (200, "application/json", json.dumps(SUBMISSIONS))})
    f = fakes.FakeFetcher(routes)
    c = _company(db, "nvidia3")
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    execute_run(db, run.id, fakes.make_deps(fakes.world_llm(), fetcher=f, edgar_annual_report=False))
    assert DOC_URL not in f.calls and SUBMISSIONS_URL.format(cik="0001045810") not in f.calls


# ---------------------------------------------------------------- ResponsibilityReports.com
def test_search_page_match():
    assert rr_company_path(SEARCH, "Tesla, Inc.") == ("/Company/tesla-inc", "Tesla, Inc.")
    assert rr_company_path(SEARCH, "Teslar Mining Corp") is None


def test_unlocked_most_recent_report_uses_the_click_link():
    url, label = rr_report_link(NVIDIA)
    assert url == "https://www.responsibilityreports.com/Click/1532" and "2024 Sustainability Report" in label


def test_locked_most_recent_falls_back_to_newest_archive():
    url, label = rr_report_link(LOCKED)
    assert url.endswith("/ResponsibilityReportArchive/t/NASDAQ_TSLA_2022.pdf") and label == "2022 report (archived)"


def test_lookup_returns_one_registry_candidate():
    from urllib.parse import quote
    routes = {RR_SEARCH.format(q=quote("tesla")): (200, "text/html", SEARCH),
              "https://www.responsibilityreports.com/Company/tesla-inc": (200, "text/html", LOCKED)}
    out = responsibility_reports(fakes.FakeFetcher(routes), "Tesla, Inc.")
    assert len(out) == 1 and out[0]["origin"] == "registry" and out[0]["covers"] == ["energy"]
    assert responsibility_reports(fakes.FakeFetcher({}), "Tesla, Inc.") == []


def test_registry_is_tried_when_no_energy_source_was_read(db):
    from urllib.parse import quote
    pdf_url = "https://www.responsibilityreports.com/HostedData/ResponsibilityReportArchive/t/NASDAQ_TSLA_2022.pdf"
    llm, routes = _world((403, "text/html", "denied"))
    # the fake resolve stage names the entity "NVIDIA Corporation"
    routes.update({RR_SEARCH.format(q=quote("nvidia")): (200, "text/html",
                   SEARCH.replace("Tesla, Inc.", "NVIDIA Corporation")),
                   "https://www.responsibilityreports.com/Company/tesla-inc": (200, "text/html", LOCKED),
                   pdf_url: (200, "text/html", fakes.html("Impact report", "Electricity use was 100 GWh. " * 30))})
    c = Company(canonical_name="examplemotors", official_name="Examplemotors", industry="Autos")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run = execute_run(db, run.id, fakes.make_deps(llm, routes=routes))
    src = db.query(Source).filter(Source.run_id == run.id, Source.url == pdf_url).one()
    assert src.origin == "registry" and src.status == "ok"
    assert db.query(Source).filter(Source.run_id == run.id, Source.url == IMPACT_URL).one().status == "dead"


def test_registry_not_tried_when_energy_was_read(db):
    f = fakes.FakeFetcher(fakes.world_routes())
    c = _company(db, "nvidia4")
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    execute_run(db, run.id, fakes.make_deps(fakes.world_llm(), fetcher=f))
    assert not any("responsibilityreports" in u for u in f.calls)
