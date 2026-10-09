"""Trust & disclosure helpers: contact address, scoring-model name, measured-vs-judged share.

Display only. Nothing here feeds scoring, ranking or publication rules.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .pipeline import methodology as meth

_EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")
CONTACT_PLACEHOLDER = "a corrections address will be published here before launch"


def contact_email() -> str | None:
    """CONTACT_EMAIL if it looks like a plain address, else None (templates show placeholder text)."""
    raw = (os.getenv("CONTACT_EMAIL") or "").strip()
    return raw if _EMAIL_RE.match(raw) else None


def scoring_model() -> str:
    from .pipeline.config import settings
    return settings().model


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)


def measured_share(items: Iterable[Any] | Mapping[str, Any] | None) -> float | None:
    """Share of the *scored* weight that comes from measured categories (0..1), or None if nothing scored.

    Accepts CategoryScore rows, dicts, or a {category: row} mapping. Uses the row's weight, falling back
    to the published weights for its category.
    """
    if not items:
        return None
    if isinstance(items, Mapping):
        items = items.values()
    measured = total = 0.0
    for it in items:
        if _get(it, "score") is None:
            continue
        cat = _get(it, "category") or _get(it, "key")
        w = _get(it, "weight")
        if w is None:
            w = meth.WEIGHTS.get(cat, 0.0)
        fam = _get(it, "family") or ("measured" if cat in meth.MEASURED_KEYS else "judged")
        total += w
        if fam == meth.MEASURED:
            measured += w
    return measured / total if total > 0 else None
