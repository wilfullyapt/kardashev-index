"""Runtime settings for the v2 pipeline (all optional env vars with safe defaults)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=lambda: os.getenv("XAI_MODEL", "grok-4.3"))
    xai_base_url: str = field(default_factory=lambda: os.getenv("XAI_BASE_URL", "https://api.x.ai/v1"))
    chat_timeout_s: float = field(default_factory=lambda: _f("XAI_TIMEOUT_S", 120))
    search_timeout_s: float = field(default_factory=lambda: _f("XAI_SEARCH_TIMEOUT_S", 240))
    max_retries: int = field(default_factory=lambda: _i("XAI_MAX_RETRIES", 1))
    # Hard per-run spend cap, checked against the API's own cost_in_usd_ticks before every call.
    max_cost_usd: float = field(default_factory=lambda: _f("JUDGE_MAX_COST_USD", 0.40))
    resolve_max_tool_calls: int = field(default_factory=lambda: _i("JUDGE_RESOLVE_MAX_SEARCHES", 3))
    research_max_tool_calls: int = field(default_factory=lambda: _i("JUDGE_RESEARCH_MAX_SEARCHES", 8))
    max_sources: int = field(default_factory=lambda: _i("JUDGE_MAX_SOURCES", 12))
    fetch_timeout_s: float = field(default_factory=lambda: _f("FETCH_TIMEOUT_S", 20))
    fetch_max_bytes: int = field(default_factory=lambda: _i("FETCH_MAX_BYTES", 15_000_000))
    snapshot_max_chars: int = field(default_factory=lambda: _i("SNAPSHOT_MAX_CHARS", 150_000))
    extract_max_chars: int = field(default_factory=lambda: _i("EXTRACT_MAX_CHARS", 64_000))
    per_source_chars: int = field(default_factory=lambda: _i("EXTRACT_PER_SOURCE_CHARS", 20_000))
    sec_user_agent: str | None = field(default_factory=lambda: os.getenv("SEC_EDGAR_USER_AGENT") or None)
    rank_min_coverage: float = field(default_factory=lambda: _f("RANK_MIN_COVERAGE", 0.60))
    rank_min_measured: float = field(default_factory=lambda: _f("RANK_MIN_MEASURED", 0.30))
    llm_max_attempts: int = field(default_factory=lambda: _i("JUDGE_MAX_ATTEMPTS", 2))


def settings() -> Settings:
    return Settings()


def code_version() -> str | None:
    """Deployed git commit. Render sets RENDER_GIT_COMMIT; GIT_COMMIT works elsewhere."""
    for name in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        val = (os.getenv(name) or "").strip()
        if val:
            return val[:40]
    return None
