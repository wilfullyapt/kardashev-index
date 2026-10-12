"""1.7.0 / pipeline-v2.10: JavaScript-built pages (e.g. Anduril) ship their content as JSON. When the
visible HTML is a shell, prose from __NEXT_DATA__, JSON-LD and JSON data islands is read too."""
import json

from app.pipeline.fetch import FetchResult, SourceChecker, extract_text
from tests import fakes

PROSE = ("Our Arsenal-1 facility in Ohio will span five million square feet and employ four thousand "
         "people when complete, producing autonomous systems at hyperscale.")
NEXT = {"props": {"pageProps": {"hero": {"title": "Arsenal-1", "body": PROSE,
                                         "image": "https://cdn.example/hero.jpg"},
                                "blocks": [{"text": "Energy use across our facilities was 41,200 MWh in 2024, "
                                                    "of which 38% came from renewable sources."},
                                           {"text": PROSE}]}},
        "buildId": "abc123", "page": "/arsenal"}
LD = {"@context": "https://schema.org", "@type": "Organization",
      "description": "Anduril Industries is a defense technology company building autonomous systems."}


def page(visible="<div id='__next'></div>", next_data=NEXT, ld=LD):
    parts = ["<!doctype html><html><head><title>Anduril | Arsenal</title>"]
    if ld is not None:
        parts.append(f'<script type="application/ld+json">{json.dumps(ld)}</script>')
    parts.append(f"</head><body>{visible}")
    if next_data is not None:
        parts.append(f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>')
    parts.append('<script>window.x = "some javascript string that is not data at all ok"</script></body></html>')
    return "".join(parts).encode()


def test_js_shell_gets_embedded_prose():
    _title, text = extract_text("text/html", page())
    assert "41,200 MWh in 2024" in text and "Arsenal-1 facility" in text
    assert "defense technology company" in text
    assert text.count("Arsenal-1 facility") == 1        # deduplicated
    assert "cdn.example" not in text and "abc123" not in text and "javascript string" not in text


def test_normal_page_is_unchanged():
    visible = "<p>" + " ".join(["Real visible paragraph about our data centers."] * 60) + "</p>"
    _t, text = extract_text("text/html", page(visible=visible))
    assert "41,200 MWh" not in text


def test_broken_json_is_ignored():
    html = b'<html><head><script id="__NEXT_DATA__" type="application/json">{not json</script></head><body>hi</body></html>'
    assert extract_text("text/html", html)[1] == "hi"


def test_js_shell_is_no_longer_thin():
    f = fakes.FakeFetcher({})
    res = FetchResult(url="https://www.anduril.com/arsenal", final_url="https://www.anduril.com/arsenal",
                      status=200, content_type="text/html", body=page())
    a = SourceChecker(f, probe_token=lambda: "P").assess(res)
    assert a.status == "ok" and "41,200 MWh" in a.snapshot.text
    shell = FetchResult(url=res.url, final_url=res.url, status=200, content_type="text/html",
                        body=page(next_data=None, ld=None))
    assert SourceChecker(f, probe_token=lambda: "P").assess(shell).status == "thin"
