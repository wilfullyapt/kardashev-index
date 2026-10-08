import json

import pytest

from app import main
from app import judgment as jm
from app.models import Company, Score, ScoreHistory, Suggestion, IngestLog, CATEGORIES
from tests.conftest import HERMES
from tests.fakes import FakeClient, payload, NVIDIA_LIKE


# ---------- helpers ----------

def make_company(db, name="nvidia"):
    c = Company(canonical_name=name, industry="Unknown")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def seed_scores(db, company_id, scores=None, model="grok-4", justification="old"):
    for cat, s in (scores or NVIDIA_LIKE).items():
        db.add(Score(company_id=company_id, category=cat, score=s, justification=justification,
                     evidence_links=[], model=model, model_version="2026-10"))
    db.commit()


def rows(db, company_id):
    db.expire_all()
    return db.query(Score).filter(Score.company_id == company_id).all()


def logs(db, action):
    db.expire_all()
    return db.query(IngestLog).filter(IngestLog.action == action).all()


def rerun(client, company_id):
    return client.post(f"/internal/rerun-judgment/{company_id}", headers=HERMES)


# ---------- parser unit tests ----------

def test_parse_valid_output():
    items = jm.parse_judgment(payload())
    assert [i["category"] for i in items] == CATEGORIES
    assert items[0]["score"] == 9.7


def test_parse_accepts_code_fences():
    assert len(jm.parse_judgment("```json\n" + payload() + "\n```")) == len(CATEGORIES)


@pytest.mark.parametrize("content, msg", [
    ('{"scores": [{"category": "energy_kardashev", "score": 7', "truncated"),
    ("", "empty"),
    (payload(scores={**NVIDIA_LIKE, "market_competition": "high"}), "must be a number"),
    (payload(scores={**NVIDIA_LIKE, "energy_kardashev": 42}), "outside 0-10"),
    (payload(scores={**NVIDIA_LIKE, "energy_kardashev": -1}), "outside 0-10"),
    (payload(scores={**NVIDIA_LIKE, "builder_culture": True}), "must be a number"),
])
def test_parse_rejects_bad_output(content, msg):
    with pytest.raises(jm.JudgmentValidationError, match=msg):
        jm.parse_judgment(content)


def test_parse_rejects_duplicate_category():
    data = json.loads(payload())
    data["scores"].append({"category": "ai_tech_acceleration", "score": 10, "justification": "dup"})
    with pytest.raises(jm.JudgmentValidationError, match="duplicate"):
        jm.parse_judgment(json.dumps(data))


def test_parse_rejects_misnamed_and_missing_categories():
    data = json.loads(payload())
    data["scores"][0]["category"] = "AI Tech Acceleration"
    with pytest.raises(jm.JudgmentValidationError, match="unknown category"):
        jm.parse_judgment(json.dumps(data))
    data = json.loads(payload())
    data["scores"] = data["scores"][:-1]
    with pytest.raises(jm.JudgmentValidationError, match="missing categories: overall"):
        jm.parse_judgment(json.dumps(data))


def test_parse_rejects_missing_justification():
    data = json.loads(payload())
    data["scores"][2]["justification"] = "  "
    with pytest.raises(jm.JudgmentValidationError, match="justification"):
        jm.parse_judgment(json.dumps(data))


# ---------- end-to-end through the routes with a fake LLM ----------

