"""Your own back office: every car wash, trials, payments, own addresses and websites."""
import calendar
import io
import re
import secrets
import zipfile
from datetime import date
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

from . import queries, site, themes
from .db import get_db
from .security import client_ip, limiter, same
from .utils import DEFAULT_TZ, local_today, ts, days_ago, zone

bp = Blueprint("admin", __name__, url_prefix="/admin")


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


DOMAIN_RE = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


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
    stats = {
        "total": len(tenants),
        "paid": sum(1 for t in tenants if t["plan"] == "paid" and t["days_left"] >= 0),
        "trial": sum(1 for t in tenants if t["plan"] == "trial" and t["days_left"] >= 0),
        "expired": sum(1 for t in tenants if t["days_left"] < 0),
        "cars_30d": sum(t["cars_30d"] for t in tenants),
        "demos_24h": db.execute(
            "SELECT COUNT(*) FROM tenants WHERE is_demo = 1 AND created_at >= ?", (ts(days_ago(1)),)
        ).fetchone()[0],
    }
    return render_template("admin/index.html", tenants=tenants, stats=stats, today=today)


def _tenant_or_404(tenant_id: int):
    tenant = get_db().execute("SELECT * FROM tenants WHERE id = ? AND is_demo = 0", (tenant_id,)).fetchone()
    if tenant is None:
        abort(404)
    return tenant


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


def _website(tenant, asset) -> str:
    _, services, _ = queries.price_list(get_db(), tenant["id"])
    book = site.page_url(tenant)
    place = site.place(tenant)
    wa = f"Hi {tenant['name']}! I'd like to book a wash."
    return render_template(
        "site/website.html", t=tenant, look=_look(tenant), asset=asset, services=services, place=place,
        book=book, login=book + "login" if tenant["domain"] else url_for("public.login", _external=True),
        map_query=", ".join(p for p in (tenant["name"], place) if p), year=date.today().year,
        wa=queries.chat_link(tenant, tenant["phone"], wa) if tenant["phone"] else "",
    )


@bp.get("/tenant/<int:tid>/website")
def website(tid):
    """The ready-made website, as it will look on the car wash's own hosting."""
    tenant = _tenant_or_404(tid)
    files = {"theme.css": f"themes/{_look(tenant)}.css"}
    return _website(tenant, lambda name: url_for("static", filename=files.get(name, name)))


@bp.get("/tenant/<int:tid>/website.zip")
def website_zip(tid):
    """The same website as files to upload to the car wash's hosting (into public_html)."""
    tenant = _tenant_or_404(tid)
    look = _look(tenant)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", _website(tenant, lambda name: name))
        z.writestr("style.css", (STATIC / "style.css").read_text().replace("url(/static/fonts/", "url(fonts/"))
        if look != "classic":
            z.write(STATIC / "themes" / f"{look}.css", "theme.css")
        for name in _font_files(look):
            z.write(STATIC / "fonts" / name, f"fonts/{name}")
        z.write(STATIC / "icon.svg", "icon.svg")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{tenant['slug'] or 'car-wash'}-website.zip")
