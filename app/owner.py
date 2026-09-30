"""The car wash owner's app: board, cars, reports, settings."""
import re
from datetime import timedelta

from flask import (
    Blueprint,
    abort,
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

from . import queries, reports, themes
from .db import get_db
from .utils import (
    TIMEZONES,
    clean_plate,
    format_phone,
    iso_z,
    local_today,
    money_input,
    normalize_phone,
    parse_money,
    phone_is_valid,
    plate_key,
    ts,
    utc_bounds,
    wa_link,
    zone,
)

bp = Blueprint("owner", __name__, url_prefix="/app")


@bp.before_request
def load_tenant():
    tenant_id = session.get("tenant_id")
    if not tenant_id:
        return redirect(url_for("public.login", next=request.path if request.method == "GET" else None))
    db = get_db()
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if tenant is None or tenant["is_disabled"]:
        session.pop("tenant_id", None)
        if tenant is not None:
            flash("This account is paused. Please contact support.", "error")
        return redirect(url_for("public.login"))
    g.tenant = tenant
    g.tz = zone(tenant["timezone"])
    g.today = local_today(g.tz)
    g.sub = queries.subscription(tenant, g.today)
    now = ts()
    if not tenant["last_seen_at"] or tenant["last_seen_at"][:15] != now[:15]:  # at most every 10 minutes
        with db:
            db.execute("UPDATE tenants SET last_seen_at = ? WHERE id = ?", (now, tenant_id))


def _tid() -> int:
    return g.tenant["id"]


def _done(message: str | None = None, to: str | None = None):
    """Board actions answer fetch() with JSON and plain forms with a redirect."""
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    if message:
        flash(message)
    return redirect(to or request.form.get("next") or url_for(".board"))


def _visit_or_404(visit_id: int) -> dict:
    visit = queries.get_visit(get_db(), _tid(), visit_id)
    if visit is None:
        abort(404)
    return visit


# --- board --------------------------------------------------------------------

@bp.get("/")
def board():
    db = get_db()
    start, end = utc_bounds(g.today, g.today + timedelta(days=1), g.tz)
    rows = db.execute(
        queries.VISIT_SELECT
        + " WHERE v.tenant_id = ? AND (v.status IN ('queued', 'washing', 'ready') "
        "OR (v.status = 'collected' AND v.collected_at >= ?)) ORDER BY v.id",
        (_tid(), start),
    ).fetchall()
    lanes = {"queued": [], "washing": [], "ready": [], "collected": []}
    for v in queries.with_details(db, rows):
        queries.add_links(g.tenant, v)
        v["since"] = iso_z(v["ready_at"] or v["started_at"] or v["created_at"])
        lanes[v["status"]].append(v)
    lanes["washing"].sort(key=lambda v: v["started_at"] or "")
    lanes["ready"].sort(key=lambda v: v["ready_at"] or "")
    lanes["collected"].sort(key=lambda v: v["collected_at"] or "", reverse=True)

    cars, revenue = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(price_cents), 0) FROM visits WHERE tenant_id = ? "
        "AND status != 'cancelled' AND created_at >= ? AND created_at < ?",
        (_tid(), start, end),
    ).fetchone()
    washers = db.execute(
        "SELECT * FROM washers WHERE tenant_id = ? AND active = 1 ORDER BY name", (_tid(),)
    ).fetchall()
    first_run = not db.execute("SELECT 1 FROM visits WHERE tenant_id = ? LIMIT 1", (_tid(),)).fetchone()
    return render_template(
        "app/board.html", lanes=lanes, cars=cars, revenue=revenue, washers=washers, first_run=first_run
    )


# --- visits -------------------------------------------------------------------

