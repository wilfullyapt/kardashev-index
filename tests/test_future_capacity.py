"""pipeline-v2.4 / prompts-v2.3: future capacity is never scored or described as operating.

Fixture: the public Crusoe 2025 Impact Report "Highlights from 2025" page, as our PDF extractor
flattens it. The 3 GW "total capacity*" carries the footnote "* Under development as of March 2026",
which the extractor places *before* the figure.
"""
from pathlib import Path

import pytest

from app.pipeline import prompts as P
from app.pipeline import semantics as sem
from app.pipeline.measures import Figure, measure_compute
from app.pipeline.runner import capacity_cross_check

CRUSOE = (Path(__file__).parent / "fixtures" / "crusoe_2025_impact_p8.txt").read_text()
QUOTE = "total capacity* 3 GW"


def _check(quote, text=None, period="2025"):
    text = text if text is not None else quote
    pos = text.find(quote)
    return sem.check_figure("datacenter_capacity_operating", quote=quote, period=period, is_primary=True,
                            current_year=2026, row_context=text[max(0, pos - 120): pos + len(quote) + 120],
                            footnotes=sem.footnote_texts(text, pos, quote))


def test_crusoe_footnote_is_found_before_the_figure():
    fns = sem.footnote_texts(CRUSOE, CRUSOE.find(QUOTE), QUOTE)
    assert any(f.startswith("Under development as of March 2026") for f in fns)


def test_crusoe_3gw_is_planned_not_operating():
    v = _check(QUOTE, CRUSOE)
    assert v.ok and v.metric_key == "datacenter_capacity_planned"
    assert "footnote" in v.note and "Under development" in v.note


def test_v22_bug_reproduction_without_footnotes_still_caught_by_unresolved_marker():
    # old call (quote only, no footnotes): footnoted figure with no operating wording -> planned
    v = sem.check_figure("datacenter_capacity_operating", quote=QUOTE, period="2025", is_primary=True,
                         current_year=2026)
    assert v.metric_key == "datacenter_capacity_planned"


@pytest.mark.parametrize("quote,text,expect", [
    ("1.2 GW operational", "Our Abilene campus has 1.2 GW operational today.", "datacenter_capacity_operating"),
    ("operates data centers with a combined capacity of 48 MW",
     ("NVIDIA operates data centers with a combined capacity of 48 MW for research. "
      "An additional 120 MW of capacity is planned for fiscal year 2026."), "datacenter_capacity_operating"),
    ("combined power capacity of approximately 1.0 gigawatt",
     "Our clusters have a combined power capacity of approximately 1.0 gigawatt", "datacenter_capacity_operating"),
    ("900 MW", "Abilene 900 MW: under construction", "datacenter_capacity_planned"),
    ("capacity† 2 GW", "† in development, expected 2027. Sites capacity† 2 GW", "datacenter_capacity_planned"),
    ("1 GW of capacity under construction", None, "datacenter_capacity_planned"),
])
def test_capacity_semantics(quote, text, expect):
    assert _check(quote, text).metric_key == expect


def _fig(key, value, unit, period="2025"):
    return Figure(1, 1, key, value, unit, period, True)


def test_cross_check_flags_crusoe_3gw_against_18_9_mw_energy():
    cap = _fig("datacenter_capacity_operating", 3, "GW")
    energy = _fig("energy_consumption", 596000, "GJ")
    bad = capacity_cross_check([cap, energy])
    assert id(cap) in bad and "inconsistent with reported energy" in bad[id(cap)]
    assert measure_compute([f for f in [cap, energy] if id(f) not in bad], 2026).score is None


@pytest.mark.parametrize("cap,energy,period_e,flag", [
    ((500, "MW"), (2, "TWh"), "2025", False),     # 500 MW vs 228 MW average: plausible
    ((3, "GW"), (596000, "GJ"), "2021", False),   # no energy figure within ±1 year: not judged
    ((3, "GW"), (596000, "GJ"), "2024", True),
    ((48, "MW"), (612000, "MWh"), "2025", False),
])
def test_cross_check_cases(cap, energy, period_e, flag):
    c = _fig("datacenter_capacity_operating", *cap)
    e = _fig("energy_consumption", *energy, period=period_e)
    assert (id(c) in capacity_cross_check([c, e])) is flag