def test_valid_judgment_writes_scores_and_metrics(client, db):
    c = make_company(db)
    fake = FakeClient(payload())
    main.grok_client = fake
    r = rerun(client, c.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rerun_complete" and body["model"] == "grok-4.3-fake"
    got = rows(db, c.id)
    assert sorted(s.category for s in got) == sorted(CATEGORIES)
    assert {s.model for s in got} == {"grok-4.3-fake"}  # real model id from the API, not the alias
    log = logs(db, "judgment")[-1].details
    assert log["model_requested"] == jm.XAI_MODEL
    assert log["prompt_tokens"] == 250 and log["completion_tokens"] == 900
    assert isinstance(log["duration_ms"], int) and log["duration_ms"] >= 0
    assert log["cost_usd"] == pytest.approx(0.00375)
    assert fake.calls[0]["model"] == jm.XAI_MODEL
    # the blocking call ran in a worker thread, not on the event loop thread
    assert fake.threads and all("AnyIO worker thread" in t for t in fake.threads)


@pytest.mark.parametrize("bad", [
    '{"scores": [{"category": "energy_kardashev", "score": 7',              # truncated JSON
    payload(scores={**NVIDIA_LIKE, "market_competition": "high"}),         # bad field
    payload(scores={**NVIDIA_LIKE, "energy_kardashev": 42}),               # out of range
])
def test_invalid_output_retries_then_logs_and_writes_nothing(client, db, bad):
    c = make_company(db)
    fake = FakeClient(bad)
    main.grok_client = fake
    r = rerun(client, c.id)
    assert r.status_code == 502 and r.json()["status"] == "rerun_failed"
    assert len(fake.calls) == jm.JUDGE_MAX_ATTEMPTS
    assert rows(db, c.id) == []          # no partial rows, no placeholders
    err = logs(db, "judge_error")[-1].details
    assert err["error_type"] == "validation_error" and len(err["attempts"]) == jm.JUDGE_MAX_ATTEMPTS


def test_duplicate_and_misnamed_categories_are_rejected_end_to_end(client, db):
    c = make_company(db)
    data = json.loads(payload())
    data["scores"].append(dict(data["scores"][0]))
    misnamed = json.loads(payload())
    misnamed["scores"][0]["category"] = "AI Tech Acceleration"
    main.grok_client = FakeClient(json.dumps(data), json.dumps(misnamed))
    assert rerun(client, c.id).status_code == 502
    assert rows(db, c.id) == []


def test_retry_recovers_from_one_invalid_response(client, db):
    c = make_company(db)
    fake = FakeClient("not json", payload())
    main.grok_client = fake
    assert rerun(client, c.id).status_code == 200
    assert len(rows(db, c.id)) == len(CATEGORIES)
    assert "rejected" in fake.calls[1]["messages"][-1]["content"]  # repair prompt sent
    assert logs(db, "judgment")[-1].details["attempts"][0]["ok"] is False


def test_api_error_logs_and_does_not_retry_validation_loop(client, db):
    c = make_company(db)
    fake = FakeClient(RuntimeError("upstream 503"))
    main.grok_client = fake
    assert rerun(client, c.id).status_code == 502
    assert len(fake.calls) == 1
    assert logs(db, "judge_error")[-1].details["error_type"] == "api_error"


def test_rerun_failure_preserves_old_scores(client, db):
    c = make_company(db)
    seed_scores(db, c.id)
    before = sorted((s.category, s.score, s.justification) for s in rows(db, c.id))
    main.grok_client = FakeClient('{"scores": [')
    assert rerun(client, c.id).status_code == 502
    after = sorted((s.category, s.score, s.justification) for s in rows(db, c.id))
    assert after == before
    assert db.query(ScoreHistory).count() == 0


def test_rerun_success_archives_old_scores_and_swaps(client, db):
    c = make_company(db)
    seed_scores(db, c.id)
    new = {cat: 6.0 for cat in CATEGORIES}
    main.grok_client = FakeClient(payload(scores=new))
    assert rerun(client, c.id).status_code == 200
    got = rows(db, c.id)
    assert len(got) == len(CATEGORIES) and {s.score for s in got} == {6.0}
    hist = db.query(ScoreHistory).filter(ScoreHistory.company_id == c.id).all()
    assert len(hist) == len(CATEGORIES) and {h.justification for h in hist} == {"old"}


def test_placeholder_rows_are_replaced_not_archived(client, db):
    c = make_company(db, "tesla")
    seed_scores(db, c.id, {cat: 5.0 for cat in CATEGORIES}, model="stub",
                justification="Placeholder — run full judge")
    main.grok_client = FakeClient(payload())
    assert rerun(client, c.id).status_code == 200
    assert {s.model for s in rows(db, c.id)} == {"grok-4.3-fake"}
    assert db.query(ScoreHistory).count() == 0


def test_no_api_key_leaves_data_untouched(client, db):
    c = make_company(db)
    seed_scores(db, c.id)
    main.grok_client = None
    assert rerun(client, c.id).status_code == 502
    assert len(rows(db, c.id)) == len(CATEGORIES)
    assert "XAI_API_KEY" in logs(db, "judge_error")[-1].details["error"]


# ---------- approve idempotency ----------

def test_approve_twice_is_idempotent(client, db):
    db.add(Suggestion(name="Acme Fusion", domain="acme.example", status="pending"))
    db.commit()
    sid = db.query(Suggestion).one().id
    fake = FakeClient(payload())
    main.grok_client = fake
    r1 = client.post(f"/internal/approve/{sid}", headers=HERMES)
    r2 = client.post(f"/internal/approve/{sid}", headers=HERMES)
    assert r1.status_code == 200 and r1.json()["judgment"] == "succeeded"
    assert r2.status_code == 404
    cid = r1.json()["company_id"]
    assert len(rows(db, cid)) == len(CATEGORIES)
    assert len(fake.calls) == 1
    assert db.get(Company, cid).domain == "acme.example"


def test_approve_existing_company_replaces_instead_of_appending(client, db):
    c = make_company(db, "acme-fusion")
    seed_scores(db, c.id)
    db.add(Suggestion(name="Acme Fusion", status="pending"))
    db.commit()
    sid = db.query(Suggestion).one().id
    main.grok_client = FakeClient(payload(scores={cat: 7.0 for cat in CATEGORIES}))
    r = client.post(f"/internal/approve/{sid}", headers=HERMES)
    assert r.status_code == 200 and r.json()["company_id"] == c.id
    got = rows(db, c.id)
    assert len(got) == len(CATEGORIES) and {s.score for s in got} == {7.0}


def test_concurrent_judgment_for_same_company_is_refused(client, db):
    c = make_company(db)
    main._judging.add(c.id)
    main.grok_client = FakeClient(payload())
    assert rerun(client, c.id).status_code == 409


# ---------- Overall / ranking ----------

def test_overall_excludes_judge_overall_category(client, db):
    c = make_company(db)
    seed_scores(db, c.id)  # dims 9.7, 4.2, 7.1, 7.8, 9.0 (+ judge overall 8.3)
    page = client.get(f"/companies/{c.id}").text
    assert '<span class="gauge__v">7.6</span>' in page   # mean of the five dims, not 7.7
    ranked = main.get_ranked_companies(db)
    assert ranked[0]["overall"] == 7.6


def test_summary_dedupes_and_ignores_placeholders(db):
    c = make_company(db)
    seed_scores(db, c.id)
    db.add(Score(company_id=c.id, category="energy_kardashev", score=5.0, model="stub",
                 justification="Placeholder — run full judge"))
    db.commit()
    summary = jm.summarize_scores(rows(db, c.id))
    assert summary["status"] == "ranked" and summary["overall"] == 7.6
    # a later duplicate wins over the earlier row for the same category
    db.add(Score(company_id=c.id, category="energy_kardashev", score=9.2, model="grok-4",
                 justification="newer"))
    db.commit()
    assert jm.summarize_scores(rows(db, c.id))["overall"] == 8.6


def test_placeholder_and_empty_companies_are_not_ranked(client, db):
    real = make_company(db, "nvidia")
    seed_scores(db, real.id)
    stub = make_company(db, "tesla")
    seed_scores(db, stub.id, {cat: 5.0 for cat in CATEGORIES}, model="stub",
                justification="Placeholder — run full judge")
    make_company(db, "empty-co")
    ranked, awaiting = main.get_leaderboard(db)
    assert [e["company"].canonical_name for e in ranked] == ["nvidia"]
    assert sorted(e["company"].canonical_name for e in awaiting) == ["empty-co", "tesla"]
    home = client.get("/").text
    assert "Awaiting judgment" in home and "tesla" in home
    page = client.get(f"/companies/{stub.id}").text
    assert '<span class="gauge__v">—</span>' in page
