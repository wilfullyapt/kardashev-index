"""Licensing: files exist and the site states the data license (display only)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CC = "https://creativecommons.org/licenses/by/4.0/"


def test_license_files():
    mit = (ROOT / "LICENSE").read_text()
    assert mit.startswith("MIT License") and "Copyright (c) 2026 Wil Mawhinney" in mit
    data = (ROOT / "DATA-LICENSE.md").read_text()
    assert CC + "legalcode" in data and "CC BY 4.0" in data
    notice = (ROOT / "NOTICE.md").read_text()
    assert "htmx" in notice and "Open Font License" in notice
    assert "## License" in (ROOT / "README.md").read_text()


def test_footer_and_about_state_cc_by(client):
    for path in ("/", "/methodology", "/about"):
        html = client.get(path).text
        assert "Scores licensed" in html and f'href="{CC}" rel="license">CC BY 4.0' in html
    about = client.get("/about").text
    assert 'id="license"' in about and CC + "legalcode" in about
