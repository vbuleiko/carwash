"""Landing page, sign up / log in, and the no-signup demo."""
import re
import sqlite3
from datetime import timedelta

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

from . import menu, queries, seed, site, themes
from .db import get_db
from .security import client_ip, limiter
from .utils import DEFAULT_TZ, format_phone, local_today, normalize_phone, parse_money, phone_is_valid, ts, zone

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
def on_car_wash_address():
    """On a car wash's own address: its name and look, and no sign-up or demo."""
    if g.host:
        g.site = g.host
        if request.endpoint in ("public.signup", "public.demo", "public.theme"):
            abort(404)


def _yearly() -> dict | None:
    """The yearly price next to twelve monthly payments, for the pricing card."""
    month, year = (parse_money(current_app.config[k]) for k in ("PRICE_MONTHLY", "PRICE_YEARLY"))
    if not month or not year or year >= month * 12:
        return None
    return {"full": month * 12, "per_month": round(year / 12), "saved": month * 12 - year}


@bp.get("/")
def landing():
    if g.host:
        return site.render_page(g.host)
    return render_template("public/landing.html", yearly=_yearly())


def _account_errors(email: str, password: str, phone: str) -> list[str]:
    errors = []
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email.")
    if len(password) < 8:
        errors.append("Password needs at least 8 characters.")
    if not phone_is_valid(phone):
        errors.append("Enter your WhatsApp number so we can reach you.")
    if not errors and get_db().execute("SELECT 1 FROM tenants WHERE email = ?", (email,)).fetchone():
        errors.append("This email already has an account. Log in instead.")
    if not errors and not limiter.allow(f"signup:{client_ip()}", 5, 3600):
        errors.append("Too many sign-ups from this network. Try again in an hour.")
    return errors


def _first_page():
    """A new owner starts with their own price list: a photo of it is enough."""
    return url_for("owner.settings_prices_upload", welcome=1) if menu.enabled() else url_for("owner.board")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    form = {}
    pilot = request.values.get("plan") == "pilot"
    days = current_app.config["PILOT_DAYS" if pilot else "TRIAL_DAYS"]
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()[:60]
        email = form.get("email", "").strip().lower()[:120]
        password = form.get("password", "")
        phone = normalize_phone(form.get("phone"), "27")
        errors = [] if name else ["Enter your car wash name."]
        errors = errors or _account_errors(email, password, phone)
        db = get_db()
        if not errors:
            try:
                with db:
                    tenant_id = seed.create_tenant(
                        db, name=name, email=email, password_hash=generate_password_hash(password),
                        phone=phone, city=form.get("city", "").strip()[:60],
                        trial_days=days, theme=themes.picked(),
                    )
            except sqlite3.IntegrityError:  # the same form sent twice
                flash("This email already has an account. Log in instead.", "error")
                return render_template("public/signup.html", form=form, pilot=pilot, days=days)
            _start_session(tenant_id)
            flash(f"Welcome! Your {days}-day free {'pilot' if pilot else 'trial'} has started.")
            return redirect(_first_page())
        for e in errors:
            flash(e, "error")
    return render_template("public/signup.html", form=form, pilot=pilot, days=days)


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
        if g.host and tenant and tenant["id"] != g.host["id"]:  # a car wash's address is for its owner only
            tenant = None
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


def _made_for(slug: str):
    """The car wash whose website the demo was opened from, if it still has no owner."""
    tenant = get_db().execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone() if slug else None
    return tenant if tenant and queries.is_prospect(tenant) and not tenant["is_disabled"] else None


