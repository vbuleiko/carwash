"""Your own back office: every car wash, trials, payments, own addresses and websites."""
import calendar
import io
import re
import secrets
import zipfile
from datetime import date, timedelta
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import generate_password_hash

from . import public, queries, seed, site, themes
from .db import get_db
from .security import client_ip, limiter, same
from .utils import DEFAULT_TZ, local_today, normalize_phone, phone_is_valid, ts, days_ago, zone

bp = Blueprint("admin", __name__, url_prefix="/admin")


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


DOMAIN_RE = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
PROSPECT_DAYS = 365  # its booking page stays open while we talk; the pilot starts when the owner gets the login


@bp.before_request
def require_admin():
    if not current_app.config["ADMIN_PASSWORD"] or g.host:
        abort(404)
    if request.endpoint != "admin.login" and not session.get("admin"):
        return redirect(url_for("admin.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not limiter.allow(f"admin:{client_ip()}", 5, 900):
            flash("Too many attempts. Wait 15 minutes.", "error")
        elif same(request.form.get("password", ""), current_app.config["ADMIN_PASSWORD"]):
            session["admin"] = True
            session.permanent = True
            return redirect(url_for("admin.index"))
        else:
            flash("Wrong password.", "error")
    return render_template("admin/login.html")


@bp.post("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("admin.login"))


@bp.get("/")
def index():
    db = get_db()
    since = ts(days_ago(30))
    tenants = [dict(r) for r in db.execute(
        "SELECT t.*, "
        "(SELECT COUNT(*) FROM visits v WHERE v.tenant_id = t.id AND v.status != 'cancelled' "
        " AND v.created_at >= ?) AS cars_30d, "
        "(SELECT MAX(created_at) FROM visits v WHERE v.tenant_id = t.id) AS last_car "
        "FROM tenants t WHERE t.is_demo = 0 ORDER BY t.id DESC",
        (since,),
    )]
    today = local_today(zone(DEFAULT_TZ))
    for t in tenants:
        t["days_left"] = (date.fromisoformat(t["paid_until"]) - today).days
        t["page_url"] = site.page_url(t)
    prospects = [t for t in tenants if queries.is_prospect(t)]
    tenants = [t for t in tenants if not queries.is_prospect(t)]
    stats = {
        "total": len(tenants),
        "prospects": len(prospects),
        "paid": sum(1 for t in tenants if t["plan"] == "paid" and t["days_left"] >= 0),
        "trial": sum(1 for t in tenants if t["plan"] == "trial" and t["days_left"] >= 0),
        "expired": sum(1 for t in tenants if t["days_left"] < 0),
        "cars_30d": sum(t["cars_30d"] for t in tenants),
        "demos_24h": db.execute(
            "SELECT COUNT(*) FROM tenants WHERE is_demo = 1 AND created_at >= ?", (ts(days_ago(1)),)
        ).fetchone()[0],
    }
    return render_template("admin/index.html", tenants=tenants, prospects=prospects, stats=stats, today=today,
                           themes=themes.THEMES, default_look=themes.default())


def _tenant_or_404(tenant_id: int):
    tenant = get_db().execute("SELECT * FROM tenants WHERE id = ? AND is_demo = 0", (tenant_id,)).fetchone()
    if tenant is None:
        abort(404)
    return tenant


MAPS_RE = re.compile(r"https://(maps\.app\.goo\.gl|goo\.gl/maps|(www\.)?google\.[a-z.]+/maps|maps\.google\.[a-z.]+)[/?]\S*")


@bp.post("/prospects")
def add_prospects():
    """Car washes that haven't signed up, one per line: Name | Address | City | WhatsApp | Google Maps link.
    Each gets the starter price list, a booking page and a website to show its owner.
    A line with the name of one already here updates it (only the fields that aren't empty)."""
    db = get_db()
    added, updated, skipped = [], [], []
    for line in request.form.get("lines", "").splitlines()[:50]:
        if not line.strip():
            continue
        name, address, city, phone, maps = ([part.strip() for part in line.split("|")] + [""] * 4)[:5]
        name, address, city = name[:60], address[:120], city[:60]
        phone = normalize_phone(phone, "27")
        if not name or (phone and not phone_is_valid(phone)):
            skipped.append(f"{line.strip()} (needs a name and a valid WhatsApp number)")
            continue
        if maps and not MAPS_RE.fullmatch(maps):
            skipped.append(f"{name} (the Google Maps link should start with https://maps.app.goo.gl/ "
                           "or https://www.google.com/maps)")
            continue
        here = db.execute("SELECT * FROM tenants WHERE name = ? AND is_demo = 0 AND email IS NULL", (name,)).fetchone()
        with db:
            if here:
                db.execute("UPDATE tenants SET address = ?, city = ?, phone = ?, maps_url = ? WHERE id = ?",
                           (address or here["address"], city or here["city"], phone or here["phone"],
                            maps or here["maps_url"], here["id"]))
                updated.append(name)
            else:
                tid = seed.create_tenant(db, name=name, phone=phone, city=city, address=address,
                                         trial_days=PROSPECT_DAYS)
                db.execute("UPDATE tenants SET maps_url = ? WHERE id = ?", (maps, tid))
                added.append(name)
    if added:
        flash(f"Added {len(added)}: {', '.join(added)}.")
    if updated:
        flash(f"Updated {len(updated)}: {', '.join(updated)}.")
    for line in skipped:
        flash(f"Skipped: {line}", "error")
    return redirect(url_for("admin.index", _anchor="prospects"))


@bp.post("/tenant/<int:tid>/owner")
def hand_over(tid):
    """The owner said yes: their login, the look they picked, and the free pilot from today."""
    tenant = _tenant_or_404(tid)
    email = request.form.get("email", "").strip().lower()[:120]
    look = request.form.get("theme", "")
    db = get_db()
    if not queries.is_prospect(tenant):
        flash(f"{tenant['name']} already has an owner.", "error")
    elif not public.EMAIL_RE.match(email):
        flash("Enter the owner's email.", "error")
    elif db.execute("SELECT 1 FROM tenants WHERE email = ?", (email,)).fetchone():
        flash("That email already has an account.", "error")
    else:
        password = secrets.token_urlsafe(8)
        until = local_today(zone(DEFAULT_TZ)) + timedelta(days=current_app.config["PILOT_DAYS"])
        with db:
            db.execute(
                "UPDATE tenants SET email = ?, password_hash = ?, theme = ?, plan = 'trial', paid_until = ?, "
                "setup = 'prices' WHERE id = ?",
                (email, generate_password_hash(password), look if look in themes.THEMES else tenant["theme"],
                 until.isoformat(), tid),
            )
        queries.session_key(db, tid, rotate=True)
        flash(f"{tenant['name']} is theirs now. Login: {email}, password: {password} — send it to the owner and "
              f"ask them to change it. Free pilot until {until:%d %b %Y}.")
    return redirect(url_for("admin.index"))


@bp.post("/tenant/<int:tid>/extend")
def extend(tid):
    tenant = _tenant_or_404(tid)
    today = local_today(zone(DEFAULT_TZ))
    months = 12 if request.form.get("months") == "12" else 1
    base = max(date.fromisoformat(tenant["paid_until"]), today)
    until = add_months(base, months)
    db = get_db()
    with db:
        db.execute("UPDATE tenants SET paid_until = ?, plan = 'paid' WHERE id = ?", (until.isoformat(), tid))
    flash(f"{tenant['name']}: paid until {until:%d %b %Y}.")
    return redirect(url_for("admin.index"))


@bp.post("/tenant/<int:tid>/date")
def set_date(tid):
    tenant = _tenant_or_404(tid)
    try:
        until = date.fromisoformat(request.form.get("paid_until", ""))
    except ValueError:
        flash("Pick a date.", "error")
        return redirect(url_for("admin.index"))
    db = get_db()
    with db:
        db.execute("UPDATE tenants SET paid_until = ? WHERE id = ?", (until.isoformat(), tid))
    flash(f"{tenant['name']}: active until {until:%d %b %Y}.")
    return redirect(url_for("admin.index"))


@bp.post("/tenant/<int:tid>/toggle")
def toggle(tid):
    tenant = _tenant_or_404(tid)
    db = get_db()
    with db:
        db.execute("UPDATE tenants SET is_disabled = 1 - is_disabled WHERE id = ?", (tid,))
    flash(f"{tenant['name']}: {'enabled' if tenant['is_disabled'] else 'paused'}.")
    return redirect(url_for("admin.index"))


@bp.post("/tenant/<int:tid>/password")
def reset_password(tid):
    tenant = _tenant_or_404(tid)
    password = secrets.token_urlsafe(8)
    db = get_db()
    with db:
        db.execute("UPDATE tenants SET password_hash = ? WHERE id = ?", (generate_password_hash(password), tid))
    queries.session_key(db, tid, rotate=True)
    flash(f"New password for {tenant['email']}: {password} — send it to the owner and ask them to change it.")
    return redirect(url_for("admin.index"))


@bp.post("/tenant/<int:tid>/open")
def open_as(tid):
    """Look at a car wash's app exactly as the owner sees it (support)."""
    _tenant_or_404(tid)
    session["tenant_id"] = tid
    session["key"] = queries.session_key(get_db(), tid)
    return redirect(url_for("owner.board"))


@bp.post("/tenant/<int:tid>/domain")
def set_domain(tid):
    """The car wash's own address: its booking page on /, its app on /app."""
    tenant = _tenant_or_404(tid)
    domain = re.sub(r"^https?://", "", request.form.get("domain", "").strip().lower()).split("/")[0].rstrip(".")
    db = get_db()
    if domain and not DOMAIN_RE.fullmatch(domain):
        flash("Domain: an address like book.joeswash.co.za, without https://.", "error")
    elif domain and domain == re.sub(r":\d+$", "", request.host.lower()):
        flash("That's this site's own address. Use the car wash's domain.", "error")
    elif domain and db.execute("SELECT 1 FROM tenants WHERE domain = ? AND id != ?", (domain, tid)).fetchone():
        flash("Another car wash already has that domain.", "error")
    else:
        with db:
            db.execute("UPDATE tenants SET domain = ? WHERE id = ?", (domain, tid))
        flash(f"{tenant['name']}: " + (f"booking page on https://{domain}/ once its DNS points here."
                                        if domain else "own address removed."))
    return redirect(url_for("admin.index"))


STATIC = Path(__file__).parent / "static"


def _look(tenant) -> str:
    return tenant["theme"] if tenant["theme"] in themes.THEMES else themes.default()


def _font_files(look: str) -> list[str]:
    """The fonts a look uses: the families its CSS names."""
    if look == "classic":
        return []
    css = (STATIC / "themes" / f"{look}.css").read_text()
    faces = re.findall(r'font-family: ("[^"]+"); src: url\(/static/fonts/([^)]+)\)', (STATIC / "style.css").read_text())
    return [name for family, name in faces if family in css]


@bp.get("/tenant/<int:tid>/website")
def website(tid):
    """The ready-made website, as it will look on the car wash's own hosting."""
    tenant = _tenant_or_404(tid)
    files = {"theme.css": f"themes/{_look(tenant)}.css"}
    return site.website(tenant, lambda name: url_for("static", filename=files.get(name, name)), _look(tenant),
                        prices=False)


@bp.get("/tenant/<int:tid>/website.zip")
def website_zip(tid):
    """The same website as files to upload to the car wash's hosting (into public_html)."""
    tenant = _tenant_or_404(tid)
    look = _look(tenant)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", site.website(tenant, lambda name: name, look, prices=False))
        z.writestr("style.css", (STATIC / "style.css").read_text().replace("url(/static/fonts/", "url(fonts/"))
        if look != "classic":
            z.write(STATIC / "themes" / f"{look}.css", "theme.css")
        for name in _font_files(look):
            z.write(STATIC / "fonts" / name, f"fonts/{name}")
        z.write(STATIC / "icon.svg", "icon.svg")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{tenant['slug'] or 'car-wash'}-website.zip")