def _read_visit_form(db, visit=None):
    """Validate the car form. Returns (data, errors)."""
    f = request.form
    errors = []
    plate = clean_plate(f.get("plate"))
    if not plate_key(plate):
        errors.append("Enter the number plate.")
    phone = normalize_phone(f.get("phone"), g.tenant["country_code"])
    if phone and not phone_is_valid(phone):
        errors.append("That phone number doesn't look right.")

    car_types, services, matrix = queries.price_list(
        db, _tid(),
        keep_types=[visit["car_type_id"]] if visit else [],
        keep_services=visit["service_ids"] if visit else [],
    )
    type_ids = {t["id"] for t in car_types}
    service_ids = {s["id"] for s in services}
    car_type_id = int(f["car_type_id"]) if f.get("car_type_id", "").isdigit() else None
    if car_type_id not in type_ids:
        errors.append("Choose the car type.")
        car_type_id = None
    chosen = [int(x) for x in f.getlist("service_id") if x.isdigit() and int(x) in service_ids]
    if not chosen:
        errors.append("Choose at least one service.")

    is_free = f.get("is_free") == "1"
    price = 0
    if not is_free:
        price_text = f.get("price", "").strip()
        price = parse_money(price_text) if price_text else sum(
            matrix.get(s, {}).get(car_type_id, 0) for s in chosen
        )
        if price is None:
            errors.append("Price should be a number, e.g. 120 or 120.50.")
    data = {
        "plate": plate,
        "make": f.get("make", "").strip()[:40],
        "phone": phone,
        "car_type_id": car_type_id,
        "service_ids": chosen,
        "is_free": is_free,
        "price_cents": price or 0,
        "note": f.get("note", "").strip()[:200],
    }
    return data, errors, matrix


def _form_context(db, form: dict):
    car_types, services, matrix = queries.price_list(
        db, _tid(),
        keep_types=[form.get("car_type_id")],
        keep_services=form.get("service_ids", []),
    )
    return {
        "form": form,
        "car_types": car_types,
        "services": services,
        "price_data": {"matrix": {str(s): {str(t): p for t, p in row.items()} for s, row in matrix.items()}},
    }


@bp.get("/lookup")
def lookup():
    """Returning customer? Used by the new car form while the plate is typed."""
    db = get_db()
    vehicle = queries.find_vehicle(db, _tid(), request.args.get("plate", ""))
    if not vehicle:
        return jsonify(found=False)
    visits = db.execute(
        "SELECT COUNT(*) FROM visits WHERE vehicle_id = ? AND status != 'cancelled'", (vehicle["id"],)
    ).fetchone()[0]
    last = db.execute(
        "SELECT id FROM visits WHERE vehicle_id = ? AND status != 'cancelled' ORDER BY created_at DESC LIMIT 1",
        (vehicle["id"],),
    ).fetchone()
    last_services = [
        r[0] for r in db.execute("SELECT service_id FROM visit_services WHERE visit_id = ?", (last["id"],))
    ] if last else []
    return jsonify(
        found=True,
        plate=vehicle["plate"],
        make=vehicle["make"],
        phone=vehicle["phone"],
        phone_display=format_phone(vehicle["phone"]),
        car_type_id=vehicle["car_type_id"],
        visits=visits,
        last_service_ids=last_services,
        loyalty=queries.loyalty(db, g.tenant, vehicle["id"]),
    )


@bp.route("/new", methods=["GET", "POST"])
def new_visit():
    db = get_db()
    if not g.sub["active"]:
        flash("Your subscription has ended, so new cars can't be added. Your data is safe.", "error")
        return redirect(url_for(".board"))
    if request.method == "POST":
        data, errors, matrix = _read_visit_form(db)
        if not errors:
            with db:
                vehicle_id = queries.upsert_vehicle(
                    db, _tid(), data["plate"], data["make"], data["car_type_id"], data["phone"]
                )
                vehicle = db.execute("SELECT phone FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
                cur = db.execute(
                    "INSERT INTO visits (tenant_id, vehicle_id, car_type_id, phone, price_cents, is_free, note, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_tid(), vehicle_id, data["car_type_id"], vehicle["phone"], data["price_cents"],
                     int(data["is_free"]), data["note"], ts()),
                )
                queries.set_services(db, cur.lastrowid, data["service_ids"], matrix, data["car_type_id"])
            flash(f"{data['plate']} added.")
            return redirect(url_for(".board"))
        for e in errors:
            flash(e, "error")
        form = dict(request.form, service_ids=data["service_ids"], car_type_id=data["car_type_id"],
                    is_free=data["is_free"])
    else:
        car_types, _, _ = queries.price_list(db, _tid())
        form = {"plate": request.args.get("plate", ""), "car_type_id": car_types[0]["id"] if car_types else None,
                "service_ids": []}
        known = queries.find_vehicle(db, _tid(), form["plate"]) if form["plate"] else None
        if known:
            form.update(plate=known["plate"], make=known["make"], phone=format_phone(known["phone"]),
                        car_type_id=known["car_type_id"] or form["car_type_id"])
    return render_template("app/visit_new.html", **_form_context(db, form))