@bp.post("/demo")
def demo():
    db = get_db()
    made_for = _made_for(request.form.get("site", ""))

    def go(tenant_id):
        if request.form.get("next") == "book":
            slug = db.execute("SELECT slug FROM tenants WHERE id = ?", (tenant_id,)).fetchone()[0]
            return redirect(url_for("site.page", slug=slug))
        return redirect(url_for("owner.board"))

    current = session.get("tenant_id")
    if current and db.execute(
        "SELECT 1 FROM tenants WHERE id = ? AND is_demo = 1 AND session_key = ? AND session_key != '' "
        "AND created_at >= ? AND name = ?",
        (current, session.get("key", ""), seed.demo_cutoff(), seed.demo_name(made_for)),
    ).fetchone():
        return go(current)
    if not limiter.allow(f"demo:{client_ip()}", 10, 3600):
        flash("Too many demos from this network. Try again later.", "error")
        return redirect(url_for("public.landing"))
    with db:
        tenant_id = seed.create_demo(db, theme=themes.picked(), made_for=made_for)
        seed.cleanup_demos(db)
    _start_session(tenant_id)
    if made_for:
        session["made_for"] = made_for["slug"]
    return go(tenant_id)


@bp.post("/theme")
def theme():
    name = request.form.get("theme", "")
    back = request.form.get("next", "")  # a website we made for a car wash, or its booking page
    if request.headers.get("X-Requested-With") == "fetch":  # app.js switches the look in place
        resp = jsonify(ok=True)
    elif back.startswith(("/site/", "/book/")) and back.isprintable():
        resp = redirect(back)
    else:
        resp = redirect(url_for("public.landing", _anchor="look"))
    return themes.remember(resp, name) if name in themes.THEMES else resp


def _site(slug: str):
    tenant = get_db().execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
    if g.host:
        abort(404)
    return site.use(tenant)


@bp.get("/site/<slug>")
def website(slug):
    """A car wash's website on our address. Before it has an owner, the owner-to-be gets the link,
    tries the looks and makes it theirs."""
    tenant = _site(slug)
    look = themes.current()
    files = {"theme.css": f"themes/{look}.css"}
    return site.website(tenant, lambda name: url_for("static", filename=files.get(name, name)), look,
                        preview=queries.is_prospect(tenant))


@bp.route("/site/<slug>/claim", methods=["GET", "POST"])
def claim(slug):
    """The owner takes the website we made: their login, the look they picked, the free pilot from today."""
    tenant = _site(slug)
    if not queries.is_prospect(tenant):
        flash(f"{tenant['name']} already has an owner. Log in instead.", "error")
        return redirect(url_for(".login"))
    days = current_app.config["PILOT_DAYS"]
    form = {"phone": format_phone(tenant["phone"])}
    if request.method == "POST":
        form = request.form
        email = form.get("email", "").strip().lower()[:120]
        password = form.get("password", "")
        phone = normalize_phone(form.get("phone"), "27")
        errors = _account_errors(email, password, phone)
        if not errors:
            db = get_db()
            until = local_today(zone(DEFAULT_TZ)) + timedelta(days=days)
            try:
                with db:
                    taken = db.execute(
                        "UPDATE tenants SET email = ?, password_hash = ?, phone = ?, theme = ?, plan = 'trial', "
                        "paid_until = ?, created_at = ? WHERE id = ? AND email IS NULL",
                        (email, generate_password_hash(password), phone, themes.current(), until.isoformat(),
                         ts(), tenant["id"]),
                    ).rowcount
            except sqlite3.IntegrityError:  # the same email at the same moment
                taken = 0
            if taken:
                queries.session_key(db, tenant["id"], rotate=True)
                _start_session(tenant["id"])
                flash(f"{tenant['name']} is yours. Free until {until:%d %b %Y}. "
                      "Your website and booking page are under Bookings.")
                return redirect(_first_page())
            errors = ["Someone has just taken this car wash, or this email already has an account."]
        for e in errors:
            flash(e, "error")
    return render_template("site/claim.html", t=tenant, form=form, days=days)


@bp.get("/privacy")
def privacy():
    return render_template("public/privacy.html")


@bp.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /app\nDisallow: /admin\nDisallow: /demo\n", mimetype="text/plain")


@bp.get("/tls-check")
def tls_check():
    """Caddy asks before it gets an HTTPS certificate for a car wash's own address."""
    domain = request.args.get("domain", "").lower()
    found = domain and get_db().execute("SELECT 1 FROM tenants WHERE domain = ?", (domain,)).fetchone()
    return ("ok", 200) if found else ("unknown", 404)


@bp.get("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return "ok"
