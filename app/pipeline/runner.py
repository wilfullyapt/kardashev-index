"""Executes one judgment run end to end, recording every stage. Pure orchestration: the LLM,
HTTP fetcher and clock are injected so tests can run the whole chain with fakes."""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from ..models import (CategoryScore, Company, Evidence, IngestLog, JudgmentRun, JudgmentStage, Metric,
                      Source)
from . import evidence as ev
from . import methodology as meth
from . import prompts as P
from .aggregate import aggregate
from .config import Settings, code_version
from .edgar import FACTS_URL, EdgarClient, EdgarError, financials
from .fetch import Fetcher, SourceChecker
from .llm import LLM, LLMError, LLMResult, extract_json
from .measures import METRIC_LABELS, Figure, canonical_unit, measure_all, to_si

PLAUSIBLE_MAX_SI = {  # beyond these a figure is almost certainly a unit/scope error
    "energy_consumption": 3e12 * 3.15576e7, "electricity_consumption": 3e12 * 3.15576e7,
    "energy_supplied": 3e12 * 3.15576e7, "datacenter_capacity_operating": 5e10,
    "datacenter_capacity_planned": 2e11, "capex": 1e12, "revenue": 3e12,
}
_ACCOUNTING = ("model", "input_tokens", "output_tokens", "reasoning_tokens", "tool_calls", "cost_usd")
STAGES = ("resolve", "research", "edgar", "fetch", "extract", "compute", "judge", "aggregate")


