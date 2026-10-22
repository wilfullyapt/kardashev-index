"""First-party visit counts (1.15.0): what is recorded and skipped, privacy (no raw IP/UA, daily
salt), batched writes, daily rollup + retention, migration 0008, the admin Traffic panel, /health."""
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app import analytics
from app.db import SessionLocal
from app.models import PageView, PageViewDaily

UA_HUMAN = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"
HTML = {"user-agent": UA_HUMAN, "accept": "text/html,*/*", "x-forwarded-for": "203.0.113.7, 10.0.0.1"}


def _rows():
    return list(analytics.BUFFER._q)


def test_records_html_get_with_referrer_utm_country(client):
    r = client.get("/?utm_source=X.com&utm_medium=social&utm_campaign=launch",
                   headers={**HTML, "referer": "https://www.news.ycombinator.com/item?id=1", "cf-ipcountry": "de"})
    assert r.status_code == 200
    (row,) = _rows()
    assert row["path"] == "/" and row["ref_host"] == "news.ycombinator.com"
    assert (row["utm_source"], row["utm_medium"], row["utm_campaign"]) == ("x.com", "social", "launch")
    assert row["country"] == "DE" and row["ua_class"] == "human" and row["status"] == 200
    assert len(row["visitor"]) == 16
    assert analytics.status()["country_header_seen"]


def test_no_country_header_is_null_and_direct_referrer(client):
    client.get("/", headers=HTML)
    (row,) = _rows()
    assert row["country"] is None and row["ref_host"] == "direct" and row["utm_source"] is None
    assert not analytics.status()["country_header_seen"]


def test_internal_referrer(client):
    client.get("/about", headers={**HTML, "referer": "http://testserver/"})
    assert _rows()[0]["ref_host"] == "internal"


def test_skips_static_health_admin_internal_partials_posts_and_non_html(client):
    for p in ("/health", "/admin", "/admin/login", "/robots.txt", "/sitemap.xml", "/static/css/kardashev.css"):
        client.get(p, headers=HTML)
    client.get("/", headers={**HTML, "hx-request": "true"})
    client.post("/suggest", data={"name": "x"}, headers=HTML)
    client.get("/does-not-exist", headers=HTML)          # 404
    assert _rows() == []


def test_disabled_records_nothing(client, monkeypatch):
    monkeypatch.setenv("ANALYTICS_ENABLED", "0")
    client.get("/", headers=HTML)
    assert _rows() == []


def test_extra_excluded_paths(client, monkeypatch):
    monkeypatch.setenv("ANALYTICS_EXCLUDE_PATHS", "/about, /privacy")
    client.get("/about", headers=HTML)
    client.get("/privacy", headers=HTML)
    assert _rows() == []


def test_ua_classes():
    assert analytics.ua_class(UA_HUMAN, "text/html") == "human"
    for ua in ("Mozilla/5.0 (compatible; Googlebot/2.1)", "GPTBot/1.0", "ClaudeBot/1.0", "curl/8.4",
               "python-requests/2.32", "Go-http-client/1.1", "facebookexternalhit/1.1", ""):
        assert analytics.ua_class(ua) == "bot", ua
    for ua in ("Mozilla/5.0 (compatible; UptimeRobot/2.0)", "Render/1.0", "Better Stack Uptime Bot"):
        assert analytics.ua_class(ua) == "monitor", ua
    assert analytics.ua_class(UA_HUMAN, "application/json") == "monitor"


def test_visitor_id_daily_salt():
    a = analytics.visitor_id("2026-10-22", "203.0.113.7", UA_HUMAN)
    assert a == analytics.visitor_id("2026-10-22", "203.0.113.7", UA_HUMAN)
    assert a != analytics.visitor_id("2026-10-22", "203.0.113.8", UA_HUMAN)
    b = analytics.visitor_id("2026-10-23", "203.0.113.7", UA_HUMAN)     # salt rotated
    assert b != a
    assert analytics.visitor_id("2026-10-22", "203.0.113.7", UA_HUMAN) != a   # old salt is gone for good
    assert list(analytics._SALT) == ["2026-10-22"]


def test_no_raw_ip_or_user_agent_stored(client):
    client.get("/", headers=HTML)
    assert analytics.flush(SessionLocal) == 1
    with SessionLocal() as db:
        (pv,) = db.query(PageView).all()
        stored = " ".join(str(getattr(pv, c.name)) for c in PageView.__table__.columns)
    assert "203.0.113" not in stored and "iPhone" not in stored and "Mozilla" not in stored


def test_request_does_not_write_db_until_flush(client):
    client.get("/", headers=HTML)
    client.get("/about", headers=HTML)
    with SessionLocal() as db:
        assert db.query(PageView).count() == 0
    assert analytics.flush(SessionLocal) == 2
    with SessionLocal() as db:
        assert db.query(PageView).count() == 2
    assert analytics.flush(SessionLocal) == 0 and analytics.status()["written"] == 2


def test_buffer_cap_drops_oldest():
    b = analytics.Buffer()
    for i in range(15):
        b.add({"i": i}, cap=10)
    assert len(b) == 10 and b.dropped == 5 and b.drain()[0] == {"i": 5}


