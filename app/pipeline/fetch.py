"""Fetch every cited source ourselves: SSRF-guarded GET, text extraction (HTML/PDF/plain),
sha256 snapshot, and dead-link / soft-404 detection (incl. a random-path probe per host)."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import re
import secrets
import socket
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from rapidfuzz import fuzz

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36 KardashevIndex/0.2 (+https://kardashev-index.onrender.com/methodology)")
MAX_REDIRECTS = 5
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
    def __init__(self, *, timeout_s: float = 20, max_bytes: int = 15_000_000,
                 transport: httpx.BaseTransport | None = None, resolve=socket.getaddrinfo):
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self._transport = transport
        self._resolve = resolve

    def get(self, url: str, *, headers: dict | None = None, max_bytes: int | None = None) -> FetchResult:
        cap = max_bytes or self.max_bytes
        result = FetchResult(url=url)
        hdrs = {"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,application/json,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Language": "en"}
        hdrs.update(headers or {})
        current = url
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout_s, follow_redirects=False) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    check_url_allowed(current, self._resolve)
                    with client.stream("GET", current, headers=hdrs) as resp:
                        if resp.is_redirect and resp.headers.get("location"):
                            result.redirects.append(current)
                            current = urljoin(current, resp.headers["location"])
                            continue
                        result.status = resp.status_code
                        result.final_url = current
                        result.content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                        chunks, size = [], 0
                        for chunk in resp.iter_bytes():
                            size += len(chunk)
                            if size > cap:
                                result.error = f"body larger than {cap} bytes (truncated)"
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
    is_pdf = content_type == "application/pdf" or body[:5] == b"%PDF-"
    if is_pdf:
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(body))
            pages = []
            for page in reader.pages[:250]:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
            title = ""
            try:
                title = (reader.metadata.title or "") if reader.metadata else ""
            except Exception:
                title = ""
            return _norm_ws(title), _norm_ws(" ".join(pages))
        except Exception as e:
            raise ValueError(f"unreadable PDF: {type(e).__name__}") from e
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
        probe = self._probe_text(result.final_url or result.url)
        if probe and fuzz.ratio(probe[:3000], snap.text[:3000]) >= 90:
            return Assessment("soft_404", "matches the host's response to a random path", snap)
        return Assessment("ok", None, snap)
