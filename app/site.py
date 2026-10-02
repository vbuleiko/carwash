"""The car wash's own page for customers: pick a car, services, day and time."""
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import bookings, queries, seed
from .db import get_db
from .security import client_ip, limiter
from .utils import (
    clean_plate,
    is_number,
    local_today,
    normalize_phone,
    now_utc,
    phone_is_valid,
    to_local,
    zone,
)

bp = Blueprint("site", __name__, url_prefix="/book")


def site_tenant():
    """In site mode: the one car wash this installation belongs to."""
    return get_db().execute("SELECT * FROM tenants WHERE is_demo = 0 ORDER BY id LIMIT 1").fetchone()


def use(tenant):
    """Checks a car wash can be shown and makes it the page's car wash (look, time zone)."""
    if tenant is None or tenant["is_disabled"] or (tenant["is_demo"] and tenant["created_at"] < seed.demo_cutoff()):
        abort(404)
    g.site = tenant
    g.tz = zone(tenant["timezone"])
    g.today = local_today(g.tz)
    return tenant


def _by_slug(slug: str):
    tenant = get_db().execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
    if current_app.config["SITE_MODE"] and tenant is not None and tenant["is_demo"]:
        abort(404)
    return use(tenant)


def page_url(tenant) -> str:
    if current_app.config["SITE_MODE"]:
        return url_for("public.landing", _external=True)
    return url_for("site.page", slug=tenant["slug"], _external=True)


def _place(tenant) -> str:
    parts = [tenant["address"], tenant["city"]]
    if parts[0] and parts[1] and parts[1].lower() in parts[0].lower():
        parts.pop()
    return ", ".join(p for p in parts if p)


def _owner_view(tenant) -> bool:
    return session.get("tenant_id") == tenant["id"] and session.get("key") == tenant["session_key"]


def _wa(tenant, text: str) -> str:
    if tenant["is_demo"]:
        return "#"
    return queries.chat_link(tenant, tenant["phone"], text) if tenant["phone"] else ""


def _common(tenant) -> dict:
    week = bookings.hours(tenant)
    span = week[g.today.weekday()]
    now = now_utc().astimezone(g.tz).strftime("%H:%M")
    return {
        "t": tenant,
        "place": _place(tenant),
        "week": [(name, bookings.hours_text(s)) for name, s in zip(bookings.WEEKDAYS, week)],
        "today_index": g.today.weekday(),
        "today_hours": bookings.hours_text(span),
        "open_now": bool(span) and span[0] <= now < span[1],
        "owner_view": _owner_view(tenant),
        "book_url": url_for("public.landing") if current_app.config["SITE_MODE"]
        else url_for("site.page", slug=tenant["slug"]),
    }


def render_page(tenant, form=None):
    db = get_db()
    use(tenant)
    car_types, services, matrix = queries.price_list(db, tenant["id"])
    is_open = bookings.can_book(tenant, g.today) and bool(car_types) and bool(services)
    days = bookings.days(db, tenant) if is_open else []
    form = form or {}
    values = {d["date"].isoformat() for d in days if d["slots"]}
    picked_day = (form.get("slot") or "")[:10]
    if picked_day not in values:
        picked_day = next((d["date"].isoformat() for d in days if d["slots"]), "")
    type_ids = [t["id"] for t in car_types]
    car_type_id = form.get("car_type_id") if form.get("car_type_id") in type_ids else next(iter(type_ids), None)
    chosen = form.get("service_ids", [])
    return render_template(
        "site/book.html",
        is_open=is_open,
        car_types=car_types,
        services=services,
        matrix=matrix,
        price_data={"matrix": {str(s): {str(t): p for t, p in row.items()} for s, row in matrix.items()}},
        days=days,
        picked_day=picked_day,
        car_type_id=car_type_id,
        chosen=chosen,
        total=sum(matrix.get(s, {}).get(car_type_id, 0) for s in chosen),
        form=form,
        wa=_wa(tenant, f"Hi {tenant['name']}! I'd like to book a wash."),
        **_common(tenant),
    )


