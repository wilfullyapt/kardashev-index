"""Judgment pipeline: LLM call, strict validation, and score aggregation.

Everything in this module is free of database sessions so the blocking LLM call can
run in a worker thread (see ``run_in_threadpool`` in ``app.main``). Persistence lives
in ``app.main.apply_judgment``.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from collections.abc import Iterable
from typing import Any

from .models import CATEGORIES

# ----- configuration (env-overridable; defaults preserve previous production behavior) -----
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4")
XAI_TIMEOUT_S = float(os.getenv("XAI_TIMEOUT_S", "120"))      # per HTTP request
XAI_MAX_RETRIES = int(os.getenv("XAI_MAX_RETRIES", "1"))      # SDK transport retries (429/5xx/timeouts)
XAI_MAX_TOKENS = int(os.getenv("XAI_MAX_TOKENS", "2000"))
JUDGE_MAX_ATTEMPTS = max(1, int(os.getenv("JUDGE_MAX_ATTEMPTS", "2")))  # attempts on invalid output
PROMPT_VERSION = "judge-prompt-v1"

# The LLM's own "overall" read is stored and displayed, but never averaged into Overall.
JUDGE_SYNTHESIS_CATEGORY = "overall"
DIMENSIONS = [c for c in CATEGORIES if c != JUDGE_SYNTHESIS_CATEGORY]
PLACEHOLDER_MODEL = "stub"


class JudgmentValidationError(ValueError):
    """The model returned output that does not satisfy the judgment contract."""


class JudgmentError(RuntimeError):
    """A judgment could not be produced. ``meta`` carries timing/usage for logging."""

    def __init__(self, message: str, meta: dict[str, Any]):
        super().__init__(message)
        self.meta = meta


@dataclass
class JudgmentResult:
    items: list[dict[str, Any]]
    meta: dict[str, Any] = field(default_factory=dict)


# ----- prompt -----

def build_prompt(canonical_name: str, industry: str | None) -> str:
    # Unchanged from the original single-call prompt (category definitions are a later step).
    return f"""You are an e/acc alignment judge for the Kardashev Index.
Rate the entity "{canonical_name}" (industry: {industry or "unknown"}) on these categories (0.0-10.0 scale):
{", ".join(CATEGORIES)}

For each category give:
- score (float 0-10)
- short justification (1-2 sentences, reference specific actions, products, or statements)
- 0-3 high-quality evidence links (official announcements, earnings calls, credible reporting — prefer primary sources)

