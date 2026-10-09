"""Executes one judgment run end to end, recording every stage. Pure orchestration: the LLM,
HTTP fetcher and clock are injected so tests can run the whole chain with fakes.

Reliability (pipeline-v2.2):
- Checkpoints: every completed stage stores what later stages need (entity, candidate sources,
  judged categories; sources/evidence are already rows), committed with the stage. A retried run
  resumes from the first incomplete stage instead of starting over (and never re-buys searches).
- Errors are classified transient (429/5xx/timeouts, invalid model output, interruptions, DB
  connection errors) or permanent (configuration, 4xx). Transient failures are re-queued
  automatically at increasing delays (RUN_RETRY_DELAYS_MIN) up to RUN_MAX_ATTEMPTS.
- Graceful degradation: when the per-run budget cap is reached, gathering stops and the run is
  computed/judged/aggregated with what it has; an invalid judge reply never discards measured
  work; on the final attempt a failing extract/judge degrades instead of failing. Degraded stages
  are recorded on the run and surfaced to admins."""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from .. import alerts, publication
from ..models import (CategoryScore, Company, Evidence, IngestLog, JudgmentRun, JudgmentStage, Metric,
                      Source)
from . import evidence as ev
from . import methodology as meth
from . import prompts as P
from . import semantics as sem
from .aggregate import aggregate
from .config import Settings, WorkerSettings, code_version, worker_settings
from .edgar import FACTS_URL, EdgarClient, EdgarError, IdentityCheck, check_identity, financials
from .fetch import ARCHIVE_STATUSES, Fetcher, SourceChecker, wayback_fetch
from .llm import LLM, LLMError, LLMResult, extract_json
from .measures import METRIC_LABELS, Figure, canonical_unit, fmt_num, measure_all, to_si

_ENERGY_MAX = 3e12 * 3.15576e7
PLAUSIBLE_MAX_SI = {  # beyond these a figure is almost certainly a unit/scope error
    "energy_consumption": _ENERGY_MAX, "electricity_consumption": _ENERGY_MAX, "energy_supplied": _ENERGY_MAX,
    "energy_generated": _ENERGY_MAX, "energy_sold": _ENERGY_MAX, "energy_storage_deployed": _ENERGY_MAX,
    "datacenter_capacity_operating": 5e10, "datacenter_capacity_planned": 2e11, "capex": 1e12, "revenue": 3e12,
}
PLAUSIBLE_MIN_SI = {"capex": 1e5, "revenue": 1e5}   # "(11,339)" read as dollars = a missed "(in millions)"
CURRENCY = ("capex", "revenue")
_ACCOUNTING = ("model", "input_tokens", "output_tokens", "reasoning_tokens", "tool_calls", "cost_usd")
STAGES = ("resolve", "research", "edgar", "fetch", "extract", "compute", "judge", "aggregate")
ALWAYS_RERUN = ("compute", "aggregate")   # deterministic and cheap: recomputed on every resume


class RunFailed(Exception):
    def __init__(self, error_type: str, message: str, *, transient: bool = False):
        super().__init__(message)
        self.error_type = error_type
        self.transient = transient


class BudgetReached(Exception):
    """The per-run cap would be exceeded by the next call: degrade instead of failing."""


class Paused(Exception):
    """The worker is shutting down; the run is re-queued and resumes from its checkpoint."""


@dataclass
class Deps:
    llm: LLM | None
    fetcher: Fetcher
    settings: Settings
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    probe_token: Callable[[], str] | None = None
    retry: WorkerSettings | None = None
    should_stop: Callable[[], bool] | None = None


def _utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def norm_url(url: str) -> str:
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", p.query, ""))


def host_matches(url: str, domain: str | None) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("sec.gov"):
        return True
    return bool(domain) and (host == domain or host.endswith("." + domain))


class Stage:
    def __init__(self, ctx: RunContext, row: JudgmentStage):
        self.ctx, self.row = ctx, row
        self.detail: dict = {}
        self.cp: dict = {}               # data checkpointed when the stage completes
        self.final_status: str | None = None

    def skip(self, reason: str):
        self.final_status = "skipped"
        self.detail["skipped"] = reason

    def soft_fail(self, error: str):
        self.final_status = "failed"
        self.row.error = error[:2000]

    def degrade(self, reason: str):
        self.final_status = "degraded"
        self.detail["degraded"] = reason
        self.ctx.degraded.append(f"{self.row.stage}: {reason}"[:300])

    def charge(self, res: LLMResult):
        r = self.row
        r.model = res.model or r.model
        r.input_tokens = (r.input_tokens or 0) + res.input_tokens
        r.output_tokens = (r.output_tokens or 0) + res.output_tokens
        r.reasoning_tokens = (r.reasoning_tokens or 0) + res.reasoning_tokens
        r.tool_calls = (r.tool_calls or 0) + res.tool_calls
        r.cost_usd = round((r.cost_usd or 0) + res.cost_usd, 6)
        self.ctx.charge(res)


