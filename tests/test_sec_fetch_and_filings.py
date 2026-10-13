"""pipeline-v2.14: SEC fetch correctness (declared User-Agent on every SEC request, no browser_first or
Wayback for SEC, no full-submission .txt), one annual report per run, archive-first for hosts that refuse
every automated client."""
import httpx
import pytest

from app.models import Company, JudgmentRun, Source
from app.pipeline import filings, largepdf
from app.pipeline.config import sec_user_agent_ok
from app.pipeline.fetch import BROWSER_HEADERS, USER_AGENT, HostProfiles, HttpFetcher, Wayback
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes
from tests.test_energy_retry_escalation import _retries
from tests.test_energy_unreadable import _run, _world

SEC_UA = "Kardashev Index ops@example.com"
SEC_DOC = "https://www.sec.gov/Archives/edgar/data/1318605/000162828026003952/tsla-20251231.htm"
SEC_TXT = "https://www.sec.gov/Archives/edgar/data/1318605/0001628280-26-003952.txt"
IR_MIRROR = "https://ir.tesla.com/_flysystem/s3/sec/000162828025003063/tsla-20241231-gen.pdf"
IR_HTM = "https://ir.tesla.com/_flysystem/s3/sec/000162828025003063/tsla-20241231.htm"
TESLA_IMPACT = "https://www.tesla.com/impact"


def PUBLIC(host, port):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


# ---------------------------------------------------------------- fetcher / large PDF / Wayback

def test_sec_user_agent_wins_over_caller_browser_headers():
    seen = []
    f = HttpFetcher(transport=httpx.MockTransport(lambda r: seen.append(r.headers["user-agent"]) or httpx.Response(200, text="ok")),
                    resolve=PUBLIC, sleep=lambda s: None, ua_strategy="honest_first", profiles=HostProfiles(),
                    sec_user_agent=SEC_UA)
    f.get(SEC_DOC, headers=dict(BROWSER_HEADERS))          # what an energy retry (browser_first) used to send
    f.get("https://data.sec.gov/submissions/CIK0001318605.json", headers={"User-Agent": "Custom/1"})
    f.get("https://e.example/", headers={"User-Agent": "Custom/1"})   # non-SEC: the caller's UA still wins
    assert seen == [SEC_UA, SEC_UA, "Custom/1"]


def test_sec_redirect_target_gets_the_sec_user_agent():
    seen = []

    def h(r):
        seen.append((r.url.host, r.headers["user-agent"]))
        if r.url.host == "ir.example":
            return httpx.Response(302, headers={"location": SEC_DOC})
        return httpx.Response(200, text="ok")
    f = HttpFetcher(transport=httpx.MockTransport(h), resolve=PUBLIC, sleep=lambda s: None, ua_strategy="browser",
                    sec_user_agent=SEC_UA)
    f.get("https://ir.example/10k", headers=dict(BROWSER_HEADERS))
    assert seen == [("ir.example", USER_AGENT), ("www.sec.gov", SEC_UA)]


