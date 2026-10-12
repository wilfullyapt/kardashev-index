"""Disclosure-registry fallback (1.7.0, pipeline-v2.13): when no energy source could be read from the
company itself, look the company up on ResponsibilityReports.com (a free public mirror of
sustainability/CSR reports) and add its newest report PDF as a candidate.

* Search: /Companies?search=<core name>, the company link whose name matches (``names_match``).
* Company page: the "most recent" report (/Click/<id>, a redirect to the hosted PDF) unless it is
  locked, else the newest archived PDF (/HostedData/ResponsibilityReportArchive/...).
Figures from it are verified against the PDF like any other source; the registry is already a
recognised energy-disclosure publisher (app/pipeline/disclosure.py REGISTRIES).

CDP (cdp.net) responses are not looked up: their public pages are built in the browser and most
responses need an account, so there is no reliable free URL to fetch. CDP pages that research finds
are still fetched and count as a registry.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from rapidfuzz import fuzz

from .edgar import _core_name, names_match
from .fetch import Fetcher

RR_BASE = "https://www.responsibilityreports.com"
RR_SEARCH = RR_BASE + "/Companies?search={q}"
_COMPANY_LINK = re.compile(r'href="(/Company/[a-z0-9\-]+)"[^>]*>\s*([^<]{2,200})<', re.IGNORECASE)
_MOST_RECENT = re.compile(r'class="most_recent_content_block"(.*?)(?:<h2|class="archived_report|$)', re.IGNORECASE | re.DOTALL)
_CLICK = re.compile(r'href="(/Click/\d+)"[^>]*>\s*([^<]{0,120})<', re.IGNORECASE)
_ARCHIVE_PDF = re.compile(r'href="(/HostedData/ResponsibilityReportArchive/[^"]+?_(\d{4})\.pdf)"', re.IGNORECASE)


def rr_company_path(search_html: str, name: str) -> tuple[str, str] | None:
    """(path, listed name) of the best-matching company on a search results page, or None."""
    best = None
    for path, listed in _COMPANY_LINK.findall(search_html or ""):
        listed = listed.strip()
        if not names_match(listed, name, threshold=85):
            continue
        score = fuzz.token_set_ratio(_core_name(listed), _core_name(name))
        if best is None or score > best[0]:
            best = (score, path, listed)
    return (best[1], best[2]) if best else None


def rr_report_link(company_html: str) -> tuple[str, str] | None:
    """(absolute URL, label) of the newest readable report on a company page, or None."""
    html = company_html or ""
    m = _MOST_RECENT.search(html)
    block = m.group(1) if m else ""
    if block and "report_locked_block" not in block:
        click = _CLICK.search(block)
        if click:
            return urljoin(RR_BASE, click.group(1)), (click.group(2).strip() or "most recent report")
    archived = sorted({(int(y), p) for p, y in _ARCHIVE_PDF.findall(html)}, reverse=True)
    if archived:
        year, path = archived[0]
        return urljoin(RR_BASE, path), f"{year} report (archived)"
    return None


def responsibility_reports(fetcher: Fetcher, name: str) -> list[dict]:
    """At most one candidate: the company's newest report on ResponsibilityReports.com."""
    core = _core_name(name)
    if len(core) < 3:
        return []
    res = fetcher.get(RR_SEARCH.format(q=quote(core)), max_bytes=2_000_000)
    if res.status != 200 or not res.body:
        return []
    hit = rr_company_path(res.body.decode("utf-8", errors="replace"), name)
    if not hit:
        return []
    page = fetcher.get(urljoin(RR_BASE, hit[0]), max_bytes=3_000_000)
    if page.status != 200 or not page.body:
        return []
    link = rr_report_link(page.body.decode("utf-8", errors="replace"))
    if not link:
        return []
    url, label = link
    return [{"url": url, "title": f"{hit[1]}: {label} (ResponsibilityReports.com)", "covers": ["energy"],
             "origin": "registry", "why": f"registry copy of the company's sustainability report: {label}"}]