class RunContext:
    def __init__(self, db: Session, run: JudgmentRun, company: Company, deps: Deps):
        self.db, self.run, self.company, self.deps = db, run, company, deps
        self.s = deps.settings
        self.seq = 0
        self.entity: dict = {}
        self.candidates: list[dict] = []
        self.texts: dict[int, ev.SourceText] = {}    # source_id -> fetched text (+ matching index)
        self.sources: dict[int, Source] = {}
        self.figures: list[Figure] = []
        self.claims: list[Evidence] = []
        self.measured = []
        self.headline: dict | None = None
        self.judged: dict[str, dict] = {}
        self.synthesis: str | None = None
        self.flags: list[str] = []
        self.cp: dict = dict(run.checkpoint or {})
        self.degraded: list[str] = list(self.cp.get("degraded") or [])
        self.final_attempt = True
        self.year = deps.now().year

    # ----- checkpoints -----
    def done(self, name: str) -> bool:
        return name in (self.cp.get("done") or [])

    def _save(self, name: str, data: dict):
        cp = {**self.cp, **data}
        done = list(cp.get("done") or [])
        if name not in done and name not in ALWAYS_RERUN:
            done.append(name)
        cp.update(done=done, degraded=list(self.degraded), flags=list(self.flags))
        self.cp = cp
        self.run.checkpoint = dict(cp)    # new object so the JSON column is marked dirty

    # ----- accounting -----
    def charge(self, res: LLMResult):
        r = self.run
        r.input_tokens = (r.input_tokens or 0) + res.input_tokens
        r.output_tokens = (r.output_tokens or 0) + res.output_tokens
        r.reasoning_tokens = (r.reasoning_tokens or 0) + res.reasoning_tokens
        r.tool_calls = (r.tool_calls or 0) + res.tool_calls
        r.cost_usd = round((r.cost_usd or 0) + res.cost_usd, 6)
        if res.model:
            r.model_returned = res.model

    def check_budget(self, reserve: float = 0.0):
        spent = self.run.cost_usd or 0
        if spent + reserve >= self.s.max_cost_usd:
            raise BudgetReached(f"spent ${spent:.4f} of the ${self.s.max_cost_usd:.2f} per-run cap "
                                f"(JUDGE_MAX_COST_USD){f'; ${reserve:.2f} kept for judging' if reserve else ''}")

    # ----- stage recording -----
    @contextmanager
    def stage(self, name: str):
        self.seq += 1
        now = self.deps.now()
        row = JudgmentStage(run_id=self.run.id, seq=self.seq, stage=name, attempt=self.run.attempt or 1,
                            status="running", started_at=now)
        self.db.add(row)
        self.run.current_stage = name
        self.run.heartbeat_at = now
        self.db.commit()
        st = Stage(self, row)
        t0 = time.monotonic()
        try:
            yield st
        except Exception as e:
            # Discard this stage's partial artifacts, but keep the accounting (money was spent).
            keep_row = {a: getattr(row, a) for a in _ACCOUNTING}
            keep_run = {a: getattr(self.run, a) for a in _ACCOUNTING + ("model_returned",) if a != "model"}
            self.db.rollback()
            for a, v in keep_row.items():
                setattr(row, a, v)
            for a, v in keep_run.items():
                setattr(self.run, a, v)
            row.status = "failed"
            row.error = f"{type(e).__name__}: {e}"[:2000]
            raise
        else:
            row.status = st.final_status or "succeeded"
            self._save(name, st.cp)
        finally:
            row.finished_at = self.deps.now()
            row.duration_ms = int((time.monotonic() - t0) * 1000)
            row.detail = st.detail or None
            self.db.commit()

    # ----- LLM helper: strict validation + bounded repair -----
    def call_json(self, st: Stage, *, kind: str, system: str, user: str, validate,
                  max_tool_calls: int = 0, max_tokens: int = 8000, reserve: float = 0.0):
        llm = self.deps.llm
        if llm is None:
            raise RunFailed("config_error", "XAI_API_KEY not configured")
        attempts = max(1, self.s.llm_max_attempts)
        last_err = None
        prev: LLMResult | None = None
        first: LLMResult | None = None
        for i in range(attempts):
            self.check_budget(reserve)
            try:
                if i == 0 and kind == "search":
                    res = llm.search(system=system, user=user, model=self.s.model, max_tool_calls=max_tool_calls,
                                     max_output_tokens=max_tokens)
                elif i == 0:
                    res = llm.chat(system=system, user=user, model=self.s.model, max_tokens=max_tokens)
                elif kind == "chat" and prev is not None and prev.finish_reason in ("length", "incomplete"):
                    # truncated, not wrong: same request with more room
                    res = llm.chat(system=system, user=user, model=self.s.model, max_tokens=min(max_tokens * 2, 32000))
                else:  # repairs never re-run web search
                    repair_user = (f"{user}\n\nYour previous reply:\n{((prev.text if prev else '') or '')[:8000]}\n\n"
                                   + P.REPAIR.format(error=last_err))
                    res = llm.chat(system=system, user=repair_user, model=self.s.model, max_tokens=max_tokens)
            except LLMError as e:
                raise RunFailed("api_error", f"{st.row.stage}: {e}", transient=e.retryable) from e
            st.charge(res)
            first = first or res
            st.detail.setdefault("attempts", []).append({
                "model": res.model, "duration_ms": res.duration_ms, "input_tokens": res.input_tokens,
                "output_tokens": res.output_tokens, "reasoning_tokens": res.reasoning_tokens,
                "tool_calls": res.tool_calls, "cost_usd": round(res.cost_usd, 6),
                "cost_estimated": res.cost_estimated, "finish_reason": res.finish_reason})
            try:
                return validate(extract_json(res.text)), first
            except (ValueError, P.StageOutputError) as e:
                last_err = str(e)[:300]
                prev = res
                st.detail["attempts"][-1]["error"] = last_err
        raise RunFailed("validation_error", f"{st.row.stage}: invalid model output after {attempts} attempts: {last_err}",
                        transient=True)


# ================= stages =================

IDENTITY_FIELDS = ("official_name", "domain", "ticker", "exchange", "is_public", "sec_filer", "cik", "industry",
                   "hq", "description", "confidence", "identity_check")


def _identity_fields(entity: dict) -> dict:
    return {k: entity.get(k) for k in IDENTITY_FIELDS if entity.get(k) is not None}


def _edgar_client(ctx: RunContext) -> EdgarClient | None:
    return EdgarClient(ctx.deps.fetcher, ctx.s.sec_user_agent) if ctx.s.sec_user_agent else None


