"""HANDOFF.md (1.0.0): present, linked from the README, covers every configured env var, no secrets."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = (ROOT / "HANDOFF.md").read_text()


def _env_example_names() -> set[str]:
    names = set()
    for line in (ROOT / ".env.example").read_text().splitlines():
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=", line)
        if m:
            names.add(m.group(1))
    return names


def test_linked_from_readme():
    assert "[HANDOFF.md](HANDOFF.md)" in (ROOT / "README.md").read_text()


def test_every_env_example_variable_is_documented():
    missing = sorted(n for n in _env_example_names() if f"`{n}`" not in HANDOFF and n not in HANDOFF)
    assert not missing, missing


def test_every_pipeline_setting_is_documented():
    src = (ROOT / "app" / "pipeline" / "config.py").read_text()
    names = set(re.findall(r'_(?:i|f|b)\("([A-Z0-9_]+)"', src)) | set(re.findall(r'os\.getenv\("([A-Z0-9_]+)"', src))
    missing = sorted(n for n in names if n not in HANDOFF)
    assert not missing, missing


def test_sections_and_key_facts():
    for heading in ("What the product is", "Architecture", "Repository layout", "Local development",
                    "Release process", "Deploys", "Environment variables", "Operations", "Monthly costs",
                    "Data licensing", "Launch checklist", "Known limitations", "Accounts and access", "Contacts"):
        assert heading in HANDOFF, heading
    for fact in ("3.12.15", "checksPass", "/health", "basic-256mb", "$0.40", "CC BY-NC 4.0",
                 "v0.5.0–v0.14.0", "lint`, `test`, `test-postgres", "fine-grained", "No un-retract"):
        assert fact in HANDOFF, fact


def test_no_secret_values():
    assert not re.search(r"(gh[pousr]_[A-Za-z0-9]{20,}|xai-[A-Za-z0-9]{20,}|\$2b\$12\$[./A-Za-z0-9]{50,}|"
                         r"postgres(?:ql)?://[^\s:]+:[^\s@]+@)", HANDOFF)
