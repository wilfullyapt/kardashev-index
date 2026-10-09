"""Reading PDFs that are too large for the normal fetch (Bug 1, part 3).

Some primary sources are huge: Tesla's 2024 Extended Impact Report is ~129 MB, over FETCH_MAX_BYTES
(30 MB), so today it is rejected as "PDF larger than cap". When that happens the fetch stage calls
``read_large_pdf``, which reads only the pages that matter:

1. **HTTP Range (option 2).** If the server answers ``Range: bytes=0-1023`` with 206 and a total size,
   the PDF is opened through ``RangeFile``: a read-only file object that downloads 256 KB blocks on
   demand (small LRU cache). pypdf reads the cross-reference table at the end of the file and then only
   the objects of the pages we extract, so typically a few MB of a 129 MB file are transferred.
2. **Streamed to disk (option 1, fallback).** If ranges are not supported (or the range read fails or
   hits its byte budget), the file is streamed to a temporary file on disk in 1 MB chunks, never held
   in memory, up to LARGE_PDF_MAX_DOWNLOAD_MB, after checking free disk space. pypdf opens the file
   *object* (a path would be read fully into memory) and parses lazily.

In both modes only pages that look relevant are extracted: pages whose outline (bookmark) titles
mention energy/environment/data/appendix/GRI/SASB/CDP first, then a scan backwards from the last page
through the last 40% (data appendices sit at the end), keeping pages with energy-figure keywords.

Safeguards for the 512 MB / 0.5 CPU Render instance (the web process also runs the worker):
- **Separate process.** All of the above runs in a child Python process (``python -m
  app.pipeline.largepdf``). A crash or out-of-memory there cannot take down the web process.
- **Memory cap.** The child runs under ``RLIMIT_AS`` = LARGE_PDF_MEMORY_MB (default 256 MB of address
  space; an idle child with pypdf is ~50 MB). Exceeding it raises MemoryError in the child (or kills
  it); the parent records "memory cap reached" and moves on. RangeFile's cache is ≤ 8 MB; downloads go
  to disk in 1 MB chunks; pypdf's per-stream decompression cap is lowered from 75 MB to
  LARGE_PDF_STREAM_MB (16 MB) and its object cache is cleared after every page. A page that still
  hits the cap is skipped (more than 5 such pages end the scan).
- **One at a time.** A process-wide semaphore allows a single large-PDF child; a second request while
  one is running is skipped (recorded as busy), so parallel fetch threads can never start several.
- **Time caps.** Wall clock LARGE_PDF_TIMEOUT_S (default 150 s; the child is killed after it),
  ``RLIMIT_CPU`` LARGE_PDF_CPU_S (default 90 s of CPU), and an internal deadline (LARGE_PDF_DEADLINE_S,
  120 s wall, or 80% of the CPU limit) that stops scanning early and returns the pages found so far. The child runs at ``nice`` +10 so page requests win the 0.5 CPU.
- **Page and output caps.** At most LARGE_PDF_SCAN_PAGES pages are examined (default 120;
  scanning also stops LARGE_PDF_QUIET_PAGES (40) pages after the last hit) and
  LARGE_PDF_KEEP_PAGES kept (default 30); output text ≤ LARGE_PDF_MAX_CHARS (default 400,000).
- **Byte caps.** Range mode stops after LARGE_PDF_RANGE_BUDGET_MB transferred (default 48 MB);
  disk mode refuses files over LARGE_PDF_MAX_DOWNLOAD_MB (default 200 MB) or when free disk is below
  twice that, and always deletes its temp file.
- **Same SSRF rules** as the normal fetcher: every URL and redirect is checked with
  ``check_url_allowed`` (public http(s) only), redirects are followed manually (max 5).
Set LARGE_PDF_ENABLED=0 to switch the whole path off.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Limits:
    enabled: bool = field(default_factory=lambda: os.getenv("LARGE_PDF_ENABLED", "1").strip().lower()
                          not in ("0", "false", "no", "off"))
    memory_mb: int = field(default_factory=lambda: int(_env_f("LARGE_PDF_MEMORY_MB", 256)))
    timeout_s: float = field(default_factory=lambda: _env_f("LARGE_PDF_TIMEOUT_S", 150))
    cpu_s: int = field(default_factory=lambda: int(_env_f("LARGE_PDF_CPU_S", 90)))
    deadline_s: float = field(default_factory=lambda: _env_f("LARGE_PDF_DEADLINE_S", 120))
    range_budget_mb: float = field(default_factory=lambda: _env_f("LARGE_PDF_RANGE_BUDGET_MB", 48))
    max_download_mb: float = field(default_factory=lambda: _env_f("LARGE_PDF_MAX_DOWNLOAD_MB", 200))
    scan_pages: int = field(default_factory=lambda: int(_env_f("LARGE_PDF_SCAN_PAGES", 120)))
    quiet_pages: int = field(default_factory=lambda: int(_env_f("LARGE_PDF_QUIET_PAGES", 40)))
    keep_pages: int = field(default_factory=lambda: int(_env_f("LARGE_PDF_KEEP_PAGES", 30)))
    max_chars: int = field(default_factory=lambda: int(_env_f("LARGE_PDF_MAX_CHARS", 400_000)))
    stream_mb: float = field(default_factory=lambda: _env_f("LARGE_PDF_STREAM_MB", 16))
    http_timeout_s: float = 20.0


@dataclass
class Result:
    ok: bool
    text: str = ""
    method: str | None = None          # range | disk
    total_bytes: int | None = None
    bytes_read: int = 0
    pages_total: int | None = None
    pages_scanned: int = 0
    pages_kept: list[int] = field(default_factory=list)
    pages_skipped: int = 0
    title: str = ""
    reason: str | None = None

    def note(self) -> str:
        if not self.ok:
            return f"large PDF not read: {self.reason}"
        mb = (self.total_bytes or 0) / 1e6
        return (f"large PDF ({mb:,.0f} MB, {self.pages_total} pages) read via "
                f"{'HTTP Range' if self.method == 'range' else 'streamed download'}: "
                f"{len(self.pages_kept)} relevant pages kept of {self.pages_scanned} scanned, "
                f"{self.bytes_read / 1e6:,.1f} MB transferred")


# ---------------------------------------------------------------- page selection

ENERGY_PAGE = re.compile(
    r"(energy (?:consumption|use|usage)|electricity (?:consumption|use|purchased)|total energy|"
    r"\b\d[\d,.]*\s*(?:GJ|TJ|PJ|MWh|GWh|TWh|kWh)\b|gigajoules?|megawatt[- ]hours?)", re.IGNORECASE)
OUTLINE_HINT = re.compile(r"energy|electric|environment|climate|emission|data|appendix|metrics|performance|"
                          r"\bgri\b|sasb|tcfd|\bcdp\b|index|kpi", re.IGNORECASE)


class Deadline(Exception):
    pass


def _outline_pages(reader, limit: int = 60) -> list[int]:
    pages: list[int] = []

    def walk(items):
        for it in items:
            if len(pages) >= limit:
                return
            if isinstance(it, list):
                walk(it)
                continue
            try:
                if OUTLINE_HINT.search(str(it.title or "")):
                    n = reader.get_destination_page_number(it)
                    if n is not None and n >= 0:
                        pages.extend([n, n + 1, n + 2])
            except Exception as e:
                log.debug("outline entry skipped: %s", e)
    try:
        walk(reader.outline)
    except Exception:
        return []
    return pages


def page_order(n_pages: int, outline: list[int]) -> list[int]:
    """Outline hits first, then the last 40% of the document from the end backwards (data
    appendices and key-metrics tables sit at the very end), then the rest; no duplicates."""
    seen: set[int] = set()
    order: list[int] = []
    tail_start = int(n_pages * 0.6)
    for p in [*outline, *range(n_pages - 1, tail_start - 1, -1), *range(tail_start)]:
        if 0 <= p < n_pages and p not in seen:
            seen.add(p)
            order.append(p)
    return order


def _decode_caps(limits: Limits):
    """pypdf decompression caps per stream (default 75 MB each) lowered to LARGE_PDF_STREAM_MB, so a
    single huge vector-art content stream is refused instead of allocating tens of MB."""
    import contextlib
    try:
        from pypdf import apply_configuration
    except ImportError:            # older pypdf: keep its defaults; RLIMIT_AS still applies
        return contextlib.nullcontext()
    n = int(limits.stream_mb * 1_000_000)
    return apply_configuration(zlib_maximum_output_length=n, lzw_maximum_output_length=n,
                                   run_length_maximum_output_length=n, brotli_maximum_output_length=n,
                                   jbig2_maximum_output_length=n, image_maximum_buffer_size=n)


def extract_relevant(stream, limits: Limits, deadline: float, res: Result, *, strict: bool = False) -> Result:
    """Open ``stream`` (file object) with pypdf and keep the relevant pages' text.

    ``strict=True`` is used over HTTP Range: in non-strict mode pypdf visits every object header to
    validate the cross-reference table, which would touch every part of the file."""
    with _decode_caps(limits):
        return _extract(stream, limits, deadline, res, strict)


def _out_of_time(limits: Limits, deadline: float) -> bool:
    # stop well before RLIMIT_CPU (which would kill the child and lose the pages found so far)
    return time.monotonic() > deadline or time.process_time() > 0.8 * limits.cpu_s


def _extract(stream, limits: Limits, deadline: float, res: Result, strict: bool) -> Result:
    from pypdf import PdfReader
    reader = PdfReader(stream, strict=strict)
    n = len(reader.pages)
    res.pages_total = n
    try:
        res.title = str((reader.metadata or {}).get("/Title") or "")[:200]
    except Exception:
        res.title = ""
    kept: list[tuple[int, str]] = []
    chars = 0
    since_hit = 0
    for p in page_order(n, _outline_pages(reader)):
        if res.pages_scanned >= limits.scan_pages or len(kept) >= limits.keep_pages:
            break
        if kept and since_hit >= limits.quiet_pages:
            break                                   # found the data pages; the rest is narrative
        if _out_of_time(limits, deadline):
            res.reason = "time limit reached while scanning; partial result"
            break
        res.pages_scanned += 1
        since_hit += 1
        try:
            t = reader.pages[p].extract_text() or ""
        except BudgetExceeded:
            if not kept:
                raise                               # nothing yet: let the caller fall back to disk
            res.reason = "range byte budget reached; partial result"
            break
        except MemoryError:
            # one oversized page hit the memory cap: the allocation failed, nothing was kept, so
            # skip the page and continue (repeated failures end the scan)
            res.pages_skipped += 1
            if res.pages_skipped > 5:
                res.reason = "memory cap reached on several pages; partial result"
                break
            continue
        except Exception as e:
            log.debug("page %d not extracted: %s", p + 1, e)
            continue
        finally:
            # pypdf caches every parsed object; dropping the cache after each page keeps memory flat
            # (Tesla's 210-page report: ~80 MB peak instead of ~235 MB)
            cache = getattr(reader, "resolved_objects", None)
            if isinstance(cache, dict):
                cache.clear()
        if ENERGY_PAGE.search(t):
            t = re.sub(r"\s+", " ", t).strip()
            kept.append((p, t))
            since_hit = 0
            chars += len(t)
            if chars > limits.max_chars:
                break
    kept.sort()
    res.pages_kept = [p + 1 for p, _ in kept]
    res.text = " ".join(f"[page {p + 1}] {t}" for p, t in kept)[: limits.max_chars]
    res.ok = bool(kept)
    if not kept and not res.reason:
        res.reason = f"no page with an energy figure among {res.pages_scanned} scanned"
    return res


class BudgetExceeded(Exception):
    pass


# ---------------------------------------------------------------- transport (child process)

def _client(timeout: float, transport=None) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)


def _resolve_final(client, url: str, headers: dict, resolve) -> tuple[httpx.Response, str]:
    """GET with manual, SSRF-checked redirects. Returns (open streaming response, final url)."""
    from .fetch import MAX_REDIRECTS, check_url_allowed
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url_allowed(current, resolve)
        req = client.build_request("GET", current, headers=headers)
        resp = client.send(req, stream=True)
        if resp.is_redirect and resp.headers.get("location"):
            resp.close()
            current = urljoin(current, resp.headers["location"])
            continue
        return resp, current
    raise httpx.TooManyRedirects("too many redirects")


def _base_headers() -> dict:
    from .fetch import BROWSER_HEADERS
    return {**BROWSER_HEADERS, "Accept-Encoding": "identity", "Accept": "application/pdf,*/*;q=0.5"}


class RangeFile:
    """Seekable read-only file over HTTP Range requests, with a bounded block cache and byte budget."""

    def __init__(self, client, url: str, total: int, *, budget: int, block: int = 256 * 1024,
                 cache_blocks: int = 32, resolve=None, sleep=time.sleep):
        self.client, self.url, self.total = client, url, total
        self.block, self.cache_blocks, self.budget = block, cache_blocks, budget
        self.pos = 0
        self.bytes_read = 0
        self.mode = "rb"
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._resolve = resolve
        self._sleep = sleep

    def _fetch(self, idx: int) -> bytes:
        if idx in self._cache:
            self._cache.move_to_end(idx)
            return self._cache[idx]
        start = idx * self.block
        end = min(self.total, start + self.block) - 1
        if self.bytes_read + (end - start + 1) > self.budget:
            raise BudgetExceeded(f"range budget of {self.budget // 1_000_000} MB reached")
        from .fetch import check_url_allowed
        check_url_allowed(self.url, self._resolve)
        hdrs = {**_base_headers(), "Range": f"bytes={start}-{end}"}
        r = self.client.get(self.url, headers=hdrs)
        if r.status_code in (429, 503):            # one polite retry (archive.org rate-limits)
            ra = r.headers.get("retry-after", "")
            self._sleep(min(float(ra) if ra.isdigit() else 2.0, 5.0))
            r = self.client.get(self.url, headers=hdrs)
        if r.status_code != 206:
            raise OSError(f"range request answered HTTP {r.status_code}")
        data = r.content[: end - start + 1]
        self.bytes_read += len(data)
        self._cache[idx] = data
        while len(self._cache) > self.cache_blocks:
            self._cache.popitem(last=False)
        return data

    # file protocol used by pypdf
    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.total + offset
        self.pos = max(0, min(self.pos, self.total))
        return self.pos

    def tell(self) -> int:
        return self.pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.total - self.pos
        n = min(n, self.total - self.pos)
        out = bytearray()
        while n > 0:
            idx, off = divmod(self.pos, self.block)
            chunk = self._fetch(idx)[off: off + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)


def probe_range(client, url: str, resolve) -> tuple[str, int | None, bool]:
    """(final url, total size or None, ranges supported?)."""
    resp, final = _resolve_final(client, url, {**_base_headers(), "Range": "bytes=0-1023"}, resolve)
    try:
        total = None
        cr = resp.headers.get("content-range", "")
        m = re.match(r"bytes \d+-\d+/(\d+)", cr)
        if resp.status_code == 206 and m:
            total = int(m.group(1))
            return final, total, True
        if resp.headers.get("content-length", "").isdigit():
            total = int(resp.headers["content-length"])
        return final, total, False
    finally:
        resp.close()


def stream_to_disk(client, url: str, path: str, max_bytes: int, resolve) -> int:
    resp, _ = _resolve_final(client, url, _base_headers(), resolve)
    size = 0
    try:
        if resp.status_code != 200:
            raise OSError(f"download answered HTTP {resp.status_code}")
        with open(path, "wb") as fh:
            for chunk in resp.iter_bytes(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise BudgetExceeded(f"file larger than {max_bytes // 1_000_000} MB download cap")
                fh.write(chunk)
    finally:
        resp.close()
    return size


def read_in_child(url: str, limits: Limits, *, transport=None, resolve=None, tmpdir: str | None = None) -> Result:
    """The child's work (also callable in-process in tests with a mock transport)."""
    import socket
    resolve = resolve or socket.getaddrinfo
    deadline = time.monotonic() + limits.deadline_s
    res = Result(ok=False)
    with _client(limits.http_timeout_s, transport) as client:
        final, total, ranged = probe_range(client, url, resolve)
        res.total_bytes = total
        if ranged and total:
            rf = RangeFile(client, final, total, budget=int(limits.range_budget_mb * 1e6), resolve=resolve)
            try:
                res.method = "range"
                extract_relevant(rf, limits, deadline, res, strict=True)
                res.bytes_read = rf.bytes_read
                return res
            except Exception as e:   # budget, HTTP or pypdf errors: fall back to disk
                if isinstance(e, MemoryError):
                    raise
                res = Result(ok=False, total_bytes=total, bytes_read=rf.bytes_read,
                             reason=f"range read failed ({type(e).__name__}: {str(e)[:120]}); trying download")
        cap = int(limits.max_download_mb * 1e6)
        if total and total > cap:
            res.reason = f"file is {total // 1_000_000} MB, over the {cap // 1_000_000} MB download cap"
            return res
        d = tmpdir or tempfile.gettempdir()
        if shutil.disk_usage(d).free < 2 * cap:
            res.reason = "not enough free disk for a streamed download"
            return res
        fd, path = tempfile.mkstemp(prefix="ki-largepdf-", suffix=".pdf", dir=d)
        os.close(fd)
        try:
            got = stream_to_disk(client, final, path, cap, resolve)
            res.method, res.total_bytes, res.bytes_read = "disk", got, res.bytes_read + got
            with open(path, "rb") as fh:          # file object: pypdf parses lazily, never loads it all
                extract_relevant(fh, limits, deadline, res)
            return res
        except (BudgetExceeded, OSError, httpx.HTTPError) as e:
            res.reason = str(e)[:200] or type(e).__name__
            return res
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------- parent side

