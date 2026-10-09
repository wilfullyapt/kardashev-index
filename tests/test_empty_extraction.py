"""Empty extractions (prompts-v2.5). Anduril #25 got {"figures": [], "claims": []} (output_tokens=12,
finish_reason=stop) twice, waited for queued retries (2 then 10 minutes) and succeeded on attempt 3
with the same texts. Now: an immediate nudged re-ask; a deliberate empty answer twice degrades the
stage; only a real cut-off queues a retry."""
from app.models import Company, JudgmentRun, JudgmentStage
from app.pipeline import prompts as P
from app.pipeline.runner import execute_run
from app.runs import enqueue_run
from tests import fakes


def judge_nothing(_user):
    return {"categories": [{"category": k, "score": None, "confidence": 0, "insufficient_evidence": True,
                            "evidence_ids": [], "rationale": "No verified evidence."}
                           for k in ("frontier_acceleration", "builder_velocity", "policy_stance")],
            "synthesis": "Too little verified evidence."}


def _run(db, llm, retry=fakes.RETRY):
    c = Company(canonical_name="anduril", official_name="Anduril", industry="Defense")
    db.add(c)
    db.commit()
    run, _ = enqueue_run(db, c.id, trigger="test", triggered_by="t")
    run = execute_run(db, run.id, fakes.make_deps(llm, retry=retry))
    st = db.query(JudgmentStage).filter_by(run_id=run.id, stage="extract").one()
    return run, st


def test_nudged_reask_recovers_in_the_same_attempt(db):
    llm = fakes.world_llm(extract=[fakes.empty_reply(), fakes.extract_ok])
    run, st = _run(db, llm)
    assert run.status == "succeeded", run.error_message
    assert (run.attempt or 1) == 1 and run.next_attempt_at is None
    assert st.detail["nudged"] and st.detail["empty_first_try"]["output_tokens"] == 12
    calls = [c for c in llm.calls if c["stage"] == "extract"]
    assert len(calls) == 2 and P.EXTRACT_NUDGE in calls[1]["user"] and P.EXTRACT_NUDGE not in calls[0]["user"]
    assert st.detail["figures_verified"]


def test_empty_twice_with_normal_finish_degrades_instead_of_queueing(db):
    llm = fakes.world_llm(extract=[fakes.empty_reply()], judge=[judge_nothing])
    run, st = _run(db, llm)
    assert run.status == "succeeded", (run.error_type, run.error_message)
    assert run.next_attempt_at is None    # not re-queued for minutes
    assert db.query(JudgmentRun).filter_by(status="queued").count() == 0
    reason = next(d for d in run.degraded if d.startswith("extract"))
    assert "no figures or claims, twice (second time nudged" in reason
    assert "output_tokens=12" in reason and "reasoning_tokens=900" in reason and "chars sent" in reason
    d = st.detail["empty_extraction"]
    assert d["finish_reason"] == "stop" and d["sources"] >= 1 and d["chars_sent"] > 0


def test_real_cut_off_still_queues_a_retry(db):
    llm = fakes.world_llm(extract=[fakes.empty_reply(output_tokens=16000, finish_reason="length")])
    run, _st = _run(db, llm)
    assert run.status == "queued" and run.next_attempt_at is not None and run.error_type == "empty_extraction"
    assert "cut off twice" in run.error_message and "finish_reason=length" in run.error_message


def test_no_output_at_all_counts_as_cut_off(db):
    llm = fakes.world_llm(extract=[fakes.empty_reply(output_tokens=0, finish_reason="stop")])
    run, _st = _run(db, llm)
    assert run.status == "queued" and run.error_type == "empty_extraction"


def test_cut_off_on_final_attempt_degrades(db):
    llm = fakes.world_llm(extract=[fakes.empty_reply(output_tokens=16000, finish_reason="length")],
                          judge=[judge_nothing])
    run, _st = _run(db, llm, retry=fakes.NO_RETRY)
    assert run.status == "succeeded" and any(d.startswith("extract") for d in run.degraded)
