"""Canonical host (PUBLIC_BASE_URL), canonical/og tags, robots.txt, sitemap.xml, /favicon.ico."""
import re
from datetime import UTC, datetime

from app.models import Company, JudgmentRun

BASE = "https://kardashev.example"


def _canon(html):
    return (re.search(r'<link rel="canonical" href="([^"]+)"', html).group(1),
            re.search(r'<meta property="og:url" content="([^"]+)"', html).group(1),
            re.search(r'<meta property="og:image" content="([^"]+)"', html).group(1))


# ---------------------------------------------------------------- unset = no-op
def test_no_redirect_when_unset(client):
    assert client.get("/", follow_redirects=False).status_code == 200
    canon, og_url, og_img = _canon(client.get("/methodology").text)
    assert canon == og_url == "http://testserver/methodology"
    assert og_img == "http://testserver/static/img/og.jpg"


# ---------------------------------------------------------------- canonical host redirect
def test_other_host_redirects_301_keeping_path_and_query(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE + "/")
    r = client.get("/companies/3?x=1", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == BASE + "/companies/3?x=1"
    r = client.get("/", headers={"host": "kardashev-index.onrender.com"}, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == BASE + "/"


def test_non_get_uses_308(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    r = client.post("/suggest", data={"name": "x"}, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == BASE + "/suggest"


def test_health_and_internal_never_redirect(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    assert client.get("/health", follow_redirects=False).status_code == 200
    assert client.get("/internal/health", follow_redirects=False).status_code == 401  # reached the app


def test_canonical_host_is_served(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    r = client.get("/methodology", headers={"host": "kardashev.example"}, follow_redirects=False)
    assert r.status_code == 200
    canon, og_url, og_img = _canon(r.text)
    assert canon == og_url == BASE + "/methodology"
    assert og_img == BASE + "/static/img/og.jpg"


def test_scheme_less_base_url_gets_https(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "kardashev.example")
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == BASE + "/"


# ---------------------------------------------------------------- canonical tags
def test_search_and_historical_pages_canonicalise(client, db, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    c = Company(canonical_name="acme", industry="x")
    db.add(c)
    db.commit()
    hdr = {"host": "kardashev.example"}
    assert _canon(client.get("/?q=acme", headers=hdr).text)[0] == BASE + "/"
    assert _canon(client.get(f"/companies/{c.id}", headers=hdr).text)[0] == f"{BASE}/companies/{c.id}"


# ---------------------------------------------------------------- robots / sitemap / favicon
def test_robots_txt(client, monkeypatch):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    for line in ("User-agent: *", "Disallow: /admin", "Disallow: /internal",
                 "Sitemap: http://testserver/sitemap.xml"):
        assert line in body
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    assert f"Sitemap: {BASE}/sitemap.xml" in client.get("/robots.txt", headers={"host": "kardashev.example"}).text


def test_sitemap_lists_pages_and_companies(client, db, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    a = Company(canonical_name="acme", industry="x")
    b = Company(canonical_name="beta", industry="y")
    db.add_all([a, b])
    db.commit()
    run = JudgmentRun(company_id=a.id, status="succeeded", trigger="t", triggered_by="t",
                      finished_at=datetime(2026, 10, 9, 11, 0, tzinfo=UTC), published=True, ranked=True)
    db.add(run)
    db.commit()
    a.current_run_id = run.id
    db.commit()
    r = client.get("/sitemap.xml", headers={"host": "kardashev.example"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    xml = r.text
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    for loc in (f"{BASE}/", f"{BASE}/methodology", f"{BASE}/suggest",
                f"{BASE}/companies/{a.id}", f"{BASE}/companies/{b.id}"):
        assert f"<loc>{loc}</loc>" in xml
    assert f"<loc>{BASE}/companies/{a.id}</loc><lastmod>2026-10-09</lastmod>" in xml
    assert "/admin" not in xml and "/internal" not in xml


def test_favicon_ico(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
