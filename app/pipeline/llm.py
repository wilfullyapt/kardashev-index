"""Thin xAI client over httpx (no SDK): Chat Completions for plain calls and the Responses
API with the server-side `web_search` tool for research. Every call returns the model id the
API reported, token usage, the exact billed cost (`usage.cost_in_usd_ticks`) and wall time."""
from __future__ import annotations

import json
import random
import re
import time
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

TICKS_PER_USD = 1e10

# Fallback prices (USD per 1M tokens / per search call) used only when the API omits
# cost_in_usd_ticks. Source: docs.x.ai/developers/models, Oct 2026. Unknown models are
# priced at the flagship rate so the budget cap errs on the safe side.
PRICES = {
    "grok-4.3": (1.25, 2.50),
    "grok-4.20": (1.25, 2.50),
    "grok-4": (1.25, 2.50),  # alias of grok-4.3
}
FLAGSHIP_PRICE = (2.00, 6.00)
WEB_SEARCH_USD = 0.005


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int, tool_calls: int = 0) -> float:
    rate = FLAGSHIP_PRICE
    for prefix, price in PRICES.items():
        if model and (model == prefix or model.startswith(prefix + "-")):
            rate = price
            break
    return input_tokens / 1e6 * rate[0] + output_tokens / 1e6 * rate[1] + tool_calls * WEB_SEARCH_USD


@dataclass
class LLMResult:
    text: str
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    cost_estimated: bool = False
    citations: list[str] = field(default_factory=list)
    duration_ms: int = 0
    finish_reason: str | None = None


class LLMError(Exception):
    """``retryable`` = transient (429, 5xx, timeout, transport): the run may be retried later."""
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def retry_after_s(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP date), or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - (now if now is not None else time.time()))


def backoff_s(attempt: int, *, base: float = 2.0, cap: float = 60.0, retry_after: float | None = None,
              rand=random.random) -> float:
    """Wait before retry number ``attempt`` (1-based): Retry-After when the server sent one (capped),
    else exponential backoff with full jitter in [base^attempt / 2, base^attempt]."""
    if retry_after is not None:
        return min(cap, retry_after)
    top = min(cap, base ** attempt)
    return top / 2 + rand() * top / 2


class LLM(Protocol):
    def chat(self, *, system: str, user: str, model: str, max_tokens: int = ...) -> LLMResult: ...
    def search(self, *, system: str, user: str, model: str, max_tool_calls: int,
               max_output_tokens: int = ...) -> LLMResult: ...


def _usage(u: dict | None) -> dict:
    u = u or {}
    inp = u.get("prompt_tokens", u.get("input_tokens")) or 0
    out = u.get("completion_tokens", u.get("output_tokens")) or 0
    details_out = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    details_in = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    return {
        "input_tokens": int(inp),
        "output_tokens": int(out),
        "reasoning_tokens": int(details_out.get("reasoning_tokens") or 0),
        "cached_tokens": int(details_in.get("cached_tokens") or 0),
        "ticks": u.get("cost_in_usd_ticks"),
        "server_tools": int(u.get("num_server_side_tools_used") or 0),
    }


def parse_chat_response(data: dict, duration_ms: int) -> LLMResult:
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    u = _usage(data.get("usage"))
    model = data.get("model")
    cost, est = _cost(u, model, 0)
    return LLMResult(text=text, model=model, input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                     reasoning_tokens=u["reasoning_tokens"], cached_tokens=u["cached_tokens"],
                     cost_usd=cost, cost_estimated=est, duration_ms=duration_ms,
                     finish_reason=choice.get("finish_reason"))


