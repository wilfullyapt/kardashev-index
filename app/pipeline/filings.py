"""SEC filing hygiene for the fetch stage (pipeline-v2.14).

- A full-submission text file (``/Archives/edgar/data/<cik>/<accession>.txt``) is the whole filing in SGML:
  every exhibit and every XBRL attachment, often 10-20 MB. It is never fetched; the primary document
  (the 10-K / 20-F ``.htm``) carries the same report text.
- At most one annual report (10-K / 20-F / 40-F) is read per run. Copies multiply quickly (the EDGAR
  stage's own candidate, research hits on sec.gov, investor-relations mirrors such as
  ``ir.<company>.com/_flysystem/s3/sec/<accession>/...``) and each one costs extraction budget for the
  same text. Preference: the EDGAR stage's candidate (latest filing, declared client) > sec.gov >
  the newest other copy.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .fetch import is_sec_host

_FULL_TXT = re.compile(r"^/Archives/edgar/data/\d+/\d{10}-\d{2}-\d{6}\.txt$", re.IGNORECASE)
_ACCESSION = re.compile(r"(?<!\d)(\d{10}-?\d{2}-?\d{6})(?!\d)")
_FORM_WORDS = re.compile(r"\b(10-?k|20-?f|40-?f|annual report)\b", re.IGNORECASE)
_IXBRL_MAIN = re.compile(r"/[a-z][a-z0-9.]*-(\d{8})\.html?$", re.IGNORECASE)   # e.g. tsla-20251231.htm


def is_full_submission_txt(url: str) -> bool:
    return is_sec_host(url) and bool(_FULL_TXT.match(urlsplit(url).path or ""))


def accession(url: str) -> str | None:
    m = _ACCESSION.search(urlsplit(url).path or "")
    return m.group(1).replace("-", "") if m else None


def is_annual_report(c: dict) -> bool:
    """A candidate that is an annual-report document: the EDGAR stage's filing, or a URL carrying an
    SEC accession number that names a 10-K/20-F/40-F (in URL, title or reason) or is an inline-XBRL
    main document (``ticker-YYYYMMDD.htm``)."""
    if c.get("origin") == "filing":
        return True
    url = c.get("url") or ""
    if not accession(url):
        return False
    words = " ".join(str(c.get(k) or "") for k in ("url", "title", "why"))
    return bool(_FORM_WORDS.search(words) or _IXBRL_MAIN.search(urlsplit(url).path or ""))


def _rank(c: dict) -> tuple:
    url = c.get("url") or ""
    m = _IXBRL_MAIN.search(urlsplit(url).path or "")
    return (c.get("origin") == "filing", is_sec_host(url), m.group(1) if m else "", accession(url) or "")


def prefilter(cands: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, skipped). Skipped items are ``{"url", "reason"}`` for the stage detail; order is kept."""
    skipped, kept = [], []
    for c in cands:
        if is_full_submission_txt(c.get("url") or ""):
            skipped.append({"url": c["url"], "reason": "SEC full-submission .txt (whole filing with exhibits); "
                                                       "the primary document is read instead"})
        else:
            kept.append(c)
    annual = [c for c in kept if is_annual_report(c)]
    if len(annual) > 1:
        best = max(annual, key=_rank)
        for c in annual:
            if c is not best:
                skipped.append({"url": c["url"], "reason": f"another copy/year of the annual report; "
                                                           f"reading {best['url']} instead"})
        kept = [c for c in kept if c is best or c not in annual]
    return kept, skipped