def test_large_pdf_reader_uses_the_sec_user_agent_on_sec_hosts(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", SEC_UA)
    assert largepdf._base_headers(SEC_DOC.replace(".htm", ".pdf"))["User-Agent"] == SEC_UA
    assert largepdf._base_headers("https://www.tesla.com/x.pdf")["User-Agent"] == USER_AGENT
    assert largepdf._base_headers(None)["User-Agent"] == USER_AGENT
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT")
    assert largepdf._base_headers(SEC_DOC)["User-Agent"] == USER_AGENT


def test_wayback_never_looks_up_sec_urls():
    f = fakes.FakeFetcher({})
    copy, assessment, att = Wayback(f).fetch(SEC_DOC, lambda r: None)
    assert copy is None and assessment is None and att.outcome == "skipped" and f.calls == []


@pytest.mark.parametrize("ua,ok", [
    ("Kardashev Index ops@example.com", True), ("KI wil@kardashevindex.com", True),
    ("ops@example.com", False), ("KardashevIndex", False), ("Kardashev Index", False), ("", False), (None, False),
])
def test_sec_user_agent_format(ua, ok):
    assert sec_user_agent_ok(ua) is ok


def test_health_reports_the_sec_user_agent_format(client, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "KardashevIndex")
    cfg = client.get("/health").json()["config"]
    assert cfg["sec_edgar_user_agent"] is True and cfg["sec_edgar_user_agent_format_ok"] is False


# ---------------------------------------------------------------- filings.prefilter

def test_full_submission_txt_is_skipped():
    kept, skipped = filings.prefilter([{"url": SEC_TXT}, {"url": SEC_DOC}])
    assert [c["url"] for c in kept] == [SEC_DOC] and skipped[0]["url"] == SEC_TXT
    assert not filings.is_full_submission_txt("https://example.com/0001628280-26-003952.txt")
    assert not filings.is_full_submission_txt(SEC_DOC)


def test_one_annual_report_prefers_the_edgar_candidate_then_sec_gov():
    edgar = {"url": SEC_DOC, "origin": "filing", "title": "10-K annual report (SEC EDGAR)"}
    research = [{"url": IR_HTM, "origin": "research", "title": "Tesla 10-K for fiscal 2024"},
                {"url": IR_MIRROR, "origin": "research", "title": "Form 10-K"},
                {"url": "https://www.tesla.com/impact", "origin": "research", "title": "Impact"}]
    kept, skipped = filings.prefilter(research + [edgar])
    assert [c["url"] for c in kept] == ["https://www.tesla.com/impact", SEC_DOC]
    assert {s["url"] for s in skipped} == {IR_HTM, IR_MIRROR} and SEC_DOC in skipped[0]["reason"]
    kept, _ = filings.prefilter(research)                    # no EDGAR candidate: sec.gov copy unavailable
    assert [c["url"] for c in kept] == [IR_HTM, "https://www.tesla.com/impact"]   # newest-dated copy wins
    kept, _ = filings.prefilter([research[0], {"url": SEC_DOC, "origin": "research", "title": "tsla-20251231"}])
    assert [c["url"] for c in kept] == [SEC_DOC]


def test_other_filings_and_single_reports_are_left_alone():
    ex99 = {"url": "https://www.sec.gov/Archives/edgar/data/1318605/000110465926012345/tm2612345d1_ex99-1.htm",
            "title": "Q3 update"}
    one = {"url": IR_HTM, "title": "10-K"}
    kept, skipped = filings.prefilter([ex99, one])
    assert kept == [ex99, one] and skipped == []


# ---------------------------------------------------------------- the fetch stage

def _sec_world(extra_sources, routes_extra):
    research = {"sources": [*fakes.RESEARCH_OK["sources"], *extra_sources]}
    llm = fakes.world_llm(research=[research], extract=[fakes.extract_ok])
    return llm, fakes.world_routes(routes_extra)


def _exec(db, llm, fetcher, **settings):
    c = Company(canonical_name="tesla", official_name="Tesla", industry="Autos")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    return execute_run(db, run.id, fakes.make_deps(llm, fetcher=fetcher, **settings))


def test_fetch_stage_reads_one_annual_report_and_never_the_full_txt(db):
    doc = (200, "text/html", fakes.html("tsla-20251231", "Annual report. " * 50))
    llm, routes = _sec_world([{"url": SEC_TXT, "title": "Tesla 10-K full text", "covers": ["energy"], "why": "10-K"},
                              {"url": IR_HTM, "title": "Tesla 10-K 2024", "covers": ["energy"], "why": "10-K"},
                              {"url": SEC_DOC, "title": "Tesla 10-K 2025", "covers": ["energy"], "why": "10-K"}],
                             {SEC_DOC: doc, IR_HTM: doc, SEC_TXT: doc})
    f = fakes.FakeFetcher(routes)
    run = _exec(db, llm, f)
    urls = {s.url for s in db.query(Source).filter(Source.run_id == run.id)}
    assert SEC_DOC in urls and IR_HTM not in urls and SEC_TXT not in urls
    assert SEC_TXT not in f.calls and IR_HTM not in f.calls
    from app.models import JudgmentStage as RunStage
    st = db.query(RunStage).filter(RunStage.run_id == run.id, RunStage.stage == "fetch").one()
    assert {s["url"] for s in st.detail["skipped"]} == {SEC_TXT, IR_HTM}


def test_sec_403_is_not_sent_to_the_wayback_machine(db):
    llm, routes = _sec_world([{"url": SEC_DOC, "title": "10-K", "covers": ["energy"], "why": "10-K"}],
                             {SEC_DOC: (403, "text/html", "Undeclared Automated Tool")})
    routes.update(fakes.wayback_routes(SEC_DOC, (200, "text/html", fakes.html("copy", "x " * 300))))
    f = fakes.FakeFetcher(routes)
    run = _exec(db, llm, f)
    src = db.query(Source).filter(Source.run_id == run.id, Source.url == SEC_DOC).one()
    assert src.status == "dead" and src.archive_url is None
    assert not any("archive.org" in u and "sec.gov" in u for u in f.calls)


def test_archive_first_host_uses_the_copy_without_asking_the_origin(db):
    copy = (200, "text/html", fakes.html("Impact | Tesla", "We used 9 TWh of electricity in 2025. " * 30))
    llm, routes = _sec_world([{"url": TESLA_IMPACT, "title": "Impact", "covers": ["energy"], "why": "energy"}],
                             {TESLA_IMPACT: (403, "text/html", "Access Denied")})
    routes.update(fakes.wayback_routes(TESLA_IMPACT, copy))
    f = fakes.FakeFetcher(routes)
    run = _exec(db, llm, f, fetch_archive_first_hosts=frozenset({"www.tesla.com"}))
    src = db.query(Source).filter(Source.run_id == run.id, Source.url == TESLA_IMPACT).one()
    assert src.archive_url and "refuses automated clients" in src.reject_reason
    assert TESLA_IMPACT not in f.calls


def test_archive_first_host_falls_back_to_live_when_no_copy(db):
    page = (200, "text/html", fakes.html("Impact | Tesla", "We used 9 TWh of electricity in 2025. " * 30))
    llm, routes = _sec_world([{"url": TESLA_IMPACT, "title": "Impact", "covers": ["energy"], "why": "energy"}],
                             {TESLA_IMPACT: page})
    f = fakes.FakeFetcher(routes)                             # CDX lookup -> 404: no capture
    run = _exec(db, llm, f, fetch_archive_first_hosts=frozenset({"www.tesla.com"}))
    src = db.query(Source).filter(Source.run_id == run.id, Source.url == TESLA_IMPACT).one()
    assert src.status == "ok" and src.archive_url is None and src.archive_attempt
    assert f.calls.count(TESLA_IMPACT) == 1


def test_archive_first_default_hosts(monkeypatch):
    from app.pipeline.config import ARCHIVE_FIRST_DEFAULT, settings
    monkeypatch.delenv("FETCH_ARCHIVE_FIRST_HOSTS")
    assert settings().fetch_archive_first_hosts == frozenset(ARCHIVE_FIRST_DEFAULT)
    assert "digitalassets.tesla.com" not in settings().fetch_archive_first_hosts
    monkeypatch.setenv("FETCH_ARCHIVE_FIRST_HOSTS", "")
    assert settings().fetch_archive_first_hosts == frozenset()


def test_browser_first_energy_retry_skips_browser_headers_for_sec(db):
    llm, routes = _world((403, "text/html", "denied"))
    _c, _first = _run(db, llm, routes)
    retry = _retries(db)[-1]
    retry.checkpoint = {**retry.checkpoint, "strategy": "browser_first",
                        "retry_urls": [*retry.checkpoint["retry_urls"], SEC_DOC]}
    db.commit()
    captured = {}

    class Spy(fakes.FakeFetcher):
        def get(self, url, *, headers=None, max_bytes=None, **kw):
            captured[url] = dict(headers or {})
            return super().get(url, headers=headers, max_bytes=max_bytes)

    llm2, routes2 = _world((403, "text/html", "denied"))
    execute_run(db, retry.id, fakes.make_deps(llm2, fetcher=Spy(routes2)))
    assert captured[SEC_DOC] == {}
    assert any(h.get("User-Agent") == USER_AGENT for u, h in captured.items() if "sec.gov" not in u)
    assert db.get(JudgmentRun, retry.id).pipeline_version == "pipeline-v2.14"
