"""Landing page, sign up / log in, and the no-signup demo."""
import re
import sqlite3

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from . import queries, seed, site, themes
from .db import get_db
from .security import client_ip, limiter
from .utils import normalize_phone, phone_is_valid

bp = Blueprint("public", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _start_session(tenant_id: int):
    key = queries.session_key(get_db(), tenant_id)
    csrf = session.get("csrf")
    session.clear()
    session.permanent = True
    session["tenant_id"] = tenant_id
    session["key"] = key
    if csrf:
        session["csrf"] = csrf


@bp.before_request
def site_mode():
    if current_app.config["SITE_MODE"]:
        g.site = site.site_tenant()


@bp.get("/")
def landing():
    if current_app.config["SITE_MODE"]:
        return site.render_page(g.site) if g.site else redirect(url_for(".signup"))
    return render_template("public/landing.html")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_app.config["SITE_MODE"] and g.site:
        abort(404)  # this installation already has its car wash
    form = {}
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()[:60]
        email = form.get("email", "").strip().lower()[:120]
        password = form.get("password", "")
        phone = normalize_phone(form.get("phone"), "27")
        errors = []
        if not name:
            errors.append("Enter your car wash name.")
        if not EMAIL_RE.match(email):
            errors.append("Enter a valid email.")
        if len(password) < 8:
            errors.append("Password needs at least 8 characters.")
        if not phone_is_valid(phone):
            errors.append("Enter your WhatsApp number so we can reach you.")
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM tenants WHERE email = ?", (email,)).fetchone():
            errors.append("This email already has an account. Log in instead.")
        if not errors and not limiter.allow(f"signup:{client_ip()}", 5, 3600):
            errors.append("Too many sign-ups from this network. Try again in an hour.")
        if not errors:
            try:
                with db:
                    tenant_id = seed.create_tenant(
                        db, name=name, email=email, password_hash=generate_password_hash(password),
                        phone=phone, city=form.get("city", "").strip()[:60],
                        trial_days=current_app.config["TRIAL_DAYS"], theme=themes.picked(),
                    )
            except sqlite3.IntegrityError:  # the same form sent twice
                flash("This email already has an account. Log in instead.", "error")
                return render_template("public/signup.html", form=form)
            _start_session(tenant_id)
            flash(f"Welcome! Your {current_app.config['TRIAL_DAYS']}-day free trial has started.")
            return redirect(url_for("owner.board"))
        for e in errors:
            flash(e, "error")
    return render_template("public/signup.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not limiter.allow(f"login:{client_ip()}", 10, 900):
            flash("Too many attempts. Wait 15 minutes and try again.", "error")
            return render_template("public/login.html", email=email), 429
        tenant = get_db().execute(
            "SELECT * FROM tenants WHERE email = ? AND is_demo = 0", (email,)
        ).fetchone()
        if tenant and check_password_hash(tenant["password_hash"] or "", request.form.get("password", "")):
            if tenant["is_disabled"]:
                flash("This account is paused. Please contact support.", "error")
            else:
                _start_session(tenant["id"])
                target = request.args.get("next", "")
                return redirect(target if target.startswith("/app") and target.isprintable() else url_for("owner.board"))
        else:
            flash("Wrong email or password.", "error")
        return render_template("public/login.html", email=email)
    return render_template("public/login.html", email="")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("public.landing"))


@bp.post("/demo")
def demo():
    if current_app.config["SITE_MODE"]:
        abort(404)
    db = get_db()

    def go(tenant_id):
        if request.form.get("next") == "book":
            slug = db.execute("SELECT slug FROM tenants WHERE id = ?", (tenant_id,)).fetchone()[0]
            return redirect(url_for("site.page", slug=slug))
        return redirect(url_for("owner.board"))

    current = session.get("tenant_id")
    if current and db.execute(
        "SELECT 1 FROM tenants WHERE id = ? AND is_demo = 1 AND session_key = ? AND session_key != '' "
        "AND created_at >= ?",
        (current, session.get("key", ""), seed.demo_cutoff()),
    ).fetchone():
        return go(current)
    if not limiter.allow(f"demo:{client_ip()}", 10, 3600):
        flash("Too many demos from this network. Try again later.", "error")
        return redirect(url_for("public.landing"))
    with db:
        tenant_id = seed.create_demo(db, theme=themes.picked())
        seed.cleanup_demos(db)
    _start_session(tenant_id)
    return go(tenant_id)


@bp.post("/theme")
def theme():
    name = request.form.get("theme", "")
    if request.headers.get("X-Requested-With") == "fetch":  # app.js switches the look in place
        resp = jsonify(ok=True)
    else:
        resp = redirect(url_for("public.landing", _anchor="look"))
    return themes.remember(resp, name) if name in themes.THEMES else resp


@bp.get("/privacy")
def privacy():
    return render_template("public/privacy.html")


@bp.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /app\nDisallow: /admin\nDisallow: /demo\n", mimetype="text/plain")


@bp.get("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return "ok"
