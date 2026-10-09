"""Fake xAI client and fake HTTP for pipeline tests (no network, no paid calls)."""
from __future__ import annotations

import json
import re
from dataclasses import replace

from app.pipeline.config import Settings, WorkerSettings
from app.pipeline.fetch import FetchResult
from app.pipeline.llm import LLMError, LLMResult
from app.pipeline.runner import Deps

FAKE_MODEL = "grok-4.3-fake"


class FakeLLM:
    """responses: stage -> list of (str | dict | callable(user)->str | Exception). Consumed in order."""

    def __init__(self, responses: dict, citations: dict | None = None, cost: float = 0.01):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.citations = citations or {}
        self.cost = cost
        self.calls: list[dict] = []

    def _next(self, system: str, user: str, kind: str) -> tuple[str, str]:
        stage = re.search(r"\[stage:(\w+)\]", system).group(1)
        self.calls.append({"stage": stage, "kind": kind, "user": user})
        queue = self.responses.get(stage)
        if not queue:
            raise AssertionError(f"unexpected LLM call for stage {stage}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(user)
        return stage, item if isinstance(item, str) else json.dumps(item)

    def chat(self, *, system, user, model, max_tokens=8000):
        _stage, text = self._next(system, user, "chat")
        return LLMResult(text=text, model=FAKE_MODEL, input_tokens=1000, output_tokens=300, reasoning_tokens=100,
                         cost_usd=self.cost, duration_ms=5)

    def search(self, *, system, user, model, max_tool_calls, max_output_tokens=6000):
        stage, text = self._next(system, user, "search")
        return LLMResult(text=text, model=FAKE_MODEL, input_tokens=20000, output_tokens=800, reasoning_tokens=300,
                         tool_calls=min(3, max_tool_calls), cost_usd=self.cost * 3,
                         citations=list(self.citations.get(stage, [])), duration_ms=8)

    def stages_called(self):
        return [c["stage"] for c in self.calls]


class FakeFetcher:
    """routes: url -> (status, content_type, body[, final_url]) | FetchResult. Unknown URLs -> 404."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, *, headers=None, max_bytes=None):
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            return FetchResult(url=url, final_url=url, status=404, content_type="text/html",
                               body=b"<html><title>Not found</title><body>Not found</body></html>")
        if isinstance(r, FetchResult):
            return r
        status, ctype, body = r[:3]
        final = r[3] if len(r) > 3 else url
        if isinstance(body, str):
            body = body.encode()
        return FetchResult(url=url, final_url=final, status=status, content_type=ctype, body=body)


def html(title: str, body: str) -> str:
    filler = " ".join(["Our mission is to build the infrastructure that powers progress."] * 6)
    return f"<html><head><title>{title}</title></head><body><nav>Menu</nav><main><h1>{title}</h1><p>{body}</p><p>{filler}</p></main></body></html>"


# ---------------- an NVIDIA-like world ----------------

SUSTAIN_URL = "https://www.nvidia.com/en-us/sustainability/report-2024/"
NEWS_URL = "https://news.example.org/nvidia-blackwell"
POLICY_URL = "https://www.nvidia.com/en-us/policy/"
DEAD_URL = "https://dead.example.net/gone"
SOFT_URL = "https://soft.example.net/energy-report"
ROOT_REDIRECT_URL = "https://www.nvidia.com/old/energy-2019/"

ENERGY_SENTENCE = "In fiscal year 2024, total energy consumption across our operations was 612,000 MWh, up from 489,600 MWh in fiscal year 2023."
DC_SENTENCE = "NVIDIA operates data centers with a combined capacity of 48 MW for research and internal workloads."
PLANNED_SENTENCE = "An additional 120 MW of capacity is planned for fiscal year 2026."
FRONTIER_QUOTE = "Blackwell delivers up to 25x lower cost and energy consumption for large language model inference than the prior generation."
BUILDER_QUOTE = "NVIDIA moved to a one-year rhythm for new data center architectures, shipping Hopper, Blackwell and Blackwell Ultra in successive years."
SOFT_PAGE = html("Energy", "Sorry, we could not locate that resource. Browse our site for more information about our company and products and services today.")

SEC_UA = "Kardashev Index tests test@example.com"
TICKERS_JSON = {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
                "1": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."}}


def _fact(fy_end: str, val: float, filed: str, form: str = "10-K"):
    from datetime import date, timedelta
    start = (date.fromisoformat(fy_end) - timedelta(days=363)).isoformat()
    y = fy_end[:4]
    return {"start": start, "end": fy_end, "val": val, "accn": f"0001045810-{y[2:]}-000010", "fy": int(y),
            "fp": "FY", "form": form, "filed": filed}


COMPANY_FACTS = {
    "cik": 1045810, "entityName": "NVIDIA CORP",
    "facts": {"us-gaap": {
        "PaymentsToAcquireProductiveAssets": {"units": {"USD": [
            _fact("2021-01-31", 1.0e9, "2021-02-26"), _fact("2022-01-30", 0.98e9, "2022-03-18"),
            _fact("2023-01-29", 1.83e9, "2023-02-24"), _fact("2024-01-28", 1.331e9, "2024-02-21"),
            # a quarterly value that must be ignored
            {"start": "2023-10-30", "end": "2024-01-28", "val": 9.9e9, "accn": "x", "fp": "Q4", "form": "10-Q",
             "filed": "2024-02-21"},
        ]}},
        "Revenues": {"units": {"USD": [
            _fact("2021-01-31", 10.0e9, "2021-02-26"), _fact("2024-01-28", 27.0e9, "2024-02-21"),
            _fact("2024-01-28", 26.9e9, "2023-03-01"),  # older filing for same FY loses
        ]}},
    }},
}


def world_routes(extra: dict | None = None) -> dict:
    routes = {
        SUSTAIN_URL: (200, "text/html", html("NVIDIA Sustainability Report FY2024",
                                             f"{ENERGY_SENTENCE} {DC_SENTENCE} {PLANNED_SENTENCE}")),
        NEWS_URL: (200, "text/html", html("Blackwell ships", f"{FRONTIER_QUOTE} {BUILDER_QUOTE}")),
        POLICY_URL: (200, "text/html", html("Public policy", "We engage with governments on many topics.")),
        DEAD_URL: (404, "text/html", "<html><title>404</title></html>"),
        SOFT_URL: (200, "text/html", SOFT_PAGE),
        "https://soft.example.net/ki-probe-PROBE": (200, "text/html", SOFT_PAGE),
        ROOT_REDIRECT_URL: (200, "text/html", html("NVIDIA", "Welcome to NVIDIA, the world leader in accelerated computing."),
                            "https://www.nvidia.com/"),
        "https://www.sec.gov/files/company_tickers.json": (200, "application/json", json.dumps(TICKERS_JSON)),
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json": (200, "application/json",
                                                                          json.dumps(COMPANY_FACTS)),
    }
    routes.update(extra or {})
    return routes


RESOLVE_OK = {"official_name": "NVIDIA Corporation", "domain": "https://www.nvidia.com/", "ticker": "nvda",
              "exchange": "NASDAQ", "is_public": True, "sec_filer": True, "industry": "Semiconductors & AI computing",
              "hq": "Santa Clara, California", "description": "Accelerated computing.", "confidence": 0.97,
              "ambiguity": None}
RESEARCH_OK = {"sources": [
    {"url": SUSTAIN_URL, "title": "Sustainability report", "covers": ["energy", "compute"], "why": "energy table"},
    {"url": NEWS_URL, "title": "Blackwell", "covers": ["frontier_acceleration"], "why": "launch"},
    {"url": DEAD_URL, "title": "Old", "covers": ["energy"], "why": "x"},
    {"url": SOFT_URL, "title": "Soft", "covers": ["energy"], "why": "x"},
    {"url": ROOT_REDIRECT_URL, "title": "Old energy page", "covers": ["energy"], "why": "x"},
]}


def extract_ok(user: str) -> dict:
    sid = {m.group(2): m.group(1) for m in re.finditer(r'<source id="(S\d+)" url="([^"]+)"', user)}
    s, n = sid[SUSTAIN_URL], sid[NEWS_URL]
    return {"figures": [
        {"source_id": s, "metric_key": "energy_consumption", "value": 612000, "unit": "MWh", "period": "FY2024",
         "scope": "operations", "quote": "total energy consumption across our operations was 612,000 MWh"},
        {"source_id": s, "metric_key": "energy_consumption", "value": 489600, "unit": "MWh", "period": "FY2023",
         "scope": "operations", "quote": "up from 489,600 MWh in fiscal year 2023"},
        {"source_id": s, "metric_key": "datacenter_capacity_operating", "value": 48, "unit": "MW", "period": "2024",
         "scope": "company", "quote": "operates data centers with a combined capacity of 48 MW"},
        {"source_id": s, "metric_key": "datacenter_capacity_planned", "value": 120, "unit": "MW", "period": "FY2026",
         "scope": "company", "quote": "An additional 120 MW of capacity is planned"},
        # hallucinated quote (not in the page) -> rejected
        {"source_id": s, "metric_key": "energy_supplied", "value": 9000000, "unit": "MWh", "period": "2024",
         "scope": "x", "quote": "NVIDIA generated 9,000,000 MWh of renewable electricity in 2024"},
        # value not in the quote -> rejected
        {"source_id": s, "metric_key": "electricity_consumption", "value": 700000, "unit": "MWh", "period": "2024",
         "scope": "x", "quote": "total energy consumption across our operations was 612,000 MWh"},
    ], "claims": [
        {"source_id": n, "category": "frontier_acceleration", "claim": "Blackwell cuts inference cost 25x",
         "quote": FRONTIER_QUOTE},
        {"source_id": n, "category": "builder_velocity", "claim": "Annual architecture cadence",
         "quote": BUILDER_QUOTE.replace("NVIDIA moved", "NVIDIA  moved")},  # whitespace differences are fine
        {"source_id": n, "category": "policy_stance", "claim": "Lobbies for open source",
         "quote": "NVIDIA has lobbied tirelessly for open-source AI and permitting reform."},  # hallucinated
    ]}


def judge_ok(user: str) -> dict:
    ids = {cat: int(i) for i, cat in re.findall(r"\[E(\d+)\] \((\w+);", user)}
    return {"categories": [
        {"category": "frontier_acceleration", "score": 9.0, "confidence": 0.8, "insufficient_evidence": False,
         "evidence_ids": [ids["frontier_acceleration"]], "rationale": "Large cost reductions [E]."},
        {"category": "builder_velocity", "score": 8.0, "confidence": 0.7, "insufficient_evidence": False,
         "evidence_ids": [ids["builder_velocity"]], "rationale": "Annual cadence."},
        {"category": "policy_stance", "score": None, "confidence": 0, "insufficient_evidence": True,
         "evidence_ids": [], "rationale": "No verified policy evidence."},
    ], "synthesis": "A compute-centric builder whose own energy footprint is modest."}


def world_llm(**overrides) -> FakeLLM:
    responses = {"resolve": [RESOLVE_OK], "research": [RESEARCH_OK], "extract": [extract_ok], "judge": [judge_ok]}
    responses.update(overrides)
    return FakeLLM(responses, citations={"research": [NEWS_URL, POLICY_URL]})


NO_RETRY = WorkerSettings(max_attempts=1, auto_retry=False)
RETRY = WorkerSettings(max_attempts=4, retry_delays_min=(2.0, 10.0, 60.0), auto_retry=True)


def make_deps(llm=None, routes=None, sec=True, retry=NO_RETRY, fetcher=None, now=None, **settings) -> Deps:
    """Deterministic deps. By default no automatic retries (each run is its own final attempt);
    reliability tests pass ``retry=RETRY``."""
    base = Settings()
    cfg = replace(base, sec_user_agent=SEC_UA if sec else None, model="grok-4.3", **settings)
    extra = {"now": now} if now else {}
    return Deps(llm=llm, fetcher=fetcher or FakeFetcher(world_routes() if routes is None else routes), settings=cfg,
                probe_token=lambda: "PROBE", retry=retry, **extra)


def api_error(msg="HTTP 503: overloaded"):
    return LLMError(msg, retryable=True, status=503)


def WAYBACK_API_URL(url: str) -> str:
    from urllib.parse import quote

    from app.pipeline.fetch import WAYBACK_API
    return WAYBACK_API.format(url=quote(url, safe=""))
