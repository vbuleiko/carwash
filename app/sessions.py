"""The login lives in a signed cookie. A browser that keeps no cookies (Samsung Internet with cookies off)
gets it in the address instead: ?s=<key> on every app page, the session itself in link_sessions."""
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from flask import has_request_context, redirect, request
from flask.sessions import SecureCookieSession, SecureCookieSessionInterface
from itsdangerous import BadSignature
from markupsafe import Markup

from .db import get_db
from .utils import now_utc, ts

PARAM = "s"
KEEP_DAYS = 14  # a link nobody opened for two weeks stops working
APP = ("owner.", "public.logout")  # endpoints that need the login: their addresses carry the key
SID = re.compile(r"[A-Za-z0-9_-]{32}")
COOKIE, LINK, NEW, DROP = "cwe.cookie", "cwe.link", "cwe.new_link", "cwe.drop_link"


class LinkSession(SecureCookieSession):
    sid = ""
    seen_at = ""


class Sessions(SecureCookieSessionInterface):
    def open_session(self, app, request):
        if request.path.startswith("/static/"):
            return self.session_class()
        signer = self.get_signing_serializer(app)
        max_age = int(app.permanent_session_lifetime.total_seconds())
        data = None
        for value in request.cookies.getlist(self.get_cookie_name(app)):  # an old one of another path can come first
            try:
                data = signer.loads(value, max_age=max_age)
                break
            except BadSignature:
                pass
        request.environ[COOKIE] = data is not None
        sid = request.args.get(PARAM, "")
        if sid and data and "tenant_id" in data:
            request.environ[DROP] = sid  # the cookie works after all: back to plain addresses
        elif sid:
            link = _load(sid, self.serializer)
            if link is not None:
                return link
        return self.session_class(data or {})

    def save_session(self, app, session, response):
        if request.endpoint == "static":  # cached for a year, the same for everyone: no login cookie
            return
        if isinstance(session, LinkSession):
            _save(session, self.serializer)
            return
        if request.environ.get(NEW):
            _insert(request.environ[NEW], dict(session), self.serializer)
        super().save_session(app, session, response)


def _cutoff() -> str:
    return ts(now_utc() - timedelta(days=KEEP_DAYS))


def _agent() -> str:
    return request.user_agent.string[:300]


def _load(sid: str, serializer) -> LinkSession | None:
    if not SID.fullmatch(sid):
        return None
    row = get_db().execute("SELECT * FROM link_sessions WHERE sid = ?", (sid,)).fetchone()
    if row is None or row["agent"] != _agent() or row["seen_at"] < _cutoff():
        return None
    session = LinkSession(serializer.loads(row["data"]))
    session.sid, session.seen_at = sid, row["seen_at"]
    request.environ[LINK] = sid
    return session


def _save(session: LinkSession, serializer):
    db = get_db()
    with db:
        if not session:  # logged out
            db.execute("DELETE FROM link_sessions WHERE sid = ?", (session.sid,))
        elif session.modified or session.seen_at[:15] != ts()[:15]:  # at most every 10 minutes if unchanged
            db.execute("UPDATE link_sessions SET data = ?, seen_at = ? WHERE sid = ?",
                       (serializer.dumps(dict(session)), ts(), session.sid))


def _insert(sid: str, data: dict, serializer):
    db = get_db()
    with db:
        db.execute("DELETE FROM link_sessions WHERE seen_at < ?", (_cutoff(),))
        db.execute("INSERT INTO link_sessions (sid, data, agent, seen_at) VALUES (?, ?, ?, ?)",
                   (sid, serializer.dumps(data), _agent(), ts()))


def start_link():
    """The browser came to log in without our cookie: the login also travels in the address from now on."""
    request.environ[NEW] = secrets.token_urlsafe(24)


def current_link() -> str:
    return (request.environ.get(LINK) or request.environ.get(NEW) or "") if has_request_context() else ""


def add_link(endpoint: str, values: dict):
    """url_for(): app addresses carry the key; public ones (website, booking page, links for customers) never."""
    sid = current_link()
    if sid and endpoint.startswith(APP) and PARAM not in values:
        values[PARAM] = sid


def keep_link(url: str) -> str:
    sid = current_link()
    return url + ("&" if "?" in url else "?") + f"{PARAM}={sid}" if sid else url


def link_field() -> Markup:
    """For a form sent with GET: the browser drops the query of its action."""
    sid = current_link()
    return Markup('<input type="hidden" name="{}" value="{}">').format(PARAM, sid) if sid else Markup("")


def drop_link():
    """The cookie works (a tab restored after a browser restart): forget the link, plain addresses again."""
    sid = request.environ.get(DROP)
    if not sid:
        return None
    db = get_db()
    with db:
        db.execute("DELETE FROM link_sessions WHERE sid = ?", (sid,))
    if request.method != "GET":
        return None
    args = request.args.to_dict(flat=False)
    args.pop(PARAM, None)
    return redirect(request.path + ("?" + urlencode(args, doseq=True) if args else ""))
