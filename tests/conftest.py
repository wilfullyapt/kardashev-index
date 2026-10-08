"""Test setup: an isolated SQLite DB with tables, no real LLM, known admin/Hermes creds.

Environment is fixed *before* the app is imported so tests can never touch a real
database or call a paid API, even if the developer's shell exports those variables.
"""
import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="kardashev-tests-")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP_DIR}/test.db"
for _k in ("XAI_API_KEY", "GROK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)
os.environ["HERMES_API_KEY"] = "test-hermes-key"
os.environ["ADMIN_EMAIL"] = "admin@example.com"
os.environ["SECRET_KEY"] = "test-secret"
from passlib.hash import bcrypt as _bcrypt
os.environ["ADMIN_PASSWORD_HASH"] = _bcrypt.hash("test-password")

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Base, engine, SessionLocal

HERMES = {"X-Hermes-Key": "test-hermes-key"}


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    main.RATE_LIMIT.clear()
    main.grok_client = None
    main._judging.clear()
    yield
    main.grok_client = None


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def admin_client():
    c = TestClient(main.app)
    r = c.post("/admin/login", data={"email": "admin@example.com", "password": "test-password"},
               follow_redirects=False)
    assert r.status_code == 302
    return c
