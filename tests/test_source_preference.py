"""Bug 1 part 2 (prompts-v2.4): compact data sources are preferred over bulky report PDFs."""
from app.pipeline import prompts as P
from app.pipeline.runner import execute_run
from app.pipeline.sources import candidate_priority, rank_candidates
from app.runs import enqueue_run
from tests import fakes

FULL = {"url": "https://www.tesla.com/ns_videos/2024-tesla-extended-impact-report.pdf", "title": "2024 Impact Report",
        "covers": ["energy", "builder_velocity"], "origin": "research"}
APPENDIX = {"url": "https://www.tesla.com/impact/data", "title": "Impact data appendix (ESG data table)",
            "covers": ["energy"], "origin": "research"}
CDP = {"url": "https://www.cdp.net/en/responses/12345?back_to=climate", "title": "Tesla CDP climate change response",
       "covers": ["energy"], "origin": "research"}
TENK = {"url": "https://www.sec.gov/Archives/tsla-10k.htm", "title": "10-K", "covers": ["growth"], "origin": "research"}
CITE = {"url": "https://news.example.org/x", "title": None, "covers": [], "origin": "citation"}


def test_compact_energy_sources_rank_first_and_bulky_pdf_last_among_energy():
    order = rank_candidates([FULL, TENK, CITE, APPENDIX, CDP])
    assert [c["url"] for c in order[:2]] == [APPENDIX["url"], CDP["url"]]
    assert order.index(TENK) < order.index(FULL) < order.index(CITE)      # still fetched if there is room


def test_ranking_is_stable_and_ignores_non_energy_data_words():
    a = {"url": "https://x.com/kpi-dashboard", "title": "Sales KPIs", "covers": ["growth"], "origin": "research"}
    b = {"url": "https://x.com/b", "title": "B", "covers": ["growth"], "origin": "research"}
    assert candidate_priority(a) == candidate_priority(b) == 2
    assert rank_candidates([a, b]) == [a, b]


def test_prompt_asks_for_smaller_equivalents():
    assert tuple(int(x) for x in P.PROMPT_VERSION.removeprefix("prompts-v").split(".")) >= (2, 4)
    for phrase in ("ESG data tables", "CDP", "HTML data page", "BEFORE the full report", "100+ MB"):
        assert phrase in P.RESEARCH_SYSTEM


def test_compact_source_survives_the_max_sources_cut(db):
    from app.models import Company
    data_url = "https://www.nvidia.com/en-us/sustainability/esg-data-appendix/"
    filler = [{"url": f"https://news.example.org/{i}", "title": f"News {i}", "covers": ["frontier_acceleration"],
               "why": "x"} for i in range(9)]
    research = {"sources": [*fakes.RESEARCH_OK["sources"], *filler,
                            {"url": data_url, "title": "ESG data appendix", "covers": ["energy"], "why": "table"}]}
    llm = fakes.world_llm(research=[research])
    routes = fakes.world_routes({data_url: (200, "text/html", fakes.html("ESG data", fakes.ENERGY_SENTENCE))})
    fetcher = fakes.FakeFetcher(routes)
    c = Company(canonical_name="nvidia", industry="Chips")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run = execute_run(db, run.id, fakes.make_deps(llm, fetcher=fetcher))
    assert run.status == "succeeded"
    assert data_url in fetcher.calls                     # 15th in the model's list: cut at 12 before
    assert "https://news.example.org/8" not in fetcher.calls   # a low-priority item is dropped instead
