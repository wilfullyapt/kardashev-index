"""Security hardening: headers, CSP-compatible templates, session cookie flags, same-origin check on
admin/internal writes, admin-login throttle, SECRET_KEY required in production."""
import importlib
import re

import pytest

from app import main, security
from app.security import LoginThrottle
from tests.conftest import HERMES

LOGIN = {"email": "admin@example.com", "password": "test-password"}
BAD = {"email": "admin@example.com", "password": "wrong"}


# ---------------------------------------------------------------- headers
def test_security_headers_on_pages_and_json(client):
    for path in ("/", "/methodology", "/health", "/nope"):
        h = client.get(path).headers
        assert "default-src 'self'" in h["content-security-policy"], path
        assert "script-src 'self'" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in h["permissions-policy"]


def test_hsts_only_over_https(client):
    assert "strict-transport-security" not in client.get("/").headers
    h = client.get("/", headers={"X-Forwarded-Proto": "https"}).headers
    assert h["strict-transport-security"] == "max-age=86400"
    assert client.get("https://testserver/").headers["strict-transport-security"].startswith("max-age=")


def test_hsts_configurable(client, monkeypatch):
    monkeypatch.setenv("HSTS_MAX_AGE", "31536000")
    monkeypatch.setenv("HSTS_INCLUDE_SUBDOMAINS", "1")
    h = client.get("/", headers={"X-Forwarded-Proto": "https"}).headers
    assert h["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_templates_have_no_inline_script_or_handlers(client, admin_client, db):
    """The CSP forbids inline script, so no page may rely on it."""
    from app.models import Company
    db.add(Company(canonical_name="acme", industry="x"))
    db.commit()
    pages = [client.get(p).text for p in ("/", "/methodology", "/suggest", "/admin/login")]
    pages.append(admin_client.get("/admin").text)
    for html in pages:
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> block"
        assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
        assert "unpkg.com" not in html
    assert 'src="/static/vendor/htmx-1.9.12.min.js" integrity="sha384-' in pages[0]


def test_vendored_htmx_matches_sri(client):
    import base64
    import hashlib
    body = client.get("/static/vendor/htmx-1.9.12.min.js").content
    sri = "sha384-" + base64.b64encode(hashlib.sha384(body).digest()).decode()
    assert sri in client.get("/").text


# ---------------------------------------------------------------- session cookie
def _set_cookie(resp) -> str:
    return "; ".join(v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie")


def test_session_cookie_flags_dev(client):
    r = client.post("/admin/login", data=LOGIN, follow_redirects=False)
    c = _set_cookie(r).lower()
    assert "httponly" in c and "samesite=lax" in c and "max-age=43200" in c
    assert "secure" not in c  # local http


def test_session_cookie_secure_in_production(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    assert security.session_cookie_secure() is True
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    assert security.session_cookie_secure() is False


# ---------------------------------------------------------------- SECRET_KEY
def test_secret_key_required_in_production(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        security.session_secret()


def test_secret_key_dev_fallback_is_random_not_shared(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    a, b = security.session_secret(), security.session_secret()
    assert a != b and a != "dev-secret" and len(a) >= 32


def test_app_import_fails_in_production_without_secret(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        importlib.reload(main)
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("APP_ENV")
    importlib.reload(main)  # restore a working module for the other tests


# ---------------------------------------------------------------- same-origin (CSRF)
def test_cross_origin_admin_post_blocked(admin_client):
    r = admin_client.post("/internal/rerun-judgment/1", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"] == "Cross-origin request blocked"
    r = admin_client.post("/admin/login", data=LOGIN, headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403


def test_same_origin_and_headerless_admin_posts_allowed(admin_client, client):
    r = admin_client.post("/internal/rerun-judgment/999", headers={"Origin": "http://testserver"})
    assert r.status_code == 404  # reached the route (unknown company), not blocked
    r = client.post("/internal/rerun-judgment/999", headers=HERMES)  # Hermes: no Origin
    assert r.status_code == 404
    r = client.post("/admin/login", data=LOGIN, follow_redirects=False,
                    headers={"Origin": "http://testserver", "Referer": "http://testserver/admin/login"})
    assert r.status_code == 302


def test_null_origin_blocked_and_public_posts_unaffected(admin_client, client):
    assert admin_client.post("/internal/rerun-judgment/1", headers={"Origin": "null"}).status_code == 403
    r = client.post("/suggest", data={"name": "Acme"}, headers={"Origin": "https://elsewhere.example"})
    assert r.status_code == 200  # public form: not an admin/internal path


# ---------------------------------------------------------------- login throttle
def test_login_lockout_per_ip(client):
    for _ in range(5):
        assert client.post("/admin/login", data=BAD).status_code == 401
    r = client.post("/admin/login", data=LOGIN)  # even the right password is refused now
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert "Too many failed attempts" in r.text
    other = {"X-Forwarded-For": "203.0.113.9"}
    assert client.post("/admin/login", data=LOGIN, headers=other, follow_redirects=False).status_code == 302


def test_login_success_clears_failures(client):
    for _ in range(4):
        client.post("/admin/login", data=BAD)
    assert client.post("/admin/login", data=LOGIN, follow_redirects=False).status_code == 302
    for _ in range(4):
        assert client.post("/admin/login", data=BAD).status_code == 401


def test_throttle_unit_window_lockout_and_global():
    t = [0.0]
    th = LoginThrottle(ip_max=3, global_max=5, window_s=60, lockout_s=120, now=lambda: t[0])
    th.failure("a"); th.failure("a")
    t[0] = 61
    th.failure("a")  # first two aged out of the window
    assert th.retry_after("a") == 0
    th.failure("a"); th.failure("a")
    assert th.retry_after("a") == 120
    t[0] = 61 + 121
    assert th.retry_after("a") == 0
    for ip in ("b", "c", "d", "e", "f"):  # IP rotation trips the global backstop
        th.failure(ip)
    assert th.retry_after("zzz") == 120
