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
    # Per-call retries for transient xAI errors (429/5xx/timeouts), exponential backoff with jitter
    # that honours Retry-After, capped at XAI_BACKOFF_MAX_S per wait.
    max_retries: int = field(default_factory=lambda: _i("XAI_MAX_RETRIES", 3))
    backoff_max_s: float = field(default_factory=lambda: _f("XAI_BACKOFF_MAX_S", 60))
    # Hard per-run spend cap, checked against the API's own cost_in_usd_ticks before every call.
    max_cost_usd: float = field(default_factory=lambda: _f("JUDGE_MAX_COST_USD", 0.40))
    resolve_max_tool_calls: int = field(default_factory=lambda: _i("JUDGE_RESOLVE_MAX_SEARCHES", 3))
    research_max_tool_calls: int = field(default_factory=lambda: _i("JUDGE_RESEARCH_MAX_SEARCHES", 8))
    max_sources: int = field(default_factory=lambda: _i("JUDGE_MAX_SOURCES", 12))
    fetch_timeout_s: float = field(default_factory=lambda: _f("FETCH_TIMEOUT_S", 20))
    fetch_max_bytes: int = field(default_factory=lambda: _i("FETCH_MAX_BYTES", 30_000_000))
    fetch_retries: int = field(default_factory=lambda: _i("FETCH_RETRIES", 2))
    # Wayback Machine fallback for sources that answer 401/403/404/410/451 (labelled "archived copy").
    wayback_enabled: bool = field(default_factory=lambda: _b("FETCH_WAYBACK", True))
    # Stored source text. Large enough that a resumed run re-verifies quotes against the full document.
    snapshot_max_chars: int = field(default_factory=lambda: _i("SNAPSHOT_MAX_CHARS", 1_000_000))
    extract_max_chars: int = field(default_factory=lambda: _i("EXTRACT_MAX_CHARS", 64_000))
    per_source_chars: int = field(default_factory=lambda: _i("EXTRACT_PER_SOURCE_CHARS", 20_000))
    sec_user_agent: str | None = field(default_factory=lambda: os.getenv("SEC_EDGAR_USER_AGENT") or None)
    # Token limits (reasoning tokens count against these on the Responses API).
    resolve_max_tokens: int = field(default_factory=lambda: _i("JUDGE_RESOLVE_MAX_TOKENS", 8000))
    research_max_tokens: int = field(default_factory=lambda: _i("JUDGE_RESEARCH_MAX_TOKENS", 16000))
    extract_max_tokens: int = field(default_factory=lambda: _i("JUDGE_EXTRACT_MAX_TOKENS", 16000))
    judge_max_tokens: int = field(default_factory=lambda: _i("JUDGE_JUDGE_MAX_TOKENS", 8000))
    # Spend kept back for the judge when gathering stages run into the per-run cap.
    judge_reserve_usd: float = field(default_factory=lambda: _f("JUDGE_RESERVE_USD", 0.04))
    # Reuse a confidently resolved identity (skips the resolve search) for this many days.
    identity_ttl_days: int = field(default_factory=lambda: _i("IDENTITY_TTL_DAYS", 90))
    identity_min_confidence: float = field(default_factory=lambda: _f("IDENTITY_MIN_CONFIDENCE", 0.75))
    # Own generation counts toward energy throughput (alongside energy consumed).
    count_generation: bool = field(default_factory=lambda: _b("ENERGY_COUNT_GENERATION", True))
    # Ranking when energy is undisclosed: coverage is measured over the remaining weight.
    energy_undisclosed_rule: bool = field(default_factory=lambda: _b("RANK_ENERGY_UNDISCLOSED", True))
    energy_undisclosed_min_sources: int = field(default_factory=lambda: _i("RANK_ENERGY_UNDISCLOSED_MIN_SOURCES", 3))
    # pipeline-v2.4: an energy source that was found but could not be read (too large, unparseable,
    # 403/429/5xx, timeout, no text layer) makes the run "energy couldn't be read": unranked, admin
    # alert, and one automatic retry ENERGY_RETRY_DELAY_HOURS later (at most ENERGY_RETRY_MAX in a row).
    energy_retry_enabled: bool = field(default_factory=lambda: _b("ENERGY_RETRY_ENABLED", True))
    energy_retry_delay_hours: float = field(default_factory=lambda: _f("ENERGY_RETRY_DELAY_HOURS", 24.0))
    energy_retry_max: int = field(default_factory=lambda: _i("ENERGY_RETRY_MAX", 1))
    rank_min_coverage: float = field(default_factory=lambda: _f("RANK_MIN_COVERAGE", 0.60))
    rank_min_measured: float = field(default_factory=lambda: _f("RANK_MIN_MEASURED", 0.30))
    # Quality gate on top of coverage (pipeline-v2.3): ranked only if measured share of the scored
    # weight > RANK_MIN_MEASURED_SHARE and confidence (0-1; shown as 0-100%) > RANK_MIN_CONFIDENCE.
    rank_min_measured_share: float = field(default_factory=lambda: _f("RANK_MIN_MEASURED_SHARE", 0.40))
    rank_min_confidence: float = field(default_factory=lambda: _f("RANK_MIN_CONFIDENCE", 0.20))
    llm_max_attempts: int = field(default_factory=lambda: _i("JUDGE_MAX_ATTEMPTS", 2))


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


def settings() -> Settings:
    return Settings()


def code_version() -> str | None:
    """Deployed git commit. Render sets RENDER_GIT_COMMIT; GIT_COMMIT works elsewhere."""
    for name in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        val = (os.getenv(name) or "").strip()
        if val:
            return val[:40]
    return None


@dataclass(frozen=True)
class WorkerSettings:
    """Background worker, automatic retries and the daily sweep (all env-configurable)."""
    # Attempts per run in total (first try + automatic retries), resuming from the last good stage.
    max_attempts: int = field(default_factory=lambda: _i("RUN_MAX_ATTEMPTS", 4))
    retry_delays_min: tuple = field(default_factory=lambda: _delays(os.getenv("RUN_RETRY_DELAYS_MIN", "2,10,60")))
    auto_retry: bool = field(default_factory=lambda: _b("RUN_AUTO_RETRY", True))
    sweep_enabled: bool = field(default_factory=lambda: _b("SWEEP_ENABLED", True))
    sweep_hour_utc: int = field(default_factory=lambda: _i("SWEEP_HOUR_UTC", 10))       # 03:00 PDT
    sweep_min_age_days: float = field(default_factory=lambda: _f("SWEEP_MIN_AGE_DAYS", 7))
    sweep_daily_cost_usd: float = field(default_factory=lambda: _f("SWEEP_DAILY_COST_USD", 2.0))
    sweep_est_run_usd: float = field(default_factory=lambda: _f("SWEEP_EST_RUN_USD", 0.35))
    drain_s: float = field(default_factory=lambda: _f("WORKER_DRAIN_S", 45))


def _delays(raw: str) -> tuple:
    out = []
    for part in (raw or "").split(","):
        try:
            out.append(max(0.0, float(part)))
        except ValueError:
            continue
    return tuple(out) or (2.0, 10.0, 60.0)


def worker_settings() -> WorkerSettings:
    return WorkerSettings()
