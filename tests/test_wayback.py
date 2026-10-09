"""Wayback fallback (pipeline-v2.7), tested against responses recorded from archive.org on
2026-10-09 (tests/fixtures/wayback): CDX lookup filtered to HTTP 200, the dead/empty availability
API, Akamai's archived 403 page, rate limits and the "Temporarily Offline" page served with 200.
Every attempt is recorded on the source row, used or not."""
import pytest

from app.models import Company, JudgmentRun, Source
from app.pipeline.fetch import (WAYBACK_API, WAYBACK_RAW, FetchResult, SourceChecker, Wayback,
                                is_block_page, snapshot)
from app.pipeline.largepdf import Result as PdfResult
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes

IMPACT = "https://www.tesla.com/impact"
PDF = "https://www.tesla.com/ns_videos/2024-extended-version-tesla-impact-report.pdf"
F = fakes.wayback_fixture
AKAMAI = (403, "text/html", F("akamai_access_denied.html"))
IMPACT_COPY = (200, "text/html", F("tesla_impact_20260611033023.html"))


def api_url(url):
    from urllib.parse import quote
    return WAYBACK_API.format(url=quote(url, safe=""))


def raw(ts, url=IMPACT):
    return WAYBACK_RAW.format(ts=ts, url=url)


def fetch(routes, url=IMPACT, **kw):
    f = fakes.FakeFetcher(routes)
    wb = Wayback(f, **kw)
    res, a, att = wb.fetch(url, SourceChecker(f, probe_token=lambda: "P").assess)
    return res, a, att, f, wb


# ---------------------------------------------------------------- lookup
def test_cdx_lookup_uses_newest_200_capture_first():
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (200, "text/plain", F("cdx_tesla_impact.json")),
              raw("20260611033023"): IMPACT_COPY}
    res, a, att, f, _ = fetch(routes)
    assert a.status == "ok" and res.archive_timestamp == "20260611033023" and res.archived_from == IMPACT
    assert att.as_dict() == {"lookup": "cdx", "outcome": "used", "note": "using the Wayback Machine copy of 2026-06-11",
                             "used": "20260611033023", "tried": [{"ts": "20260611033023", "result": "ok"}]}
    assert len(f.calls) == 2          # one lookup, one copy
    assert "Impact Report 2024" in a.snapshot.text


def test_archived_block_page_is_skipped_for_an_older_good_copy():
    # The newest 200 capture can still be a bot wall archived with status 200; the next one is used.
    cdx = fakes.cdx_body(IMPACT, "20260611033023", "20260701000000")
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (200, "application/json", cdx),
              raw("20260701000000"): (200, "text/html", F("akamai_access_denied.html")),
              raw("20260611033023"): IMPACT_COPY}
    _, _, att, *_ = fetch(routes)
    assert att.outcome == "used" and att.used == "20260611033023"
    assert att.tried[0] == {"ts": "20260701000000", "result": "block page"}


def test_akamai_page_reads_as_block_page():
    snap = snapshot("text/html", F("akamai_access_denied.html").encode())
    assert is_block_page(snap.title, snap.text)
    real = snapshot("text/html", F("tesla_impact_20260611033023.html").encode())
    assert not is_block_page(real.title, real.text)


def test_no_200_capture_is_recorded_as_not_found():
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (200, "application/json", F("cdx_empty.json"))}
    res, _, att, f, _ = fetch(routes)
    assert res is None and att.outcome == "not_found" and att.note == "Wayback has no HTTP-200 capture"
    assert len(f.calls) == 1          # an empty CDX answer is authoritative: no availability call


@pytest.mark.parametrize("api,expect", [
    ((200, "application/json", F("availability_empty.json")), "not_found"),
    ((404, "text/html", F("web_archive_available_404.html")), "unavailable"),
])
def test_cdx_failure_falls_back_to_availability_api(api, expect):
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (404, "text/html", F("web_archive_available_404.html")),
              api_url(IMPACT): api}
    _, _, att, *_ = fetch(routes)
    assert att.lookup == "availability" and att.outcome == expect


def test_availability_success_shape_is_used_when_cdx_is_unreadable():
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (200, "text/plain", "garbage"),
              api_url(IMPACT): (200, "application/json", F("availability_ok.json")),
              raw("20260611033023"): IMPACT_COPY}
    _, a, att, *_ = fetch(routes)
    assert a.status == "ok" and att.lookup == "availability" and att.used == "20260611033023"


def test_old_web_archive_availability_endpoint_is_not_used():
    assert WAYBACK_API.startswith("https://archive.org/wayback/available")


# ---------------------------------------------------------------- outages, rate limits, cap
def test_temporarily_offline_page_with_200_trips_the_breaker():
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (200, "text/html", F("ia_temporarily_offline.html")),
              fakes.WAYBACK_CDX_URL(PDF): (200, "application/json", F("cdx_tesla_extended_pdf.json"))}
    f = fakes.FakeFetcher(routes)
    wb = Wayback(f)
    check = SourceChecker(f, probe_token=lambda: "P").assess
    _, _, att = wb.fetch(IMPACT, check)
    assert att.outcome == "unavailable" and "outage page" in att.note
    _, _, att2 = wb.fetch(PDF, check)                 # later sources in the run don't hit archive.org
    assert att2.outcome == "unavailable" and att2.note.startswith("Wayback skipped")
    assert len(f.calls) == 1


