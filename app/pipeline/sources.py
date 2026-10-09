"""Source preference: compact, readable data sources before bulky report PDFs (Bug 1, part 2).

Full sustainability/impact reports are often 100+ MB PDFs that our fetcher cannot read within its
caps, while the same energy figures usually appear in a compact equivalent: an ESG data table or
databook, a KPI/performance-data appendix, a GRI/SASB/TCFD index, a CDP climate response, a CSV/XLSX
download or an HTML data page. Research is asked for those first (prompts-v2.4); this module re-orders
the candidates in code so they survive the JUDGE_MAX_SOURCES cut and are fetched and excerpted first.
"""
from __future__ import annotations

import re

ENERGY_HINT = re.compile(r"impact|sustainab|\besg\b|environment|energy|climate|\bcdp\b|emission|carbon|\bghg\b|"
                         r"\bgri\b", re.IGNORECASE)
COMPACT_DATA = re.compile(
    r"(esg[-_ ]?data|data[-_ ]?(?:book|appendix|table|sheet|pack|center|centre|hub)|databook|factsheet|"
    r"fact[-_ ]sheet|performance[-_ ]data|\bkpis?\b|key[-_ ]metrics|\bgri\b|gri[-_ ]?index|\bsasb\b|\btcfd\b|"
    r"\bcdp\b|climate[-_ ]change[-_ ]response|\.csv\b|\.xlsx?\b|esg[-_ ]?index)", re.IGNORECASE)
BULKY_REPORT = re.compile(r"(extended|full|complete|integrated|annual[-_ ]report|impact[-_ ]report|"
                          r"sustainability[-_ ]report)[^/]*\.pdf\b", re.IGNORECASE)


def is_energy_candidate(c: dict) -> bool:
    return "energy" in (c.get("covers") or []) or bool(ENERGY_HINT.search(f"{c.get('url', '')} {c.get('title') or ''}"))


def candidate_priority(c: dict) -> int:
    """Higher first: research picks beat bare citations; among energy sources, compact data sources
    get a boost and bulky full-report PDFs a small penalty (they are still fetched if there is room)."""
    p = 2 if c.get("origin") == "research" else 0
    if is_energy_candidate(c):
        if COMPACT_DATA.search(f"{c.get('url', '')} {c.get('title') or ''}"):
            p += 3
        elif BULKY_REPORT.search(c.get("url", "")):
            p -= 1
    return p


def rank_candidates(cands: list[dict]) -> list[dict]:
    """Stable sort by priority (ties keep the model's order)."""
    return sorted(cands, key=lambda c: -candidate_priority(c))