def test_summary_guard_removes_future_capacity_described_as_operating():
    synth = ("Large-scale data-center builds underway with 3 GW operating capacity. "
             "Energy-first approach with stranded gas and renewables.")
    out, n = sem.strip_future_as_current(synth, [3000.0])
    assert n == 1 and "3 GW" not in out and out.startswith("Energy-first")
    for ok in ("Crusoe reports 3 GW planned and 200 MW operating.", "A 3,000 MW pipeline is under development.",
               "Operates 206 MW today."):
        assert sem.strip_future_as_current(ok, [3000.0]) == (ok, 0)
    assert sem.strip_future_as_current("Currently running 3,000 MW.", [3000.0]) == (None, 1)


def test_prompts_carry_the_rule():
    assert P.PROMPT_VERSION >= "prompts-v2.3"
    assert "Under development as of March 2026" in P.EXTRACT_SYSTEM
    assert "never describe it as operating" in P.JUDGE_SYSTEM


# ------------------------------------------------------------- end to end (fake LLM, no network)
from app.models import Company, Evidence, JudgmentRun
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes

ENERGY = "Total energy consumption in 2025 was 596,000 GJ across our operations."


def _world(page_text, cap_quote):
    def extract(user):
        import re
        sid = {m.group(2): m.group(1) for m in re.finditer(r'<source id="(S\d+)" url="([^"]+)"', user)}
        s, n = sid[fakes.SUSTAIN_URL], sid[fakes.NEWS_URL]
        return {"figures": [
            {"source_id": s, "metric_key": "energy_consumption", "value": 596000, "unit": "GJ", "period": "2025",
             "scope": "operations", "quote": "Total energy consumption in 2025 was 596,000 GJ"},
            {"source_id": s, "metric_key": "datacenter_capacity_operating", "value": 3, "unit": "GW",
             "period": "2025", "scope": "company", "quote": cap_quote},
        ], "claims": [{"source_id": n, "category": "builder_velocity", "claim": "Annual cadence",
                       "quote": fakes.BUILDER_QUOTE}]}

    def judge(user):
        import re
        ids = {cat: int(i) for i, cat in re.findall(r"\[E(\d+)\] \((\w+);", user)}
        return {"categories": [
            {"category": "frontier_acceleration", "score": None, "confidence": 0, "insufficient_evidence": True,
             "evidence_ids": [], "rationale": "No evidence."},
            {"category": "builder_velocity", "score": 7.0, "confidence": 0.6, "insufficient_evidence": False,
             "evidence_ids": [ids["builder_velocity"]],
             "rationale": "Fast builder with 3 GW total operating capacity. Annual cadence."},
            {"category": "policy_stance", "score": None, "confidence": 0, "insufficient_evidence": True,
             "evidence_ids": [], "rationale": "No evidence."},
        ], "synthesis": "Large-scale data-center builds underway with 3 GW operating capacity. Energy-first."}

    routes = fakes.world_routes({fakes.SUSTAIN_URL: (200, "text/html", fakes.html("Impact report",
                                                                                  f"{page_text} {ENERGY}"))})
    return fakes.world_llm(extract=[extract], judge=[judge]), routes


@pytest.mark.parametrize("page,cap_quote,why", [
    (CRUSOE, QUOTE, "footnote"),                                        # real Crusoe layout
    ("Data Centers total capacity 3 GW sqft 8.4M", "Data Centers total capacity 3 GW", "inconsistent"),   # no footnote
])
def test_run_never_scores_or_summarises_future_capacity_as_current(db, page, cap_quote, why):
    c = Company(canonical_name="crusoe", official_name="Crusoe", industry="AI infrastructure")
    db.add(c)
    db.commit()
    llm, routes = _world(page, cap_quote)
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run = execute_run(db, run.id, fakes.make_deps(llm, routes=routes))
    assert run.status == "succeeded"
    cs = {s.category: s for s in run.category_scores}
    assert cs["compute_capacity"].score is None and cs["energy_throughput"].score is not None
    cap = db.query(Evidence).filter(Evidence.run_id == run.id, Evidence.metric_key.like("datacenter%")).one()
    if why == "footnote":
        assert cap.metric_key == "datacenter_capacity_planned" and "Under development" in cap.note
    else:
        assert cap.metric_key == "datacenter_capacity_operating" and "inconsistent with reported energy" in cap.note
    synth = (run.summary or {}).get("synthesis") or ""
    assert "3 GW" not in synth and synth.startswith("Energy-first")
    assert "3 GW" not in cs["builder_velocity"].rationale
    assert not run.ranked                                               # 42% coverage, as diagnosed
    assert isinstance(db.get(JudgmentRun, run.id).index_score, float)
