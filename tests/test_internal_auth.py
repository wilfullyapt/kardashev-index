from app.models import Suggestion, IngestLog
from tests.conftest import HERMES


def _pending(db, name="Pending Co"):
    s = Suggestion(name=name, status="pending")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s.id


def test_hermes_key_via_header_and_query(client):
    assert client.get("/internal/health", headers=HERMES).status_code == 200
    assert client.get("/internal/health?hermes_key=test-hermes-key").status_code == 200
    assert client.get("/internal/health?hermes_key=nope").status_code == 401


def test_admin_session_works_on_internal_routes(admin_client):
    for path in ("/internal/health", "/internal/stats", "/internal/recent-judgments",
                 "/internal/logs", "/internal/hermes-activity"):
        assert admin_client.get(path).status_code == 200, path


def test_hermes_activity_no_longer_always_401(client):
    assert client.get("/internal/hermes-activity", headers=HERMES).status_code == 200
    assert client.get("/internal/hermes-activity").status_code == 401


def test_admin_dashboard_requires_login(client):
    assert client.get("/admin").status_code == 401


def test_deny_route_with_admin_form(admin_client, db):
    sid = _pending(db)
    r = admin_client.post(f"/internal/deny/{sid}", data={"reason": "not a builder"})
    assert r.status_code == 200 and r.json()["status"] == "denied"
    db.expire_all()
    s = db.get(Suggestion, sid)
    assert s.status == "denied" and s.denial_reason == "not a builder"
    assert db.query(IngestLog).filter(IngestLog.action == "deny").count() == 1
    assert admin_client.post(f"/internal/deny/{sid}", data={"reason": "again"}).status_code == 404


def test_deny_route_with_hermes_and_requires_reason(client, db):
    sid = _pending(db)
    assert client.post(f"/internal/deny/{sid}", headers=HERMES).status_code == 422
    assert client.post(f"/internal/deny/{sid}?reason=dup", headers=HERMES).status_code == 200
    assert client.post(f"/internal/deny/{_pending(db, 'X')}", data={"reason": "x"}).status_code == 401


def test_suggest_rate_limited(client):
    for i in range(5):
        assert client.post("/suggest", data={"name": f"Builder {i}"}).status_code == 200
    r = client.post("/suggest", data={"name": "Builder 6"})
    assert r.status_code == 429 and "Too many suggestions" in r.text
    # a different client IP is unaffected
    ok = client.post("/suggest", data={"name": "Other"}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert ok.status_code == 200