class RunFailed(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class Deps:
    llm: LLM | None
    fetcher: Fetcher
    settings: Settings
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    probe_token: Callable[[], str] | None = None


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
        self.final_status: str | None = None

    def skip(self, reason: str):
        self.final_status = "skipped"
        self.detail["skipped"] = reason

    def soft_fail(self, error: str):
        self.final_status = "failed"
        self.row.error = error[:2000]

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
        self.texts: dict[int, tuple[str, str]] = {}   # source_id -> (text, normalized text)
        self.sources: dict[int, Source] = {}
        self.figures: list[Figure] = []
        self.claims: list[Evidence] = []
        self.measured = []
        self.headline: dict | None = None
        self.judged: dict[str, dict] = {}
        self.synthesis: str | None = None
        self.flags: list[str] = []

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

    def check_budget(self):
        spent = self.run.cost_usd or 0
        if spent >= self.s.max_cost_usd:
            raise RunFailed("budget_exceeded",
                            f"spent ${spent:.4f} of the ${self.s.max_cost_usd:.2f} per-run cap (JUDGE_MAX_COST_USD)")

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
        finally:
            row.finished_at = self.deps.now()
            row.duration_ms = int((time.monotonic() - t0) * 1000)
            row.detail = st.detail or None
            self.db.commit()

    # ----- LLM helper: strict validation + bounded repair -----
    def call_json(self, st: Stage, *, kind: str, system: str, user: str, validate,
                  max_tool_calls: int = 0, max_tokens: int = 8000):
        llm = self.deps.llm
        if llm is None:
            raise RunFailed("config_error", "XAI_API_KEY not configured")
        attempts = max(1, self.s.llm_max_attempts)
        last_err = None
        prev_text = None
        first: LLMResult | None = None
        for i in range(attempts):
            self.check_budget()
            try:
                if i == 0 and kind == "search":
                    res = llm.search(system=system, user=user, model=self.s.model, max_tool_calls=max_tool_calls,
                                     max_output_tokens=max_tokens)
                elif i == 0:
                    res = llm.chat(system=system, user=user, model=self.s.model, max_tokens=max_tokens)
                else:  # repairs never re-run web search
                    repair_user = (f"{user}\n\nYour previous reply:\n{(prev_text or '')[:8000]}\n\n"
                                   + P.REPAIR.format(error=last_err))
                    res = llm.chat(system=system, user=repair_user, model=self.s.model, max_tokens=max_tokens)
            except LLMError as e:
                raise RunFailed("api_error", f"{st.row.stage}: {e}") from e
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
                prev_text = res.text
                st.detail["attempts"][-1]["error"] = last_err
        raise RunFailed("validation_error", f"{st.row.stage}: invalid model output after {attempts} attempts: {last_err}")


# ================= stages =================

def stage_resolve(ctx: RunContext):
    with ctx.stage("resolve") as st:
        c = ctx.company
        name = c.official_name or c.canonical_name.replace("-", " ")
        entity, _ = ctx.call_json(st, kind="search", system=P.RESOLVE_SYSTEM, user=P.resolve_user(name, c.domain),
                                  validate=P.validate_resolve, max_tool_calls=ctx.s.resolve_max_tool_calls,
                                  max_tokens=3000)
        if not entity.get("domain") and c.domain:
            entity["domain"] = c.domain
        ctx.entity = entity
        st.detail["entity"] = entity


def stage_research(ctx: RunContext):
    with ctx.stage("research") as st:
        found, res = ctx.call_json(st, kind="search", system=P.RESEARCH_SYSTEM, user=P.research_user(ctx.entity),
                                   validate=P.validate_research, max_tool_calls=ctx.s.research_max_tool_calls,
                                   max_tokens=5000)
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


def stage_edgar(ctx: RunContext):
    with ctx.stage("edgar") as st:
        e = ctx.entity
        if not ctx.s.sec_user_agent:
            st.skip("SEC_EDGAR_USER_AGENT not set")
            return
        if not e.get("ticker") or not (e.get("sec_filer") or e.get("is_public")):
            st.skip("no US-listed ticker")
            return
        client = EdgarClient(ctx.deps.fetcher, ctx.s.sec_user_agent)
        try:
            hit = client.lookup_ticker(e["ticker"])
            if not hit:
                st.skip(f"ticker {e['ticker']} not in SEC registry")
                return
            cik, title = hit
            if fuzz.token_set_ratio(title.lower(), (e.get("official_name") or "").lower()) < 70:
                st.skip(f"SEC registrant '{title}' does not match '{e.get('official_name')}'")
                return
            facts = client.company_facts(cik)
        except EdgarError as err:
            st.soft_fail(str(err))
            return
        e["cik"] = cik
        fin = financials(facts)
        src = Source(run_id=ctx.run.id, url=FACTS_URL.format(cik=cik), final_url=FACTS_URL.format(cik=cik),
                     origin="edgar", http_status=200, content_type="application/json",
                     title=f"SEC XBRL company facts — {title}", fetched_at=ctx.deps.now(), status="ok",
                     is_primary=True)
        ctx.db.add(src)
        ctx.db.flush()
        n = 0
        for metric, series in fin.items():
            if metric not in ("capex", "revenue"):
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

        def work(c):
            res = ctx.deps.fetcher.get(c["url"])
            return c, res, checker.assess(res)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(work, ctx.candidates))
        counts: dict[str, int] = {}
        domain = ctx.entity.get("domain")
        for c, res, a in results:
            counts[a.status] = counts.get(a.status, 0) + 1
            snap = a.snapshot
            src = Source(run_id=ctx.run.id, url=c["url"], final_url=res.final_url, origin=c["origin"],
                         http_status=res.status, content_type=res.content_type or None,
                         title=(snap.title if snap and snap.title else c.get("title")),
                         fetched_at=ctx.deps.now(), sha256=snap.sha256 if snap else None,
                         byte_size=snap.byte_size if snap else (len(res.body) if res.body else None),
                         text=snap.text[: ctx.s.snapshot_max_chars] if snap and a.status == "ok" else None,
                         status=a.status, reject_reason=a.reason,
                         is_primary=host_matches(res.final_url or c["url"], domain))
            ctx.db.add(src)
            ctx.db.flush()
            if a.status == "ok":
                ctx.sources[src.id] = src
                ctx.texts[src.id] = (snap.text, ev.normalize(snap.text))
        st.detail["status_counts"] = counts