def test_flush_failure_keeps_rows_and_never_raises(client):
    client.get("/", headers=HTML)

    def broken():
        raise RuntimeError("db down")
    assert analytics.flush(broken) == 0
    assert len(analytics.BUFFER) == 1 and "db down" in analytics.status()["last_error"]
    assert analytics.flush(SessionLocal) == 1 and analytics.status()["last_error"] is None


def _view(day, path="/", ref="direct", cc=None, utm=None, ua="human", vis="v1"):
    return {"ts": datetime.now(UTC), "day": day, "path": path, "ref_host": ref, "utm_source": utm,
            "utm_medium": None, "utm_campaign": None, "country": cc, "ua_class": ua, "visitor": vis, "status": 200}


NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)     # 11:00 PDT on 2026-10-22


def _seed(rows):
    with SessionLocal() as db:
        db.bulk_insert_mappings(PageView, rows)
        db.commit()


def test_rollup_totals_idempotent_and_purge():
    _seed([_view("2026-10-21", "/", "x.com", "US", "x", vis="a"), _view("2026-10-21", "/", "x.com", "US", vis="a"),
           _view("2026-10-21", "/about", cc="DE", vis="b"), _view("2026-10-21", "/", ua="bot", vis="c"),
           _view("2026-10-22", "/", vis="today"),                       # today: not rolled up
           _view("2026-07-01", "/old", vis="o")])                       # older than 90 days: purged
    with SessionLocal() as db:
        out = analytics.rollup(db, NOW, raw_days=90)
        assert out == {"rolled_up_days": 2, "purged_raw": 1}
        again = analytics.rollup(db, NOW, raw_days=90)
        assert again["rolled_up_days"] == 1
        d = {(r.dim, r.key, r.ua_class): (r.views, r.visitors)
             for r in db.query(PageViewDaily).filter(PageViewDaily.day == "2026-10-21")}
        assert d[("total", "", "human")] == (3, 2) and d[("total", "", "bot")] == (1, 1)
        assert d[("path", "/", "human")] == (2, 1) and d[("ref", "x.com", "human")] == (2, 1)
        assert d[("country", "US", "human")] == (2, 1) and d[("country", "DE", "human")] == (1, 1)
        assert d[("utm_source", "x", "human")] == (1, 1)
        assert db.query(PageViewDaily).filter(PageViewDaily.day == "2026-10-22").count() == 0
        assert db.query(PageView).filter(PageView.day == "2026-07-01").count() == 0
        assert db.query(PageViewDaily).filter(PageViewDaily.day == "2026-07-01").count() > 0   # totals kept


def test_runner_rolls_up_once_per_day_after_3am_pt():
    r = analytics.Runner(SessionLocal)
    early = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)      # 02:00 PDT
    assert r.maybe_rollup(early) is None
    assert r.maybe_rollup(NOW) is not None
    assert r.maybe_rollup(NOW + timedelta(hours=1)) is None


def test_report_humans_only_and_bots(db):
    _seed([_view("2026-10-21", "/", "x.com", "US", vis="a"), _view("2026-10-21", "/about", vis="b"),
           _view("2026-10-21", "/", ua="bot", vis="c"), _view("2026-10-22", "/", vis="t")])
    analytics.rollup(db, NOW)
    a = analytics.report(db, now=NOW, use_cache=False)
    assert a["today"] == {"day": "2026-10-22", "visitors": 1, "views": 1, "pct": a["today"]["pct"]}
    assert dict(a["pages"]) == {"/": 2, "/about": 1} and ("x.com", 1) in a["refs"]
    assert a["countries"] == [("US", 1)] and a["share"]["bot"] == 25.0
    b = analytics.report(db, now=NOW, include_bots=True, use_cache=False)
    assert dict(b["pages"])["/"] == 3


def test_admin_panel_requires_login(client):
    r = client.get("/admin/analytics", follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)


def test_admin_panel_renders_empty_and_seeded(admin_client):
    t = admin_client.get("/admin/analytics").text
    assert "Visitors per day" in t and "No page views yet." in t and "Country unavailable" in t
    assert "humans only" in t
    _seed([_view(analytics.pt_day(), "/companies/1", "x.com", "JP")])
    analytics.clear_cache()
    t = admin_client.get("/admin/analytics?bots=1").text
    assert "/companies/1" in t and "x.com" in t and "JP" in t and "all traffic" in t


def test_board_has_traffic_section(admin_client):
    t = admin_client.get("/admin").text
    assert 'id="traffic"' in t and 'hx-get="/admin/analytics"' in t


def test_health_reports_analytics(client):
    a = client.get("/health").json()["analytics"]
    assert set(a) >= {"enabled", "buffered", "written", "dropped", "country_header_seen"}


def test_migration_0008_up_down(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/mig8.db"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0008")
    eng = sa.create_engine(url)
    assert {"page_views", "page_view_daily"} <= set(sa.inspect(eng).get_table_names())
    command.downgrade(cfg, "0007")
    names = set(sa.inspect(eng).get_table_names())
    assert "page_views" not in names and "page_view_daily" not in names and "sources" in names


def test_lifespan_final_flush_on_shutdown():
    from fastapi.testclient import TestClient

    from app import main
    with TestClient(main.app) as c:
        c.get("/", headers=HTML)
    with SessionLocal() as db:
        assert db.query(PageView).count() == 1