_SLOT = threading.BoundedSemaphore(1)     # one large-PDF child at a time, process-wide


def apply_child_limits(limits: Limits) -> None:
    """Run first thing in the child, before pypdf is imported or anything is downloaded. (The child
    limits itself rather than using ``preexec_fn``, which is unsafe in the multi-threaded parent.)"""
    import resource
    mem = limits.memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_s, limits.cpu_s + 5))
    os.nice(10)
    logging.getLogger("pypdf").setLevel(logging.ERROR)    # keep stderr small


def run_child(args: list[str], limits: Limits) -> Result:
    """Run the child with the memory/CPU/time limits and parse its JSON line."""
    cmd = [sys.executable, "-m", "app.pipeline.largepdf", *args]
    env = {**os.environ, "LARGE_PDF_LIMITS": json.dumps(asdict(limits))}
    try:
        p = subprocess.run(cmd, capture_output=True, check=False, timeout=limits.timeout_s, env=env,
                           stdin=subprocess.DEVNULL, cwd=os.path.dirname(os.path.dirname(
                               os.path.dirname(os.path.abspath(__file__)))))
    except subprocess.TimeoutExpired:
        return Result(ok=False, reason=f"time limit: stopped after {limits.timeout_s:.0f}s")
    out = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    if p.returncode != 0 or not out:
        err = (p.stderr or b"").decode("utf-8", "replace")
        if "MemoryError" in err or p.returncode in (-9, 137):
            why = f"memory cap ({limits.memory_mb} MB) reached"
        elif p.returncode in (-24, 152) or "CPU" in err:
            why = f"CPU limit ({limits.cpu_s}s) reached"
        else:
            why = f"reader exited with code {p.returncode}: {err.strip().splitlines()[-1][:160] if err.strip() else ''}"
        return Result(ok=False, reason=why)
    try:
        return Result(**json.loads(out[-1]))
    except (ValueError, TypeError) as e:
        return Result(ok=False, reason=f"unreadable reader output ({e})")


