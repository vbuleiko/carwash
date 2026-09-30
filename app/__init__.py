"""Washbook — a car wash notebook that texts customers on WhatsApp."""
import mimetypes
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from . import admin, db, owner, public, seed, themes
from .security import check_csrf, csrf_token
from .utils import fmt_minutes, format_phone, money, money_input, to_local, wa_link


def _secret_key(data_dir: Path) -> str:
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = data_dir / "secret_key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


mimetypes.add_type("font/woff2", ".woff2")  # slim Python images have no system mime table


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    data_dir = Path(os.environ.get("DATA_DIR", "data")).resolve()
    if not test_config:
        data_dir.mkdir(parents=True, exist_ok=True)
    app.config.update(
        APP_NAME=os.environ.get("APP_NAME", "Washbook"),
        DATABASE=str(data_dir / "washbook.db"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
        PRICE_MONTHLY=os.environ.get("PRICE_MONTHLY", "R299"),
        SUPPORT_WHATSAPP=os.environ.get("SUPPORT_WHATSAPP", ""),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", ""),
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "30")),
        THEME=os.environ.get("THEME", "carbon"),
        RATELIMIT_ENABLED=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=90),
        MAX_CONTENT_LENGTH=256 * 1024,
        ASSET_VERSION=str(int(time.time())),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _secret_key(data_dir)

    # behind Caddy: trust one proxy hop for client IP and https
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    with app.app_context():
        conn = db.get_db()
        with conn:
            seed.cleanup_demos(conn)

    app.register_blueprint(public.bp)
    app.register_blueprint(owner.bp)
    app.register_blueprint(admin.bp)

    app.before_request(check_csrf)
    app.jinja_env.globals.update(csrf_token=csrf_token, wa_link=wa_link)
    app.jinja_env.filters.update(money=money, amount=money_input, phone=format_phone, minutes=fmt_minutes)

    @app.template_filter("local")
    def local_filter(value, fmt="%H:%M"):
        dt = to_local(value, g.tz) if value and "tz" in g else None
        return dt.strftime(fmt) if dt else ""

    @app.context_processor
    def theme():
        return {"theme": themes.current(), "themes": themes.THEMES}

    @app.context_processor
    def support_link():
        number = app.config["SUPPORT_WHATSAPP"]
        return {"support_wa": wa_link(number, f"Hi! I have a question about {app.config['APP_NAME']}.") if number else ""}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        return resp

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(429)
    def error_page(err):
        return render_template("error.html", error=err), err.code

    return app
