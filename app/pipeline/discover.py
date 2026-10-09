"""Report-PDF discovery (pipeline-v2.8): follow PDF links from a report landing page we could read
(the original or its Wayback copy), so "tesla.com/impact" leads to the impact-report PDF itself.

No model calls. Only links that look like a sustainability / impact / ESG / CDP / energy document
are kept; the newest year and the full ("extended", "data") edition rank first, a "highlights" or
"summary" edition is skipped when a fuller one of the same year was chosen. At most
``per_page`` links per page; the fetch stage caps discoveries per run."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

# Landing pages worth mining: the research step said the page covers energy, or its URL path / title
# names a sustainability-type report page.
REPORT_PAGE_RE = re.compile(r"impact|sustainab|\besg\b|climate|environment|responsib|\bcdp\b|energy|emission",
                            re.IGNORECASE)
# The PDF itself must name a disclosure (file name or link text).
REPORT_PDF_RE = re.compile(r"impact|sustainab|\besg\b|climate|environment|responsib|\bcdp\b|\bghg\b|emission|"
                           r"carbon|energy|tcfd|\bgri\b|data[-_ ]?(?:appendix|table|sheet|pack)|kpi",
                           re.IGNORECASE)
FULL_RE = re.compile(r"extended|full|complete|data|appendix|kpi|metrics|disclosure", re.IGNORECASE)
SUMMARY_RE = re.compile(r"highlight|summary|brief|overview|executive|snapshot|infographic|fact[-_ ]?sheet",
                        re.IGNORECASE)
YEAR_RE = re.compile(r"(?<!\d)(20[0-4]\d)(?!\d)")
HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*(["'])(.*?)\1[^>]*>(.*?)</a>""", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Link:
    url: str
    text: str
    year: int | None
    full: bool
    summary: bool


def is_report_page(url: str, title: str | None, covers=()) -> bool:
    return "energy" in (covers or ()) or bool(REPORT_PAGE_RE.search(f"{urlsplit(url).path} {title or ''}"))


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def report_links(html: bytes | str, base_url: str) -> list[Link]:
    """All disclosure-looking PDF links on the page, best first (deduplicated)."""
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    seen, out = set(), []
    for _q, href, inner in HREF_RE.findall(text):
        href = href.strip().replace("&amp;", "&")
        if not href or href.startswith(("#", "mailto:", "javascript:", "data:")):
            continue
        url = urljoin(base_url, href)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.path.lower().endswith(".pdf"):
            continue
        url = parts._replace(fragment="").geturl()
        if url in seen:
            continue
        name = parts.path.rsplit("/", 1)[-1]
        label = _clean(inner)
        if not REPORT_PDF_RE.search(f"{name} {label}"):
            continue
        seen.add(url)
        years = [int(y) for y in YEAR_RE.findall(f"{name} {label}")]
        out.append(Link(url=url, text=label[:200], year=max(years) if years else None,
                        full=bool(FULL_RE.search(f"{name} {label}")),
                        summary=bool(SUMMARY_RE.search(f"{name} {label}"))))
    out.sort(key=lambda lk: (-(lk.year or 0), lk.summary, not lk.full))
    return out


def pick(links: list[Link], per_page: int = 2) -> list[Link]:
    """Newest/full first. Once a full (non-summary) edition of year Y is chosen, summary editions of Y
    and every older year are skipped: an older report adds a large download for figures the newer
    one already restates. A second link is taken for the same year (e.g. a separate data appendix) or
    when the newest edition is only a summary."""
    chosen: list[Link] = []
    for lk in links:
        if len(chosen) >= per_page:
            break
        full = [c for c in chosen if not c.summary]
        if any(lk.summary and c.year == lk.year for c in full):
            continue
        if any(c.year and lk.year and lk.year < c.year for c in full):
            continue
        chosen.append(lk)
    return chosen
