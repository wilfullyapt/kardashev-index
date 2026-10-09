"""Which unreadable sources mean "the company's energy disclosure exists but we couldn't read it"
(pipeline-v2.9). Only then is a run without an energy figure left unranked (energy_unreadable)
instead of being scored as "no energy figure found".

A source counts only when all of these hold:
  1. publisher: the company's own domain (subdomains included) or a disclosure registry (CDP,
     ResponsibilityReports). Third-party news, magazines and blogs never count: figures must be
     verified against the primary document anyway, so an unreadable article hides nothing we could
     have used; the primary document is what discovery (pipeline-v2.8) goes after.
  2. page type: listing / announcement pages (SEC-filings index, financial results, events, news,
     press releases, blogs, the investor-relations root) are excluded, unless research tagged the
     page as covering energy AND its claim is about energy consumption.
  3. an energy-disclosure signal in the URL, title or research claim (impact/sustainability/ESG
     report or data, CDP, GHG, scope 1/2, energy or electricity use, kWh/MWh, data appendix, GRI
     index), or the URL is the company's own impact / sustainability / ESG page (path depth <= 2) or
     a report PDF. Research's "covers energy" tag alone is not enough.
  4. a "thin" page (little text, typically a JavaScript shell) needs the signal in its URL or title,
     not only in research's claim, so ordinary JS-rendered marketing pages never count.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

REGISTRIES = ("cdp.net", "responsibilityreports.com")
INDEX_RE = re.compile(
    r"/(?:sec-filings|filings|financial-information|financials|quarterly-results|annual-reports?|events|"
    r"news(?:room)?|press(?:-releases?|room)?|press-release|media(?:-center)?|blog|stories|articles?)(?:/|$)",
    re.IGNORECASE)
INVESTOR_ROOT_RE = re.compile(r"^/(?:investors?|ir)?/?$", re.IGNORECASE)
CONSUMPTION_RE = re.compile(
    r"energy (?:use|usage|consumption|consumed)|electricity (?:use|usage|consumption|consumed)|"
    r"(?:k|m|g)wh (?:consumed|of electricity)|power (?:use|consumption)|scope ?2|\bghg\b|emissions? inventory",
    re.IGNORECASE)
SIGNAL_RE = re.compile(
    r"(?:impact|sustainab\w*|esg|climate|environmental|responsibility)[-_ ]?(?:report|data|appendix|disclosure|"
    r"index|databook|factbook|performance)|\bcdp\b|\bghg\b|scope[-_ ]?[12]\b|"
    r"energy[-_ ](?:use|usage|consumption|data)|electricity[-_ ](?:use|usage|consumption)|\b(?:k|m|g)wh\b|"
    r"data[-_ ]?(?:appendix|table|sheet)|\bgri[-_ ](?:index|content)|tcfd",
    re.IGNORECASE)
REPORT_PATH_RE = re.compile(r"^/(?:[a-z]{2}(?:[-_][a-z]{2})?/)?(?:impact|sustainability|esg|environment|climate|"
                            r"responsibility|corporate-responsibility)(?:/[^/]*)?/?$", re.IGNORECASE)
REPORT_PDF_RE = re.compile(r"impact|sustainab|\besg\b|\bcdp\b|climate|environment|emission|\bghg\b|energy",
                           re.IGNORECASE)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def publisher(url: str, domain: str | None) -> str | None:
    """'own' | 'registry' | 'unknown-domain' when the company domain is unknown | None (third party)."""
    host = _host(url)
    if any(_under(host, r) for r in REGISTRIES):
        return "registry"
    domain = (domain or "").lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
    if not domain:
        return "unknown-domain"
    return "own" if _under(host, domain) else None


def classify(url: str, title: str | None, status: str | None, covers=(), why: str | None = None,
             domain: str | None = None) -> str | None:
    """Why this unreadable source counts as the company's energy disclosure, or None."""
    pub = publisher(url, domain)
    if pub is None:
        return None
    path = urlsplit(url).path or "/"
    own_text = f"{url} {title or ''}"
    is_energy = "energy" in (covers or ())
    if INDEX_RE.search(path) or (pub == "own" and INVESTOR_ROOT_RE.match(path) and _host(url).startswith(("ir.", "investor"))):
        if is_energy and CONSUMPTION_RE.search(f"{why or ''} {title or ''}"):
            return f"{pub}: listing/announcement page whose claim is about energy consumption"
        return None
    report_page = bool(REPORT_PATH_RE.match(path)) or (path.lower().endswith(".pdf") and bool(REPORT_PDF_RE.search(path)))
    if report_page:
        return f"{pub}: {'report PDF' if path.lower().endswith('.pdf') else 'impact/sustainability page'}"
    if SIGNAL_RE.search(own_text):
        return f"{pub}: energy-disclosure signal in URL/title"
    if status != "thin" and is_energy and SIGNAL_RE.search(why or ""):
        return f"{pub}: research claim names an energy disclosure"
    return None