@bp.route("/visit/<int:vid>", methods=["GET", "POST"])
def visit(vid):
    db = get_db()
    v = _visit_or_404(vid)
    if request.method == "POST":
        data, errors, matrix = _read_visit_form(db, v)
        status = request.form.get("status", v["status"])
        if status not in queries.STATUS_LABELS:
            errors.append("Unknown status.")
        if not errors:
            with db:
                vehicle_id = queries.upsert_vehicle(
                    db, _tid(), data["plate"], data["make"], data["car_type_id"], data["phone"]
                )
                updates = {
                    "vehicle_id": vehicle_id,
                    "car_type_id": data["car_type_id"],
                    "phone": data["phone"] or db.execute(
                        "SELECT phone FROM vehicles WHERE id = ?", (vehicle_id,)
                    ).fetchone()[0],
                    "price_cents": data["price_cents"],
                    "is_free": int(data["is_free"]),
                    "note": data["note"],
                }
                if status != v["status"]:
                    updates.update(queries.status_updates(v, status))
                queries.apply_updates(db, vid, updates)
                if sorted(data["service_ids"]) != sorted(v["service_ids"]) or data["car_type_id"] != v["car_type_id"]:
                    queries.set_services(db, vid, data["service_ids"], matrix, data["car_type_id"])
                washer_ids = [int(x) for x in request.form.getlist("washer_id") if x.isdigit()]
                queries.set_washers(db, _tid(), vid, washer_ids)
            flash("Saved.")
            return redirect(url_for(".board") if status in ("queued", "washing", "ready") else url_for(".visit", vid=vid))
        for e in errors:
            flash(e, "error")
        form = dict(request.form, service_ids=data["service_ids"], car_type_id=data["car_type_id"],
                    is_free=data["is_free"], washer_ids=request.form.getlist("washer_id"))
    else:
        form = {
            "plate": v["plate"], "make": v["make"], "phone": format_phone(v["phone"]),
            "car_type_id": v["car_type_id"], "service_ids": v["service_ids"], "is_free": v["is_free"],
            "price": money_input(v["price_cents"]), "note": v["note"], "status": v["status"],
            "washer_ids": v["washer_ids"],
        }
    queries.add_links(g.tenant, v)
    washers = db.execute(
        f"SELECT * FROM washers WHERE tenant_id = ? AND (active = 1 OR id IN ({','.join('?' * len(v['washer_ids'])) or 'NULL'})) "
        "ORDER BY name",
        (_tid(), *v["washer_ids"]),
    ).fetchall()
    return render_template(
        "app/visit.html", v=v, washers=washers, statuses=queries.STATUS_LABELS, **_form_context(db, form)
    )


@bp.post("/visit/<int:vid>/start")
def start(vid):
    db = get_db()
    v = _visit_or_404(vid)
    with db:
        queries.set_washers(db, _tid(), vid, [x for x in request.form.getlist("washer_id") if x.isdigit()])
        if v["status"] == "queued":
            queries.apply_updates(db, vid, queries.status_updates(v, "washing"))
    return _done()


