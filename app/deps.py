"""Production dependencies for a judgment run. Shared by the web process (in-process worker mode) and
the per-run child process (``python -m app.runproc``), which must not import the FastAPI app."""
from __future__ import annotations

import os

from .pipeline.config import settings as pipeline_settings
from .pipeline.fetch import HttpFetcher
from .pipeline.llm import XAIClient
from .pipeline.runner import Deps


def xai_api_key() -> str | None:
    return os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")


def build_deps() -> Deps:
    """Fresh settings each run, so env changes apply."""
    cfg = pipeline_settings()
    key = xai_api_key()
    llm = XAIClient(key, base_url=cfg.xai_base_url, chat_timeout_s=cfg.chat_timeout_s,
                    search_timeout_s=cfg.search_timeout_s, max_retries=cfg.max_retries,
                    backoff_max_s=cfg.backoff_max_s) if key else None
    return Deps(llm=llm, fetcher=HttpFetcher(timeout_s=cfg.fetch_timeout_s, max_bytes=cfg.fetch_max_bytes,
                                             retries=cfg.fetch_retries, sec_user_agent=cfg.sec_user_agent,
                                             ua_strategy=cfg.fetch_ua_strategy,
                                             bot_user_agent=cfg.fetch_bot_user_agent,
                                             html_max_bytes=cfg.fetch_html_max_bytes,
                                             pdf_max_bytes=cfg.fetch_pdf_inprocess_max_bytes),
                settings=cfg)