def _apply_identity_check(ctx: RunContext, entity: dict, *, pinned: bool = False) -> IdentityCheck:
    """Cross-check ticker / public status against SEC's company tickers list (when the SEC user
    agent is set) and internal consistency; fixes contradictions in ``entity`` (not when pinned)."""
    try:
        chk = check_identity(_edgar_client(ctx), entity)
    except EdgarError as e:
        chk = IdentityCheck("unchecked", note=f"SEC registry unavailable: {e}"[:300])
    consistent = True
    if pinned:
        if chk.status in ("confirmed", "name_match") and not entity.get("cik"):
            entity["cik"] = chk.cik
    elif chk.status == "confirmed":
        entity.update(cik=chk.cik, ticker=chk.ticker, is_public=True, sec_filer=True)
    elif chk.status == "name_match":
        entity.update(cik=chk.cik, is_public=True, sec_filer=True)
        entity["ticker"] = entity.get("ticker") or chk.ticker
    elif chk.status == "mismatch":
        consistent = False
        ctx.flags.append(f"identity: {chk.note}")
        entity.update(ticker=None, cik=None, sec_filer=False)
    elif chk.status == "not_listed" and entity.get("ticker"):
        consistent = False
        ctx.flags.append(f"identity: {chk.note}")
        entity.update(ticker=None, exchange=None, cik=None, is_public=False, sec_filer=False)
    else:  # unchecked: internal consistency only
        if entity.get("ticker") and entity.get("is_public") is False:
            consistent = False
            chk.note = "ticker given but marked private (contradictory; not cross-checked: no SEC user agent)"
            ctx.flags.append(f"identity: {chk.note}")
    entity["identity_check"] = {"status": chk.status, "note": chk.note, "registrant": chk.title,
                                "consistent": consistent}
    return chk


def stage_resolve(ctx: RunContext):
    with ctx.stage("resolve") as st:
        c = ctx.company
        ident = c.identity if isinstance(c.identity, dict) and c.identity.get("official_name") else None
        entity, mode = None, "search"
        if ident and c.identity_status == "pinned":
            entity, mode = dict(ident), "pinned"
        elif ident and c.identity_status == "auto" and c.identity_resolved_at and \
                _utc(c.identity_resolved_at) > ctx.deps.now() - timedelta(days=ctx.s.identity_ttl_days):
            entity, mode = dict(ident), "reused"
        if entity is None:
            name = c.official_name or c.canonical_name.replace("-", " ")
            try:
                entity, _ = ctx.call_json(st, kind="search", system=P.RESOLVE_SYSTEM,
                                          user=P.resolve_user(name, c.domain), validate=P.validate_resolve,
                                          max_tool_calls=ctx.s.resolve_max_tool_calls,
                                          max_tokens=ctx.s.resolve_max_tokens, reserve=ctx.s.judge_reserve_usd)
            except BudgetReached as e:
                raise RunFailed("budget_exceeded", f"resolve: {e}") from e
            if not entity.get("domain") and c.domain:
                entity["domain"] = c.domain
        before = (entity.get("identity_check") or {}).get("status")
        chk = _apply_identity_check(ctx, entity, pinned=mode == "pinned")
        st.detail.update({"identity": mode, "entity": entity})
        ctx.entity = entity
        consistent = entity["identity_check"]["consistent"]
        conf = entity.get("confidence") or 0
        confident = consistent and conf >= ctx.s.identity_min_confidence and \
            (not entity.get("ambiguity") or conf >= 0.9)
        if mode == "search" and confident:
            c.identity, c.identity_status, c.identity_resolved_at = _identity_fields(entity), "auto", ctx.deps.now()
            st.detail["identity_saved"] = True
        elif mode == "reused" and chk.status != before and consistent:
            c.identity = _identity_fields(entity)    # e.g. first SEC cross-check after the UA was set
            st.detail["identity_updated"] = True
        elif mode == "search":
            st.detail["identity_saved"] = False
            st.detail["identity_not_saved_because"] = ("inconsistent" if not consistent else
                                                       f"confidence {conf:.2f}" + (" / ambiguous" if entity.get("ambiguity") else ""))
        st.cp["entity"] = entity


def stage_research(ctx: RunContext):
    with ctx.stage("research") as st:
        try:
            found, res = ctx.call_json(st, kind="search", system=P.RESEARCH_SYSTEM, user=P.research_user(ctx.entity),
                                       validate=P.validate_research, max_tool_calls=ctx.s.research_max_tool_calls,
                                       max_tokens=ctx.s.research_max_tokens, reserve=ctx.s.judge_reserve_usd)
        except BudgetReached as e:
            st.degrade(f"budget cap reached; no research ({e})")
            found, res = [], None
        seen: set[str] = set()
        cands: list[dict] = []
        for s in found:
            key = norm_url(s["url"])
            if key not in seen:
                seen.add(key)
                cands.append({**s, "origin": "research"})
        for url in (res.citations if res else []):
            key = norm_url(url)
            if key not in seen and url.startswith(("http://", "https://")):
                seen.add(key)
                cands.append({"url": url, "title": None, "covers": [], "why": "cited during search",
                              "origin": "citation"})
        ctx.candidates = cands[: ctx.s.max_sources]
        st.detail.update({"proposed": len(found), "citations": len(res.citations) if res else 0,
                          "kept": len(ctx.candidates)})
        st.cp["candidates"] = ctx.candidates