def read_large_pdf(url: str, limits: Limits | None = None) -> Result:
    limits = limits or Limits()
    if not limits.enabled:
        return Result(ok=False, reason="large-PDF reading disabled (LARGE_PDF_ENABLED=0)")
    if not _SLOT.acquire(blocking=False):
        return Result(ok=False, reason="another large PDF is being read; skipped to protect memory")
    try:
        return run_child(["--url", url], limits)
    finally:
        _SLOT.release()


def rescue_oversized(url: str, res, a, reader=None):
    """Called by the fetch stage for a source rejected as an over-cap PDF. Returns an ``ok`` Assessment
    with only the relevant pages' text, or the original rejection with the reason appended."""
    from .fetch import Assessment, Snapshot, is_pdf_body
    if a.status != "error" or not res.truncated or not is_pdf_body(res.content_type, res.body):
        return a
    res.body = b""                       # drop the 30 MB partial buffer before reading
    out = (reader or read_large_pdf)(res.final_url or url)
    if not out.ok:
        return Assessment(a.status, f"{a.reason}; {out.note()}"[:500], a.snapshot)
    import hashlib
    snap = Snapshot(title=out.title, text=out.text, sha256=hashlib.sha256(out.text.encode()).hexdigest(),
                    byte_size=out.total_bytes or 0)
    return Assessment("ok", out.note()[:500], snap)


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Read the relevant pages of a large PDF (child process).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url")
    g.add_argument("--file", help="a local PDF (debugging and tests); no network")
    a = ap.parse_args(argv)
    raw = os.getenv("LARGE_PDF_LIMITS")
    limits = Limits(**json.loads(raw)) if raw else Limits()
    apply_child_limits(limits)
    try:
        if a.file:
            res = Result(ok=False, method="disk", total_bytes=os.path.getsize(a.file))
            with open(a.file, "rb") as fh:
                extract_relevant(fh, limits, time.monotonic() + limits.deadline_s, res)
        else:
            res = read_in_child(a.url, limits)
    except MemoryError:
        print("MemoryError", file=sys.stderr)
        return 3
    except Exception as e:
        if re.search(r"map segment|allocate memory|Cannot allocate", str(e)):
            print("MemoryError", file=sys.stderr)      # address-space cap hit while loading a module
            return 3
        res = Result(ok=False, reason=f"{type(e).__name__}: {str(e)[:160]}")
    print(json.dumps(asdict(res)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
