"""The app version is SemVer, has a changelog section, and is what /health reports."""
import re
from pathlib import Path

from app.version import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_changelog_has_section_for_current_version():
    assert f"## [{__version__}]" in (ROOT / "CHANGELOG.md").read_text()


def test_pyproject_reads_version_from_app_version():
    assert 'attr = "app.version.__version__"' in (ROOT / "pyproject.toml").read_text()


def test_health_reports_version(client):
    r = client.get("/health")
    assert r.json()["version"] == __version__