def stage_edgar(ctx: RunContext):
    with ctx.stage("edgar") as st:
        e = ctx.entity
        if not ctx.s.sec_user_agent:
            st.skip("SEC_EDGAR_USER_AGENT not set")
            return
        chk = e.get("identity_check") or {}
        cik = e.get("cik")
        if not cik:
            st.skip(f"no SEC registrant for this entity ({chk.get('status', 'unchecked')}"
                    f"{': ' + chk['note'] if chk.get('note') else ''})")
            return
        client = _edgar_client(ctx)
        try:
            facts = client.company_facts(cik)
        except EdgarError as err:
            st.soft_fail(str(err))
            return
        title = chk.get("registrant") or facts.get("entityName") or e.get("official_name")
        fin = financials(facts)
        src = Source(run_id=ctx.run.id, url=FACTS_URL.format(cik=cik), final_url=FACTS_URL.format(cik=cik),
                     origin="edgar", http_status=200, content_type="application/json",
                     title=f"SEC XBRL company facts — {title}", fetched_at=ctx.deps.now(), status="ok",
                     is_primary=True)
        ctx.db.add(src)
        ctx.db.flush()
        n = 0
        for metric, series in fin.items():
            if metric not in CURRENCY:
                continue
            for v in series:
                year = v.fiscal_year_end[:4]
                quote = f"{v.concept} = {v.value:,.0f} USD for fiscal year ended {v.fiscal_year_end} ({v.form}, accession {v.accn})"
                row = Evidence(run_id=ctx.run.id, source_id=src.id, category="growth", kind="filing",
                               claim=f"{METRIC_LABELS[metric]} FY{year}", quote=quote, quote_verified=True,
                               metric_key=metric, value=v.value, unit="USD", period=f"FY{year}",
                               scope="company-wide (SEC filing)", context=v.filing_url(cik))
                ctx.db.add(row)
                ctx.db.flush()
                ctx.figures.append(Figure(row.id, src.id, metric, v.value, "USD", f"FY{year}", True,
                                          v.filing_url(cik), quote, origin="edgar"))
                n += 1
        st.detail.update({"cik": cik, "registrant": title, "figures": n,
                          "concepts": {m: (s[0].concept if s else None) for m, s in fin.items()}})


def stage_fetch(ctx: RunContext):
    with ctx.stage("fetch") as st:
        if not ctx.candidates:
            st.skip("no candidate sources")
            return
        checker = SourceChecker(ctx.deps.fetcher, **({"probe_token": ctx.deps.probe_token}
                                                     if ctx.deps.probe_token else {}))
        wayback = ctx.s.wayback_enabled

        def work(c):
            res = ctx.deps.fetcher.get(c["url"])
            a = checker.assess(res)
            if a.status != "ok" and wayback and res.status in ARCHIVE_STATUSES:
                arch = wayback_fetch(ctx.deps.fetcher, c["url"])
                if arch is not None:
                    aa = checker.assess(arch)
                    if aa.status == "ok":
                        return c, arch, aa, res
            return c, res, a, None

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(work, ctx.candidates))
        counts: dict[str, int] = {}
        domain = ctx.entity.get("domain")
        seen_sha: dict[str, int] = {}
        archived = []
        for c, res, a, original in results:
            snap = a.snapshot
            if a.status == "ok" and snap and snap.sha256 in seen_sha:
                a = type(a)("duplicate", f"same content as source #{seen_sha[snap.sha256]}", snap)
            counts[a.status] = counts.get(a.status, 0) + 1
            src = Source(run_id=ctx.run.id, url=c["url"], final_url=res.final_url, origin=c["origin"],
                         http_status=res.status, content_type=res.content_type or None,
                         title=(snap.title if snap and snap.title else c.get("title")),
                         fetched_at=ctx.deps.now(), sha256=snap.sha256 if snap else None,
                         byte_size=snap.byte_size if snap else (len(res.body) if res.body else None),
                         text=snap.text[: ctx.s.snapshot_max_chars] if snap and a.status == "ok" else None,
                         status=a.status, reject_reason=a.reason,
                         is_primary=host_matches(c["url"] if res.archived_from else (res.final_url or c["url"]), domain))
            if res.archived_from:
                src.archive_url = res.final_url
                src.archive_timestamp = res.archive_timestamp
                src.reject_reason = f"original answered HTTP {original.status}; using the Wayback Machine copy"
                archived.append(c["url"])
            ctx.db.add(src)
            ctx.db.flush()
            if a.status == "ok":
                seen_sha[snap.sha256] = src.id
                ctx.sources[src.id] = src
                ctx.texts[src.id] = ev.SourceText(snap.text)
        st.detail["status_counts"] = counts
        if archived:
            st.detail["archived"] = archived


def _locate_any(ctx: RunContext, quote: str, sid: int) -> tuple[int, ev.Match | None, bool]:
    """Find the quote in the source the model cited, else in any other fetched source (models
    sometimes cite the wrong S-id). Verification always uses the full fetched text."""
    m = ev.locate(quote, ctx.texts[sid])
    if m is not None:
        return sid, m, False
    for other, text in ctx.texts.items():
        if other != sid:
            m = ev.locate(quote, text)
            if m is not None:
                return other, m, True
    return sid, None, False


def _scaled_number(value: float, span: str, factor: float) -> float | None:
    for n in ev.numbers_in(span):
        if n and abs(value - n * factor) <= 0.005 * abs(n * factor):
            return n
    return None


def _extract_call(ctx: RunContext, st: Stage, avoid: dict | None = None):
    plans = {sid: ev.plan(t.text) for sid, t in ctx.texts.items()}
    budgets = ev.allocate(plans, {sid: len(t.text) for sid, t in ctx.texts.items()},
                          ctx.s.extract_max_chars, ctx.s.per_source_chars)
    blocks, sent, chosen = [], {}, {}
    for sid, t in ctx.texts.items():
        w, chosen[sid] = ev.select(t.text, budgets[sid], plans[sid], (avoid or {}).get(sid))
        src = ctx.sources[sid]
        blocks.append((f"S{sid}", src.url if src.archive_url else (src.final_url or src.url), w))
        sent[f"S{sid}"] = {"chars": len(t.text), "sent": len(w), "relevance": round(plans[sid].relevance, 1)}
    st.detail["input" if avoid is None else "input_retry"] = sent
    sid_map = {f"S{sid}": sid for sid in ctx.texts}
    (figures, claims, rejected), _ = ctx.call_json(
        st, kind="chat", system=P.EXTRACT_SYSTEM, user=P.extract_user(ctx.entity.get("official_name", ""), blocks),
        validate=lambda d: P.validate_extract(d, set(sid_map)), max_tokens=ctx.s.extract_max_tokens,
        reserve=ctx.s.judge_reserve_usd)
    return figures, claims, rejected, chosen, sid_map