def parse_responses_response(data: dict, duration_ms: int) -> LLMResult:
    texts: list[str] = []
    citations: list[str] = []
    searches = 0
    for item in data.get("output") or []:
        kind = item.get("type")
        if kind == "web_search_call":
            searches += 1
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    texts.append(part.get("text") or "")
                    for ann in part.get("annotations") or []:
                        if ann.get("type") == "url_citation" and ann.get("url"):
                            citations.append(ann["url"])
    for url in data.get("citations") or []:
        if isinstance(url, str):
            citations.append(url)
    u = _usage(data.get("usage"))
    tool_calls = u["server_tools"] or searches
    model = data.get("model")
    cost, est = _cost(u, model, tool_calls)
    seen: set[str] = set()
    uniq = [c for c in citations if not (c in seen or seen.add(c))]
    return LLMResult(text="".join(texts).strip(), model=model, input_tokens=u["input_tokens"],
                     output_tokens=u["output_tokens"], reasoning_tokens=u["reasoning_tokens"],
                     cached_tokens=u["cached_tokens"], tool_calls=tool_calls, cost_usd=cost,
                     cost_estimated=est, citations=uniq, duration_ms=duration_ms,
                     finish_reason=data.get("status"))


def _cost(u: dict, model: str | None, tool_calls: int) -> tuple[float, bool]:
    if isinstance(u.get("ticks"), (int, float)):
        return u["ticks"] / TICKS_PER_USD, False
    return estimate_cost(model, u["input_tokens"], u["output_tokens"], tool_calls), True


class XAIClient:
    def __init__(self, api_key: str, *, base_url: str = "https://api.x.ai/v1", chat_timeout_s: float = 120,
                 search_timeout_s: float = 240, max_retries: int = 3, backoff_max_s: float = 60,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep, rand=random.random):
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._base = base_url.rstrip("/")
        self._chat_timeout = chat_timeout_s
        self._search_timeout = search_timeout_s
        self._max_retries = max(0, max_retries)
        self._backoff_max = backoff_max_s
        self._transport = transport
        self._sleep = sleep
        self._rand = rand
        self.waits: list[float] = []   # backoff waits taken (diagnostics/tests)

    def _post(self, path: str, payload: dict, timeout: float) -> tuple[dict, int]:
        last: LLMError | None = None
        retry_after: float | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                wait = backoff_s(attempt, cap=self._backoff_max, retry_after=retry_after, rand=self._rand)
                self.waits.append(round(wait, 3))
                self._sleep(wait)
            retry_after = None
            t0 = time.monotonic()
            try:
                with httpx.Client(transport=self._transport, timeout=timeout) as client:
                    resp = client.post(self._base + path, headers=self._headers, json=payload)
            except httpx.TimeoutException as e:
                last = LLMError(f"timeout after {timeout:.0f}s: {e}", retryable=True)
                continue
            except httpx.HTTPError as e:
                last = LLMError(f"transport error: {type(e).__name__}: {e}", retryable=True)
                continue
            ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                retry_after = retry_after_s(resp.headers.get("retry-after"))
                last = LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}", retryable=True,
                                status=resp.status_code)
                continue
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}", status=resp.status_code)
            try:
                return resp.json(), ms
            except json.JSONDecodeError as e:
                raise LLMError(f"non-JSON response: {e}") from e
        raise last or LLMError("request failed")

    def chat(self, *, system: str, user: str, model: str, max_tokens: int = 8000) -> LLMResult:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        data, ms = self._post("/chat/completions", payload, self._chat_timeout)
        return parse_chat_response(data, ms)

    def search(self, *, system: str, user: str, model: str, max_tool_calls: int,
               max_output_tokens: int = 6000) -> LLMResult:
        payload: dict[str, Any] = {
            "model": model,
            "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": [{"type": "web_search"}],
            "max_tool_calls": max_tool_calls,
            "max_output_tokens": max_output_tokens,
            "temperature": 0.1,
        }
        try:
            data, ms = self._post("/responses", payload, self._search_timeout)
        except LLMError as e:
            # Defensive: if this API version rejects max_tool_calls, retry once without it
            # (cost is still bounded by the per-run budget check).
            if e.status == 400 and "max_tool_calls" in str(e):
                payload.pop("max_tool_calls")
                data, ms = self._post("/responses", payload, self._search_timeout)
            else:
                raise
        return parse_responses_response(data, ms)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Parse a JSON object from model output (tolerates code fences / leading prose)."""
    s = _FENCE.sub("", (text or "").strip()).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in model output") from None
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e.msg} at char {e.pos}") from None
