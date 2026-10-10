"""Licensing: files exist and the site states the data license (display only).

From 0.15.0 the data is CC BY-NC 4.0; content published by v0.5.0–v0.14.0 stays CC BY 4.0."""
from pathlib import Path

from app.version import __version__

ROOT = Path(__file__).resolve().parent.parent
NC = "https://creativecommons.org/licenses/by-nc/4.0/"
BY = "https://creativecommons.org/licenses/by/4.0/"


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def test_license_files():
    mit = (ROOT / "LICENSE").read_text()
    assert mit.startswith("MIT License") and "Copyright (c) 2026 Wil Mawhinney" in mit
    data = (ROOT / "DATA-LICENSE.md").read_text()
    assert data.startswith("# Data and methodology license: CC BY-NC 4.0")
    assert NC + "legalcode" in data and "CC BY-NC 4.0" in data
    # earlier grants stand, with the official CC BY 4.0 links
    assert "v0.5.0 through v0.14.0" in data and BY + "legalcode" in data and "cannot be revoked" in data
    for need in ("commercial use", "bulk or automated access", "resale", "CONTACT_EMAIL",
                 "Facts and figures quoted from company sources", "MIT License"):
        assert need in data, need
    readme = (ROOT / "README.md").read_text()
    assert "## License" in readme and "CC BY-NC 4.0" in readme and "[MIT](LICENSE)" in readme
    notice = (ROOT / "NOTICE.md").read_text()
    assert "htmx" in notice and "Open Font License" in notice and "CC BY-NC 4.0" in notice


def test_license_change_ships_with_0_15_0_or_later():
    assert _ver(__version__) >= (0, 15, 0)


def test_footer_states_cc_by_nc_mit_and_commercial_licensing(client):
    for path in ("/", "/methodology", "/about"):
        html = client.get(path).text
        assert f'Scores <a href="{NC}" rel="license">CC BY-NC 4.0</a>' in html
        assert 'rel="license">MIT</a>' in html and '<a href="/about#commercial-licensing">Commercial licensing</a>' in html
        assert f'href="{BY}" rel="license"' not in html          # the current license is never shown as CC BY


def test_about_license_section(client, monkeypatch):
    about = client.get("/about").text
    assert 'id="license"' in about and NC + "legalcode" in about and "non-commercial" in about
    assert 'id="earlier-license"' in about and BY + "legalcode" in about and "0.5.0 through 0.14.0" in about
    assert 'id="commercial-licensing"' in about and "bulk or automated access" in about and "resale" in about
    assert "not covered by this license" in about and "MIT licensed" in about
    assert "a corrections address will be published here before launch" in about   # placeholder without CONTACT_EMAIL
    monkeypatch.setenv("CONTACT_EMAIL", "licensing@example.com")
    assert 'href="mailto:licensing@example.com"' in client.get("/about").text
