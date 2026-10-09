"""Report-PDF discovery (pipeline-v2.8): PDF links followed from a readable report page or its
Wayback copy, using the archived tesla.com/impact page recorded on 2026-10-09."""
import re
from dataclasses import replace

import pytest

from app.models import Company, Evidence, JudgmentRun, Source
from app.pipeline.discover import is_report_page, pick, report_links
from app.pipeline.fetch import WAYBACK_RAW, FetchResult
from app.pipeline.largepdf import Result as PdfResult
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes

F = fakes.wayback_fixture
IMPACT = "https://www.tesla.com/impact"
EXT = "https://www.tesla.com/ns_videos/2024-extended-version-tesla-impact-report.pdf"
HIGHLIGHTS = "https://www.tesla.com/ns_videos/2024-tesla-impact-report-highlights.pdf"
AKAMAI = (403, "text/html", F("akamai_access_denied.html"))
PAGE_TS, PDF_TS = "20260611033023", "20260826110621"


def test_archived_tesla_impact_page_yields_the_2024_extended_report():
    links = report_links(F("tesla_impact_20260611033023.html"), IMPACT)
    assert [lk.url for lk in links[:3]] == [EXT, HIGHLIGHTS, "https://www.tesla.com/ns_videos/2023-tesla-impact-report.pdf"]
    assert len(links) == 6 and links[0].year == 2024 and links[0].full and links[1].summary
    # the highlights edition of the same year and all older reports are skipped
    assert [lk.url for lk in pick(links)] == [EXT]


def test_summary_only_newest_year_also_takes_the_previous_full_report():
    html = ('<a href="/r/2025-impact-highlights.pdf">2025 highlights</a>'
            '<a href="https://cdn.example.com/2024-sustainability-report.pdf">2024 report</a>'
            '<a href="/r/2023-sustainability-report.pdf">2023</a>')
    got = [lk.url for lk in pick(report_links(html, "https://example.com/sustainability/"))]
    assert got == ["https://example.com/r/2025-impact-highlights.pdf", "https://cdn.example.com/2024-sustainability-report.pdf"]


def test_same_year_data_appendix_is_the_second_pick():
    html = ('<a href="2024-esg-report.pdf">ESG Report 2024</a><a href="2024-esg-data-appendix.pdf">Data appendix</a>'
            '<a href="2023-esg-report.pdf">2023</a>')
    assert [lk.url.rsplit("/", 1)[1] for lk in pick(report_links(html, "https://x.com/esg/"))] == [
        "2024-esg-data-appendix.pdf", "2024-esg-report.pdf"]


def test_non_disclosure_pdfs_and_non_pdf_links_are_ignored():
    html = ('<a href="/careers/benefits-2025.pdf">Benefits</a><a href="/impact/report">Impact</a>'
            '<a href="mailto:x@y.z">mail</a><a href="/legal/terms.pdf">Terms</a>'
            '<a href="/docs/Q3.pdf">2024 Climate disclosure (TCFD)</a>')
    assert [lk.url for lk in report_links(html, "https://x.com/")] == ["https://x.com/docs/Q3.pdf"]


@pytest.mark.parametrize("url,title,covers,expect", [
    (IMPACT, "Impact | Tesla", (), True),
    ("https://x.com/about", "About", ("energy",), True),
    ("https://x.com/sustainability", None, (), True),
    ("https://x.com/careers", "Careers", ("growth",), False),
])
def test_is_report_page(url, title, covers, expect):
    assert is_report_page(url, title, covers) is expect


# ---------------------------------------------------------------- the pipeline
TEXT = ("Key metrics. Total energy consumption in 2024 was 1,673,681,511 kWh across our operations. " * 8)