@bp.post("/visit/<int:vid>/ready")
def ready(vid):
    db = get_db()
    v = _visit_or_404(vid)
    with db:
        updates = {}
        if v["status"] in ("queued", "washing"):
            updates = {"status": "ready", "ready_at": ts()}
        if request.form.get("notified") == "1" or request.headers.get("X-Requested-With") == "fetch":
            updates["notified_at"] = ts()
        if updates:
            queries.apply_updates(db, vid, updates)
    return _done()


@bp.post("/visit/<int:vid>/collected")
def collected(vid):
    db = get_db()
    v = _visit_or_404(vid)
    with db:
        queries.apply_updates(db, vid, queries.status_updates(v, "collected"))
    return _done()


@bp.post("/visit/<int:vid>/review")
def review(vid):
    db = get_db()
    _visit_or_404(vid)
    with db:
        queries.apply_updates(db, vid, {"review_requested_at": ts()})
    return _done()


@bp.post("/visit/<int:vid>/cancel")
def cancel(vid):
    db = get_db()
    v = _visit_or_404(vid)
    with db:
        queries.apply_updates(db, vid, {"status": "cancelled"})
    return _done(f"{v['plate']} cancelled. It won't count in reports.")


@bp.post("/visit/<int:vid>/delete")
def delete(vid):
    db = get_db()
    v = _visit_or_404(vid)
    with db:
        db.execute("DELETE FROM visits WHERE id = ? AND tenant_id = ?", (vid, _tid()))
    return _done(f"Visit for {v['plate']} deleted.", url_for(".board"))


# --- cars / history -------------------------------------------------------------

