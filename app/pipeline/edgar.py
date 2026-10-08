"""SEC EDGAR (free): ticker -> CIK, and annual capex / revenue / R&D from XBRL company facts.
SEC requires a descriptive User-Agent with contact info (env SEC_EDGAR_USER_AGENT); without it
this stage is skipped rather than sending an anonymous client."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from .fetch import Fetcher

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/"
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "40-F", "10-KT"}

CONCEPTS = {
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
        "PaymentsForCapitalImprovements",
    ],
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "rnd": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
}


class EdgarError(Exception):
    pass


@dataclass
class AnnualValue:
    metric: str
    concept: str
    fiscal_year_end: str   # ISO date
    value: float           # USD
    form: str
    accn: str
    filed: str

    def filing_url(self, cik: str) -> str:
        return FILING_INDEX_URL.format(cik_int=int(cik), accn_nodash=self.accn.replace("-", ""))


class EdgarClient:
    def __init__(self, fetcher: Fetcher, user_agent: str):
        self.fetcher = fetcher
        self.user_agent = user_agent

    def _json(self, url: str, max_bytes: int = 60_000_000) -> dict:
        res = self.fetcher.get(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                               max_bytes=max_bytes)
        if res.error or res.status != 200:
            raise EdgarError(f"{url}: {res.error or 'HTTP ' + str(res.status)}")
        try:
            return json.loads(res.body)
        except json.JSONDecodeError as e:
            raise EdgarError(f"{url}: invalid JSON") from e

    def lookup_ticker(self, ticker: str) -> tuple[str, str] | None:
        """Returns (10-digit CIK, SEC registrant title) or None."""
        data = self._json(TICKERS_URL, max_bytes=10_000_000)
        want = ticker.strip().upper().replace(".", "-")
        for row in data.values():
            if str(row.get("ticker", "")).upper() == want:
                return f"{int(row['cik_str']):010d}", row.get("title", "")
        return None

    def company_facts(self, cik: str) -> dict:
        return self._json(FACTS_URL.format(cik=cik))


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def annual_series(facts: dict, metric: str, years: int = 4) -> list[AnnualValue]:
    """Full-fiscal-year USD values for the first concept with data, newest first, one per FY end
    (latest filing wins, so restatements replace originals)."""
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    best: list[AnnualValue] = []
    for concept in CONCEPTS[metric]:
        units = ((gaap.get(concept) or {}).get("units") or {}).get("USD") or []
        by_end: dict[str, AnnualValue] = {}
        for f in units:
            if f.get("form") not in ANNUAL_FORMS or not f.get("start") or not f.get("end"):
                continue
            try:
                if not 340 <= _days(f["start"], f["end"]) <= 380:
                    continue
            except ValueError:
                continue
            v = AnnualValue(metric, concept, f["end"], float(f["val"]), f.get("form", ""), f.get("accn", ""),
                            f.get("filed", ""))
            prev = by_end.get(f["end"])
            if prev is None or v.filed > prev.filed:
                by_end[f["end"]] = v
        series = sorted(by_end.values(), key=lambda v: v.fiscal_year_end, reverse=True)[:years]
        if series and (not best or series[0].fiscal_year_end > best[0].fiscal_year_end):
            best = series
    return best


def financials(facts: dict) -> dict[str, list[AnnualValue]]:
    return {m: annual_series(facts, m) for m in CONCEPTS}