def stage_extract(ctx: RunContext):
    with ctx.stage("extract") as st:
        if not ctx.texts:
            st.skip("no verified sources to read")
            return
        n = len(ctx.texts)
        per = min(ctx.s.per_source_chars, max(1500, ctx.s.extract_max_chars // n))
        blocks = [(f"S{sid}", ctx.sources[sid].final_url or ctx.sources[sid].url, ev.windows(text, per))
                  for sid, (text, _) in ctx.texts.items()]
        sid_map = {f"S{sid}": sid for sid in ctx.texts}
        (figures, claims, rejected), _ = ctx.call_json(
            st, kind="chat", system=P.EXTRACT_SYSTEM, user=P.extract_user(ctx.entity.get("official_name", ""), blocks),
            validate=lambda d: P.validate_extract(d, set(sid_map)), max_tokens=8000)
        kept_f = kept_c = 0
        reasons: dict[str, int] = {}

        def reject(row: Evidence, why: str):
            row.quote_verified = False
            row.rejected_reason = why
            reasons[why] = reasons.get(why, 0) + 1

        for f in figures:
            sid = sid_map[f["source_id"]]
            text_norm = ctx.texts[sid][1]
            unit = canonical_unit(f["metric_key"], f["unit"])
            row = Evidence(run_id=ctx.run.id, source_id=sid, category=_cat_for_metric(f["metric_key"]),
                           kind="figure", claim=f"{METRIC_LABELS[f['metric_key']]}: {f['value']:g} {f['unit']}",
                           quote=f["quote"], metric_key=f["metric_key"], value=f["value"], unit=unit or f["unit"],
                           period=f["period"], scope=f["scope"])
            ctx.db.add(row)
            idx = ev.find_quote(f["quote"], text_norm)
            if idx < 0:
                reject(row, "quote not found verbatim in fetched source")
            elif unit is None:
                reject(row, f"unsupported unit {f['unit']!r} for {f['metric_key']}")
            elif not ev.number_matches(f["value"], f["quote"]):
                reject(row, "value does not appear in the quote")
            elif not ev.unit_present(unit, ev.context_around(text_norm, idx, f["quote"], 200)):
                reject(row, "unit not found near the quote")
            elif (to_si(f["metric_key"], f["value"], unit) or 0) > PLAUSIBLE_MAX_SI[f["metric_key"]]:
                reject(row, "implausible magnitude (likely unit or scope error)")
                ctx.flags.append(f"implausible {f['metric_key']}: {f['value']} {unit}")
            else:
                row.quote_verified = True
                row.context = ev.context_around(text_norm, idx, f["quote"])
                kept_f += 1
            ctx.db.flush()
            if row.quote_verified:
                src = ctx.sources[sid]
                ctx.figures.append(Figure(row.id, sid, f["metric_key"], f["value"], unit, f["period"],
                                          bool(src.is_primary), src.final_url or src.url, f["quote"]))
        for c in claims:
            sid = sid_map[c["source_id"]]
            text_norm = ctx.texts[sid][1]
            row = Evidence(run_id=ctx.run.id, source_id=sid, category=c["category"], kind="claim",
                           claim=c["claim"], quote=c["quote"])
            ctx.db.add(row)
            idx = ev.find_quote(c["quote"], text_norm)
            if idx < 0:
                reject(row, "quote not found verbatim in fetched source")
            else:
                row.quote_verified = True
                row.context = ev.context_around(text_norm, idx, c["quote"])
                kept_c += 1
                ctx.claims.append(row)
            ctx.db.flush()
        st.detail.update({"figures_proposed": len(figures), "figures_verified": kept_f,
                          "claims_proposed": len(claims), "claims_verified": kept_c,
                          "malformed_dropped": len(rejected), "rejections": reasons})


def _cat_for_metric(key: str) -> str:
    if key.startswith(("energy", "electricity")):
        return "energy_throughput"
    if key.startswith("datacenter"):
        return "compute_capacity"
    return "growth"


def stage_compute(ctx: RunContext):
    with ctx.stage("compute") as st:
        ctx.measured, ctx.headline = measure_all(ctx.figures, ctx.deps.now().year)
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


def stage_judge(ctx: RunContext):
    with ctx.stage("judge") as st:
        claims = [c for c in ctx.claims if c.quote_verified][:30]
        if not claims:
            st.skip("no verified qualitative evidence")
            ctx.judged = {k: {"score": None, "insufficient": True, "confidence": 0.0, "evidence_ids": [],
                              "rationale": "No verified evidence was available, so this opinion category is unscored."}
                          for k in meth.JUDGED_KEYS}
            return
        items = [{"id": c.id, "category": c.category, "claim": c.claim, "quote": c.quote,
                  "domain": urlsplit(ctx.sources[c.source_id].final_url or ctx.sources[c.source_id].url).hostname}
                 for c in claims]
        ids = {c.id for c in claims}
        (judged, synthesis), _ = ctx.call_json(
            st, kind="chat", system=P.JUDGE_SYSTEM, user=P.judge_user(ctx.entity.get("official_name", ""), items),
            validate=lambda d: P.validate_judge(d, ids), max_tokens=6000)
        for v in judged.values():
            if v["score"] is not None:
                support = min(1.0, len(set(v["evidence_ids"])) / 3)
                v["confidence"] = round(min(v["confidence"], 0.85) * (0.6 + 0.4 * support), 3)
        ctx.judged, ctx.synthesis = judged, synthesis
        st.detail.update({k: v["score"] for k, v in judged.items()})


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
        agg = aggregate(scores, min_coverage=ctx.s.rank_min_coverage, min_measured=ctx.s.rank_min_measured)
        run.index_score, run.measured_score, run.judged_score = agg.index_score, agg.measured_score, agg.judged_score
        run.coverage, run.confidence, run.ranked = agg.coverage, agg.confidence, agg.ranked
        if ctx.headline:
            run.avg_power_w, run.k_equivalent = ctx.headline["avg_power_w"], ctx.headline["k_equivalent"]
        prev = ctx.db.get(JudgmentRun, company.current_run_id) if company.current_run_id else None
        publish = agg.ranked or prev is None or not prev.ranked
        run.published = publish
        e = ctx.entity
        run.summary = {"entity": e, "synthesis": ctx.synthesis, "headline": ctx.headline,
                       "not_ranked_reason": agg.reason, "measured_coverage": agg.measured_coverage,
                       "flags": ctx.flags, "previous_run_id": prev.id if prev else None,
                       "previous_index": prev.index_score if prev else None,
                       "publish_note": None if publish else
                       f"kept run {prev.id} published: it was ranked and this run was not"}
        # Identity facts from resolution (never scores) are applied to the company.
        for attr, val in (("official_name", e.get("official_name")), ("ticker", e.get("ticker")),
                          ("exchange", e.get("exchange")), ("cik", e.get("cik")), ("is_public", e.get("is_public"))):
            if val is not None:
                setattr(company, attr, val)
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
                          "ranked": agg.ranked, "published": publish, "reason": agg.reason})


