"""Fetch every cited source ourselves: SSRF-guarded GET, text extraction (HTML/PDF/plain),
sha256 snapshot, and dead-link / soft-404 detection (incl. a random-path probe per host).

Robustness: browser-like headers; retries with backoff (honouring Retry-After) for 429/5xx/
timeouts/transport errors; SEC hosts get the declared SEC_EDGAR_USER_AGENT and are rate limited
below SEC's 10 requests/second; bodies are streamed with a size cap; PDF parsing is bounded
(pages, characters, concurrent parses) with an optional second parser; and a Wayback Machine
snapshot can stand in for a page that refuses us (401/403/404/410/451), clearly labelled."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpx
from rapidfuzz import fuzz

from .llm import backoff_s, retry_after_s

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
MAX_REDIRECTS = 5
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
ARCHIVE_STATUSES = {401, 403, 404, 410, 451}
# CDX lists captures; filtered to HTTP 200 so an archived block page (e.g. Akamai's 403) is never picked.
WAYBACK_CDX = ("https://web.archive.org/cdx/search/cdx?url={url}&output=json"
               "&fl=timestamp,original,mimetype,statuscode,length&filter=statuscode:200&limit=-{n}")
# Fallback only: web.archive.org/wayback/available answers 404, and this host often answers empty.
WAYBACK_API = "https://archive.org/wayback/available?url={url}"
WAYBACK_RAW = "https://web.archive.org/web/{ts}id_/{url}"
WAYBACK_CAPTURES = 3            # newest 200 captures tried per URL
WAYBACK_MAX_REQUESTS = 16       # per run, all archive.org requests together
# Bot walls and archive outages. Matched on the title and the start of the text of a candidate copy.
BLOCK_PAGE_RE = re.compile(
    r"access denied|you don.t have permission to access|errors\.edgesuite\.net|attention required|"
    r"just a moment\.\.\.|checking your browser|cf-browser-verification|request rejected|"
    r"pardon our interruption|are you a robot|captcha|temporarily offline|too many requests",
    re.IGNORECASE)
PDF_MAX_PAGES = 400
PDF_MAX_CHARS = 3_000_000
_PDF_SLOTS = threading.BoundedSemaphore(2)    # bound memory: at most two PDFs parsed at once


def is_sec_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "sec.gov" or host.endswith(".sec.gov")


class RateLimiter:
    """Minimum interval between requests (thread-safe). SEC asks for at most 10 requests/second."""

    def __init__(self, per_second: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep

    def wait(self):
        with self._lock:
            now = self._clock()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            self._sleep(delay)


SEC_LIMITER = RateLimiter(8)
MIN_TEXT_CHARS = 300
SOFT_404_RE = re.compile(
    r"\b(404|page not found|not be found|cannot be found|could not be found|no longer available|"
    r"page (?:does not|doesn't) exist|nothing was found|error 404|we can.t find)\b", re.IGNORECASE)


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    content_type: str = ""
    body: bytes = b""
    error: str | None = None
    redirects: list[str] = field(default_factory=list)
    retry_after: float | None = None
    truncated: bool = False
    attempts: int = 1
    archived_from: str | None = None     # original URL when this is a Wayback Machine copy
    archive_timestamp: str | None = None  # YYYYMMDDhhmmss of the snapshot


class Fetcher(Protocol):
    def get(self, url: str, *, headers: dict | None = None, max_bytes: int | None = None) -> FetchResult: ...


class BlockedURL(Exception):
    pass


def check_url_allowed(url: str, resolve=socket.getaddrinfo) -> None:
    """Only public http(s) on default ports; refuse private, loopback, link-local, reserved IPs."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme not allowed: {parts.scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise BlockedURL("no host")
    if parts.port not in (None, 80, 443):
        raise BlockedURL(f"port not allowed: {parts.port}")
    if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal", ".localhost")):
        raise BlockedURL("local host name")
    try:
        infos = resolve(host, None)
    except OSError as e:
        raise BlockedURL(f"DNS failed: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise BlockedURL(f"non-public address {ip}")


class HttpFetcher:
    def __init__(self, *, timeout_s: float = 20, max_bytes: int = 30_000_000, retries: int = 2,
                 sec_user_agent: str | None = None, transport: httpx.BaseTransport | None = None,
                 resolve=socket.getaddrinfo, sleep=time.sleep, sec_limiter: RateLimiter | None = None):
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.retries = max(0, retries)
        self.sec_user_agent = sec_user_agent
        self._transport = transport
        self._resolve = resolve
        self._sleep = sleep
        self._sec = sec_limiter or SEC_LIMITER

    def get(self, url: str, *, headers: dict | None = None, max_bytes: int | None = None,
            retries: int | None = None) -> FetchResult:
        """GET with retries for transient failures (429/5xx/timeouts/transport), backing off
        exponentially and honouring Retry-After (capped at 20 s). ``retries=0`` turns this off for
        callers that pace and back off themselves (the Wayback fallback), so every hit is counted."""
        res = self._get_once(url, headers=headers, max_bytes=max_bytes)
        for attempt in range(1, (self.retries if retries is None else max(0, retries)) + 1):
            transient = (res.status in RETRY_STATUSES or
                         (res.status is None and res.error and not res.error.startswith("blocked")))
            if not transient:
                break
            self._sleep(backoff_s(attempt, base=2.0, cap=20.0, retry_after=res.retry_after))
            res = self._get_once(url, headers=headers, max_bytes=max_bytes)
            res.attempts = attempt + 1
        return res

    def _get_once(self, url: str, *, headers: dict | None = None, max_bytes: int | None = None) -> FetchResult:
        cap = max_bytes or self.max_bytes
        result = FetchResult(url=url)
        hdrs = dict(BROWSER_HEADERS)
        hdrs.update(headers or {})
        current = url
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout_s, follow_redirects=False) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    check_url_allowed(current, self._resolve)
                    h = hdrs
                    if is_sec_host(current):
                        # SEC requires a declared User-Agent with contact details and <= 10 req/s.
                        self._sec.wait()
                        if self.sec_user_agent and "User-Agent" not in (headers or {}):
                            h = {**hdrs, "User-Agent": self.sec_user_agent, "Sec-Fetch-Site": "none"}
                    with client.stream("GET", current, headers=h) as resp:
                        if resp.is_redirect and resp.headers.get("location"):
                            result.redirects.append(current)
                            current = urljoin(current, resp.headers["location"])
                            continue
                        result.status = resp.status_code
                        result.final_url = current
                        result.retry_after = retry_after_s(resp.headers.get("retry-after"))
                        result.content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                        chunks, size = [], 0
                        for chunk in resp.iter_bytes():
                            size += len(chunk)
                            if size > cap:
                                result.error = f"body larger than {cap} bytes (truncated)"
                                result.truncated = True
                                break
                            chunks.append(chunk)
                        result.body = b"".join(chunks)
                        return result
                result.error = "too many redirects"
        except BlockedURL as e:
            result.error = f"blocked: {e}"
        except httpx.TimeoutException:
            result.error = f"timeout after {self.timeout_s:.0f}s"
        except httpx.HTTPError as e:
            result.error = f"{type(e).__name__}: {e}"
        return result