def stage_extract(ctx: RunContext):
    with ctx.stage("extract") as st:
        if not ctx.texts:
            st.skip("no verified sources to read")
            return
        figures, claims, rejected, sid_map = [], [], [], {f"S{sid}": sid for sid in ctx.texts}
        try:
            figures, claims, rejected, chosen, sid_map = _extract_call(ctx, st)
            if not figures and not claims:
                # Empty extraction is never silent: keep diagnostics, retry once on other excerpts.
                st.detail["empty_first_try"] = dict(st.detail["attempts"][-1])
                figures, claims, rejected, _, sid_map = _extract_call(ctx, st, avoid=chosen)
                st.detail["retried_with_other_excerpts"] = True
                if not figures and not claims:
                    last = st.detail["attempts"][-1]
                    aborted = (last.get("output_tokens") or 0) < 40 or \
                        last.get("finish_reason") not in (None, "stop", "completed", "end_turn")
                    if aborted and not ctx.final_attempt:
                        raise RunFailed("empty_extraction",
                                        f"extract: model returned no figures or claims twice (output_tokens="
                                        f"{last.get('output_tokens')}, finish_reason={last.get('finish_reason')})",
                                        transient=True)
                    st.degrade("model returned no figures or claims (twice, on different excerpts)")
        except BudgetReached as e:
            st.degrade(f"budget cap reached before extraction ({e})")
        except RunFailed as e:
            if ctx.final_attempt and e.error_type in ("api_error", "validation_error", "empty_extraction"):
                st.degrade(f"extraction unavailable on the final attempt: {e}"[:300])
            else:
                raise
        kept_f = kept_c = 0
        reasons: dict[str, int] = {}
        methods: dict[str, int] = {}
        reclassified: dict[str, int] = {}

        def reject(row: Evidence, why: str):
            row.quote_verified = False
            row.rejected_reason = why
            reasons[why] = reasons.get(why, 0) + 1

        for f in figures:
            key, value, raw_unit = f["metric_key"], f["value"], f["unit"]
            cited = sid_map[f["source_id"]]
            sid, m, moved = _locate_any(ctx, f["quote"], cited)
            unit = canonical_unit(key, raw_unit)
            row = Evidence(run_id=ctx.run.id, source_id=sid, category=_cat_for_metric(key),
                           kind="figure", claim=f"{METRIC_LABELS[key]}: {fmt_num(value)} {raw_unit}",
                           quote=f["quote"][:2000], metric_key=key, value=value,
                           unit=unit or raw_unit, period=f["period"], scope=f["scope"])
            notes = [f"quote found in S{sid} (model cited S{cited})"] if moved else []
            ctx.db.add(row)
            if m is None:
                reject(row, "quote not found in fetched source")
            else:
                text = ctx.texts[sid]
                scale = sem.table_scale(text.text, m.start) if key in CURRENCY else None
                value_ok = ev.number_matches(value, m.text) and ev.number_matches(value, f["quote"])
                if key in CURRENCY and scale:
                    if value_ok and unit == "USD" and not sem.has_inline_scale(m.text):
                        unit = scale[0]
                        notes.append(f"scale from the table header ({scale[0].split('_')[1]})")
                    elif not value_ok:
                        n = _scaled_number(value, m.text, scale[1])
                        if n is not None:
                            value, unit, value_ok = n, scale[0], True
                            notes.append(f"value as printed with the table's '{scale[0].split('_')[1]}' scale")
                if unit is None:
                    reject(row, f"unsupported unit {raw_unit!r} for {key}")
                elif not value_ok:
                    reject(row, "value does not appear in the quote")
                elif not (scale or ev.unit_present(unit, text.context(m, 300), raw_unit)):
                    reject(row, "unit not found near the quote")
                else:
                    si = to_si(key, value, unit) or 0
                    src = ctx.sources[sid]
                    verdict = sem.check_figure(key, quote=m.text, period=f["period"], is_primary=bool(src.is_primary),
                                               current_year=ctx.year, row_context=text.context(m, 120))
                    if si > PLAUSIBLE_MAX_SI[key]:
                        reject(row, "implausible magnitude (likely unit or scope error)")
                        ctx.flags.append(f"implausible {key}: {value} {unit}")
                    elif key in PLAUSIBLE_MIN_SI and si < PLAUSIBLE_MIN_SI[key]:
                        reject(row, "implausibly small amount (missing an 'in millions' table scale?)")
                    elif not verdict.ok:
                        reject(row, verdict.reason)
                    else:
                        if verdict.metric_key != key:
                            reclassified[f"{key}->{verdict.metric_key}"] = reclassified.get(
                                f"{key}->{verdict.metric_key}", 0) + 1
                            notes.append(f"reclassified from {key}: {verdict.note}")
                            key = verdict.metric_key
                            row.metric_key, row.category = key, _cat_for_metric(key)
                            row.claim = f"{METRIC_LABELS[key]}: {fmt_num(value)} {raw_unit}"
                        row.quote_verified = True
                        row.quote = m.text           # store exactly what the source says
                        row.context = text.context(m)
                        row.value, row.unit = value, unit
                        methods[m.method] = methods.get(m.method, 0) + 1
                        kept_f += 1
            row.note = "; ".join(notes) or None
            ctx.db.flush()
            if row.quote_verified:
                src = ctx.sources[sid]
                ctx.figures.append(Figure(row.id, sid, key, value, unit, f["period"],
                                          bool(src.is_primary), src.final_url or src.url, row.quote))
        for c in claims:
            cited = sid_map[c["source_id"]]
            sid, m, moved = _locate_any(ctx, c["quote"], cited)
            row = Evidence(run_id=ctx.run.id, source_id=sid, category=c["category"], kind="claim",
                           claim=c["claim"], quote=c["quote"][:2000],
                           note=f"quote found in S{sid} (model cited S{cited})" if moved else None)
            ctx.db.add(row)
            if m is None:
                reject(row, "quote not found in fetched source")
            else:
                row.quote_verified = True
                row.quote = m.text
                row.context = ctx.texts[sid].context(m)
                methods[m.method] = methods.get(m.method, 0) + 1
                kept_c += 1
                ctx.claims.append(row)
            ctx.db.flush()
        st.detail.update({"figures_proposed": len(figures), "figures_verified": kept_f,
                          "claims_proposed": len(claims), "claims_verified": kept_c,
                          "malformed_dropped": len(rejected), "rejections": reasons, "match_methods": methods})
        if reclassified:
            st.detail["reclassified"] = reclassified
        if rejected:
            st.detail["malformed"] = rejected[:20]


