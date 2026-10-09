"""pipeline-v2.9: only the company's own energy disclosure (or a registry copy) that couldn't be read
leaves a run unranked. Cases from Tesla #23, whose four 403 sources all counted under v2.6."""
import pytest

from app.models import Company
from app.pipeline.disclosure import classify
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes

T = "tesla.com"


@pytest.mark.parametrize("url,title,status,covers,why,expect", [
    # Tesla #23 (all HTTP 403): only the impact page is a real disclosure
    ("https://www.tesla.com/impact", "Impact | Tesla", "dead", ["energy"], "impact report", "own: impact/sustainability page"),
    ("https://ir.tesla.com/sec-filings", "SEC Filings", "dead", ["energy", "growth"], "10-K energy storage", None),
    ("https://ir.tesla.com/press-release/tesla-third-quarter-2026-production-deliveries-and-deployments",
     "Tesla Third Quarter 2026 Production, Deliveries & Deployments", "dead", ["energy"],
     "12.5 GWh of energy storage deployed in Q3", None),
    ("https://sustainabilitymag.com/news/teslas-sustainability-impact-report-2025-reaching-net-zero",
     "Tesla's Sustainability Impact Report 2025", "dead", ["energy"], "report summary", None),
    # own report PDFs, data pages and registries
    ("https://www.tesla.com/ns_videos/2024-extended-version-tesla-impact-report.pdf", None, "error", ["energy"], "", "own: report PDF"),
    ("https://www.tesla.com/en_us/impact", None, "dead", [], "", "own: impact/sustainability page"),
    ("https://investors.acme.com/esg-data-appendix", None, "dead", [], "", "own: energy-disclosure signal in URL/title"),
    ("https://www.cdp.net/en/responses/12345/climate-change-2024", "CDP climate response", "dead", [], "",
     "registry: energy-disclosure signal in URL/title"),
    # a press release counts only when its claim is about energy consumption
    ("https://ir.tesla.com/press-release/tesla-publishes-2025-energy-use", "Press release", "dead", ["energy"],
     "reports total energy consumption for 2025", "own: listing/announcement page whose claim is about energy consumption"),
    # covers=energy alone is not enough
    ("https://www.tesla.com/megapack", "Megapack", "dead", ["energy"], "utility-scale storage product", None),
    # thin JS shells: a signal only in research's claim doesn't count; one in the URL does
    ("https://www.anduril.com/", "Anduril Industries", "thin", ["energy"], "sustainability report data", None),
    ("https://www.anduril.com/sustainability-report", "", "thin", [], "", "own: energy-disclosure signal in URL/title"),
])
def test_classify(url, title, status, covers, why, expect):
    domain = "anduril.com" if "anduril" in url else ("acme.com" if "acme" in url else T)
    assert classify(url, title, status, covers, why, domain) == expect


def test_domain_forms_and_unknown_domain():
    assert classify("https://www.tesla.com/impact", None, "dead", (), None, "https://www.tesla.com/")
    assert classify("https://news.example.org/sustainability-report-2025", None, "dead", (), None, None) \
        == "unknown-domain: energy-disclosure signal in URL/title"    # no domain known: previous behaviour


# ---------------------------------------------------------------- the run (domain nvidia.com in the fake world)
FALSE = [  # Tesla #23's false triggers, transposed to the fake world's company domain
    ("https://ir.nvidia.com/sec-filings", "SEC Filings", ["energy"], "10-K"),
    ("https://ir.nvidia.com/press-release/q3-2026-production-deliveries-and-deployments", "Q3 2026 deliveries",
     ["energy"], "energy storage deployments"),
    ("https://sustainabilitymag.com/news/nvidia-sustainability-impact-report-2025", "NVIDIA impact report",
     ["energy"], "summary"),
]
REAL = ("https://www.nvidia.com/impact", "Impact", ["energy"], "impact report")


def _no_energy(user):
    out = fakes.extract_ok(user)
    out["figures"] = [f for f in out["figures"] if not f["metric_key"].startswith(("energy", "electricity"))]
    return out


def _run(db, extra):
    research = {"sources": [*fakes.RESEARCH_OK["sources"],
                            *({"url": u, "title": t, "covers": c, "why": w} for u, t, c, w in extra)]}
    routes = fakes.world_routes({u: (403, "text/html", fakes.wayback_fixture("akamai_access_denied.html"))
                                 for u, *_ in extra})
    c = Company(canonical_name=f"n{len(extra)}", official_name="N", industry="Chips")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    return execute_run(db, run.id, fakes.make_deps(fakes.world_llm(research=[research], extract=[_no_energy]),
                                                   routes=routes))


def test_false_triggers_alone_do_not_make_the_run_energy_unreadable(db):
    run = _run(db, FALSE)
    assert run.status == "succeeded"
    assert run.summary["rank_basis"] != "energy_unreadable" and not run.summary["energy_unreadable"]


def test_own_impact_page_still_does_and_says_why(db):
    run = _run(db, [*FALSE, REAL])
    assert run.summary["rank_basis"] == "energy_unreadable"
    assert [(x["url"], x["counted_as"]) for x in run.summary["energy_unreadable"]] == [
        (REAL[0], "own: impact/sustainability page")]
