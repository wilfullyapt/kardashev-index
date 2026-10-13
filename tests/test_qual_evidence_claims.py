"""pipeline-v2.15: qualitative evidence budget, claims-only follow-up call, claims recorded per run,
0-claims visibility, page-scoped embedded JSON."""
import json
import re
from datetime import UTC, datetime

from bs4 import BeautifulSoup

from app import observability
from app.models import Company, JudgmentRun, JudgmentStage
from app.pipeline import evidence as ev
from app.pipeline import prompts as P
from app.pipeline.fetch import embedded_json_texts
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes

POLICY_SENTENCE = "We engage with governments on many topics."


def _exec(db, llm, **settings):
    c = Company(canonical_name="nvidia", official_name="NVIDIA", industry="Chips")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    return execute_run(db, run.id, fakes.make_deps(llm, **settings))


def _stage(db, run, name="extract"):
    return db.query(JudgmentStage).filter(JudgmentStage.run_id == run.id, JudgmentStage.stage == name).one()


def claims_reply(user):
    sid = re.search(r'<source id="(S\d+)"', user).group(1)
    return {"figures": [], "claims": [
        {"source_id": sid, "category": "frontier_acceleration", "claim": "dup", "quote": fakes.FRONTIER_QUOTE},
        {"source_id": sid, "category": "policy_stance", "claim": "Engages governments", "quote": POLICY_SENTENCE},
        {"source_id": sid, "category": "policy_stance", "claim": "made up", "quote": "We lobby for nothing at all here."},
    ]}


# ---------------------------------------------------------------- follow-up call

def test_followup_adds_claims_when_fewer_than_three(db):
    llm = fakes.world_llm(claims=[claims_reply])
    run = _exec(db, llm, claims_followup=True)
    assert "claims" in llm.stages_called()
    d = _stage(db, run).detail
    assert d["claims_followup"]["verified_before"] == 2 and d["claims_followup"]["verified"] == 1
    assert d["claims_followup"]["proposed"] == 3 and d["claims_followup"]["cost_usd"] > 0
    assert d["claims_verified"] == 3 and run.summary["claims_verified"] == 3
    sent = next(c["user"] for c in llm.calls if c["stage"] == "claims")
    assert "[stage:claims]" not in sent and "<source id=" in sent


def test_followup_disabled_by_default_in_tests_and_by_env(db):
    llm = fakes.world_llm()
    run = _exec(db, llm, claims_followup=False)
    assert "claims" not in llm.stages_called()
    assert _stage(db, run).detail["claims_followup"]["skipped"].startswith("disabled")
    assert run.summary["claims_verified"] == 2


def test_followup_skipped_when_budget_would_break_the_cap(db):
    llm = fakes.world_llm(claims=[claims_reply])
    run = _exec(db, llm, claims_followup=True, max_cost_usd=0.12)
    assert "claims" not in llm.stages_called()
    assert "budget" in _stage(db, run).detail["claims_followup"]["skipped"]


def test_followup_not_needed_when_enough_claims(db):
    llm = fakes.world_llm(claims=[claims_reply])
    run = _exec(db, llm, claims_followup=True, claims_min=2)
    assert "claims" not in llm.stages_called() and "claims_followup" not in _stage(db, run).detail


def test_followup_failure_never_fails_the_run(db):
    llm = fakes.world_llm(claims=[fakes.api_error()])
    run = _exec(db, llm, claims_followup=True)
    assert run.status == "succeeded" and "error" in _stage(db, run).detail["claims_followup"]
    assert run.summary["claims_verified"] == 2


def test_prompt_bundle_includes_the_claims_prompt(db):
    assert P.PROMPT_VERSION == "prompts-v2.6" and "[stage:claims]" in P.CLAIMS_SYSTEM
    assert "figures" in P.CLAIMS_SYSTEM and "ONLY claims" in P.CLAIMS_SYSTEM


# ---------------------------------------------------------------- budget allocation

def _plan(n_quant, n_qual):
    return ev.Plan(spans=[(i * 1000, (i + 1) * 1000) for i in range(n_quant + n_qual)],
                   quant=[50.0] * n_quant + [0.0] * n_qual, qual=[0.0] * n_quant + [5.0] * n_qual)


def test_allocate_caps_filings_and_floors_qualitative_sources():
    plans = {"filing": _plan(40, 0), "esg": _plan(30, 0), "news": _plan(0, 6)}
    lengths = {"filing": 40000, "esg": 30000, "news": 6000}
    old = ev.allocate(plans, lengths, 12000, 20000)
    new = ev.allocate(plans, lengths, 12000, 20000, caps={"filing": 8000}, qual_keys=["news"], qual_floor=6000)
    assert old["news"] < 4000 and new["news"] == 6000
    assert new["filing"] <= 8000
    big_old = ev.allocate(plans, lengths, 64000, 20000)
    big_new = ev.allocate(plans, lengths, 64000, 20000, caps={"filing": 8000})
    assert big_old["filing"] == 20000 and big_new["filing"] == 8000
    assert sum(new.values()) <= 12000