def _extract_from_discovered(user):
    sid = {u: s for s, u in re.findall(r'<source id="(S\d+)" url="([^"]+)"', user)}
    out = fakes.extract_ok(user)
    out["figures"] = [f for f in out["figures"] if not f["metric_key"].startswith(("energy", "electricity"))]
    out["figures"].append({"source_id": sid[EXT], "metric_key": "energy_consumption", "value": 1673681511,
                           "unit": "kWh", "period": "2024", "scope": "company",
                           "quote": "Total energy consumption in 2024 was 1,673,681,511 kWh"})
    return out


def test_403_impact_page_archived_copy_leads_to_the_report_pdf_read_by_the_large_pdf_reader(db):
    seen = []

    def reader(url):
        seen.append(url)
        return PdfResult(ok=True, text=TEXT, method="range", total_bytes=178_715_126)
    big = FetchResult(url=WAYBACK_RAW.format(ts=PDF_TS, url=EXT), final_url=WAYBACK_RAW.format(ts=PDF_TS, url=EXT),
                      status=200, content_type="application/pdf", body=b"%PDF-1.7 " + b"x" * 1000, truncated=True)
    routes = fakes.world_routes({
        IMPACT: AKAMAI,
        fakes.WAYBACK_CDX_URL(IMPACT): (200, "text/plain", F("cdx_tesla_impact.json")),
        WAYBACK_RAW.format(ts=PAGE_TS, url=IMPACT): (200, "text/html", F("tesla_impact_20260611033023.html")),
        EXT: AKAMAI,
        fakes.WAYBACK_CDX_URL(EXT): (200, "application/json", F("cdx_tesla_extended_pdf.json")),
        WAYBACK_RAW.format(ts=PDF_TS, url=EXT): big,
    })
    research = {"sources": [*fakes.RESEARCH_OK["sources"],
                            {"url": IMPACT, "title": "Tesla Impact", "covers": ["energy"], "why": "impact report"}]}
    llm = fakes.world_llm(research=[research], extract=[_extract_from_discovered])
    c = Company(canonical_name="tesla", official_name="Tesla", industry="Autos")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run = execute_run(db, run.id, replace(fakes.make_deps(llm, routes=routes), large_pdf=reader))
    assert run.status == "succeeded", run.error_message
    pdf = db.query(Source).filter_by(run_id=run.id, url=EXT).one()
    assert pdf.origin == "discovered" and pdf.status == "ok" and pdf.archive_timestamp == PDF_TS
    assert seen == [WAYBACK_RAW.format(ts=PDF_TS, url=EXT)]
    assert db.query(Source).filter_by(run_id=run.id, url=HIGHLIGHTS).count() == 0
    fig = db.query(Evidence).filter_by(run_id=run.id, source_id=pdf.id, kind="figure").one()
    assert fig.quote_verified and fig.value == 1673681511
    assert run.summary.get("rank_basis") != "energy_unreadable"
    st = next(s for s in db.get(JudgmentRun, run.id).stages if s.stage == "fetch")
    assert st.detail["discovered"] == [{"url": EXT, "from": IMPACT}]


def test_discovery_cap_and_switch(db):
    links = "".join(f'<a href="/s/{y}-sustainability-data-{i}.pdf">x</a>' for y in (2024,) for i in range(5))
    page = "https://acme.example/sustainability"
    routes = fakes.world_routes({page: (200, "text/html", fakes.html("Sustainability", "Our reports. " * 40 + links))})
    research = {"sources": [*fakes.RESEARCH_OK["sources"], {"url": page, "title": "S", "covers": ["energy"], "why": "x"}]}

    def go(**settings):
        c = Company(canonical_name=f"acme{len(settings)}{sorted(settings.items())}", official_name="Acme", industry="X")
        db.add(c)
        db.commit()
        run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
        run = execute_run(db, run.id, fakes.make_deps(fakes.world_llm(research=[research]), routes=routes, **settings))
        return db.query(Source).filter_by(run_id=run.id, origin="discovered").count()
    assert go() == 2                                  # per page
    assert go(discover_per_page=5) == 4               # per run (FETCH_DISCOVER_MAX)
    assert go(discover_max=0, discover_per_page=2) == 0   # switched off