def _cat_for_metric(key: str) -> str:
    if key.startswith(("energy", "electricity")):
        return "energy_throughput"
    if key.startswith("datacenter"):
        return "compute_capacity"
    return "growth"


def stage_compute(ctx: RunContext):
    with ctx.stage("compute") as st:
        ctx.measured, ctx.headline = measure_all(ctx.figures, ctx.deps.now().year, ctx.s.count_generation)
        for f in ctx.figures:
            ctx.db.add(Metric(run_id=ctx.run.id, metric_key=f.metric_key, value=f.value, unit=f.unit,
                              value_si=f.si, unit_si={"capex": "USD", "revenue": "USD"}.get(f.metric_key) or
                              ("W" if f.metric_key.startswith("datacenter") else "J/yr"),
                              period=f.period, method=f"reported ({f.origin})", source_id=f.source_id,
                              evidence_ids=[f.evidence_id] if f.evidence_id else []))
        if ctx.headline:
            h = ctx.headline
            for key, val, unit in (("avg_power", h["avg_power_w"], "W"), ("k_equivalent", h["k_equivalent"], "K")):
                ctx.db.add(Metric(run_id=ctx.run.id, metric_key=key, value=val, unit=unit, value_si=val,
                                  unit_si=unit, period=h["period"], method="derived in code from reported energy",
                                  evidence_ids=[h["evidence_id"]] if h.get("evidence_id") else []))
        st.detail.update({m.key: m.score for m in ctx.measured})
        if ctx.flags:
            st.detail["flags"] = ctx.flags


def _insufficient(rationale: str) -> dict:
    return {k: {"score": None, "insufficient": True, "confidence": 0.0, "evidence_ids": [], "rationale": rationale}
            for k in meth.JUDGED_KEYS}


def stage_judge(ctx: RunContext):
    """Opinion categories are judged whenever there is any verified evidence — qualitative quotes
    and, as supporting context, verified figures — even if the measured data is too thin to rank."""
    with ctx.stage("judge") as st:
        claims = [c for c in ctx.claims if c.quote_verified][:30]
        fig_ids = [f.evidence_id for f in ctx.figures if f.evidence_id][:15]
        figs = ctx.db.query(Evidence).filter(Evidence.id.in_(fig_ids)).all() if fig_ids else []
        if not claims and not figs:
            st.skip("no verified evidence")
            ctx.judged = _insufficient("No verified evidence was available, so this opinion category is unscored.")
            st.cp.update(judged=ctx.judged, synthesis=None)
            return
        domains: dict[int, str | None] = {}

        def domain(source_id):
            if source_id not in domains:
                src = ctx.sources.get(source_id) or ctx.db.get(Source, source_id)
                domains[source_id] = urlsplit(src.final_url or src.url).hostname if src else None
            return domains[source_id]

        items = [{"id": c.id, "category": c.category, "claim": c.claim, "quote": c.quote,
                  "domain": domain(c.source_id)} for c in claims]
        items += [{"id": e.id, "category": "measured figure", "claim": e.claim, "quote": e.quote,
                   "domain": domain(e.source_id)} for e in figs]
        ids = {i["id"] for i in items}
        st.detail["evidence_items"] = {"claims": len(claims), "figures": len(figs)}
        try:
            (judged, synthesis), _ = ctx.call_json(
                st, kind="chat", system=P.JUDGE_SYSTEM, user=P.judge_user(ctx.entity.get("official_name", ""), items),
                validate=lambda d: P.validate_judge(d, ids), max_tokens=ctx.s.judge_max_tokens)
            notes = judged.pop("_notes", None)
            if notes:
                st.detail["validation_notes"] = notes[:20]
        except BudgetReached as e:
            st.degrade(f"budget cap reached; opinion categories not judged ({e})")
            judged, synthesis = _insufficient("Not judged: the run reached its budget cap."), None
        except RunFailed as e:
            if e.error_type == "validation_error" or (e.error_type == "api_error" and ctx.final_attempt):
                st.degrade(f"judge unavailable ({e.error_type}); measured scores kept"[:300])
                judged, synthesis = _insufficient("Not judged: the model's reply could not be used."), None
            else:
                raise
        for v in judged.values():
            if v["score"] is not None:
                support = min(1.0, len(set(v["evidence_ids"])) / 3)
                v["confidence"] = round(min(v["confidence"], 0.85) * (0.6 + 0.4 * support), 3)
        ctx.judged, ctx.synthesis = judged, synthesis
        st.detail.update({k: v["score"] for k, v in judged.items()})
        st.cp.update(judged=judged, synthesis=synthesis)


