"""SEC EDGAR (free): ticker -> CIK, and annual capex / revenue / R&D from XBRL company facts.
SEC requires a descriptive User-Agent with contact info (env SEC_EDGAR_USER_AGENT); without it
this stage is skipped rather than sending an anonymous client."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz

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


_TICKERS_CACHE: dict = {"at": 0.0, "data": None}
_TICKERS_LOCK = threading.Lock()
TICKERS_TTL_S = 24 * 3600
_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|sa|nv|ag|se|"
                     r"holdings?|group|the|class [a-z])\b\.?", re.IGNORECASE)


def clear_tickers_cache():
    with _TICKERS_LOCK:
        _TICKERS_CACHE.update(at=0.0, data=None)


def _core_name(name: str) -> str:
    t = _SUFFIX.sub(" ", (name or "").lower().replace("&", " and "))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def names_match(a: str, b: str, threshold: int = 70) -> bool:
    """Registrant title vs a resolved name ('NVIDIA CORP' ~ 'NVIDIA Corporation')."""
    ca, cb = _core_name(a), _core_name(b)
    if not ca or not cb:
        return False
    return ca == cb or fuzz.token_set_ratio(ca, cb) >= threshold


class EdgarClient:
    def __init__(self, fetcher: Fetcher, user_agent: str, clock=time.monotonic):
        self.fetcher = fetcher
        self.user_agent = user_agent
        self._clock = clock

    def tickers(self) -> dict:
        """company_tickers.json, cached in-process for 24 h (SEC asks clients not to re-download it)."""
        with _TICKERS_LOCK:
            data, at = _TICKERS_CACHE["data"], _TICKERS_CACHE["at"]
            if data is not None and self._clock() - at < TICKERS_TTL_S:
                return data
        data = self._json(TICKERS_URL, max_bytes=10_000_000)
        with _TICKERS_LOCK:
            _TICKERS_CACHE.update(at=self._clock(), data=data)
        return data

    def lookup_cik(self, cik: str) -> tuple[str, str, str] | None:
        """(10-digit CIK, title, ticker) for a CIK, or None."""
        want = int(cik)
        for row in self.tickers().values():
            if int(row.get("cik_str", -1)) == want:
                return f"{want:010d}", row.get("title", ""), str(row.get("ticker", "")).upper()
        return None

    def lookup_name(self, name: str, min_score: int = 92) -> tuple[str, str, str] | None:
        """Best registrant whose core name matches ``name`` closely and unambiguously:
        (CIK, title, ticker) or None."""
        core = _core_name(name)
        if len(core) < 3:
            return None
        best: dict[str, tuple[float, str, str]] = {}
        for row in self.tickers().values():
            title = row.get("title", "")
            ct = _core_name(title)
            score = 100.0 if ct == core else fuzz.ratio(ct, core)
            if score >= min_score:
                cik = f"{int(row['cik_str']):010d}"
                if cik not in best or score > best[cik][0]:
                    best[cik] = (score, title, str(row.get("ticker", "")).upper())
        if not best:
            return None
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        if len(ranked) > 1 and ranked[1][1][0] >= ranked[0][1][0] - 2:
            return None   # ambiguous
        cik, (_, title, ticker) = ranked[0]
        return cik, title, ticker

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
        data = self.tickers()
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


@dataclass
class IdentityCheck:
    """Result of cross-checking a model-resolved identity against SEC's registry."""
    status: str                      # confirmed | not_listed | mismatch | name_match | unchecked
    cik: str | None = None
    title: str | None = None
    ticker: str | None = None
    note: str | None = None


US_EXCHANGES = ("NASDAQ", "NYSE", "NYSE AMERICAN", "NYSE ARCA", "AMEX", "CBOE", "BATS")


def check_identity(client: EdgarClient | None, entity: dict) -> IdentityCheck:
    """Ticker/public status vs company_tickers.json:
    - ticker found and registrant name matches -> confirmed (public SEC filer, CIK set);
    - ticker found but registrant differs       -> mismatch (ticker dropped);
    - ticker not in the registry                -> not_listed (ticker kept only for non-US exchanges);
    - no ticker, but an unambiguous registrant name match -> name_match (CIK set, public)."""
    if client is None:
        return IdentityCheck("unchecked", note="SEC_EDGAR_USER_AGENT not set")
    name = entity.get("official_name") or ""
    ticker = (entity.get("ticker") or "").strip().upper()
    if ticker:
        hit = client.lookup_ticker(ticker)
        if hit:
            cik, title = hit
            if names_match(title, name):
                return IdentityCheck("confirmed", cik, title, ticker)
            return IdentityCheck("mismatch", None, title, None,
                                 f"ticker {ticker} belongs to SEC registrant '{title}', not '{name}'")
        exch = (entity.get("exchange") or "").upper()
        if any(exch.startswith(x) for x in US_EXCHANGES):
            return IdentityCheck("not_listed", note=f"{exch}: {ticker} is not in SEC's company tickers list")
    hit = client.lookup_name(name)
    if hit:
        return IdentityCheck("name_match", hit[0], hit[1], hit[2] or None)
    if ticker:   # a non-US (or unstated) exchange: SEC lists US registrants only, so keep the ticker
        exch = entity.get("exchange") or "exchange not stated"
        return IdentityCheck("unchecked", note=f"{ticker} ({exch}) not cross-checked: SEC lists US registrants only")
    return IdentityCheck("unchecked", note="no ticker and no SEC registrant with this name")