@bp.get("/cars")
def cars():
    db = get_db()
    q = request.args.get("q", "").strip()
    vehicles = visits = []
    if q:
        key = plate_key(q)
        digits = re.sub(r"\D", "", q).lstrip("0")
        vehicles = db.execute(
            "SELECT vh.*, ct.name AS car_type, COUNT(v.id) AS visits, MAX(v.created_at) AS last_visit "
            "FROM vehicles vh LEFT JOIN visits v ON v.vehicle_id = vh.id AND v.status != 'cancelled' "
            "LEFT JOIN car_types ct ON ct.id = vh.car_type_id "
            "WHERE vh.tenant_id = ? AND (vh.plate_key LIKE ? OR (? != '' AND vh.phone LIKE ?) "
            "OR vh.make LIKE ?) GROUP BY vh.id ORDER BY last_visit DESC LIMIT 50",
            (_tid(), f"%{key}%", digits if len(digits) >= 4 else "", f"%{digits}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = db.execute(
            queries.VISIT_SELECT + " WHERE v.tenant_id = ? ORDER BY v.created_at DESC LIMIT 80", (_tid(),)
        ).fetchall()
        visits = queries.with_details(db, rows)
    return render_template("app/cars.html", q=q, vehicles=vehicles, visits=visits)


@bp.route("/car/<int:car_id>", methods=["GET", "POST"])
def car(car_id):
    db = get_db()
    vehicle = db.execute(
        "SELECT vh.*, ct.name AS car_type FROM vehicles vh LEFT JOIN car_types ct ON ct.id = vh.car_type_id "
        "WHERE vh.id = ? AND vh.tenant_id = ?",
        (car_id, _tid()),
    ).fetchone()
    if vehicle is None:
        abort(404)
    if request.method == "POST":
        plate = clean_plate(request.form.get("plate"))
        phone = normalize_phone(request.form.get("phone"), g.tenant["country_code"])
        other = queries.find_vehicle(db, _tid(), plate)
        if not plate_key(plate):
            flash("Enter the number plate.", "error")
        elif other and other["id"] != car_id:
            flash(f"{other['plate']} already exists.", "error")
        elif phone and not phone_is_valid(phone):
            flash("That phone number doesn't look right.", "error")
        else:
            with db:
                db.execute(
                    "UPDATE vehicles SET plate = ?, plate_key = ?, make = ?, phone = ? WHERE id = ?",
                    (plate, plate_key(plate), request.form.get("make", "").strip()[:40], phone, car_id),
                )
            flash("Saved.")
        return redirect(url_for(".car", car_id=car_id))
    rows = db.execute(
        queries.VISIT_SELECT + " WHERE v.vehicle_id = ? ORDER BY v.created_at DESC", (car_id,)
    ).fetchall()
    visits = queries.with_details(db, rows)
    counted = [v for v in visits if v["status"] != "cancelled"]
    return render_template(
        "app/car.html",
        car=vehicle,
        visits=visits,
        total=sum(v["price_cents"] for v in counted),
        count=len(counted),
        loyalty=queries.loyalty(db, g.tenant, car_id),
        chat_link=wa_link(vehicle["phone"]) if vehicle["phone"] else "",
    )


# --- reports ------------------------------------------------------------------

@bp.get("/reports")
def report():
    key = request.args.get("p", "today")
    if key not in dict(reports.PERIODS):
        key = "today"
    return render_template("app/reports.html", r=reports.build(get_db(), g.tenant, g.tz, key),
                           periods=reports.PERIODS)


# --- settings -----------------------------------------------------------------

@bp.get("/settings")
def settings():
    return render_template("app/settings.html")


@bp.route("/settings/business", methods=["GET", "POST"])
def settings_business():
    db = get_db()
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()[:60]
        review_url = f.get("review_url", "").strip()[:300]
        loyalty_every = f.get("loyalty_every", "0").strip() or "0"
        country_code = re.sub(r"\D", "", f.get("country_code", "")) or "27"
        errors = []
        if not name:
            errors.append("Enter your business name.")
        if review_url and not review_url.startswith(("https://", "http://")):
            errors.append("The review link should start with https://")
        if not loyalty_every.isdigit() or not (int(loyalty_every) == 0 or 2 <= int(loyalty_every) <= 50):
            errors.append("Loyalty: use 0 (off) or a number from 2 to 50.")
        if f.get("timezone") not in TIMEZONES:
            errors.append("Choose a time zone.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            with db:
                db.execute(
                    "UPDATE tenants SET name = ?, review_url = ?, loyalty_every = ?, country_code = ?, "
                    "timezone = ?, msg_ready = ?, msg_review = ? WHERE id = ?",
                    (name, review_url, int(loyalty_every), country_code[:4], f["timezone"],
                     f.get("msg_ready", "").strip()[:600], f.get("msg_review", "").strip()[:600], _tid()),
                )
            flash("Saved.")
            return redirect(url_for(".settings"))
    return render_template("app/settings_business.html", timezones=TIMEZONES)


@bp.route("/settings/prices", methods=["GET", "POST"])
def settings_prices():
    db = get_db()
    car_types = db.execute("SELECT * FROM car_types WHERE tenant_id = ? ORDER BY active DESC, sort, id", (_tid(),)).fetchall()
    services = db.execute("SELECT * FROM services WHERE tenant_id = ? ORDER BY active DESC, sort, id", (_tid(),)).fetchall()
    if request.method == "POST":
        f = request.form
        bad = []
        with db:
            for t in car_types:
                name = f.get(f"type_name_{t['id']}", t["name"]).strip()[:30] or t["name"]
                db.execute("UPDATE car_types SET name = ?, active = ? WHERE id = ?",
                           (name, int(f.get(f"type_active_{t['id']}") == "1"), t["id"]))
            for s in services:
                name = f.get(f"service_name_{s['id']}", s["name"]).strip()[:40] or s["name"]
                db.execute("UPDATE services SET name = ?, active = ? WHERE id = ?",
                           (name, int(f.get(f"service_active_{s['id']}") == "1"), s["id"]))
                for t in car_types:
                    field = f"price_{s['id']}_{t['id']}"
                    if field not in f:  # hidden car type: leave its prices alone
                        continue
                    raw = f[field].strip()
                    if not raw:
                        db.execute("DELETE FROM prices WHERE service_id = ? AND car_type_id = ?", (s["id"], t["id"]))
                        continue
                    cents = parse_money(raw)
                    if cents is None:
                        bad.append(f"{name} / {t['name']}")
                        continue
                    db.execute(
                        "INSERT INTO prices (service_id, car_type_id, price_cents) VALUES (?, ?, ?) "
                        "ON CONFLICT (service_id, car_type_id) DO UPDATE SET price_cents = excluded.price_cents",
                        (s["id"], t["id"], cents),
                    )
            new_type = f.get("new_type", "").strip()[:30]
            if new_type:
                db.execute("INSERT INTO car_types (tenant_id, name, sort) VALUES (?, ?, ?)",
                           (_tid(), new_type, len(car_types)))
            new_service = f.get("new_service", "").strip()[:40]
            if new_service:
                db.execute("INSERT INTO services (tenant_id, name, sort) VALUES (?, ?, ?)",
                           (_tid(), new_service, len(services)))
        if bad:
            flash("Some prices weren't numbers and were skipped: " + ", ".join(bad), "error")
        else:
            flash("Price list saved." + (" Now set prices for the new row." if new_type or new_service else ""))
        return redirect(url_for(".settings_prices"))
    _, _, matrix = queries.price_list(db, _tid())
    return render_template("app/settings_prices.html", car_types=car_types, services=services, matrix=matrix)


@bp.route("/settings/staff", methods=["GET", "POST"])
def settings_staff():
    db = get_db()
    washers = db.execute("SELECT * FROM washers WHERE tenant_id = ? ORDER BY active DESC, name", (_tid(),)).fetchall()
    if request.method == "POST":
        f = request.form
        errors = []

        def pay(prefix):
            pay_type = "fixed" if f.get(f"{prefix}_pay_type") == "fixed" else "percent"
            raw = f.get(f"{prefix}_pay_value", "").strip().rstrip("%").strip() or "0"
            if pay_type == "percent":
                value = int(raw) if raw.isdigit() and int(raw) <= 100 else None
            else:
                value = parse_money(raw)
            return pay_type, value

        with db:
            for w in washers:
                name = f.get(f"w{w['id']}_name", "").strip()[:30] or w["name"]
                pay_type, value = pay(f"w{w['id']}")
                if value is None:
                    errors.append(f"Check the pay for {name}.")
                    continue
                db.execute(
                    "UPDATE washers SET name = ?, pay_type = ?, pay_value = ?, active = ? WHERE id = ?",
                    (name, pay_type, value, int(f.get(f"w{w['id']}_active") == "1"), w["id"]),
                )
            new_name = f.get("new_name", "").strip()[:30]
            if new_name:
                pay_type, value = pay("new")
                if value is None:
                    errors.append(f"Check the pay for {new_name}.")
                else:
                    db.execute(
                        "INSERT INTO washers (tenant_id, name, pay_type, pay_value, created_at) VALUES (?, ?, ?, ?, ?)",
                        (_tid(), new_name, pay_type, value, ts()),
                    )
        for e in errors:
            flash(e, "error")
        if not errors:
            flash("Staff saved.")
        return redirect(url_for(".settings_staff"))
    return render_template("app/settings_staff.html", washers=washers)


@bp.route("/settings/theme", methods=["GET", "POST"])
def settings_theme():
    if request.method == "POST":
        name = request.form.get("theme", "")
        if name not in themes.THEMES:
            abort(400)
        db = get_db()
        with db:
            db.execute("UPDATE tenants SET theme = ? WHERE id = ?", (name, _tid()))
        return themes.remember(redirect(url_for(".settings_theme")), name)
    return render_template("app/settings_theme.html")


@bp.route("/settings/account", methods=["GET", "POST"])
def settings_account():
    db = get_db()
    if request.method == "POST" and not g.tenant["is_demo"]:
        current, new = request.form.get("current", ""), request.form.get("new", "")
        if not check_password_hash(g.tenant["password_hash"] or "", current):
            flash("Current password is wrong.", "error")
        elif len(new) < 8:
            flash("New password needs at least 8 characters.", "error")
        else:
            with db:
                db.execute("UPDATE tenants SET password_hash = ? WHERE id = ?", (generate_password_hash(new), _tid()))
            flash("Password changed.")
        return redirect(url_for(".settings_account"))
    return render_template("app/settings_account.html")