def stage_aggregate(ctx: RunContext):
    with ctx.stage("aggregate") as st:
        run, company = ctx.run, ctx.company
        scores: dict[str, tuple[float | None, float]] = {}
        for m in ctx.measured:
            scores[m.key] = (m.score, m.confidence)
            ctx.db.add(CategoryScore(run_id=run.id, category=m.key, family=meth.MEASURED, score=m.score,
                                     confidence=m.confidence, weight=meth.WEIGHTS[m.key], rationale=m.rationale,
                                     evidence_ids=m.evidence_ids, inputs=m.inputs, insufficient=m.score is None,
                                     rubric_version=meth.WEIGHTS_VERSION))
        for key in meth.JUDGED_KEYS:
            j = ctx.judged.get(key) or {"score": None, "confidence": 0.0, "evidence_ids": [], "insufficient": True,
                                        "rationale": "Not judged."}
            scores[key] = (j["score"], j["confidence"])
            ctx.db.add(CategoryScore(run_id=run.id, category=key, family=meth.JUDGED, score=j["score"],
                                     confidence=j["confidence"], weight=meth.WEIGHTS[key], rationale=j["rationale"],
                                     evidence_ids=j["evidence_ids"], insufficient=j["insufficient"],
                                     rubric_version=meth.RUBRIC_VERSION))
        ok_sources = ctx.db.query(Source).filter(Source.run_id == run.id, Source.status == "ok",
                                                 Source.origin != "edgar").count()
        energy_missing = scores.get("energy_throughput", (None, 0))[0] is None
        undisclosed = (ctx.s.energy_undisclosed_rule and energy_missing
                       and ok_sources >= ctx.s.energy_undisclosed_min_sources)
        agg = aggregate(scores, min_coverage=ctx.s.rank_min_coverage, min_measured=ctx.s.rank_min_measured,
                        energy_undisclosed=undisclosed, min_measured_share=ctx.s.rank_min_measured_share,
                        min_confidence=ctx.s.rank_min_confidence)
        run.index_score, run.measured_score, run.judged_score = agg.index_score, agg.measured_score, agg.judged_score
        run.coverage, run.confidence, run.ranked = agg.coverage, agg.confidence, agg.ranked
        if ctx.headline:
            run.avg_power_w, run.k_equivalent = ctx.headline["avg_power_w"], ctx.headline["k_equivalent"]
        prev = ctx.db.get(JudgmentRun, company.current_run_id) if company.current_run_id else None
        if prev is not None and prev.id == run.id:
            prev = None
        publish, note = publication.decide(agg.ranked, agg.coverage, prev,
                                           publication.has_legacy_scores(ctx.db, company.id))
        run.published = publish
        run.degraded = list(ctx.degraded) or None
        if agg.basis == "energy_undisclosed" and agg.ranked:
            ctx.flags.append("energy undisclosed: ranked on the remaining 70% of the weight, confidence ×0.8")
        e = ctx.entity
        run.summary = {"entity": e, "synthesis": ctx.synthesis, "headline": ctx.headline,
                       "not_ranked_reason": agg.reason, "measured_coverage": agg.measured_coverage,
                       "flags": ctx.flags, "previous_run_id": prev.id if prev else None,
                       "previous_index": prev.index_score if prev else None,
                       "publish_note": note, "rank_basis": agg.basis, "adjusted_coverage": agg.adjusted_coverage,
                       "energy_undisclosed": agg.basis == "energy_undisclosed", "degraded": run.degraded,
                       "identity_check": e.get("identity_check"), "ok_sources": ok_sources}
        # Identity facts from resolution (never scores) are applied to the company.
        for attr, val in (("official_name", e.get("official_name")), ("ticker", e.get("ticker")),
                          ("exchange", e.get("exchange")), ("cik", e.get("cik")), ("is_public", e.get("is_public"))):
            if val is not None:
                setattr(company, attr, val)
        if (e.get("identity_check") or {}).get("status") in ("mismatch", "not_listed"):
            company.ticker, company.cik = e.get("ticker"), e.get("cik")
        if not company.domain and e.get("domain"):
            company.domain = e["domain"]
        if (not company.industry or company.industry == "Unknown") and e.get("industry"):
            company.industry = e["industry"]
        if not company.hq and e.get("hq"):
            company.hq = e["hq"]
        if publish:
            company.current_run_id = run.id
            company.last_ingested_at = ctx.deps.now()
        st.detail.update({"index": agg.index_score, "coverage": agg.coverage, "confidence": agg.confidence,
                          "ranked": agg.ranked, "published": publish, "reason": agg.reason, "basis": agg.basis})


PIPELINE = (("resolve", stage_resolve), ("research", stage_research), ("edgar", stage_edgar),
            ("fetch", stage_fetch), ("extract", stage_extract), ("compute", stage_compute),
            ("judge", stage_judge), ("aggregate", stage_aggregate))


def reset_artifacts(db: Session, run_id: int):
    """A run starting over begins clean (stage rows are kept: they carry the attempt number)."""
    for model in (CategoryScore, Metric, Evidence, Source):
        db.query(model).filter(model.run_id == run_id).delete(synchronize_session=False)
    db.commit()


def _restore(ctx: RunContext):
    """Rebuild in-memory state for completed stages from the checkpoint and the run's rows."""
    db, run_id, cp = ctx.db, ctx.run.id, ctx.cp
    if ctx.done("resolve"):
        ctx.entity = dict(cp.get("entity") or {})
    if ctx.done("research"):
        ctx.candidates = list(cp.get("candidates") or [])
    sources = {s.id: s for s in db.query(Source).filter(Source.run_id == run_id)}
    if ctx.done("edgar"):
        for e in db.query(Evidence).filter(Evidence.run_id == run_id, Evidence.kind == "filing",
                                           Evidence.quote_verified.is_(True)).order_by(Evidence.id):
            ctx.figures.append(Figure(e.id, e.source_id, e.metric_key, e.value, e.unit, e.period, True,
                                      e.context, e.quote, origin="edgar"))
    if ctx.done("fetch"):
        for s in sources.values():
            if s.status == "ok" and s.origin != "edgar":
                ctx.sources[s.id] = s
                ctx.texts[s.id] = ev.SourceText(s.text or "")
    if ctx.done("extract"):
        for e in db.query(Evidence).filter(Evidence.run_id == run_id, Evidence.kind.in_(("figure", "claim")),
                                           Evidence.quote_verified.is_(True)).order_by(Evidence.id):
            if e.kind == "claim":
                ctx.claims.append(e)
                continue
            src = sources.get(e.source_id)
            ctx.figures.append(Figure(e.id, e.source_id, e.metric_key, e.value, e.unit, e.period,
                                      bool(src and src.is_primary), (src.final_url or src.url) if src else None,
                                      e.quote))
    ctx.flags = list(cp.get("flags") or [])
    if ctx.done("judge"):
        ctx.judged = dict(cp.get("judged") or {})
        ctx.synthesis = cp.get("synthesis")


