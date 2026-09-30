"""CSRF protection and a small rate limiter shared by all workers."""
import hmac
import secrets
import time

from flask import abort, current_app, request, session

from .db import get_db


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
    if not token or not same(sent, token):
        abort(400, "This form has expired. Go back, refresh the page and try again.")


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class RateLimiter:
    """Sliding window stored in SQLite, so every gunicorn worker counts the same hits."""

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        if not current_app.config.get("RATELIMIT_ENABLED", True):
            return True
        db, now = get_db(), time.time()
        with db:
            db.execute("DELETE FROM rate_hits WHERE at <= ?", (now - 86400,))
            hits = db.execute(
                "SELECT COUNT(*) FROM rate_hits WHERE key = ? AND at > ?", (key, now - window_seconds)
            ).fetchone()[0]
            if hits >= limit:
                return False
            db.execute("INSERT INTO rate_hits (key, at) VALUES (?, ?)", (key, now))
        return True


limiter = RateLimiter()


def client_ip() -> str:
    return request.remote_addr or "?"