def test_qualitative_excerpts_pick_qual_chunks_and_skip_excluded():
    texts = {"a": "x" * 3000, "b": "y" * 3000}
    plans = {"a": ev.Plan([(0, 1000), (1000, 2000), (2000, 3000)], [0, 0, 0], [1.0, 0, 3.0]),
             "b": ev.Plan([(0, 1000), (1000, 2000), (2000, 3000)], [0, 0, 0], [9.0, 9.0, 9.0])}
    out = ev.qualitative_excerpts(texts, plans, 2500, exclude={"b"})
    assert set(out) == {"a"} and out["a"] == "x" * 1000 + ev.GAP + "x" * 1000
    out = ev.qualitative_excerpts(texts, plans, 2000)
    assert set(out) == {"b"} and len(out["b"]) == 2000      # adjacent chunks joined without a gap


def test_extract_stage_records_the_budget_plan(db):
    run = _exec(db, fakes.world_llm())
    plan = _stage(db, run).detail["budget_plan"]
    assert plan["filings"] == [] and isinstance(plan["qualitative"], list)


# ---------------------------------------------------------------- visibility

def _run_with_claims(db, n, name="acme"):
    c = Company(canonical_name=name, official_name=name.title(), industry="x")
    db.add(c)
    db.commit()
    r = JudgmentRun(company_id=c.id, status="succeeded", published=True, ranked=False, index_score=3.0, coverage=0.5,
                    confidence=0.4, trigger="t", triggered_by="t", attempt=1, queued_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC), pipeline_version="pipeline-v2.15",
                    summary={"claims_verified": n, "not_ranked_reason": "insufficient data", "measured_coverage": 0.5})
    db.add(r)
    db.commit()
    c.current_run_id = r.id
    db.commit()
    return c, r


def test_health_counts_zero_claim_runs(db):
    _c, zero = _run_with_claims(db, 0)
    _run_with_claims(db, 4, "beta")
    obs = observability.collect(db)
    assert obs["runs_7d"]["zero_claims"] == 1 and obs["zero_claims_runs"][0]["run_id"] == zero.id


def test_board_card_warns_on_zero_claims_only(admin_client, db):
    observability.reset_cache()
    zc, _ = _run_with_claims(db, 0)
    ok, _ = _run_with_claims(db, 5, "beta")
    old, r = _run_with_claims(db, 0, "gamma")
    r.summary = {"not_ranked_reason": "insufficient data"}      # before pipeline-v2.15: unknown, no chip
    db.commit()
    html = admin_client.get("/admin").text

    def card(c):
        part = html[html.index(f'id="company-{c.id}"'):]
        return part[:part.index("</article>")]
    assert "0 claims" in card(zc) and "0 claims" not in card(ok) and "0 claims" not in card(old)
    assert "1 with 0 claims" in admin_client.get("/admin/board/top").text
    observability.reset_cache()


# ---------------------------------------------------------------- embedded JSON scope

def _soup(*scripts):
    return BeautifulSoup("<html><body>" + "".join(scripts) + "</body></html>", "html.parser")


def _script(id_, data, kind="application/json"):
    return f'<script type="{kind}" id="{id_}">{json.dumps(data)}</script>'


def test_embedded_json_skips_site_wide_islands():
    page = {"body": "Menace-X demonstrates an AI-enabled rapid targeting solution at a Marine exercise."}
    site = {"cookie": "We use optional analytics cookies to understand how you use our site so we can improve it."}
    out = embedded_json_texts(_soup(_script("ssgContent", page), _script("ssgSiteSettings", site),
                                    _script("navMenu", site), _script("x-consent", site)))
    assert out == [page["body"]]


def test_next_data_uses_page_props_only():
    nd = {"props": {"pageProps": {"article": "Our new 1 GW campus came online in March, two months ahead of plan."}},
          "runtimeConfig": {"banner": "Sign up for our newsletter to get the latest updates every week."}}
    out = embedded_json_texts(_soup(f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'))
    assert out == [nd["props"]["pageProps"]["article"]]


def test_each_island_is_capped():
    from app.pipeline import fetch
    big = {"items": [f"Paragraph number {i} describes another milestone reached by the team." for i in range(2000)]}
    out = embedded_json_texts(_soup(_script("pageData", big)))
    assert sum(len(t) for t in out) <= fetch.EMBEDDED_PER_SCRIPT_MAX + 200