def _classify(e: Exception) -> tuple[str, bool, str]:
    """(error_type, transient, message)."""
    if isinstance(e, RunFailed):
        return e.error_type, e.transient, str(e)
    if isinstance(e, OperationalError) or (isinstance(e, DBAPIError) and e.connection_invalidated):
        return "db_error", True, f"{type(e).__name__}: {e}"
    return "internal_error", False, f"{type(e).__name__}: {e}"


def first_pending_stage(checkpoint: dict | None) -> str | None:
    done = set((checkpoint or {}).get("done") or [])
    return next((n for n, _ in PIPELINE if n not in done and n not in ALWAYS_RERUN), "compute")


def execute_run(db: Session, run_id: int, deps: Deps) -> JudgmentRun:
    run = db.get(JudgmentRun, run_id)
    company = db.get(Company, run.company_id)
    policy = deps.retry or worker_settings()
    cp = run.checkpoint or {}
    resume = bool(cp.get("done")) and cp.get("v") == meth.PIPELINE_VERSION
    if resume:
        # completed stages are kept; derived rows are recomputed
        for model in (CategoryScore, Metric):
            db.query(model).filter(model.run_id == run_id).delete(synchronize_session=False)
    else:
        reset_artifacts(db, run_id)
        run.checkpoint = {"v": meth.PIPELINE_VERSION, "done": []}
        for col in ("input_tokens", "output_tokens", "reasoning_tokens", "tool_calls"):
            setattr(run, col, 0)
        run.cost_usd = 0.0
        run.degraded = None
    run.model = deps.settings.model
    run.budget_usd = deps.settings.max_cost_usd
    run.pipeline_version = meth.PIPELINE_VERSION
    run.prompt_version = P.PROMPT_VERSION
    run.rubric_version = meth.RUBRIC_VERSION
    run.weights_version = meth.WEIGHTS_VERSION
    run.prompt_hash = meth.bundle_hash(P.RESOLVE_SYSTEM, P.RESEARCH_SYSTEM, P.EXTRACT_SYSTEM, P.JUDGE_SYSTEM)
    run.code_version = code_version()
    run.started_at = run.started_at or deps.now()
    run.next_attempt_at = None
    db.commit()
    ctx = RunContext(db, run, company, deps)
    attempt = run.attempt or 1
    ctx.final_attempt = (not policy.auto_retry) or attempt >= policy.max_attempts
    t0 = time.monotonic()
    try:
        if deps.llm is None:
            raise RunFailed("config_error", "XAI_API_KEY not configured")
        if resume:
            _restore(ctx)
        for name, fn in PIPELINE:
            if name not in ALWAYS_RERUN and ctx.done(name):
                continue
            if deps.should_stop is not None and deps.should_stop():
                raise Paused(name)
            fn(ctx)
        run.status = "succeeded"
        run.current_stage = None
        run.error_type = run.error_message = run.error_class = None
        _finish(ctx, t0)
        db.add(IngestLog(company_id=company.id, suggestion_id=run.suggestion_id, action="judgment_run",
                         admin_id=run.triggered_by, details=run_log_details(run)))
        db.commit()
        if run.degraded or not run.published:
            alerts.notify(alerts.run_event(run, "run_degraded" if run.degraded else "run_withheld"))
    except Paused as p:
        db.rollback()
        run = db.get(JudgmentRun, run_id)
        run.status = "queued"
        run.attempt = max(0, (run.attempt or 1) - 1)     # a pause does not use up an attempt
        run.current_stage = None
        run.heartbeat_at = None
        run.error_message = f"paused for shutdown before {p}; resumes from its checkpoint"
        db.commit()
    except Exception as e:
        db.rollback()
        run = db.get(JudgmentRun, run_id)
        etype, transient, msg = _classify(e)
        run.error_type, run.error_message = etype, msg[:2000]
        run.error_class = "transient" if transient else "permanent"
        run.published = False
        run.current_stage = None
        attempt = run.attempt or 1
        if transient and policy.auto_retry and attempt < policy.max_attempts:
            delays = policy.retry_delays_min or (2.0,)
            delay = delays[min(attempt - 1, len(delays) - 1)]
            run.status = "queued"
            run.next_attempt_at = deps.now() + timedelta(minutes=delay)
            db.add(IngestLog(company_id=run.company_id, suggestion_id=run.suggestion_id, action="run_retry_scheduled",
                             admin_id=run.triggered_by,
                             details={"run_id": run.id, "attempt": attempt, "max_attempts": policy.max_attempts,
                                      "error_type": etype, "error": msg[:500],
                                      "next_attempt_at": run.next_attempt_at.isoformat(),
                                      "resumes_from": first_pending_stage(run.checkpoint)}))
            db.commit()
        else:
            run.status = "failed"
            ctx.run = run
            _finish(ctx, t0)
            db.add(IngestLog(company_id=run.company_id, suggestion_id=run.suggestion_id, action="judge_error",
                             admin_id=run.triggered_by,
                             details={**run_log_details(run), "existing_scores_untouched": True}))
            db.commit()
            alerts.notify(alerts.run_event(run, "run_failed"))
    return run


def _finish(ctx: RunContext, t0: float):
    ctx.run.finished_at = ctx.deps.now()
    started = _utc(ctx.run.started_at)
    ctx.run.duration_ms = int((ctx.run.finished_at - started).total_seconds() * 1000) if started else \
        int((time.monotonic() - t0) * 1000)


def run_log_details(run: JudgmentRun) -> dict:
    return {"run_id": run.id, "status": run.status, "model": run.model, "model_returned": run.model_returned,
            "duration_ms": run.duration_ms, "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
            "reasoning_tokens": run.reasoning_tokens, "tool_calls": run.tool_calls, "cost_usd": run.cost_usd,
            "index_score": run.index_score, "confidence": run.confidence, "coverage": run.coverage,
            "ranked": run.ranked, "published": run.published, "error_type": run.error_type,
            "error_class": run.error_class, "error": run.error_message, "degraded": run.degraded,
            "prompt_version": run.prompt_version, "attempt": run.attempt, "code_version": run.code_version}