PIPELINE = (stage_resolve, stage_research, stage_edgar, stage_fetch, stage_extract, stage_compute,
            stage_judge, stage_aggregate)


def reset_artifacts(db: Session, run_id: int):
    """A retried run starts clean (stage rows are kept: they carry the attempt number)."""
    for model in (CategoryScore, Metric, Evidence, Source):
        db.query(model).filter(model.run_id == run_id).delete(synchronize_session=False)
    db.commit()


def execute_run(db: Session, run_id: int, deps: Deps) -> JudgmentRun:
    run = db.get(JudgmentRun, run_id)
    company = db.get(Company, run.company_id)
    reset_artifacts(db, run_id)
    run.model = deps.settings.model
    run.budget_usd = deps.settings.max_cost_usd
    run.pipeline_version = meth.PIPELINE_VERSION
    run.prompt_version = P.PROMPT_VERSION
    run.rubric_version = meth.RUBRIC_VERSION
    run.weights_version = meth.WEIGHTS_VERSION
    run.prompt_hash = meth.bundle_hash(P.RESOLVE_SYSTEM, P.RESEARCH_SYSTEM, P.EXTRACT_SYSTEM, P.JUDGE_SYSTEM)
    run.code_version = code_version()
    for col in ("input_tokens", "output_tokens", "reasoning_tokens", "tool_calls"):
        setattr(run, col, 0)
    run.cost_usd = 0.0
    run.started_at = run.started_at or deps.now()
    db.commit()
    ctx = RunContext(db, run, company, deps)
    t0 = time.monotonic()
    try:
        if deps.llm is None:
            raise RunFailed("config_error", "XAI_API_KEY not configured")
        for fn in PIPELINE:
            fn(ctx)
        run.status = "succeeded"
        run.current_stage = None
        _finish(ctx, t0)
        db.add(IngestLog(company_id=company.id, suggestion_id=run.suggestion_id, action="judgment_run",
                         admin_id=run.triggered_by, details=run_log_details(run)))
        db.commit()
    except Exception as e:
        db.rollback()
        run = db.get(JudgmentRun, run_id)
        run.status = "failed"
        run.published = False
        run.error_type = e.error_type if isinstance(e, RunFailed) else "internal_error"
        run.error_message = f"{e}" if isinstance(e, RunFailed) else f"{type(e).__name__}: {e}"
        run.error_message = run.error_message[:2000]
        ctx.run = run
        _finish(ctx, t0)
        db.add(IngestLog(company_id=run.company_id, suggestion_id=run.suggestion_id, action="judge_error",
                         admin_id=run.triggered_by,
                         details={**run_log_details(run), "existing_scores_untouched": True}))
        db.commit()
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
            "error": run.error_message, "prompt_version": run.prompt_version, "attempt": run.attempt,
            "code_version": run.code_version}
