"""Regression guard: security-relevant pins never drop below the versions that fixed known
advisories (pip-audit, 2026-10-09). Raise these floors, never lower them."""
import re
from pathlib import Path

FLOORS = {
    "starlette": (1, 3, 1), "python-multipart": (0, 0, 31), "pypdf": (6, 19, 0),
    "jinja2": (3, 1, 6), "lxml": (6, 1, 0), "python-dotenv": (1, 2, 2),
}


def _pins() -> dict[str, tuple[int, ...]]:
    out = {}
    for line in (Path(__file__).resolve().parent.parent / "requirements.txt").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)==([0-9.]+)", line.strip())
        if m:
            out[m.group(1).split("[")[0].lower()] = tuple(int(x) for x in m.group(2).split("."))
    return out


def test_security_floors():
    pins = _pins()
    for name, floor in FLOORS.items():
        assert name in pins, f"{name} must be pinned in requirements.txt"
        assert pins[name] >= floor, f"{name} {pins[name]} is below the fixed version {floor}"


def test_templates_render_with_request_first_signature(client):
    # Starlette 1.x: TemplateResponse(request, name, context); the old (name, context) form breaks
    for path in ("/", "/methodology", "/suggest", "/admin/login"):
        assert client.get(path).status_code == 200, path
