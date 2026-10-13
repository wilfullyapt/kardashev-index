"""Process memory helpers (1.14.0): resident-set size for run summaries and /health, and returning
freed memory to the OS between runs.

Linux reads /proc/self/status (VmRSS = now, VmHWM = peak since the process started); elsewhere
falls back to getrusage's ru_maxrss. All functions are best-effort and never raise.
"""
from __future__ import annotations

import ctypes
import gc
import logging
import resource
import sys

log = logging.getLogger("kardashev.mem")


def _status_kb(field: str) -> int | None:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _maxrss_mb(who=resource.RUSAGE_SELF) -> float:
    r = resource.getrusage(who).ru_maxrss
    return round(r / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def rss_mb() -> float | None:
    """Resident memory right now, MB."""
    kb = _status_kb("VmRSS")
    return round(kb / 1024, 1) if kb is not None else None


def peak_rss_mb() -> float:
    """Highest resident memory of this process since it started, MB."""
    kb = _status_kb("VmHWM")
    return round(kb / 1024, 1) if kb is not None else _maxrss_mb()


def maxrss_kb_to_mb(kb: int) -> float:
    return round(kb / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def release() -> dict:
    """gc.collect() then glibc malloc_trim(0), so freed arenas go back to the OS instead of keeping
    the process's RSS at its high-water mark. Returns RSS before/after (MB)."""
    before = rss_mb()
    try:
        gc.collect()
    except Exception:  # pragma: no cover
        pass
    trimmed = False
    try:
        trimmed = bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
    except (OSError, AttributeError):
        pass          # not glibc (macOS, musl): gc alone
    return {"rss_before_mb": before, "rss_after_mb": rss_mb(), "malloc_trim": trimmed}