Be specific and evidence-based. Return ONLY valid JSON (no extra text):
{{
  "scores": [
    {{"category": "ai_tech_acceleration", "score": 8.5, "justification": "...", "evidence_links": []}},
    ...
  ]
}}
"""


def repair_message(error: str) -> str:
    return (
        f"Your previous response was rejected: {error}\n"
        f"Return ONLY a JSON object of the form {{\"scores\": [...]}} with exactly one entry for each of "
        f"these categories, spelled exactly: {', '.join(CATEGORIES)}. Each entry needs a numeric \"score\" "
        f"between 0 and 10, a non-empty \"justification\" string, and an \"evidence_links\" array of URLs."
    )


# ----- validation -----

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)


def _load_json(content: str) -> Any:
    text = (content or "").strip()
    if not text:
        raise JudgmentValidationError("empty response")
    if text.startswith("```"):
        text = _FENCE.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError as e:
                raise JudgmentValidationError(f"invalid JSON: {e.msg}") from e
        raise JudgmentValidationError("invalid or truncated JSON") from None


def parse_judgment(content: str) -> list[dict[str, Any]]:
    """Parse and strictly validate model output. Returns one clean item per category,
    in CATEGORIES order, or raises JudgmentValidationError."""
    data = _load_json(content)
    if not isinstance(data, dict) or not isinstance(data.get("scores"), list):
        raise JudgmentValidationError('expected an object with a "scores" array')

    seen: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(data["scores"]):
        if not isinstance(item, dict):
            raise JudgmentValidationError(f"scores[{i}] is not an object")
        cat = item.get("category")
        if cat not in CATEGORIES:
            raise JudgmentValidationError(f"unknown category {cat!r}")
        if cat in seen:
            raise JudgmentValidationError(f"duplicate category {cat!r}")
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise JudgmentValidationError(f"{cat}: score must be a number, got {score!r}")
        if not 0.0 <= float(score) <= 10.0:
            raise JudgmentValidationError(f"{cat}: score {score} outside 0-10")
        justification = item.get("justification")
        if not isinstance(justification, str) or not justification.strip():
            raise JudgmentValidationError(f"{cat}: justification missing")
        links = item.get("evidence_links") or []
        if not isinstance(links, list):
            raise JudgmentValidationError(f"{cat}: evidence_links must be an array")
        clean_links = [u.strip() for u in links if isinstance(u, str) and u.strip().startswith(("http://", "https://"))]
        seen[cat] = {
            "category": cat,
            "score": round(float(score), 2),
            "justification": justification.strip(),
            "evidence_links": clean_links[:5],
        }

    missing = [c for c in CATEGORIES if c not in seen]
    if missing:
        raise JudgmentValidationError(f"missing categories: {', '.join(missing)}")
    return [seen[c] for c in CATEGORIES]


# ----- LLM call (blocking; run it in a worker thread) -----

def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    out: dict[str, Any] = {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = getattr(usage, k, None)
        if isinstance(v, int):
            out[k] = v
    details = getattr(usage, "completion_tokens_details", None)
    rt = getattr(details, "reasoning_tokens", None) if details is not None else None
    if isinstance(rt, int):
        out["reasoning_tokens"] = rt
    ticks = getattr(usage, "cost_in_usd_ticks", None)
    if ticks is None and getattr(usage, "model_extra", None):
        ticks = usage.model_extra.get("cost_in_usd_ticks")
    if isinstance(ticks, (int, float)):
        out["cost_usd"] = round(ticks / 1e10, 6)
    return out


def judge_company(client: Any, canonical_name: str, industry: str | None,
                  model: str = None, max_attempts: int = None) -> JudgmentResult:
    """Ask the LLM to judge one entity. Retries invalid output up to ``max_attempts`` times.
    Never touches the database. Raises JudgmentError (with timing/usage meta) on failure."""
    model = model or XAI_MODEL
    max_attempts = max_attempts or JUDGE_MAX_ATTEMPTS
    meta: dict[str, Any] = {
        "model_requested": model,
        "prompt_version": PROMPT_VERSION,
        "attempts": [],
        "started_at": datetime.now(UTC).isoformat(),
    }
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    t0 = time.perf_counter()

    def finish() -> None:
        meta["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        meta["finished_at"] = datetime.now(UTC).isoformat()
        meta.update({k: v for k, v in totals.items()})
        costs = [a["cost_usd"] for a in meta["attempts"] if "cost_usd" in a]
        if costs:
            meta["cost_usd"] = round(sum(costs), 6)

    if client is None:
        meta["attempts"].append({"attempt": 1, "ok": False, "error": "XAI_API_KEY not configured"})
        finish()
        raise JudgmentError("XAI_API_KEY not configured", meta)

    messages: list[dict[str, str]] = [{"role": "user", "content": build_prompt(canonical_name, industry)}]
    last_error = "no attempts made"
    for attempt in range(1, max_attempts + 1):
        a0 = time.perf_counter()
        record: dict[str, Any] = {"attempt": attempt}
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
                max_tokens=XAI_MAX_TOKENS,
            )
        except Exception as e:  # API/transport error after SDK retries: stop, don't burn more calls
            record.update(ok=False, error=f"{type(e).__name__}: {e}"[:500],
                          duration_ms=int((time.perf_counter() - a0) * 1000))
            meta["attempts"].append(record)
            meta["error_type"] = "api_error"
            finish()
            raise JudgmentError(record["error"], meta) from e

        usage = _usage_dict(getattr(resp, "usage", None))
        for k in totals:
            totals[k] += usage.get(k, 0)
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        content = (getattr(getattr(choice, "message", None), "content", None) or "") if choice else ""
        record.update(usage)
        record["model"] = getattr(resp, "model", None) or model
        record["finish_reason"] = getattr(choice, "finish_reason", None) if choice else None
        record["duration_ms"] = int((time.perf_counter() - a0) * 1000)
        meta["model"] = record["model"]
        try:
            items = parse_judgment(content)
        except JudgmentValidationError as e:
            last_error = str(e)
            record.update(ok=False, error=last_error)
            meta["attempts"].append(record)
            messages = messages[:1] + [
                {"role": "assistant", "content": content[:4000]},
                {"role": "user", "content": repair_message(last_error)},
            ]
            continue
        record["ok"] = True
        meta["attempts"].append(record)
        finish()
        return JudgmentResult(items=items, meta=meta)

    meta["error_type"] = "validation_error"
    finish()
    raise JudgmentError(f"invalid model output after {max_attempts} attempt(s): {last_error}", meta)


# ----- aggregation (read side) -----

def is_placeholder(score: Any) -> bool:
    return (getattr(score, "model", None) == PLACEHOLDER_MODEL
            or (getattr(score, "justification", None) or "").startswith("Placeholder"))


def _sort_key(score: Any):
    judged = getattr(score, "judged_at", None)
    ts = judged.timestamp() if judged else 0.0
    return (ts, getattr(score, "id", 0) or 0)


def latest_by_category(scores: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for s in sorted(scores, key=_sort_key):
        out[s.category] = s  # later judged_at / higher id wins
    return out


def summarize_scores(scores: Iterable[Any]) -> dict[str, Any]:
    """Collapse raw Score rows into what the site shows.

    - placeholder rows are ignored for ranking;
    - duplicates collapse to the latest row per category;
    - Overall = mean of the five real dimensions only (the judge's own "overall" is excluded);
    - an entity is ranked only when all five dimensions have real scores.
    """
    scores = list(scores)
    real = latest_by_category(s for s in scores if not is_placeholder(s))
    if all(d in real for d in DIMENSIONS):
        overall = round(sum(float(real[d].score) for d in DIMENSIONS) / len(DIMENSIONS), 1)
        return {"status": "ranked", "scores": real, "overall": overall}
    shown = real or latest_by_category(scores)
    return {"status": "awaiting", "scores": shown, "overall": None}
