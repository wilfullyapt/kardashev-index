"""Fetching, SSRF guard, text extraction and dead-link / soft-404 detection."""
import httpx
import pytest

from app.pipeline.fetch import BlockedURL, FetchResult, HttpFetcher, SourceChecker, check_url_allowed, extract_text
from tests.fakes import FakeFetcher, html


def public_dns(host, port):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def private_dns(host, port):
    return [(2, 1, 6, "", ("10.0.0.5", 0))]


@pytest.mark.parametrize("url", ["ftp://x.com/a", "http://localhost/a", "https://x.com:8443/a", "file:///etc/passwd",
                                 "http://intranet.local/x"])
def test_url_guard_rejects_non_public_targets(url):
    with pytest.raises(BlockedURL):
        check_url_allowed(url, public_dns)


def test_url_guard_rejects_private_ips():
    with pytest.raises(BlockedURL):
        check_url_allowed("https://evil.example/", private_dns)
    with pytest.raises(BlockedURL):
        check_url_allowed("http://169.254.169.254/latest", lambda h, p: [(2, 1, 6, "", ("169.254.169.254", 0))])
    check_url_allowed("https://ok.example/", public_dns)


def _transport(routes):
    def handler(request):
        fn = routes.get(str(request.url))
        return fn(request) if fn else httpx.Response(404, text="nope")
    return httpx.MockTransport(handler)


def test_fetcher_follows_redirects_and_records_final_url():
    t = _transport({
        "https://a.example/old": lambda r: httpx.Response(301, headers={"location": "/new"}),
        "https://a.example/new": lambda r: httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                                          text="<html>hi</html>"),
    })
    res = HttpFetcher(transport=t, resolve=public_dns).get("https://a.example/old")
    assert res.status == 200 and res.final_url == "https://a.example/new" and res.content_type == "text/html"
    assert res.redirects == ["https://a.example/old"]


def test_fetcher_blocks_redirect_into_private_network():
    t = _transport({"https://a.example/x": lambda r: httpx.Response(302, headers={"location": "http://localhost/admin"})})
    res = HttpFetcher(transport=t, resolve=public_dns).get("https://a.example/x")
    assert res.error.startswith("blocked") and res.status is None


def test_fetcher_caps_body_size_and_handles_timeouts():
    t = _transport({"https://a.example/big": lambda r: httpx.Response(200, content=b"x" * 5000)})
    res = HttpFetcher(transport=t, resolve=public_dns, max_bytes=1000).get("https://a.example/big")
    assert "larger than" in res.error and len(res.body) <= 1000

    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    res = HttpFetcher(transport=httpx.MockTransport(boom), resolve=public_dns, timeout_s=3).get("https://a.example/")
    assert res.error == "timeout after 3s"


def _pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>stream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_text_extraction_html_pdf_plain():
    title, text = extract_text("text/html", b"<html><title>T</title><script>evil()</script><p>Hello  <b>world</b></p></html>")
    assert title == "T" and "Hello world" in text and "evil" not in text
    _, text = extract_text("application/pdf", _pdf("Total energy consumption 612,000 MWh"))
    assert "612,000 MWh" in text
    assert extract_text("text/plain", b"a\n\nb")[1] == "a b"
    with pytest.raises(ValueError):
        extract_text("image/png", b"\x89PNG")


GOOD = html("Report", "Total energy consumption was 2.1 TWh in 2024. " * 5)


def checker(routes):
    return SourceChecker(FakeFetcher(routes), probe_token=lambda: "P")


def test_dead_links_and_errors():
    c = checker({})
    assert c.assess(FetchResult("https://a.example/x", status=404)).status == "dead"
    assert c.assess(FetchResult("https://a.example/x", status=503)).status == "dead"
    assert c.assess(FetchResult("https://a.example/x", error="timeout after 20s")).status == "dead"
    assert c.assess(FetchResult("https://a.example/x", error="blocked: non-public address")).status == "blocked"
    assert c.assess(FetchResult("https://a.example/x", status=200, content_type="image/png", body=b"x")).status == "error"


def test_soft_404_variants():
    c = checker({"https://b.example/ki-probe-P": (200, "text/html", html("Home", "Generic landing page. " * 30))})
    root = FetchResult("https://a.example/deep/report", final_url="https://a.example/", status=200,
                       content_type="text/html", body=GOOD.encode())
    assert c.assess(root).status == "soft_404"
    nf = FetchResult("https://a.example/x", final_url="https://a.example/x", status=200, content_type="text/html",
                     body=html("Page Not Found", "Sorry, this page could not be found.").encode())
    assert c.assess(nf).status == "soft_404"
    thin = FetchResult("https://a.example/x", final_url="https://a.example/x", status=200, content_type="text/html",
                       body=b"<html><title>Ok</title><p>tiny</p></html>")
    assert c.assess(thin).status == "thin"
    same_as_probe = FetchResult("https://b.example/report", final_url="https://b.example/report", status=200,
                                content_type="text/html", body=html("Home", "Generic landing page. " * 30).encode())
    a = c.assess(same_as_probe)
    assert a.status == "soft_404" and "random path" in a.reason


def test_real_page_passes_and_probe_is_cached_per_host():
    fetcher = FakeFetcher({"https://b.example/ki-probe-P": (200, "text/html", html("Home", "Generic landing page. " * 30))})
    c = SourceChecker(fetcher, probe_token=lambda: "P")
    for path in ("/r1", "/r2"):
        res = FetchResult(f"https://b.example{path}", final_url=f"https://b.example{path}", status=200,
                          content_type="text/html", body=GOOD.encode())
        a = c.assess(res)
        assert a.status == "ok" and len(a.snapshot.sha256) == 64
    assert fetcher.calls.count("https://b.example/ki-probe-P") == 1
