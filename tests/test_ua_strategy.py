"""1.4.0: per-host header profile. Ask as the declared bot first; on 401/403/406 try browser headers
once; remember per host what worked. SEC keeps its declared User-Agent; "browser" keeps the old way."""
import httpx

from app.pipeline.fetch import BOT_USER_AGENT, USER_AGENT, HostProfiles, HttpFetcher


def PUBLIC(host, port):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def fetcher(handler, **kw):
    kw.setdefault("profiles", HostProfiles())
    return HttpFetcher(transport=httpx.MockTransport(handler), resolve=PUBLIC, sleep=lambda s: None,
                       ua_strategy="honest_first", **kw)


def blocks_bots(seen):
    def h(req):
        ua = req.headers["user-agent"]
        seen.append(ua)
        return httpx.Response(403 if "KardashevIndexBot" in ua else 200, text="ok")
    return h


def test_bot_first_and_no_fallback_when_it_works():
    seen = []
    f = fetcher(lambda r: seen.append(r.headers["user-agent"]) or httpx.Response(200, text="ok"))
    res = f.get("https://a.example/x")
    assert res.status == 200 and res.profile == "bot" and seen == [BOT_USER_AGENT]
    assert "sec-fetch-mode" not in {k.lower() for k in f.bot_headers}


def test_403_falls_back_to_browser_and_is_remembered():
    seen = []
    f = fetcher(blocks_bots(seen))
    res = f.get("https://tesla.example/impact")
    assert res.status == 200 and res.profile == "browser" and seen == [BOT_USER_AGENT, USER_AGENT]
    assert f.profiles.get("tesla.example") == "browser"
    seen.clear()
    f.get("https://tesla.example/other")
    assert seen == [USER_AGENT]                      # remembered: browser first, one request


def test_browser_blocked_but_bot_allowed_is_remembered_too():
    seen = []

    def h(req):
        ua = req.headers["user-agent"]
        seen.append(ua)
        return httpx.Response(403 if "Mozilla" in ua else 200, text="ok")
    profiles = HostProfiles()
    profiles.remember("b.example", "browser")
    f = fetcher(h, profiles=profiles)
    assert f.get("https://b.example/").status == 200 and seen == [USER_AGENT, BOT_USER_AGENT]
    assert profiles.get("b.example") == "bot"


def test_both_blocked_returns_last_answer_and_remembers_nothing():
    f = fetcher(lambda r: httpx.Response(403, text="no"))
    res = f.get("https://c.example/")
    assert res.status == 403 and f.profiles.get("c.example") is None


def test_404_is_not_a_block():
    seen = []
    f = fetcher(lambda r: seen.append(1) or httpx.Response(404, text="nf"))
    assert f.get("https://d.example/").status == 404 and len(seen) == 1


def test_sec_and_explicit_user_agent_are_untouched():
    seen = []
    f = fetcher(lambda r: seen.append(r.headers["user-agent"]) or httpx.Response(403), sec_user_agent="KI ops@x")
    f.get("https://www.sec.gov/x")
    f.get("https://e.example/", headers={"User-Agent": "Custom/1"})
    assert seen == ["KI ops@x", "Custom/1"]


def test_browser_strategy_is_the_old_behaviour():
    seen = []
    f = HttpFetcher(transport=httpx.MockTransport(lambda r: seen.append(r.headers["user-agent"]) or httpx.Response(403)),
                    resolve=PUBLIC, sleep=lambda s: None, ua_strategy="browser")
    f.get("https://f.example/")
    assert seen == [USER_AGENT]


def test_custom_bot_user_agent_and_profile_expiry():
    seen = []
    f = fetcher(lambda r: seen.append(r.headers["user-agent"]) or httpx.Response(200), bot_user_agent="KIBot/2 (+x)")
    f.get("https://g.example/")
    assert seen == ["KIBot/2 (+x)"]
    t = [0.0]
    p = HostProfiles(ttl_s=10, clock=lambda: t[0])
    p.remember("h", "browser")
    t[0] = 11
    assert p.get("h") is None


def test_settings_default_is_honest_first(monkeypatch):
    from app.pipeline.config import settings
    monkeypatch.delenv("FETCH_UA_STRATEGY", raising=False)
    assert settings().fetch_ua_strategy == "honest_first"
