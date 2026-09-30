from datetime import date, datetime, time, timedelta

from app import queries, reports, seed
from app.utils import (
    fill_template,
    money,
    normalize_phone,
    parse_money,
    plate_key,
    local_today,
    ts,
    zone,
)

from .conftest import Browser, signup


# --- helpers ------------------------------------------------------------------

def tenant_id(db, email="owner@example.com"):
    return db.execute("SELECT id FROM tenants WHERE email = ?", (email,)).fetchone()[0]


def ids(db, table, tid):
    return [r[0] for r in db.execute(f"SELECT id FROM {table} WHERE tenant_id = ? ORDER BY sort, id", (tid,))]


def add_car(owner, db, plate="CA 123-456", phone="082 111 2222", services=None, type_index=0, **extra):
    tid = tenant_id(db)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    data = {"plate": plate, "phone": phone, "make": "Toyota Corolla", "car_type_id": types[type_index],
            "service_id": services or [svcs[0]], **extra}
    resp = owner.post("/app/new", data)
    assert resp.status_code == 302, resp.data
    return db.execute("SELECT * FROM visits WHERE tenant_id = ? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()


# --- utils --------------------------------------------------------------------

def test_phone_normalisation():
    assert normalize_phone("082 123 4567") == "27821234567"
    assert normalize_phone("+27 82 123 4567") == "27821234567"
    assert normalize_phone("27821234567") == "27821234567"
    assert normalize_phone("0027821234567") == "27821234567"
    assert normalize_phone("821234567") == "27821234567"
    assert normalize_phone("") == ""


def test_money_parsing_and_formatting():
    assert parse_money("120") == 12000
    assert parse_money("120.5") == 12050
    assert parse_money("R 1 200,50") == 120050
    assert parse_money("1,200") == 120000
    assert parse_money("abc") is None
    assert parse_money("") is None
    assert money(120000) == "R1,200"
    assert money(12050) == "R120.50"


def test_plates_and_templates():
    assert plate_key("ca 123-456") == "CA123456"
    assert fill_template("Hi {plate}, {unknown}", {"plate": "CA 1"}) == "Hi CA 1, {unknown}"


# --- public -------------------------------------------------------------------

def test_landing_and_health(client):
    assert client.get("/").status_code == 200
    assert b"Try the live demo" in client.get("/").data
    assert client.get("/healthz").data == b"ok"
    assert client.get("/privacy").status_code == 200
    assert b"Disallow: /app" in client.get("/robots.txt").data


def test_app_requires_login(client):
    resp = client.get("/app/")
    assert resp.status_code == 302 and "/login" in resp.location


def test_post_without_csrf_is_rejected(client):
    assert client.post("/signup", data={"name": "x"}).status_code == 400


def test_signup_seeds_price_list_and_trial(owner, db):
    t = db.execute("SELECT * FROM tenants WHERE email = 'owner@example.com'").fetchone()
    assert t["phone"] == "27825551234"
    assert t["plan"] == "trial"
    assert date.fromisoformat(t["paid_until"]) >= date.today() + timedelta(days=29)
    assert len(ids(db, "car_types", t["id"])) == 4
    assert db.execute("SELECT COUNT(*) FROM prices p JOIN services s ON s.id = p.service_id "
                      "WHERE s.tenant_id = ?", (t["id"],)).fetchone()[0] == 20


def test_duplicate_email_and_login(browser):
    signup(browser)
    browser.post("/logout")
    browser.get("/signup")  # fresh page, fresh form token
    resp = signup(browser)
    assert b"already has an account" in resp.data
    assert b"Wrong email" in browser.post("/login", {"email": "owner@example.com", "password": "nope"}).data
    resp = browser.post("/login", {"email": "OWNER@example.com", "password": "secret123"})
    assert resp.status_code == 302 and resp.location.endswith("/app/")


# --- the main flow ---------------------------------------------------------------

def test_car_goes_through_the_whole_flow(owner, db):
    tid = tenant_id(db)
    owner.post("/app/settings/staff", {"new_name": "Sipho", "new_pay_type": "percent", "new_pay_value": "30"})
    washer = db.execute("SELECT id FROM washers WHERE tenant_id = ?", (tid,)).fetchone()[0]

    v = add_car(owner, db)
    assert v["status"] == "queued"
    assert v["price_cents"] == 7000  # Wash & Go, hatch/sedan
    assert v["phone"] == "27821112222"
    board = owner.get("/app/").data
    assert b"CA 123-456" in board

    owner.post(f"/app/visit/{v['id']}/start", {"washer_id": washer})
    v = db.execute("SELECT * FROM visits WHERE id = ?", (v["id"],)).fetchone()
    assert v["status"] == "washing" and v["started_at"]

    # the board's "Ready" button is a wa.me link with the message
    board = owner.get("/app/").data.decode()
    assert "https://wa.me/27821112222?text=" in board
    assert "is%20ready%20for%20collection" in board

    resp = owner.post(f"/app/visit/{v['id']}/ready", headers={"X-Requested-With": "fetch"})
    assert resp.json == {"ok": True}
    v = db.execute("SELECT * FROM visits WHERE id = ?", (v["id"],)).fetchone()
    assert v["status"] == "ready" and v["ready_at"] and v["notified_at"]

    owner.post(f"/app/visit/{v['id']}/collected")
    owner.post(f"/app/visit/{v['id']}/review")
    v = db.execute("SELECT * FROM visits WHERE id = ?", (v["id"],)).fetchone()
    assert v["status"] == "collected" and v["collected_at"] and v["review_requested_at"]


def test_price_from_matrix_and_manual_override(owner, db):
    tid = tenant_id(db)
    svcs = ids(db, "services", tid)
    v = add_car(owner, db, services=[svcs[1], svcs[4]], type_index=2)  # Wash & Vac + Tyre Shine, bakkie
    assert v["price_cents"] == 13000 + 2500
    v = add_car(owner, db, plate="CA 999", price="99.50")
    assert v["price_cents"] == 9950


def test_new_car_validation(owner, db):
    resp = owner.post("/app/new", {"plate": "", "phone": "12"})
    assert resp.status_code == 200
    assert b"Enter the number plate" in resp.data
    assert b"Choose at least one service" in resp.data


def test_returning_customer_and_loyalty(owner, db):
    owner.post("/app/settings/business", {
        "name": "Bubbles", "loyalty_every": "3", "country_code": "27",
        "timezone": "Africa/Johannesburg", "msg_ready": "{plate} ready", "msg_review": "review {review_link}",
    })
    add_car(owner, db, plate="GP 11 AA GP")
    add_car(owner, db, plate="gp11aagp", phone="")  # same car, typed differently
    assert db.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1

    d = owner.get("/app/lookup?plate=GP 11 AA GP").json
    assert d["found"] and d["visits"] == 2
    assert d["phone"] == "27821112222"
    assert d["loyalty"] == {"every": 3, "count": 2, "needed": 2, "next_free": True}

    v = add_car(owner, db, plate="GP 11 AA GP", is_free="1")
    assert v["is_free"] == 1 and v["price_cents"] == 0
    assert owner.get("/app/lookup?plate=GP11AAGP").json["loyalty"]["count"] == 0
    assert owner.get("/app/lookup?plate=ZZZ").json == {"found": False}


def test_edit_visit_and_move_status_back(owner, db):
    v = add_car(owner, db)
    owner.post(f"/app/visit/{v['id']}/start")
    owner.post(f"/app/visit/{v['id']}/ready")
    tid = tenant_id(db)
    resp = owner.post(f"/app/visit/{v['id']}", {
        "plate": "CA 123-456", "phone": "082 111 2222", "car_type_id": ids(db, "car_types", tid)[1],
        "service_id": ids(db, "services", tid)[0], "price": "", "status": "washing", "note": "mirror",
    })
    assert resp.status_code == 302
    v = db.execute("SELECT * FROM visits WHERE id = ?", (v["id"],)).fetchone()
    assert v["status"] == "washing" and v["started_at"] and v["ready_at"] is None
    assert v["price_cents"] == 9000 and v["note"] == "mirror"

    owner.post(f"/app/visit/{v['id']}/cancel")
    assert db.execute("SELECT status FROM visits WHERE id = ?", (v["id"],)).fetchone()[0] == "cancelled"
    owner.post(f"/app/visit/{v['id']}/delete")
    assert db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0


def test_car_wash_cannot_see_another_car_wash(app, owner, db):
    v = add_car(owner, db)
    other = Browser(app.test_client())
    signup(other, email="other@example.com", name="Other Wash")
    assert other.get(f"/app/visit/{v['id']}").status_code == 404
    assert other.post(f"/app/visit/{v['id']}/ready").status_code == 404
    assert other.get(f"/app/car/{v['vehicle_id']}").status_code == 404
    assert other.get("/app/lookup?plate=CA123456").json == {"found": False}
    assert b"CA 123-456" not in other.get("/app/cars").data


def test_search_and_car_page(owner, db):
    v = add_car(owner, db)
    assert b"CA 123-456" in owner.get("/app/cars?q=123").data
    assert b"CA 123-456" in owner.get("/app/cars?q=0821112222").data
    page = owner.get(f"/app/car/{v['vehicle_id']}").data
    assert b"History" in page and b"R70" in page
    owner.post(f"/app/car/{v['vehicle_id']}", {"plate": "CA 123-456", "make": "VW Polo", "phone": "083 000 0000"})
    assert db.execute("SELECT make, phone FROM vehicles").fetchone()[:] == ("VW Polo", "27830000000")


# --- reports ------------------------------------------------------------------

def test_reports_revenue_speed_and_pay(app, db):
    with db:
        tid = seed.create_tenant(db, name="R", email="r@example.com", password_hash="x")
        types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
        _, _, matrix = queries.price_list(db, tid)
        fast = db.execute("INSERT INTO washers (tenant_id, name, pay_type, pay_value, created_at) "
                          "VALUES (?, 'Fast', 'percent', 30, ?)", (tid, ts())).lastrowid
        slow = db.execute("INSERT INTO washers (tenant_id, name, pay_type, pay_value, created_at) "
                          "VALUES (?, 'Slow', 'fixed', 2000, ?)", (tid, ts())).lastrowid
        tz = zone("Africa/Johannesburg")
        morning = datetime.combine(local_today(tz), time(0, 30), tzinfo=tz)
        vehicle = queries.upsert_vehicle(db, tid, "CA 1", "", types[0], "")
        for i, (washer, minutes) in enumerate([(fast, 20), (fast, 20), (fast, 20), (slow, 30), (slow, 30), (slow, 30)]):
            start = ts(morning + timedelta(minutes=i))
            end = ts(morning + timedelta(minutes=i + minutes))
            vid = db.execute(
                "INSERT INTO visits (tenant_id, vehicle_id, car_type_id, price_cents, status, created_at, "
                "started_at, ready_at) VALUES (?, ?, ?, 7000, 'collected', ?, ?, ?)",
                (tid, vehicle, types[0], start, start, end),
            ).lastrowid
            queries.set_services(db, vid, [svcs[0]], matrix, types[0])
            queries.set_washers(db, tid, vid, [washer])
        tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone()

    with app.app_context():
        r = reports.build(db, tenant, tz, "today")
    assert r["cars"] == 6 and r["revenue"] == 42000 and r["avg_ticket"] == 7000
    staff = {s["name"]: s for s in r["staff"]}
    assert staff["Fast"]["speed"] == -20 and staff["Slow"]["speed"] == 20
    assert staff["Fast"]["earned"] == 3 * 2100  # 30% of R70
    assert staff["Slow"]["earned"] == 3 * 2000  # R20 per car
    assert r["services"][0] == {"name": "Wash & Go", "count": 6, "revenue": 42000}


def test_reports_page_renders_for_every_period(owner, db):
    add_car(owner, db)
    for key, _ in reports.PERIODS:
        assert owner.get(f"/app/reports?p={key}").status_code == 200


# --- settings -----------------------------------------------------------------

def test_price_list_and_staff_settings(owner, db):
    tid = tenant_id(db)
    svc, typ = ids(db, "services", tid)[0], ids(db, "car_types", tid)[0]
    owner.post("/app/settings/prices", {
        f"price_{svc}_{typ}": "85", f"service_name_{svc}": "Quick Wash", f"service_active_{svc}": "1",
        f"type_active_{typ}": "1", "new_service": "Wax",
    })
    assert db.execute("SELECT price_cents FROM prices WHERE service_id = ? AND car_type_id = ?",
                      (svc, typ)).fetchone()[0] == 8500
    assert db.execute("SELECT name FROM services WHERE id = ?", (svc,)).fetchone()[0] == "Quick Wash"
    assert db.execute("SELECT COUNT(*) FROM services WHERE tenant_id = ? AND name = 'Wax'", (tid,)).fetchone()[0] == 1

    owner.post("/app/settings/staff", {"new_name": "Thabo", "new_pay_type": "fixed", "new_pay_value": "25"})
    w = db.execute("SELECT * FROM washers WHERE tenant_id = ?", (tid,)).fetchone()
    assert (w["name"], w["pay_type"], w["pay_value"]) == ("Thabo", "fixed", 2500)
    for page in ("", "/business", "/prices", "/staff", "/account"):
        assert owner.get(f"/app/settings{page}").status_code == 200


def test_change_password(owner, db):
    owner.post("/app/settings/account", {"current": "secret123", "new": "newpass123"})
    owner.post("/logout")
    owner.get("/login")
    assert owner.post("/login", {"email": "owner@example.com", "password": "newpass123"}).status_code == 302


def test_expired_subscription_blocks_new_cars(owner, db):
    with db:
        db.execute("UPDATE tenants SET paid_until = ?", ((date.today() - timedelta(days=3)).isoformat(),))
    assert b"has ended" in owner.get("/app/").data
    resp = owner.get("/app/new")
    assert resp.status_code == 302


def test_owner_picks_a_look(owner, db):
    assert b"themes/carbon.css" in owner.get("/app/").data
    resp = owner.post("/app/settings/theme", {"theme": "noir"})
    assert resp.status_code == 302 and "theme=noir" in resp.headers["Set-Cookie"]
    assert db.execute("SELECT theme FROM tenants").fetchone()[0] == "noir"
    assert b"themes/noir.css" in owner.get("/app/settings").data
    assert owner.post("/app/settings/theme", {"theme": "nope"}).status_code == 400
    owner.post("/app/settings/theme", {"theme": "classic"})
    assert b"themes/" not in owner.get("/app/").data


def test_visitor_look_carries_into_signup_and_demo(app, browser, db):
    assert b"themes/carbon.css" in browser.get("/").data
    browser.post("/theme", {"theme": "nope"})
    assert b"themes/carbon.css" in browser.get("/").data
    resp = browser.post("/theme", {"theme": "volt"})
    assert resp.status_code == 302 and resp.location.endswith("/#look")
    assert b"themes/volt.css" in browser.get("/").data
    browser.post("/demo")
    assert db.execute("SELECT theme FROM tenants WHERE is_demo = 1").fetchone()[0] == "volt"
    browser.post("/logout")
    browser.get("/signup")
    signup(browser)
    assert db.execute("SELECT theme FROM tenants WHERE is_demo = 0").fetchone()[0] == "volt"


# --- demo & admin -------------------------------------------------------------

def test_demo_sandbox(app, browser, db):
    resp = browser.post("/demo")
    assert resp.status_code == 302 and resp.location.endswith("/app/")
    demo = db.execute("SELECT * FROM tenants WHERE is_demo = 1").fetchone()
    assert demo["email"] is None
    assert db.execute("SELECT COUNT(*) FROM visits WHERE tenant_id = ?", (demo["id"],)).fetchone()[0] > 100
    board = browser.get("/app/").data
    assert b"Demo car wash" in board and b"Ready for collection" in board
    for page in ("/app/cars", "/app/reports?p=7d", "/app/reports", "/app/settings/prices"):
        assert browser.get(page).status_code == 200
    # same visitor gets the same sandbox
    browser.post("/demo")
    assert db.execute("SELECT COUNT(*) FROM tenants WHERE is_demo = 1").fetchone()[0] == 1

    with db:
        db.execute("UPDATE tenants SET created_at = '2000-01-01 00:00:00' WHERE is_demo = 1")
        seed.cleanup_demos(db)
    assert db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0


def test_admin(app, owner, db):
    admin = Browser(app.test_client())
    assert admin.get("/admin/").status_code == 302
    assert b"Wrong password" in admin.post("/admin/login", {"password": "nope"}).data
    admin.post("/admin/login", {"password": "admin-pass"})
    page = admin.get("/admin/").data
    assert b"Bubbles Car Wash" in page

    tid = tenant_id(db)
    before = date.fromisoformat(db.execute("SELECT paid_until FROM tenants").fetchone()[0])
    admin.post(f"/admin/tenant/{tid}/extend", {"months": "1"})
    t = db.execute("SELECT * FROM tenants").fetchone()
    assert t["plan"] == "paid" and date.fromisoformat(t["paid_until"]) > before

    admin.post(f"/admin/tenant/{tid}/toggle")
    assert owner.get("/app/").status_code == 302  # paused account is logged out
    admin.post(f"/admin/tenant/{tid}/toggle")

    resp = admin.post(f"/admin/tenant/{tid}/password", follow_redirects=True)
    assert b"New password for owner@example.com" in resp.data
    assert admin.post(f"/admin/tenant/{tid}/open").location.endswith("/app/")


def test_admin_disabled_without_password(tmp_path):
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x", "DATABASE": str(tmp_path / "a.db"), "ADMIN_PASSWORD": ""})
    assert app.test_client().get("/admin/login").status_code == 404
