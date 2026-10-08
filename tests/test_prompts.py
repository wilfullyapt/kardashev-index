"""Strict validation of every LLM stage's output."""
import pytest

from app.pipeline import prompts as P

IDS = {11, 12, 13}


def judged(**over):
    base = {"frontier_acceleration": {"category": "frontier_acceleration", "score": 7.25, "confidence": 0.7,
                                      "evidence_ids": [11], "rationale": "r"},
            "builder_velocity": {"category": "builder_velocity", "score": None, "insufficient_evidence": True},
            "policy_stance": {"category": "policy_stance", "score": 4, "confidence": 0.5, "evidence_ids": [12, 13],
                              "rationale": "r"}}
    base.update(over)
    return {"categories": [v for v in base.values() if v is not None], "synthesis": "s"}


def test_valid_judge_output():
    out, synth = P.validate_judge(judged(), IDS)
    assert out["frontier_acceleration"]["score"] == 7.2 or out["frontier_acceleration"]["score"] == 7.3
    assert out["builder_velocity"]["score"] is None and out["builder_velocity"]["confidence"] == 0
    assert synth == "s"


@pytest.mark.parametrize("data,msg", [
    ({"categories": judged()["categories"] + [{"category": "policy_stance", "score": 1, "evidence_ids": [11]}]}, "duplicate"),
    (judged(policy_stance={"category": "regulatory_stance", "score": 5, "evidence_ids": [11]}), "unknown category"),
    (judged(policy_stance=None), "missing categories"),
    (judged(policy_stance={"category": "policy_stance", "score": 10.5, "evidence_ids": [11]}), "out of range"),
    (judged(policy_stance={"category": "policy_stance", "score": "8", "evidence_ids": [11]}), "finite number"),
    (judged(policy_stance={"category": "policy_stance", "score": True, "evidence_ids": [11]}), "finite number"),
    (judged(policy_stance={"category": "policy_stance", "score": 5, "evidence_ids": [99]}), "not in the provided"),
    (judged(policy_stance={"category": "policy_stance", "score": 5, "evidence_ids": []}), "at least one evidence"),
    ({"overall": 7}, "expected"),
])
def test_invalid_judge_output(data, msg):
    with pytest.raises(P.StageOutputError, match=msg):
        P.validate_judge(data, IDS)


def test_extract_drops_malformed_items_but_keeps_good_ones():
    data = {"figures": [
        {"source_id": "S1", "metric_key": "energy_consumption", "value": 5, "unit": "TWh", "period": "2024", "quote": "q" * 30},
        {"source_id": "S9", "metric_key": "energy_consumption", "value": 5, "unit": "TWh", "quote": "q" * 30},
        {"source_id": "S1", "metric_key": "vibes", "value": 5, "unit": "TWh", "quote": "q" * 30},
        {"source_id": "S1", "metric_key": "capex", "value": -3, "unit": "USD", "quote": "q" * 30},
        {"source_id": "S1", "metric_key": "capex", "value": 3, "unit": "USD"},
    ], "claims": [{"source_id": "S1", "category": "builder_velocity", "claim": "c", "quote": "q" * 30},
                  {"source_id": "S1", "category": "overall", "claim": "c", "quote": "q" * 30}]}
    figs, claims, rejected = P.validate_extract(data, {"S1"})
    assert len(figs) == 1 and len(claims) == 1 and len(rejected) == 5
    with pytest.raises(P.StageOutputError):
        P.validate_extract({"figures": "nope"}, {"S1"})


def test_resolve_normalizes_domain_and_ticker():
    out = P.validate_resolve({"official_name": "Tesla, Inc.", "domain": "https://www.tesla.com/about", "ticker": "tsla",
                              "is_public": True, "confidence": 0.9})
    assert out["domain"] == "tesla.com" and out["ticker"] == "TSLA" and out["sec_filer"] is False
    with pytest.raises(P.StageOutputError):
        P.validate_resolve({"domain": "x.com"})


def test_research_keeps_only_http_urls():
    out = P.validate_research({"sources": [{"url": "https://a.example/r", "covers": ["energy"]}, {"url": "javascript:x"},
                                           "junk", {"url": "ftp://x"}]})
    assert [s["url"] for s in out] == ["https://a.example/r"]


def test_prompts_carry_stage_markers_and_no_overall():
    for system in (P.RESOLVE_SYSTEM, P.RESEARCH_SYSTEM, P.EXTRACT_SYSTEM, P.JUDGE_SYSTEM):
        assert "[stage:" in system and "JSON" in system
    assert "Do not produce an overall score" in P.JUDGE_SYSTEM
    assert "never compute" in P.EXTRACT_SYSTEM
