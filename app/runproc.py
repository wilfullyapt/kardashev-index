"""One judgment run in its own process (1.14.0): ``python -m app.runproc <run_id>``.

The in-process worker (app.worker, still inside the web process) claims the run, then spawns this
child and supervises it: heartbeat, shutdown, and what happens if the child dies. The child opens its
own database connection, builds fresh production deps and calls ``execute_run`` - so checkpoints,
automatic retries, defers and energy retries behave exactly as in-process. When it exits, every byte
the run allocated (page bodies, parse trees, pypdf objects, caches) goes back to the OS; an
out-of-memory kill takes down only this run, which the supervisor re-queues (error_type "oom").

SIGTERM (graceful shutdown) asks the run to stop before its next stage; it is then re-queued at its
checkpoint ("paused for shutdown"), the same as the in-process drain.

WORKER_CHILD_DEPS="module:function" swaps the deps factory (tests only; default app.deps:build_deps).
Header-profile memory (which hosts need browser headers) is kept across runs in
FETCH_PROFILES_FILE (default <tmp>/kardashev-host-profiles.json).
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time

log = logging.getLogger("kardashev.runproc")
PROFILES_MAX_HOSTS = 2000


def profiles_path() -> str:
    return os.getenv("FETCH_PROFILES_FILE") or os.path.join(tempfile.gettempdir(), "kardashev-host-profiles.json")


def load_profiles(profiles, path: str | None = None) -> int:
    """Seed HOST_PROFILES from the file written by the previous run (entries keep their age)."""
    try:
        with open(path or profiles_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    now_wall, now_mono, n = time.time(), profiles._clock(), 0
    for host, (profile, at_wall) in list(data.items())[:PROFILES_MAX_HOSTS]:
        if profile in ("bot", "browser") and isinstance(at_wall, (int, float)):
            with profiles._lock:
                profiles._m[host] = (profile, now_mono - max(0.0, now_wall - at_wall))
            n += 1
    return n


def save_profiles(profiles, path: str | None = None) -> None:
    now_wall, now_mono = time.time(), profiles._clock()
    with profiles._lock:
        items = sorted(profiles._m.items(), key=lambda kv: kv[1][1], reverse=True)[:PROFILES_MAX_HOSTS]
    data = {h: (p, now_wall - (now_mono - t)) for h, (p, t) in items if now_mono - t < profiles.ttl_s}
    path = path or profiles_path()
    try:
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        log.warning("could not save header profiles to %s", path)


def _deps_factory():
    spec = os.getenv("WORKER_CHILD_DEPS")
    if not spec:
        from .deps import build_deps
        return build_deps
    mod, _, fn = spec.partition(":")
    return getattr(importlib.import_module(mod), fn)


def main(argv: list[str]) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    if len(argv) != 1 or not argv[0].isdigit():
        print("usage: python -m app.runproc <run_id>", file=sys.stderr)
        return 2
    run_id = int(argv[0])
    os.environ["KI_RUN_MODE"] = "subprocess"
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    from .db import SessionLocal
    from .pipeline.config import worker_settings
    from .pipeline.fetch import HOST_PROFILES
    from .pipeline.runner import execute_run

    load_profiles(HOST_PROFILES)
    deps = _deps_factory()()
    if deps.retry is None:
        deps.retry = worker_settings()
    if deps.should_stop is None:
        deps.should_stop = stop.is_set
    with SessionLocal() as db:
        run = execute_run(db, run_id, deps)
        status = run.status
    save_profiles(HOST_PROFILES)
    print(json.dumps({"run_id": run_id, "status": status}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
