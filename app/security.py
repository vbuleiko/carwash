"""CSRF protection and a tiny in-memory rate limiter."""
import hmac
import secrets
import time
from collections import defaultdict, deque

from flask import abort, current_app, request, session


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def check_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
    token = session.get("csrf", "")
    if not token or not hmac.compare_digest(sent, token):
        abort(400, "This form has expired. Go back, refresh the page and try again.")


class RateLimiter:
    """Per-process sliding window. Good enough for a single small VM."""

    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        if not current_app.config.get("RATELIMIT_ENABLED", True):
            return True
        now = time.monotonic()
        if len(self.hits) > 10_000:
            self.hits.clear()
        q = self.hits[key]
        while q and q[0] <= now - window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


limiter = RateLimiter()


def client_ip() -> str:
    return request.remote_addr or "?"
