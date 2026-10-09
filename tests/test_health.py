from datetime import timedelta

from app import main
from app.db import SessionLocal
from app.worker import Worker


def test_public_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_internal_health_no_key(client):
    response = client.get("/internal/health")
    assert response.status_code == 401


# ---- Render health-check semantics (healthCheckPath: /health) ----


def _worker(session_factory=SessionLocal, ticked_ago_s=0):
    w = Worker(session_factory, lambda: None)
    w.started_at = w.now() - timedelta(hours=1)
    w.last_tick = w.now() - timedelta(seconds=ticked_ago_s)
    return w


def test_health_200_when_db_ok_and_worker_alive(client, monkeypatch):
    monkeypatch.setattr(main, "worker", _worker())
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_health_stays_200_when_worker_dead(client, monkeypatch):
    """A stalled worker must never make Render restart the web instance or fail a deploy."""
    monkeypatch.setattr(main, "worker", _worker(ticked_ago_s=3600))
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded" and body["worker"]["alive"] is False


def test_health_503_when_db_unreachable(client, monkeypatch):
    def broken_session():
        raise ConnectionError("db down")
    monkeypatch.setattr(main, "worker", _worker(session_factory=broken_session))
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded" and body["worker"]["db_error"] == "ConnectionError"
    assert body["version"]  # body still describes the instance
