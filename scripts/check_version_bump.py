#!/usr/bin/env python3
"""CI guard: a change that alters what a score means must ship with an app version bump.

The check fails when the PR (diff against BASE, default origin/main):
  * changes PIPELINE_VERSION / WEIGHTS_VERSION / RUBRIC_VERSION (app/pipeline/methodology.py)
    or PROMPT_VERSION (app/pipeline/prompts.py), or
  * adds a database migration under alembic/versions/,
and does not bump app/version.py by at least a MINOR step and add a CHANGELOG.md section for the
new version. A bump to app/version.py on its own also needs a matching CHANGELOG.md section.

Usage: python scripts/check_version_bump.py [BASE_REF]
"""
from __future__ import annotations

import re
import subprocess
import sys

COMPONENTS = {
    "app/pipeline/methodology.py": ("PIPELINE_VERSION", "WEIGHTS_VERSION", "RUBRIC_VERSION"),
    "app/pipeline/prompts.py": ("PROMPT_VERSION",),
}
VERSION_FILE = "app/version.py"
MIGRATIONS_DIR = "alembic/versions/"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def show(ref: str, path: str) -> str:
    try:
        return git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return ""


def const(src: str, name: str) -> str | None:
    m = re.search(rf'^{name}\s*=\s*["\']([^"\']+)["\']', src, re.MULTILINE)
    return m.group(1) if m else None


def parse(v: str | None) -> tuple[int, int, int] | None:
    m = SEMVER.match(v or "")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    merge_base = git("merge-base", base, "HEAD").strip()

    reasons: list[str] = []
    for path, names in COMPONENTS.items():
        old, new = show(merge_base, path), show("HEAD", path)
        for n in names:
            if const(old, n) != const(new, n):
                reasons.append(f"{n} changed: {const(old, n)} -> {const(new, n)}")
    added = git("diff", "--name-only", "--diff-filter=A", merge_base, "HEAD", "--", MIGRATIONS_DIR)
    for f in added.split():
        if f.endswith(".py"):
            reasons.append(f"new migration: {f}")

    old_v = const(show(merge_base, VERSION_FILE), "__version__")
    new_v = const(show("HEAD", VERSION_FILE), "__version__")
    changelog = show("HEAD", "CHANGELOG.md")
    errors: list[str] = []

    if new_v is None or parse(new_v) is None:
        errors.append(f"{VERSION_FILE} must define __version__ = 'X.Y.Z' (got {new_v!r}).")
    bumped = old_v != new_v
    if reasons:
        o, n = parse(old_v), parse(new_v)
        if not bumped:
            errors.append(f"{VERSION_FILE} was not bumped (still {new_v}).")
        elif o and n and n[:2] <= o[:2]:
            errors.append(f"needs at least a MINOR bump ({old_v} -> {new_v}); see docs/RELEASING.md.")
    if (reasons or bumped) and new_v and f"## [{new_v}]" not in changelog:
        errors.append(f"CHANGELOG.md has no '## [{new_v}]' section.")

    print(f"app version: {old_v} -> {new_v}")
    for r in reasons:
        print(f"  requires bump: {r}")
    if errors:
        print("\nversion-check FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("version-check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
