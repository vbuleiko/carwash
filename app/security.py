"""CSRF protection and a small rate limiter shared by all workers."""
import hmac
import secrets
import time

from flask import abort, current_app, flash, g, redirect, request, session

from .db import get_db
from .sessions import COOKIE


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
        session.permanent = True  # a browser-session cookie is gone after a restart, the restored tab is not
    return token


RETRY = {"public.login", "public.signup", "public.claim"}  # an expired form here comes back filled in
KEEP = ("email", "name", "phone", "city")
# forms that work without a login: a browser that keeps no cookies may send them from our own pages
NO_COOKIE = RETRY | {"public.demo", "site.book", "site.cancel"}


def check_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
    token = session.get("csrf", "")
    if token and same(sent, token):
        return
    if request.endpoint in NO_COOKIE and not token and not request.environ.get(COOKIE) and same_origin():
        g.cookieless = True
        current_app.logger.warning("No cookie from the browser: %s %s", request.path, request.user_agent.string)
        return
    if request.endpoint in RETRY:
        session["retry"] = {k: request.form[k][:120] for k in KEEP if request.form.get(k)}
        session.permanent = True
        flash("This page was open for a while. Please try again.", "error")
        return redirect(request.full_path.rstrip("?"), 303)
    abort(400, "This form has expired. Go back, refresh the page and try again.")


def same_origin() -> bool:
    """The form was sent from a page of this address (browsers send Origin with every POST)."""
    origin = request.headers.get("Origin")
    if origin is None:
        return request.headers.get("Referer", "").startswith(request.host_url)
    return origin + "/" == request.host_url


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