@dataclass
class Snapshot:
    title: str
    text: str
    sha256: str
    byte_size: int


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def extract_text(content_type: str, body: bytes, url: str = "") -> tuple[str, str]:
    """Returns (title, text). Supports HTML, PDF and plain text/JSON."""
    if is_pdf_body(content_type, body):
        with _PDF_SLOTS:
            return _pdf_text(body)
    if "html" in content_type or body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(body, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
            tag.decompose()
        return _norm_ws(title), _norm_ws(soup.get_text(" "))
    if content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        return "", _norm_ws(body.decode("utf-8", errors="replace"))
    raise ValueError(f"unsupported content type: {content_type or 'unknown'}")


def is_pdf_body(content_type: str, body: bytes) -> bool:
    return content_type == "application/pdf" or body[:5] == b"%PDF-"


def _pdf_text(body: bytes) -> tuple[str, str]:
    """pypdf first (bounded pages/characters); if it cannot open the file, pdfminer.six when it is
    installed (optional, not a requirement); otherwise a clear 'unreadable PDF' error."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(body))
        pages, total = [], 0
        for page in reader.pages[:PDF_MAX_PAGES]:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            pages.append(t)
            total += len(t)
            if total > PDF_MAX_CHARS:
                break
        title = ""
        try:
            title = (reader.metadata.title or "") if reader.metadata else ""
        except Exception:
            title = ""
        text = _norm_ws(" ".join(pages))
        if text:
            return _norm_ws(title), text
        first_error = None          # opened fine but no text layer (scanned or blank)
    except Exception as e:
        title, first_error = "", f"{type(e).__name__}"
    try:
        from pdfminer.high_level import extract_text as pdfminer_text  # optional second parser
    except ImportError:
        if first_error is None:
            return _norm_ws(title), ""      # classified "thin" by the checker
        raise ValueError(f"unreadable PDF: {first_error}") from None
    try:
        text = _norm_ws(pdfminer_text(io.BytesIO(body), maxpages=PDF_MAX_PAGES) or "")
    except Exception as e:
        raise ValueError(f"unreadable PDF: {first_error}; pdfminer: {type(e).__name__}") from None
    if not text and first_error:
        raise ValueError(f"unreadable PDF: {first_error}")
    return "", text


def snapshot(content_type: str, body: bytes, url: str = "") -> Snapshot:
    title, text = extract_text(content_type, body, url)
    return Snapshot(title=title, text=text, sha256=hashlib.sha256(body).hexdigest(), byte_size=len(body))


@dataclass
class Assessment:
    status: str               # ok | dead | soft_404 | thin | blocked | error
    reason: str | None = None
    snapshot: Snapshot | None = None


class SourceChecker:
    """Classifies fetched pages. Soft-404 detection: error-ish titles, thin pages, deep links that
    redirect to the site root, and pages that look like what the host serves for a random path."""

    def __init__(self, fetcher: Fetcher, *, probe_token=lambda: secrets.token_hex(8)):
        self.fetcher = fetcher
        self._probe_token = probe_token
        self._probe_cache: dict[str, str | None] = {}

    def _probe_text(self, final_url: str) -> str | None:
        parts = urlsplit(final_url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key not in self._probe_cache:
            probe = self.fetcher.get(f"{key}/ki-probe-{self._probe_token()}", max_bytes=2_000_000)
            text = None
            if probe.status == 200 and probe.body and not probe.error:
                try:
                    text = snapshot(probe.content_type, probe.body).text
                except ValueError:
                    text = None
            self._probe_cache[key] = text
        return self._probe_cache[key]

    def assess(self, result: FetchResult) -> Assessment:
        if result.error and result.error.startswith("blocked"):
            return Assessment("blocked", result.error)
        if result.status is None:
            return Assessment("dead", result.error or "no response")
        if result.status >= 400:
            return Assessment("dead", f"HTTP {result.status}")
        if result.status != 200:
            return Assessment("dead", f"unexpected HTTP {result.status}")
        if result.truncated and is_pdf_body(result.content_type, result.body):
            return Assessment("error", f"PDF larger than {len(result.body) // 1_000_000} MB cap (FETCH_MAX_BYTES); not parsed")
        try:
            snap = snapshot(result.content_type, result.body, result.final_url or result.url)
        except ValueError as e:
            return Assessment("error", str(e))
        requested, final = urlsplit(result.url), urlsplit(result.final_url or result.url)
        if requested.path.strip("/") and not final.path.strip("/") and not final.query:
            return Assessment("soft_404", "deep link redirected to the site root", snap)
        head = f"{snap.title} {snap.text[:400]}"
        if SOFT_404_RE.search(snap.title or "") or (len(snap.text) < 2000 and SOFT_404_RE.search(head)):
            return Assessment("soft_404", "page reads as 'not found'", snap)
        if len(snap.text) < MIN_TEXT_CHARS:
            return Assessment("thin", f"too little text ({len(snap.text)} chars)", snap)
        probe = None if result.archived_from else self._probe_text(result.final_url or result.url)
        if probe and fuzz.ratio(probe[:3000], snap.text[:3000]) >= 90:
            return Assessment("soft_404", "matches the host's response to a random path", snap)
        return Assessment("ok", None, snap)


def is_block_page(title: str, text: str) -> bool:
    """A bot wall, CDN error or archive outage page rather than the document."""
    return bool(BLOCK_PAGE_RE.search(f"{title} {text[:600]}")) and len(text) < 5000


@dataclass
class ArchiveAttempt:
    """What the Wayback fallback did for one source; stored on Source.archive_attempt."""
    lookup: str = ""                       # cdx | availability | none
    outcome: str = "not_found"             # used | not_found | unusable | rate_limited | unavailable | capped
    note: str = ""
    used: str | None = None                # timestamp of the copy used
    tried: list = field(default_factory=list)   # [{"ts":..., "result":...}]

    def as_dict(self) -> dict:
        return {"lookup": self.lookup, "outcome": self.outcome, "note": self.note, "used": self.used,
                "tried": self.tried}


def _why(tripped: str | None, res: FetchResult) -> str | None:
    if tripped == "rate_limited":
        return "HTTP 429 (rate-limited)"
    if tripped:
        return f"unavailable ({'outage page' if res.status == 200 else res.error or f'HTTP {res.status}'})"
    return f"HTTP {res.status}" if res.status != 200 else None


class ArchiveBudget:
    """Daily cap on real archive.org requests across all runs in this process (resets at 00:00 UTC
    and on restart; Render runs one web process, so this is effectively per service)."""

    def __init__(self, clock=lambda: time.gmtime()):
        self._clock = clock
        self._day: str | None = None
        self.used = 0
        self._lock = threading.Lock()

    def take(self, limit: int | None) -> bool:
        if not limit or limit <= 0:
            return True
        with self._lock:
            day = time.strftime("%Y-%m-%d", self._clock())
            if day != self._day:
                self._day, self.used = day, 0
            if self.used >= limit:
                return False
            self.used += 1
            return True


ARCHIVE_BUDGET = ArchiveBudget()
_ARCHIVE_LIMITERS: dict[float, RateLimiter] = {}


def archive_limiter(min_interval_s: float) -> RateLimiter | None:
    """One shared pacer per interval, so concurrent runs/threads space out archive.org requests."""
    if min_interval_s <= 0:
        return None
    if min_interval_s not in _ARCHIVE_LIMITERS:
        _ARCHIVE_LIMITERS[min_interval_s] = RateLimiter(1.0 / min_interval_s)
    return _ARCHIVE_LIMITERS[min_interval_s]


def _archive_down(res: FetchResult) -> str | None:
    if res.status == 429:
        return "rate_limited"
    if (res.status is None or res.status >= 500
            or (res.status == 200 and b"internet archive: temporarily offline" in res.body[:4000].lower())):
        return "unavailable"
    return None


class Wayback:
    """Wayback Machine fallback for one run: CDX lookup of the newest HTTP-200 captures (availability
    API only when CDX itself fails), block-page rejection, a per-run request cap, at most two archive
    requests at a time, and a circuit breaker once archive.org rate-limits us or is offline.

    Resilience (1.1.0; all off by default here, switched on by the pipeline from settings):
      * ``min_interval_s``: shared pacing between archive.org requests (all runs and threads);
      * ``retries``: on 429/5xx/outage, back off (Retry-After honoured up to ``retry_after_cap_s``,
        else exponential with jitter) and try again; every real hit counts toward ``max_requests``;
      * ``resumes``: when archive.org still refuses after the backoff, pause (Retry-After or
        ``pause_s``) and probe once more before the breaker opens for the rest of the run;
      * ``daily_max``: a cap on real archive.org requests per day across runs (``ArchiveBudget``)."""

    def __init__(self, fetcher: Fetcher, *, max_requests: int = WAYBACK_MAX_REQUESTS,
                 captures: int = WAYBACK_CAPTURES, min_interval_s: float = 0.0, retries: int = 0,
                 retry_after_cap_s: float = 120.0, resumes: int = 0, pause_s: float = 60.0,
                 daily_max: int = 0, budget: ArchiveBudget | None = None, sleep=time.sleep,
                 limiter: RateLimiter | None = None):
        self.fetcher = fetcher
        self.max_requests = max_requests
        self.captures = captures
        self.requests = 0                   # real HTTP requests to archive.org (retries included)
        self.down: str | None = None        # "rate_limited" | "unavailable" once tripped
        self.capped: str | None = None      # "run" | "daily" once a cap stops us
        self.retries, self.retry_after_cap_s = max(0, retries), retry_after_cap_s
        self.resumes, self.pause_s = max(0, resumes), pause_s
        self.paused = 0                     # pause-and-probe cycles used
        self.daily_max = daily_max
        self.budget = budget or ARCHIVE_BUDGET
        self._sleep = sleep
        self._limiter = limiter if limiter is not None else archive_limiter(min_interval_s)
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(2 if self._limiter is None else 1)
        self._own_retries = isinstance(fetcher, HttpFetcher)

    def _hit(self, url: str, **kw) -> FetchResult | None:
        """One real request, or None when a cap or the breaker stops it."""
        with self._lock:
            if self.down:
                return None
            if self.requests >= self.max_requests:
                self.capped = self.capped or "run"
                return None
            if not self.budget.take(self.daily_max):
                self.capped = self.capped or "daily"
                return None
            self.requests += 1
        if self._limiter is not None:
            self._limiter.wait()
        if self._own_retries:
            kw["retries"] = 0          # we back off ourselves, and count every hit
        return self.fetcher.get(url, **kw)

    def _get(self, url: str, **kw) -> FetchResult | None:
        with self._slots:
            res = self._hit(url, **kw)
            attempt = 0
            while res is not None and _archive_down(res):
                if attempt < self.retries:
                    attempt += 1
                    self._sleep(backoff_s(attempt, base=2.0, cap=self.retry_after_cap_s,
                                          retry_after=res.retry_after))
                elif self.paused < self.resumes:
                    self.paused += 1
                    attempt = self.retries     # one probe after the pause, then the breaker
                    self._sleep(min(self.retry_after_cap_s, res.retry_after or self.pause_s))
                else:
                    break
                nxt = self._hit(url, **kw)
                if nxt is None:
                    break
                res = nxt
            return res

    def _trip(self, res: FetchResult) -> str | None:
        """Mark archive.org down for the rest of the run on 429/5xx or an outage page."""
        why = _archive_down(res)
        if why:
            with self._lock:
                self.down = self.down or why
        return why

    def _stopped(self, att: ArchiveAttempt) -> ArchiveAttempt:
        att.outcome = self.down or "capped"
        if self.down:
            att.note = f"Wayback skipped: archive.org {'rate-limited us' if self.down == 'rate_limited' else 'unavailable'}"
        elif self.capped == "daily":
            att.note = f"Wayback skipped: daily cap of {self.daily_max} archive requests reached"
        else:
            att.note = f"Wayback skipped: per-run cap of {self.max_requests} archive requests reached"
        return att

    def lookup(self, url: str, att: ArchiveAttempt) -> list[str] | None:
        """Newest-first timestamps of HTTP-200 captures; None when no lookup could be made."""
        res = self._get(WAYBACK_CDX.format(url=quote(url, safe=""), n=self.captures), max_bytes=200_000)
        if res is None:
            return None
        att.lookup = "cdx"
        cdx_err = _why(self._trip(res), res)
        if not cdx_err:
            try:
                rows = json.loads(res.body or b"[]")
                if not isinstance(rows, list):
                    raise TypeError("not a list")
                stamps = [str(r[0]) for r in rows[1:] if isinstance(r, list) and r and str(r[0]).isdigit()]
                return list(reversed(stamps))
            except (ValueError, IndexError, TypeError):
                cdx_err = "unreadable CDX answer"
        att.note = f"CDX {cdx_err}"
        if self.down:
            return None
        res = self._get(WAYBACK_API.format(url=quote(url, safe="")), max_bytes=200_000)
        if res is None:
            return None
        att.lookup = "availability"
        api_err = _why(self._trip(res), res)
        if api_err:
            att.note = f"CDX {cdx_err}; availability API {api_err}"
            return None
        try:
            closest = (json.loads(res.body).get("archived_snapshots") or {}).get("closest") or {}
        except (ValueError, AttributeError):
            att.note = f"CDX {cdx_err}; availability API answer unreadable"
            return None
        ts = str(closest.get("timestamp") or "")
        if closest.get("available") and str(closest.get("status", "200")) == "200" and ts.isdigit():
            return [ts]
        return []

    def fetch(self, url: str, accept) -> tuple[FetchResult | None, Assessment | None, ArchiveAttempt]:
        """Try the newest good capture(s) of ``url``. ``accept(result) -> Assessment`` judges a copy
        (the caller's checks, e.g. assess + large-PDF rescue). Returns (copy, assessment, attempt);
        copy is None unless an ``ok`` copy was found."""
        att = ArchiveAttempt()
        stamps = self.lookup(url, att)
        if stamps is None:
            if not att.note:
                return None, None, self._stopped(att)
            att.outcome = self.down or "unavailable"
            att.note = f"Wayback lookup failed ({att.note})"
            return None, None, att
        if not stamps:
            att.outcome, att.note = "not_found", "Wayback has no HTTP-200 capture"
            return None, None, att
        for ts in stamps:
            res = self._get(WAYBACK_RAW.format(ts=ts, url=url))
            if res is None:
                break
            if self._trip(res):
                att.tried.append({"ts": ts, "result": self.down})
                break
            res.archived_from, res.archive_timestamp = url, ts
            a = accept(res)
            snap = a.snapshot
            if snap is not None and is_block_page(snap.title, snap.text):
                att.tried.append({"ts": ts, "result": "block page"})
                continue
            if a.status == "ok":
                att.tried.append({"ts": ts, "result": "ok"})
                att.outcome, att.used = "used", ts
                att.note = f"using the Wayback Machine copy of {ts[:4]}-{ts[4:6]}-{ts[6:8]}"
                return res, a, att
            att.tried.append({"ts": ts, "result": f"{a.status}: {a.reason or ''}"[:120]})
        if att.tried:
            att.outcome = "unusable" if not self.down else self.down
            att.note = "Wayback copies unusable: " + "; ".join(f"{t['ts']} {t['result']}" for t in att.tried)
        else:
            self._stopped(att)
        return None, None, att


def wayback_fetch(fetcher: Fetcher, url: str) -> FetchResult | None:
    """The newest usable Wayback copy of ``url`` (raw, without the archive toolbar), or None.
    Kept for scripts; the pipeline uses ``Wayback.fetch`` so every attempt is recorded."""
    checker = SourceChecker(fetcher)
    res, _a, _att = Wayback(fetcher).fetch(url, checker.assess)
    return res