@bp.get("/<slug>")
def page(slug):
    tenant = _by_slug(slug)
    if current_app.config["SITE_MODE"]:
        return redirect(url_for("public.landing"))
    return render_page(tenant)


@bp.post("/<slug>")
def book(slug):
    tenant = _by_slug(slug)
    db = get_db()
    if not bookings.can_book(tenant, g.today):
        flash("Online booking is closed right now.", "error")
        return redirect(url_for(".page", slug=slug))
    f = request.form
    car_types, services, matrix = queries.price_list(db, tenant["id"])
    errors = []
    car_type_id = int(f["car_type_id"]) if is_number(f.get("car_type_id")) else None
    if car_type_id not in {t["id"] for t in car_types}:
        errors.append("Choose your car type.")
        car_type_id = None
    service_ids = {s["id"] for s in services}
    chosen = list(dict.fromkeys(int(x) for x in f.getlist("service_id") if is_number(x) and int(x) in service_ids))
    if not chosen:
        errors.append("Choose at least one service.")
    unpriced = [s["name"] for s in services if s["id"] in chosen and car_type_id not in matrix.get(s["id"], {})]
    if car_type_id and unpriced:  # no price for this car type: the car wash doesn't take it online
        errors.append(f"{', '.join(unpriced)} can't be booked online for this car. Message us about it.")
    if not f.get("slot"):
        errors.append("Pick a day and a time.")
    name = f.get("name", "").strip()[:60]
    if not name:
        errors.append("Enter your name.")
    phone = normalize_phone(f.get("phone"), tenant["country_code"])
    if not phone_is_valid(phone):
        errors.append("Enter your WhatsApp number, so we can confirm your booking.")
    if not errors and not limiter.allow(f"book:{tenant['id']}:{client_ip()}", 10, 3600):  # mobile networks share IPs
        errors.append("Too many bookings from this network. Please try again later.")
    if not errors:
        db.execute("BEGIN IMMEDIATE")  # two people taking the last place at once
        with db:
            slot_at = bookings.free_slot(db, tenant, f.get("slot"))
            if slot_at:
                code = bookings.create(
                    db, tenant["id"], slot_at=slot_at, name=name, phone=phone, plate=clean_plate(f.get("plate")),
                    car_type_id=car_type_id, service_ids=chosen,
                    price_cents=sum(matrix.get(s, {}).get(car_type_id, 0) for s in chosen),
                    note=f.get("note", "").strip()[:200],
                )
        if slot_at:
            return redirect(url_for(".done", slug=slug, code=code))
        errors.append("Sorry, that time is no longer free. Please pick another one.")
    for e in errors:
        flash(e, "error")
    return render_page(tenant, dict(f, car_type_id=car_type_id, service_ids=chosen))


STATUS_TITLES = {
    "new": "Booking received",
    "confirmed": "You're booked",
    "arrived": "Thanks for coming",
    "cancelled": "Booking cancelled",
}


@bp.get("/<slug>/<code>")
def done(slug, code):
    tenant = _by_slug(slug)
    db = get_db()
    rows = db.execute(bookings.SELECT + " WHERE b.tenant_id = ? AND b.code = ?", (tenant["id"], code)).fetchall()
    if not rows:
        abort(404)
    b = bookings.with_services(db, rows)[0]
    when = bookings.message_values(tenant, b)
    when["day"] = bookings.day_name(to_local(b["slot_at"], g.tz).date(), g.today)
    tell = (f"Hi {tenant['name']}! I've booked {when['services']} for {when['date']} at {when['time']}. "
            f"— {b['name']}" + (f", {b['plate']}" if b["plate"] else ""))
    return render_template("site/done.html", b=b, when=when, title=STATUS_TITLES[b["status"]], tell=tell,
                           tell_link=_wa(tenant, tell), **_common(tenant))
