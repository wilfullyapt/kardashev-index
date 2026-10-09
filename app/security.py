"""Security hardening: response headers, same-origin check for state-changing requests, and an
in-process admin-login throttle. Kept framework-light so it is easy to audit."""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

log = logging.getLogger("kardashev.security")


# ---------------------------------------------------------------- environment
def is_production() -> bool:
    """Render sets RENDER=true on every service; APP_ENV=production forces it elsewhere."""
    return os.getenv("RENDER", "").lower() == "true" or os.getenv("APP_ENV", "").lower() == "production"


def session_secret() -> str:
    """SECRET_KEY signs the admin session cookie. Production refuses to start without it (no
    shared 'dev-secret' fallback); local/dev gets a random per-process key (sessions reset on
    restart) and a warning."""
    key = os.getenv("SECRET_KEY", "").strip()
    if key:
        return key
    if is_production():
        raise RuntimeError("SECRET_KEY is not set. Refusing to start in production without it "
                           "(it signs admin session cookies). Set it in the Render environment.")
    log.warning("SECRET_KEY not set; using a random per-process key (development only)")
    return secrets.token_urlsafe(32)


def _bool_env(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "yes", "on")


def session_cookie_secure() -> bool:
    """Secure flag on the session cookie: on in production (HTTPS only), off for local http."""
    return _bool_env("SESSION_COOKIE_SECURE", is_production())


SESSION_MAX_AGE_S = int(os.getenv("SESSION_MAX_AGE_S", str(12 * 3600)))


# ---------------------------------------------------------------- headers
# Everything the templates load: own static files, Google Fonts (CSS + font files), data: images.
# 'unsafe-inline' for styles only: templates use style="" attributes and htmx injects its
# indicator <style>. No inline scripts or event handlers remain, so script-src is 'self' only.
CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)
CSP = "; ".join(CSP_DIRECTIVES)

STATIC_HEADERS = {
    "content-security-policy": CSP,
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "cross-origin-opener-policy": "same-origin",
}


def hsts_value() -> str:
    """Conservative by default (1 day, no includeSubDomains) so a launch mistake is cheap to undo.
    Raise HSTS_MAX_AGE (e.g. 31536000) once the domain is settled; HSTS_INCLUDE_SUBDOMAINS=1 only
    if every subdomain of the custom domain is HTTPS."""
    v = f"max-age={int(os.getenv('HSTS_MAX_AGE', '86400'))}"
    if _bool_env("HSTS_INCLUDE_SUBDOMAINS", False):
        v += "; includeSubDomains"
    return v


def request_is_https(scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-proto":
            return v.decode("latin-1").split(",")[0].strip().lower() == "https"
    return False


# ---------------------------------------------------------------- same-origin (CSRF) check
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return None


def csrf_violation(scope) -> str | None:
    """Reject cross-site state-changing requests to /admin/* and /internal/* (OWASP 'verify origin
    with standard headers'). Browsers always send Origin (or at least Referer) on cross-site form
    POSTs, so a mismatch is refused; requests without either header are non-browser clients
    (Hermes, curl, tests) and are left to the normal auth (Hermes key / session). Combined with the
    SameSite=Lax session cookie. Returns a reason string when the request must be refused."""
    if scope.get("type") != "http" or scope.get("method") not in UNSAFE_METHODS:
        return None
    path = scope.get("path", "")
    if not path.startswith(("/admin", "/internal")):
        return None
    host = (_header(scope, b"host") or "").lower()
    source = _header(scope, b"origin")
    if source is None or source == "null":
        ref = _header(scope, b"referer")
        if source == "null" and not ref:
            return "Origin: null"
        source = ref
    if not source:
        return None
    if urlsplit(source).netloc.lower() != host:
        return f"cross-origin {urlsplit(source).netloc or source!r} != {host!r}"
    return None


class SecurityMiddleware:
    """Pure ASGI: adds security headers to every HTTP response and refuses cross-origin
    state-changing admin/internal requests with 403."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        reason = csrf_violation(scope)
        if reason:
            log.warning("blocked cross-origin %s %s (%s)", scope.get("method"), scope.get("path"), reason)
            body = b'{"detail":"Cross-origin request blocked"}'
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]
                        + [(k.encode(), v.encode()) for k, v in STATIC_HEADERS.items()]})
            await send({"type": "http.response.body", "body": body})
            return
        https = request_is_https(scope)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for k, v in STATIC_HEADERS.items():
                    if k.encode() not in present:
                        headers.append((k.encode(), v.encode()))
                if https and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", hsts_value().encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------- login throttle
class LoginThrottle:
    """In-process brute-force guard for POST /admin/login (single instance; resets on deploy).

    * per IP: after `ip_max` failures within `window_s`, that IP is locked out for `lockout_s`;
    * global: after `global_max` failures within `window_s` from anywhere, all logins are paused
      for `lockout_s` (backstop against IP rotation / spoofed X-Forwarded-For).
    A successful login clears that IP's failures."""

    def __init__(self, ip_max=5, global_max=50, window_s=900, lockout_s=900, now=time.monotonic):
        self.ip_max, self.global_max, self.window_s, self.lockout_s = ip_max, global_max, window_s, lockout_s
        self.now = now
        self._fails: dict[str, deque] = defaultdict(deque)
        self._global: deque = deque()
        self._locked_until: dict[str, float] = {}
        self._global_locked_until = 0.0
        self._lock = threading.Lock()

    def _prune(self, q: deque, t: float):
        while q and t - q[0] >= self.window_s:
            q.popleft()

    def retry_after(self, ip: str) -> int:
        """Seconds until this IP may try again (0 = allowed now)."""
        with self._lock:
            t = self.now()
            until = max(self._locked_until.get(ip, 0.0), self._global_locked_until)
            return max(0, int(until - t + 0.999))

    def failure(self, ip: str):
        with self._lock:
            t = self.now()
            q = self._fails[ip]
            q.append(t)
            self._prune(q, t)
            self._global.append(t)
            self._prune(self._global, t)
            if len(q) >= self.ip_max:
                self._locked_until[ip] = t + self.lockout_s
                q.clear()
                log.warning("admin login: IP %s locked out for %ss after repeated failures", ip, self.lockout_s)
            if len(self._global) >= self.global_max:
                self._global_locked_until = t + self.lockout_s
                self._global.clear()
                log.warning("admin login: global lockout for %ss (%s failures in %ss)",
                            self.lockout_s, self.global_max, self.window_s)

    def success(self, ip: str):
        with self._lock:
            self._fails.pop(ip, None)
            self._locked_until.pop(ip, None)

    def reset(self):
        with self._lock:
            self._fails.clear()
            self._global.clear()
            self._locked_until.clear()
            self._global_locked_until = 0.0