def test_cdx_5xx_trips_the_breaker_without_trying_the_availability_api():
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (502, "text/html", "<html>Bad gateway</html>")}
    _, _, att, f, wb = fetch(routes)
    assert att.outcome == "unavailable" and wb.down == "unavailable" and len(f.calls) == 1


def test_rate_limit_trips_the_breaker():
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (429, "text/html", F("ia_429.html"))}
    f = fakes.FakeFetcher(routes)
    wb = Wayback(f)
    _, _, att = wb.fetch(IMPACT, SourceChecker(f).assess)
    assert att.outcome == "rate_limited" and "429" in att.note and wb.down == "rate_limited"
    _, _, att2 = wb.fetch(PDF, SourceChecker(f).assess)
    assert att2.outcome == "rate_limited" and len(f.calls) == 1


def test_offline_page_instead_of_copy_stops_trying():
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (200, "text/plain", F("cdx_tesla_impact.json")),
              raw("20260611033023"): (200, "text/html", F("ia_temporarily_offline.html"))}
    _, _, att, f, _ = fetch(routes)
    assert att.outcome == "unavailable" and att.tried == [{"ts": "20260611033023", "result": "unavailable"}]
    assert len(f.calls) == 2


def test_per_run_request_cap():
    routes = {fakes.WAYBACK_CDX_URL(IMPACT): (200, "text/plain", F("cdx_tesla_impact.json")),
              raw("20260611033023"): (200, "text/html", F("akamai_access_denied.html")),
              raw("20260609112824"): (200, "text/html", F("akamai_access_denied.html"))}
    _, _, att, f, wb = fetch(routes, max_requests=3)
    assert len(f.calls) == 3 and wb.requests == 3
    assert att.outcome == "unusable" and len(att.tried) == 2
    _, _, att2 = wb.fetch(PDF, SourceChecker(f).assess)
    assert att2.outcome == "capped" and "cap of 3" in att2.note and len(f.calls) == 3


# ---------------------------------------------------------------- the pipeline
def _tesla_world(extra_routes, sources):
    research = {"sources": [*fakes.RESEARCH_OK["sources"], *sources]}
    llm = fakes.world_llm(research=[research])
    return llm, fakes.world_routes(extra_routes)


def _run(db, llm, routes, **deps):
    c = Company(canonical_name="tesla", official_name="Tesla", industry="Autos")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    from dataclasses import replace
    d = fakes.make_deps(llm, routes=routes)
    if deps:
        d = replace(d, **deps)
    return execute_run(db, run.id, d)


def test_run_uses_archived_impact_page_and_records_attempts_on_every_403(db, admin_client):
    other = "https://ir.tesla.com/sec-filings"
    llm, routes = _tesla_world({
        IMPACT: AKAMAI,
        fakes.WAYBACK_CDX_URL(IMPACT): (200, "text/plain", F("cdx_tesla_impact.json")),
        raw("20260611033023"): IMPACT_COPY,
        other: AKAMAI,
        fakes.WAYBACK_CDX_URL(other): (200, "application/json", F("cdx_empty.json")),
    }, [{"url": IMPACT, "title": "Impact", "covers": ["energy"], "why": "impact report"},
        {"url": other, "title": "SEC filings", "covers": ["growth"], "why": "filings"}])
    run = _run(db, llm, routes)
    assert run.status == "succeeded", run.error_message
    imp = db.query(Source).filter_by(run_id=run.id, url=IMPACT).one()
    assert imp.status == "ok" and imp.archive_timestamp == "20260611033023"
    assert imp.archive_attempt["outcome"] == "used"
    sec = db.query(Source).filter_by(run_id=run.id, url=other).one()
    assert sec.status == "dead" and sec.http_status == 403 and sec.archive_url is None
    assert sec.archive_attempt["outcome"] == "not_found"
    assert sec.reject_reason == "HTTP 403; Wayback has no HTTP-200 capture"
    fetch_stage = next(s for s in db.get(JudgmentRun, run.id).stages if s.stage == "fetch")
    outcomes = fetch_stage.detail["wayback"]["outcomes"]
    assert outcomes["used"] == 1 and outcomes["not_found"] == 1
    page = admin_client.get(f"/admin/runs/{run.id}").text
    assert "Wayback: not found" in page and "archived copy 2026-06-11" in page


def test_archived_oversized_pdf_goes_to_the_large_pdf_reader_via_the_archive_url(db):
    seen = []

    def reader(url):
        seen.append(url)
        return PdfResult(ok=True, text="Energy Consumption (kWh) 1,673,681,511 681,364,318 1,477,221,711 " * 10,
                         method="range", total_bytes=178_715_126)
    ts = "20260826110621"
    big = FetchResult(url=raw(ts, PDF), final_url=raw(ts, PDF), status=200, content_type="application/pdf",
                      body=b"%PDF-1.7 " + b"x" * 1000, truncated=True)
    llm, routes = _tesla_world({
        PDF: AKAMAI,
        fakes.WAYBACK_CDX_URL(PDF): (200, "application/json", F("cdx_tesla_extended_pdf.json")),
        raw(ts, PDF): big,
    }, [{"url": PDF, "title": "2024 Impact Report (extended)", "covers": ["energy"], "why": "energy table"}])
    run = _run(db, llm, routes, large_pdf=reader)
    src = db.query(Source).filter_by(run_id=run.id, url=PDF).one()
    assert seen == [raw(ts, PDF)]
    assert src.status == "ok" and src.archive_timestamp == ts and src.archive_attempt["used"] == ts
