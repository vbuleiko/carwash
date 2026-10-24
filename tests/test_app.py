import json
import re
from datetime import date, datetime, time, timedelta

import brotli

from app import queries, reports, seed
from app.utils import (
    DEFAULT_TZ,
    fill_template,
    money,
    normalize_phone,
    now_utc,
    parse_money,
    phone_is_valid,
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
    assert normalize_phone("+27 (0)82 123 4567") == "27821234567"
    assert normalize_phone("27 082 123 4567") == "27821234567"
    assert normalize_phone("") == ""
    assert phone_is_valid("27821234567") and phone_is_valid("447911123456")
    assert not phone_is_valid("2782123456")


def test_money_parsing_and_formatting():
    assert parse_money("120") == 12000
    assert parse_money("120.5") == 12050
    assert parse_money("R 1 200,50") == 120050
    assert parse_money("1,200") == 120000
    assert parse_money("abc") is None
    assert parse_money(",50") is None
    assert parse_money("99999999999999999999") is None
    assert parse_money("1,234.50") == 123450
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


def test_post_without_csrf_is_rejected(client, db):
    assert client.post("/demo").status_code == 400
    assert client.post("/signup", data={"name": "x"}).status_code == 303
    assert db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0


def test_expired_form_comes_back_filled_in(browser):
    browser.get("/login")
    resp = browser.post("/login", {"email": "Owner@Example.com", "password": "x", "csrf": "old"})
    assert resp.status_code == 303 and resp.location.endswith("/login")
    page = browser.get("/login").data
    assert b'value="Owner@Example.com"' in page and b"open for a while" in page
    assert b'value="Owner@Example.com"' not in browser.get("/login").data  # only once
    resp = browser.post("/signup?plan=pilot", {"name": "Bubbles", "phone": "082 555 1234", "email": "a@b.co",
                                               "password": "secret123", "csrf": "old"})
    assert resp.location.endswith("/signup?plan=pilot")
    page = browser.get("/signup?plan=pilot").data
    assert b'value="Bubbles"' in page and b'value="082 555 1234"' in page and b"secret123" not in page
    assert browser.post("/demo", {"csrf": "old"}).status_code == 400


def test_form_cookie_outlives_a_browser_restart(client):
    resp = client.get("/login")
    assert "Expires=" in resp.headers["Set-Cookie"]


class NoCookies:
    """A browser that keeps no cookies: Samsung Internet with cookies off, on a K&L supervisor's phone."""

    def __init__(self, app, agent="SamsungBrowser/30.0", origin="http://localhost"):
        self.client = app.test_client(use_cookies=False)
        self.headers = {"User-Agent": agent, **({"Origin": origin} if origin else {})}
        self.token = ""

    def get(self, url):
        resp = self.client.get(url, headers=self.headers)
        m = re.search(rb'name="csrf" content="([^"]+)"', resp.data)
        if m:
            self.token = m.group(1).decode()
        return resp

    def post(self, url, data=None):
        return self.client.post(url, data={"csrf": self.token, **(data or {})}, headers=self.headers)


LOGIN = {"email": "owner@example.com", "password": "secret123"}


def links(db):
    return db.execute("SELECT COUNT(*) FROM link_sessions").fetchone()[0]


def test_browser_without_cookies_logs_in_by_link(app, owner, db):
    phone = NoCookies(app)
    phone.get("/login")
    resp = phone.post("/login", LOGIN)
    sid = re.fullmatch(r"/app/\?s=([\w-]{32})", resp.location)[1]
    board = phone.get(resp.location).data.decode()
    assert f'href="/app/cars?s={sid}"' in board and f'href="/app/settings?s={sid}"' in board
    page = phone.get(f"/app/settings?s={sid}").data.decode()
    assert f'action="/logout?s={sid}"' in page
    resp = phone.post(f"/app/settings/theme?s={sid}", {"theme": "noir"})
    assert resp.status_code == 302 and resp.location.endswith(f"?s={sid}")
    assert db.execute("SELECT theme FROM tenants WHERE email = 'owner@example.com'").fetchone()[0] == "noir"
    new = phone.get(f"/app/new?s={sid}").data.decode()
    assert f'data-lookup="/app/lookup?s={sid}"' in new
    assert phone.get(f"/app/lookup?s={sid}&plate=CA1").json == {"found": False}
    assert f'<input type="hidden" name="s" value="{sid}">' in phone.get(f"/app/cars?s={sid}").data.decode()
    bookings = phone.get(f"/app/bookings?s={sid}").data.decode()
    assert f"/book/{SLUG}" in bookings and "?s=" not in bookings.split("/book/", 1)[1].split('"', 1)[0]
    assert NoCookies(app, agent="Another phone").get(f"/app/?s={sid}").location.startswith("/login")
    phone.post(f"/logout?s={sid}")
    assert links(db) == 0 and phone.get(f"/app/?s={sid}").location.startswith("/login")


def test_browser_without_cookies_is_not_logged_in_from_another_site(app, owner, db):
    for origin in ("https://evil.example", None):
        phone = NoCookies(app, origin=origin)
        phone.get("/login")
        resp = phone.post("/login", LOGIN)
        assert resp.status_code == 303 and "s=" not in resp.location
    assert NoCookies(app, origin="https://evil.example").post("/demo").status_code == 400
    assert links(db) == 0


def test_tab_restored_without_the_cookie_logs_in_with_plain_addresses(app, owner, db):
    tab = Browser(app.test_client())
    tab.get("/login")
    tab.client.delete_cookie("session")
    resp = tab.post("/login", LOGIN, headers={"Origin": "http://localhost"})
    assert "?s=" in resp.location and links(db) == 1
    resp = tab.get(resp.location)
    assert (resp.status_code, resp.location) == (302, "/app/") and links(db) == 0
    assert tab.get("/app/").status_code == 200


def test_an_old_cookie_coming_first_does_not_hide_the_login(app, owner):
    good = owner.client.get_cookie("session").value
    other = app.test_client(use_cookies=False)
    assert other.get("/app/", headers={"Cookie": f"session=old.stale.cookie; session={good}"}).status_code == 200


def test_customer_without_cookies_can_book(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    phone = NoCookies(app)
    page = phone.get(f"/book/{SLUG}")
    resp = phone.post(f"/book/{SLUG}", {"car_type_id": types[0], "service_id": [svcs[0]], "slot": first_slot(page.data),
                                        "name": "Thandi", "phone": "083 222 3333"})
    assert resp.status_code == 302 and "s=" not in resp.location
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1 and links(db) == 0


def test_signup_seeds_price_list_and_trial(owner, db):
    t = db.execute("SELECT * FROM tenants WHERE email = 'owner@example.com'").fetchone()
    assert t["phone"] == "27825551234"
    assert t["plan"] == "trial"
    assert date.fromisoformat(t["paid_until"]) >= date.today() + timedelta(days=29)
    assert len(ids(db, "car_types", t["id"])) == 4
    assert db.execute("SELECT COUNT(*) FROM prices p JOIN services s ON s.id = p.service_id "
                      "WHERE s.tenant_id = ?", (t["id"],)).fetchone()[0] == 20


def test_first_setup_step_by_step(browser, db):
    resp = signup(browser)
    assert resp.location.endswith("/app/setup")
    assert browser.get("/app/").location.endswith("/app/setup")  # the board waits until setup is done
    assert browser.get("/app/setup").location.endswith("/app/settings/prices")  # no price reader here
    page = browser.get("/app/settings/prices").data
    assert b"Step 1 of 4" in page and b'class="tabbar"' not in page
    assert browser.post("/app/settings/prices", {"new_service": "Wax"}).location.endswith("/app/settings/prices")
    resp = browser.post("/app/settings/prices", {})
    assert resp.location.endswith("/app/setup") and browser.get(resp.location).location.endswith("/app/setup/hours")

    browser.get("/app/setup/hours")
    resp = browser.post("/app/setup/hours", {"open_0": "1", "from_0": "07:00", "to_0": "18:00"})
    assert browser.get(resp.location).location.endswith("/app/setup/staff")
    t = db.execute("SELECT * FROM tenants").fetchone()
    assert json.loads(t["hours"]) == [["07:00", "18:00"], None, None, None, None, None, None]

    assert b'name="name_0"' not in browser.get("/app/setup/staff").data  # how many first
    page = browser.post("/app/setup/staff", {"resize": "2", "name_0": "Sipho"}).data  # "+" without JavaScript
    assert b'value="Sipho"' in page and b'name="name_1"' in page and b'name="name_2"' not in page
    resp = browser.post("/app/setup/staff", {"name_0": "Sipho", "name_1": "  thabo ", "name_2": "SIPHO"})
    assert browser.get(resp.location).location.endswith("/app/setup/look")
    assert [r[0] for r in db.execute("SELECT name FROM washers ORDER BY id")] == ["Sipho", "thabo"]

    look = browser.get("/app/setup/look")
    assert b"Step 4 of 4" in look.data and b'src="/site/bubbles-car-wash?embed=1"' in look.data
    assert b'src="/book/bubbles-car-wash?embed=1"' in look.data and b"Your own design" in look.data
    assert b"wa.me/27820000000?text=Hello" in look.data
    shown = browser.get("/book/bubbles-car-wash?embed=1")
    assert b"owner-bar" not in shown.data and shown.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in shown.headers["Content-Security-Policy"]
    assert b"owner-bar" in browser.get("/book/bubbles-car-wash").data
    assert browser.get("/site/bubbles-car-wash?embed=1").headers["X-Frame-Options"] == "SAMEORIGIN"
    assert look.headers["X-Frame-Options"] == "DENY"
    resp = browser.post("/app/settings/theme", {"theme": "noir", "next": "/app/setup/look"})
    assert resp.location.endswith("/app/setup/look")
    resp = browser.post("/app/setup/next", {"step": "look"})
    assert resp.location.endswith("/app/setup/done")
    done = browser.get("/app/setup/done").data
    assert b"/site/bubbles-car-wash" in done and b"/book/bubbles-car-wash" in done
    assert browser.get("/app/").status_code == 200
    assert browser.post("/app/setup/next", {"step": "look"}).location.endswith("/app/setup")  # an old tab


def test_setup_goes_on_where_it_stopped(browser):
    signup(browser)
    browser.get("/app/setup")
    browser.post("/app/setup/next", {"step": "prices"})
    browser.post("/app/setup/next", {"step": "hours"})
    browser.post("/logout")
    browser.get("/login")
    resp = browser.post("/login", {"email": "owner@example.com", "password": "secret123"})
    assert browser.get(browser.get(resp.location).location).location.endswith("/app/setup/staff")


def test_no_washers_no_washer_questions(owner, db):
    v = add_car(owner, db)
    board = owner.get("/app/").data
    assert b"start-dialog" not in board and b"add your washers" not in board.lower()
    owner.post(f"/app/visit/{v['id']}/start")
    assert db.execute("SELECT status FROM visits WHERE id = ?", (v["id"],)).fetchone()[0] == "washing"
    assert b"Washed by" not in owner.get(f"/app/visit/{v['id']}").data
    assert b"<h2>Staff</h2>" not in owner.get("/app/reports").data
    owner.post("/app/settings/staff", {"new_name": "Sipho"})
    assert b"start-dialog" in owner.get("/app/").data


def test_demo_has_no_setup(browser):
    browser.post("/demo")
    assert browser.get("/app/").status_code == 200


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
    owner.post("/app/settings/staff", {"new_name": "Sipho"})
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
    v = add_car(owner, db, plate="GP 11 AA GP")
    owner.post(f"/app/visit/{v['id']}/collected")
    v = add_car(owner, db, plate="gp11aagp", phone="")  # same car, typed differently
    owner.post(f"/app/visit/{v['id']}/collected")
    assert db.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1

    d = owner.get("/app/lookup?plate=GP 11 AA GP").json
    assert d["found"] and d["visits"] == 2
    assert d["phone"] == "27821112222"
    assert d["loyalty"] == {"every": 3, "count": 2, "needed": 2, "next_free": True}

    v = add_car(owner, db, plate="GP 11 AA GP", is_free="1")
    assert v["is_free"] == 1 and v["price_cents"] == 0
    assert owner.get("/app/lookup?plate=GP11AAGP").json["loyalty"]["count"] == 0
    assert owner.get("/app/lookup?plate=ZZZ").json == {"found": False}


def test_a_car_cannot_be_on_the_board_twice(owner, db):
    tid = tenant_id(db)
    v = add_car(owner, db)
    d = owner.get("/app/lookup?plate=ca123456").json
    assert d["on_board"] == {"status": "Waiting", "url": f"/app/visit/{v['id']}"}
    resp = owner.post("/app/new", {"plate": "CA123456", "car_type_id": ids(db, "car_types", tid)[0],
                                   "service_id": ids(db, "services", tid)[0]})
    assert b"CA 123-456 is already on the board" in resp.data
    assert db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 1

    other = add_car(owner, db, plate="CA 999")
    form = {"plate": "CA 123-456", "car_type_id": ids(db, "car_types", tid)[0],
            "service_id": ids(db, "services", tid)[0], "status": "queued", "status_was": "queued"}
    assert b"already on the board" in owner.post(f"/app/visit/{other['id']}", form).data
    owner.post(f"/app/visit/{v['id']}/collected")
    assert owner.get("/app/lookup?plate=ca123456").json["on_board"] is None
    add_car(owner, db)
    assert db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 3


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

def test_team_can_be_cleared_and_start_keeps_it(owner, db):
    tid = tenant_id(db)
    owner.post("/app/settings/staff", {"new_name": "Sipho"})
    washer = db.execute("SELECT id FROM washers").fetchone()[0]
    v = add_car(owner, db)
    form = {"plate": "CA 123-456", "phone": "082 111 2222", "car_type_id": ids(db, "car_types", tid)[0],
            "service_id": ids(db, "services", tid)[0], "price": "", "status": "queued"}
    owner.post(f"/app/visit/{v['id']}", {**form, "washer_id": washer})
    owner.post(f"/app/visit/{v['id']}/start")
    assert db.execute("SELECT COUNT(*) FROM visit_washers").fetchone()[0] == 1
    owner.post(f"/app/visit/{v['id']}", {**form, "status": "washing"})
    assert db.execute("SELECT COUNT(*) FROM visit_washers").fetchone()[0] == 0


def test_tap_on_another_phone_is_not_undone(owner, db):
    tid = tenant_id(db)
    v = add_car(owner, db)
    owner.post(f"/app/visit/{v['id']}/start")
    assert b'name="status_was" value="washing"' in owner.get(f"/app/visit/{v['id']}").data
    owner.post(f"/app/visit/{v['id']}/ready")  # meanwhile, on the tablet
    form = {"plate": "CA 123-456", "phone": "082 111 2222", "car_type_id": ids(db, "car_types", tid)[0],
            "service_id": ids(db, "services", tid)[0], "price": "70", "note": "mirror"}
    owner.post(f"/app/visit/{v['id']}", {**form, "status": "washing", "status_was": "washing"})
    v = db.execute("SELECT * FROM visits WHERE id = ?", (v["id"],)).fetchone()
    assert (v["status"], v["note"]) == ("ready", "mirror") and v["ready_at"]
    owner.post(f"/app/visit/{v['id']}", {**form, "status": "queued", "status_was": "washing"})
    assert db.execute("SELECT status FROM visits WHERE id = ?", (v["id"],)).fetchone()[0] == "queued"

    owner.post(f"/app/visit/{v['id']}/cancel")
    owner.post(f"/app/visit/{v['id']}/collected")  # a stale board
    assert db.execute("SELECT status FROM visits WHERE id = ?", (v["id"],)).fetchone()[0] == "cancelled"



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

def test_reports_revenue_and_speed(app, db):
    with db:
        tid = seed.create_tenant(db, name="R", email="r@example.com", password_hash="x")
        types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
        _, _, matrix = queries.price_list(db, tid)
        fast = db.execute("INSERT INTO washers (tenant_id, name, created_at) VALUES (?, 'Fast', ?)",
                          (tid, ts())).lastrowid
        slow = db.execute("INSERT INTO washers (tenant_id, name, created_at) VALUES (?, 'Slow', ?)",
                          (tid, ts())).lastrowid
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
    assert staff["Fast"]["cars"] == 3 and "earned" not in staff["Fast"]
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
        f"service_minutes_{svc}": "20", f"type_active_{typ}": "1", "new_service": "Wax",
    })
    assert db.execute("SELECT price_cents FROM prices WHERE service_id = ? AND car_type_id = ?",
                      (svc, typ)).fetchone()[0] == 8500
    assert db.execute("SELECT name, minutes FROM services WHERE id = ?", (svc,)).fetchone()[:] == ("Quick Wash", 20)
    assert db.execute("SELECT minutes FROM services WHERE tenant_id = ? AND name = 'Wax'", (tid,)).fetchone()[0] == 30
    resp = owner.post("/app/settings/prices", {f"service_minutes_{svc}": "half an hour"}, follow_redirects=True)
    assert b"Quick Wash: minutes" in resp.data
    assert db.execute("SELECT minutes FROM services WHERE id = ?", (svc,)).fetchone()[0] == 20

    owner.post("/app/settings/staff", {"new_name": "Thabo"})
    w = db.execute("SELECT * FROM washers WHERE tenant_id = ?", (tid,)).fetchone()
    assert (w["name"], w["active"]) == ("Thabo", 1)
    for page in ("", "/business", "/prices", "/staff", "/account"):
        assert owner.get(f"/app/settings{page}").status_code == 200


def test_change_password(owner, db):
    owner.post("/app/settings/account", {"current": "secret123", "new": "newpass123"})
    owner.post("/logout")
    owner.get("/login")
    assert owner.post("/login", {"email": "owner@example.com", "password": "newpass123"}).status_code == 302

def test_old_login_cookie_never_opens_another_car_wash(app, owner, db):
    tid = tenant_id(db)
    with db:
        db.execute("DELETE FROM tenants WHERE id = ?", (tid,))
    newcomer = Browser(app.test_client())
    signup(newcomer, email="new@example.com", name="New Wash")
    assert tenant_id(db, "new@example.com") == tid  # SQLite hands out the same id again
    resp = owner.get("/app/")
    assert resp.status_code == 302 and "/login" in resp.location


def test_password_change_logs_out_other_phones(app, owner, db):
    other = Browser(app.test_client())
    other.post("/login", {"email": "owner@example.com", "password": "secret123"})
    assert other.get("/app/").status_code == 200
    owner.post("/app/settings/account", {"current": "secret123", "new": "newpass123"})
    assert owner.get("/app/").status_code == 200
    assert other.get("/app/").status_code == 302



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
    resp = owner.post("/app/settings/theme", {"theme": "volt"}, headers={"X-Requested-With": "fetch"})
    assert resp.json == {"ok": True} and "theme=volt" in resp.headers["Set-Cookie"]  # switched in place, no reload
    assert db.execute("SELECT theme FROM tenants").fetchone()[0] == "volt"


def test_visitor_look_carries_into_signup_and_demo(app, browser, db):
    assert b"themes/carbon.css" in browser.get("/").data
    browser.post("/theme", {"theme": "nope"})
    assert b"themes/carbon.css" in browser.get("/").data
    resp = browser.post("/theme", {"theme": "volt"})
    assert resp.status_code == 302 and resp.location.endswith("/#look")
    assert b"themes/volt.css" in browser.get("/").data
    resp = browser.post("/theme", {"theme": "noir"}, headers={"X-Requested-With": "fetch"})
    assert resp.json == {"ok": True} and "theme=noir" in resp.headers["Set-Cookie"]
    browser.post("/theme", {"theme": "volt"})
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

def test_old_demo_ends_and_limits_are_shared(app, browser, db):
    browser.post("/demo")
    with db:
        db.execute("UPDATE tenants SET created_at = '2000-01-01 00:00:00' WHERE is_demo = 1")
    resp = browser.get("/app/")
    assert resp.status_code == 302 and resp.location.endswith("/")
    browser.get("/")
    browser.post("/demo")
    assert db.execute("SELECT COUNT(*) FROM tenants WHERE is_demo = 1").fetchone()[0] == 1

    app.config["RATELIMIT_ENABLED"] = True
    with app.test_request_context():
        from app.security import limiter
        assert limiter.allow("t", 2, 60) and limiter.allow("t", 2, 60)
        assert not limiter.allow("t", 2, 60)
    assert db.execute("SELECT COUNT(*) FROM rate_hits WHERE key = 't'").fetchone()[0] == 2



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


# --- online booking -----------------------------------------------------------

SLUG = "bubbles-car-wash"


def open_all_week(owner, capacity=2, **extra):
    data = {"booking_on": "1", "slug": SLUG, "slot_capacity": str(capacity)}
    for i in range(7):
        data.update({f"open_{i}": "1", f"from_{i}": "00:00", f"to_{i}": "23:30"})
    return owner.post("/app/settings/booking", {**data, **extra})


def first_slot(page: bytes) -> str:
    return re.search(rb'name="slot" value="([^"]+)"', page).group(1).decode()


def book(customer, slot, services, car_type, **extra):
    data = {"car_type_id": car_type, "service_id": services, "slot": slot, "name": "Thandi",
            "phone": "083 222 3333", **extra}
    return customer.post(f"/book/{SLUG}", data)


def test_customer_books_and_owner_confirms_and_checks_in(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    customer = Browser(app.test_client())
    page = customer.get(f"/book/{SLUG}")
    assert page.status_code == 200 and b"Bubbles Car Wash" in page.data and b"Wash &amp; Vac" in page.data

    resp = book(customer, first_slot(page.data), [svcs[1], svcs[4]], types[1], plate="ca 777")
    assert resp.status_code == 302
    b = db.execute("SELECT * FROM bookings").fetchone()
    assert (b["status"], b["phone"], b["plate"], b["price_cents"]) == ("new", "27832223333", "CA 777", 13000 + 2500)
    assert b"Booking received" in customer.get(resp.location).data

    listed = owner.get("/app/bookings").data.decode()
    assert "Thandi" in listed and "https://wa.me/27832223333?text=" in listed and "is%20confirmed" in listed
    assert 'class="tab-badge"' in listed
    resp = owner.post(f"/app/booking/{b['id']}/confirm", headers={"X-Requested-With": "fetch"})
    assert resp.json == {"ok": True}
    assert db.execute("SELECT status FROM bookings").fetchone()[0] == "confirmed"
    assert b"You&#39;re booked" in customer.get(f"/book/{SLUG}/{b['code']}").data

    assert b"Booked by" in owner.get(f"/app/new?booking={b['id']}").data
    resp = owner.post(f"/app/new?booking={b['id']}", {
        "plate": "CA 777", "phone": "083 222 3333", "car_type_id": types[1], "service_id": [svcs[1], svcs[4]],
    })
    assert resp.status_code == 302
    visit = db.execute("SELECT * FROM visits").fetchone()
    b = db.execute("SELECT * FROM bookings").fetchone()
    assert (b["status"], b["visit_id"]) == ("arrived", visit["id"]) and visit["price_cents"] == 15500

    owner.post(f"/app/booking/{b['id']}/cancel")  # already here: stays
    assert db.execute("SELECT status FROM bookings").fetchone()[0] == "arrived"
    owner.post(f"/app/visit/{visit['id']}/delete")  # "Arrived" on the wrong booking
    b = db.execute("SELECT * FROM bookings").fetchone()
    assert (b["status"], b["visit_id"]) == ("confirmed", None)


def test_booking_holds_its_place_for_its_services_time(app, owner, db):
    from app import bookings
    tid = tenant_id(db)
    open_all_week(owner, capacity=1)
    with db:
        db.execute("UPDATE tenants SET hours = ?", (json.dumps([["08:00", "12:00"]] + [None] * 6),))
    tenant = db.execute("SELECT * FROM tenants").fetchone()
    monday = datetime(2026, 10, 5, 4, 0, tzinfo=zone("UTC"))  # 06:00 in Johannesburg

    def fits(now=monday):
        return {s["time"]: s["fit"] for s in bookings.days(db, tenant, now)[0]["slots"]}

    assert fits()["08:00"] == 240 and fits()["11:30"] == 30
    with db:
        bookings.create(db, tid, slot_at="2026-10-05 07:00:00", minutes=110, name="Valet", phone="27831112222",
                        plate="", car_type_id=None, service_ids=[], price_cents=0, note="")  # 09:00 to 11:00
    assert fits() == {"08:00": 60, "08:30": 30, "11:00": 60, "11:30": 30}
    assert bookings.slot_minutes(45) == 60 and bookings.slot_minutes(0) == 30
    with db:
        db.execute("UPDATE bookings SET status = 'cancelled'")
    assert fits()["08:00"] == 240


def test_long_services_need_a_long_enough_time(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner, capacity=1)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)  # Full Valet holds 120 min, Wash & Go 30
    customer = Browser(app.test_client())
    page = customer.get(f"/book/{SLUG}").data.decode()
    assert '"minutes": {' in page
    times = re.findall(r'name="slot" value="([^"]+)"[^>]*data-fit="(\d+)"', page)
    start = next(v for v, fit in times if int(fit) >= 240)
    at = datetime.fromisoformat(start)
    later = lambda minutes: (at + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
    assert book(customer, start, [svcs[2]], types[0]).status_code == 302
    assert db.execute("SELECT minutes FROM bookings").fetchone()[0] == 120

    second = Browser(app.test_client())
    page = second.get(f"/book/{SLUG}").data.decode()
    assert f'value="{later(90)}"' not in page and f'value="{later(120)}"' in page
    assert b"no longer free" in book(second, later(60), [svcs[0]], types[0]).data
    assert book(second, later(120), [svcs[0]], types[0]).status_code == 302


def test_today_bookings_are_on_the_board(owner, db):
    from app import bookings
    tid = tenant_id(db)
    with db:
        bookings.create(db, tid, slot_at=ts(), minutes=30, name="Lerato", phone="27831112222", plate="", note="",
                        car_type_id=ids(db, "car_types", tid)[0], service_ids=[ids(db, "services", tid)[0]],
                        price_cents=7000)
    board = owner.get("/app/").data
    assert b"Booked today" in board and b"Lerato" in board and b"Arrived" in board


def test_full_and_past_times_cannot_be_booked(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner, capacity=1)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    first, second = Browser(app.test_client()), Browser(app.test_client())
    slot = first_slot(first.get(f"/book/{SLUG}").data)
    assert book(first, slot, [svcs[0]], types[0]).status_code == 302
    page = second.get(f"/book/{SLUG}").data
    assert f'value="{slot}"'.encode() not in page
    resp = book(second, slot, [svcs[0]], types[0])
    assert resp.status_code == 200 and b"no longer free" in resp.data
    assert b"no longer free" in book(second, "2000-01-01 09:00", [svcs[0]], types[0]).data
    assert b"no longer free" in book(second, "garbage", [svcs[0]], types[0]).data
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1


def test_customer_cancels_their_booking(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner, capacity=1)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    customer = Browser(app.test_client())
    slot = first_slot(customer.get(f"/book/{SLUG}").data)
    page_url = book(customer, slot, [svcs[0]], types[0]).location
    code = page_url.rsplit("/", 1)[-1]
    assert b"Cancel booking" in customer.get(page_url).data
    customer.post(f"/book/{SLUG}/{code}/cancel")
    b = db.execute("SELECT * FROM bookings").fetchone()
    assert (b["status"], b["cancelled_by"]) == ("cancelled", "customer")
    page = customer.get(page_url).data
    assert b"You cancelled this booking" in page and b"Cancel booking" not in page
    assert b"I%27ve%20cancelled%20my%20booking" in page
    assert b"Customer cancelled" in owner.get("/app/bookings").data
    assert f'value="{slot}"'.encode() in Browser(app.test_client()).get(f"/book/{SLUG}").data  # the time is free again

    book(customer, slot, [svcs[0]], types[0])
    with db:  # the time has passed: too late to cancel online
        db.execute("UPDATE bookings SET slot_at = '2000-01-01 09:00:00' WHERE status = 'new'")
    code = db.execute("SELECT code FROM bookings WHERE status = 'new'").fetchone()[0]
    assert b"Cancel booking" not in customer.get(f"/book/{SLUG}/{code}").data
    customer.post(f"/book/{SLUG}/{code}/cancel")
    assert db.execute("SELECT status FROM bookings WHERE code = ?", (code,)).fetchone()[0] == "new"
    assert customer.post(f"/book/{SLUG}/nope/cancel").status_code == 404


def test_owner_cancel_tells_the_customer(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    customer = Browser(app.test_client())
    page_url = book(customer, first_slot(customer.get(f"/book/{SLUG}").data), [svcs[0]], types[0]).location
    bid = db.execute("SELECT id FROM bookings").fetchone()[0]
    listed = owner.get("/app/bookings").data.decode()
    assert "https://wa.me/27832223333?text=Hi%20Thandi%2C%20sorry" in listed
    assert f'data-mark="/app/booking/{bid}/cancel"' in listed and "data-ask=" in listed
    owner.post(f"/app/booking/{bid}/cancel", headers={"X-Requested-With": "fetch"})
    assert db.execute("SELECT status, cancelled_by FROM bookings").fetchone()[:] == ("cancelled", "owner")
    assert b"we had to cancel" in customer.get(page_url).data


def test_arrived_keeps_the_booked_price(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    customer = Browser(app.test_client())
    book(customer, first_slot(customer.get(f"/book/{SLUG}").data), [svcs[1]], types[1])
    bid = db.execute("SELECT id FROM bookings").fetchone()[0]
    with db:
        db.execute("UPDATE prices SET price_cents = 99900")
    page = owner.get(f"/app/new?booking={bid}").data.decode()
    assert 'name="price" value="130"' in page and f'data-booking="{bid}"' in page


def test_booking_form_errors(app, owner, db):
    open_all_week(owner)
    customer = Browser(app.test_client())
    customer.get(f"/book/{SLUG}")
    resp = customer.post(f"/book/{SLUG}", {"name": "", "phone": "12", "car_type_id": "x", "service_id": "999"})
    for error in (b"Choose your car type", b"Choose at least one service", b"Pick a day", b"Enter your name",
                  b"Enter your WhatsApp"):
        assert error in resp.data
    assert customer.get("/book/no-such-wash").status_code == 404


def test_service_without_a_price_cannot_be_booked(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner)
    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    with db:  # no Engine Wash for minibuses
        db.execute("DELETE FROM prices WHERE service_id = ? AND car_type_id = ?", (svcs[3], types[3]))
        db.execute("DELETE FROM prices WHERE service_id = ? AND car_type_id = ?", (svcs[0], types[0]))
    customer = Browser(app.test_client())
    page = customer.get(f"/book/{SLUG}").data.decode()
    assert re.search(rf'value="{svcs[0]}"\s+disabled', page) and not re.search(rf'value="{svcs[1]}"\s+disabled', page)
    slot = first_slot(page.encode())
    resp = book(customer, slot, [svcs[1], svcs[3]], types[3])
    assert b"Engine Wash can&#39;t be booked online" in resp.data
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0
    assert book(customer, slot, [svcs[3]], types[2]).status_code == 302


def test_bookings_are_private(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner)
    customer = Browser(app.test_client())
    book(customer, first_slot(customer.get(f"/book/{SLUG}").data), [ids(db, "services", tid)[0]],
         ids(db, "car_types", tid)[0])
    bid = db.execute("SELECT id FROM bookings").fetchone()[0]
    other = Browser(app.test_client())
    signup(other, email="other@example.com", name="Other Wash")
    assert b"Thandi" not in other.get("/app/bookings").data
    assert other.post(f"/app/booking/{bid}/confirm").status_code == 404
    assert other.post(f"/app/booking/{bid}/cancel").status_code == 404
    assert b"Booked by" not in other.get(f"/app/new?booking={bid}").data
    owner.post(f"/app/booking/{bid}/cancel")
    assert db.execute("SELECT status FROM bookings").fetchone()[0] == "cancelled"


def test_booking_settings(app, owner, db):
    resp = open_all_week(owner, capacity=3, open_6="", from_0="12:00", to_0="09:00")
    assert b"Monday: closing time" in resp.data
    resp = open_all_week(owner, capacity=3, open_6="", slug="Bad Slug!")
    assert b"Page address" in resp.data
    open_all_week(owner, capacity=3, open_6="")
    t = db.execute("SELECT * FROM tenants").fetchone()
    assert t["slot_capacity"] == 3 and t["slug"] == SLUG and json.loads(t["hours"])[6] is None

    other = Browser(app.test_client())
    signup(other, email="other@example.com", name="Other Wash")
    resp = other.post("/app/settings/booking", {"slug": SLUG, "slot_capacity": "2"})
    assert b"That page address is taken" in resp.data

    owner.post("/app/settings/booking", {"slug": SLUG, "slot_capacity": "2"})  # booking off, every day closed
    customer = Browser(app.test_client())
    assert b"Online booking is closed" in customer.get(f"/book/{SLUG}").data
    resp = customer.post(f"/book/{SLUG}", {"slot": "x"})
    assert resp.status_code == 302 and db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0


def test_expired_subscription_closes_booking(app, owner, db):
    open_all_week(owner)
    with db:
        db.execute("UPDATE tenants SET paid_until = ?", ((date.today() - timedelta(days=3)).isoformat(),))
    assert b"Online booking is closed" in Browser(app.test_client()).get(f"/book/{SLUG}").data


def test_demo_has_a_booking_page_with_its_own_look_switch(browser, db):
    resp = browser.post("/demo", {"next": "book"})
    demo = db.execute("SELECT * FROM tenants WHERE is_demo = 1").fetchone()
    assert resp.location.endswith(f"/book/{demo['slug']}")
    page = browser.get(resp.location).data
    assert b"this is the page your customers see" in page and b'noindex' in page
    assert db.execute("SELECT COUNT(*) FROM bookings WHERE tenant_id = ?", (demo["id"],)).fetchone()[0] > 0
    resp = browser.post("/app/settings/theme", {"theme": "noir", "next": f"/book/{demo['slug']}"})
    assert resp.location.endswith(f"/book/{demo['slug']}")
    assert b"themes/noir.css" in browser.get(resp.location).data
    resp = browser.post("/app/settings/theme", {"theme": "volt", "next": "https://evil.example"})
    assert resp.location.endswith("/app/settings/theme")
    assert browser.post("/demo", {"next": "book"}).location.endswith(f"/book/{demo['slug']}")


def test_car_washes_from_before_booking_get_a_page_address(db):
    from app import bookings
    with db:
        tid = seed.create_tenant(db, name="Old Wash", email="old@example.com")
        db.execute("UPDATE tenants SET slug = '' WHERE id = ?", (tid,))
        seed.create_tenant(db, name="Old Wash", email="old2@example.com")
        bookings.fill_slugs(db)
    assert db.execute("SELECT slug FROM tenants WHERE id = ?", (tid,)).fetchone()[0] == "old-wash-2"
    assert bookings.slugify("!!") == "car-wash" and bookings.slugify("Joe's  Car-Wash") == "joe-s-car-wash"


HOST = "https://book.bubbles.co.za"


def admin_login(app):
    admin = Browser(app.test_client())
    admin.post("/admin/login", {"password": "admin-pass"})
    return admin


def test_car_wash_on_its_own_address(app, owner, db):
    tid = tenant_id(db)
    open_all_week(owner)
    signup(Browser(app.test_client()), email="other@example.com", name="Other Wash")
    admin = admin_login(app)
    admin.post(f"/admin/tenant/{tid}/domain", {"domain": "https://Book.Bubbles.co.za/"})
    assert db.execute("SELECT domain FROM tenants WHERE id = ?", (tid,)).fetchone()[0] == "book.bubbles.co.za"
    assert f"{HOST}/".encode() in owner.get("/app/bookings").data
    assert b'name="slug"' not in owner.get("/app/settings/booking").data

    visitor = Browser(app.test_client())
    page = visitor.get("/", base_url=HOST)
    assert page.status_code == 200 and b"Bubbles Car Wash" in page.data and b"Wash &amp; Vac" in page.data
    assert b"Owner login" in page.data and b"Bookings by" not in page.data
    assert visitor.get(f"/book/{SLUG}", base_url=HOST).location.endswith("/")
    assert visitor.get("/book/other-wash", base_url=HOST).status_code == 404
    assert visitor.get("/signup", base_url=HOST).status_code == 404
    assert visitor.post("/demo", base_url=HOST).status_code == 404
    assert visitor.get("/admin/login", base_url=HOST).status_code == 404
    assert b"Bubbles Car Wash" in visitor.get("/login", base_url=HOST).data
    assert b"Try the live demo" in app.test_client().get("/").data  # the main site stays the same

    types, svcs = ids(db, "car_types", tid), ids(db, "services", tid)
    resp = visitor.post(f"/book/{SLUG}", {"car_type_id": types[0], "service_id": [svcs[0]], "name": "Thandi",
                                          "phone": "083 222 3333", "slot": first_slot(page.data)}, base_url=HOST)
    done = visitor.get(resp.location, base_url=HOST).data
    assert b"Booking received" in done and b'href="/"' in done

    other = Browser(app.test_client())
    other.get("/login", base_url=HOST)
    login = {"email": "other@example.com", "password": "secret123"}
    assert b"Wrong email or password" in other.post("/login", login, base_url=HOST).data
    assert other.post("/login", {**login, "email": "owner@example.com"}, base_url=HOST).location.endswith("/app/")
    assert b"Your booking page, as customers see it" in other.get("/", base_url=HOST).data


def test_only_saved_addresses_get_a_certificate(app, owner, db):
    client = app.test_client()
    assert client.get("/tls-check?domain=book.bubbles.co.za").status_code == 404
    admin_login(app).post(f"/admin/tenant/{tenant_id(db)}/domain", {"domain": "book.bubbles.co.za"})
    assert client.get("/tls-check?domain=book.bubbles.co.za").status_code == 200
    assert client.get("/tls-check?domain=evil.example.com").status_code == 404
    assert client.get("/tls-check").status_code == 404


def test_admin_checks_the_address(app, owner, db):
    tid = tenant_id(db)
    signup(Browser(app.test_client()), email="other@example.com", name="Other Wash")
    admin = admin_login(app)
    domain = lambda: db.execute("SELECT domain FROM tenants WHERE id = ?", (tid,)).fetchone()[0]
    for bad in ("not a domain", "localhost", "joe@wash.co.za"):
        assert b"an address like" in admin.post(f"/admin/tenant/{tid}/domain", {"domain": bad},
                                                 follow_redirects=True).data
    main = Browser(app.test_client())
    main.get("/admin/login", base_url="https://carwasheasy.co.za")
    main.post("/admin/login", {"password": "admin-pass"}, base_url="https://carwasheasy.co.za")
    assert b"this site&#39;s own address" in main.post(f"/admin/tenant/{tid}/domain", {"domain": "carwasheasy.co.za"},
                                                     base_url="https://carwasheasy.co.za", follow_redirects=True).data
    admin.post(f"/admin/tenant/{tenant_id(db, 'other@example.com')}/domain", {"domain": "book.other.co.za"})
    assert b"already has" in admin.post(f"/admin/tenant/{tid}/domain", {"domain": "book.other.co.za"},
                                        follow_redirects=True).data
    assert domain() == ""
    admin.post(f"/admin/tenant/{tid}/domain", {"domain": "book.bubbles.co.za"})
    assert domain() == "book.bubbles.co.za" and f"{HOST}/".encode() in admin.get("/admin/").data
    admin.post(f"/admin/tenant/{tid}/domain", {"domain": ""})
    assert domain() == ""


def test_ready_made_website_for_their_hosting(app, owner, db):
    import io
    import zipfile
    tid = tenant_id(db)
    owner.post("/app/settings/theme", {"theme": "noir"})
    admin = admin_login(app)
    preview = admin.get(f"/admin/tenant/{tid}/website")
    assert preview.status_code == 200 and b"themes/noir.css" in preview.data and b"Wash &amp; Vac" in preview.data
    resp = admin.get(f"/admin/tenant/{tid}/website.zip")
    assert resp.mimetype == "application/zip" and "bubbles-car-wash-website.zip" in resp.headers["Content-Disposition"]
    z = zipfile.ZipFile(io.BytesIO(resp.data))
    assert sorted(z.namelist()) == ["fonts/archivo.woff2", "icon.svg", "index.html", "style.css", "theme.css"]
    page = z.read("index.html").decode()
    assert f'href="http://localhost/book/{SLUG}"' in page and 'href="style.css"' in page and "/static/" not in page
    assert "url(fonts/archivo.woff2)" in z.read("style.css").decode()
    admin.post(f"/admin/tenant/{tid}/domain", {"domain": "book.bubbles.co.za"})
    page = zipfile.ZipFile(io.BytesIO(admin.get(f"/admin/tenant/{tid}/website.zip").data)).read("index.html").decode()
    assert f'href="{HOST}/"' in page and f'href="{HOST}/login"' in page
    owner.post("/app/settings/theme", {"theme": "carbon"})
    names = zipfile.ZipFile(io.BytesIO(admin.get(f"/admin/tenant/{tid}/website.zip").data)).namelist()
    assert "fonts/barlow-400.woff2" in names and "fonts/barlow-condensed-700.woff2" in names
    assert "fonts/inter.woff2" not in names



def test_website_of_a_walk_in_car_wash_has_no_booking(app, owner, db):
    visitor = Browser(app.test_client())
    page = visitor.get(f"/site/{SLUG}").data
    assert b"Book online" in page and f'href="http://localhost/book/{SLUG}"'.encode() in page
    assert b"Just drive in" not in page and b"Opening hours" not in page
    data = {"slug": SLUG, "slot_capacity": "2", "open_0": "1", "from_0": "07:00", "to_0": "18:00"}
    owner.post("/app/settings/booking", data)
    page = visitor.get(f"/site/{SLUG}").data
    assert b"Just drive in" in page and b"Get directions" in page and b"WhatsApp us" in page
    assert b"Book online" not in page and b"Book in a minute" not in page and b"/book/" not in page
    assert b"<span>Monday</span><span>07:00\xe2\x80\x9318:00</span>" in page
    assert b"<span>Tuesday</span><span>Closed</span>" in page

def test_website_shows_prices_on_service_cards(app, owner, db):
    import io
    import zipfile
    tid = tenant_id(db)
    shine = db.execute("SELECT id FROM services WHERE tenant_id = ? AND name = 'Tyre Shine'", (tid,)).fetchone()[0]
    engine = db.execute("SELECT id FROM services WHERE tenant_id = ? AND name = 'Engine Wash'", (tid,)).fetchone()[0]
    minibus = db.execute("SELECT id FROM car_types WHERE tenant_id = ? AND name = 'Minibus / Van'", (tid,)).fetchone()[0]
    with db:
        db.execute("UPDATE prices SET price_cents = 2500 WHERE service_id = ?", (shine,))
        db.execute("DELETE FROM prices WHERE service_id = ?", (engine,))
    visitor = Browser(app.test_client())
    page = visitor.get(f"/site/{SLUG}").data.decode()
    # car type buttons, the first one picked, types with the same prices share one;
    # the CSS shows the price of the picked type, "from" without :has()
    assert 'id="car-type-0" checked><span>Hatch / Sedan</span>' in page
    assert 'id="car-type-1" ><span>SUV / Bakkie</span>' in page and 'id="car-type-2" ><span>Minibus / Van' in page
    assert ".web-prices:has(#car-type-2:checked) .web-p2 {" in page and "car-type-3" not in page
    assert ('<th scope="row">Wash &amp; Go</th><td><span class="web-any">from R70</span><span class="web-p web-p0">R70</span>'
            '<span class="web-p web-p1">R90</span><span class="web-p web-p2">R120</span></td>') in page
    assert '<th scope="row">Tyre Shine</th><td><span class="web-any">R25</span><span class="web-p web-p0">R25</span>' in page
    assert '<th scope="row">Engine Wash</th><td></td>' in page and "See free times" in page
    with db:
        db.execute("DELETE FROM prices WHERE service_id = ? AND car_type_id = ?", (shine, minibus))
    page = visitor.get(f"/site/{SLUG}").data.decode()
    assert '<span class="web-p web-p1">R25</span><span class="web-p web-p2">—</span>' in page
    with db:  # the same price for every car type (and a type with no prices at all): no buttons
        db.execute("INSERT INTO car_types (tenant_id, name, sort) VALUES (?, 'Truck', 9)", (tid,))
        db.execute("DELETE FROM prices WHERE service_id IN (SELECT id FROM services WHERE tenant_id = ?) "
                   "AND service_id != ?", (tid, shine))
        db.execute("INSERT INTO prices VALUES (?, ?, 2500)", (shine, minibus))
    page = visitor.get(f"/site/{SLUG}").data.decode()
    assert '<th scope="row">Tyre Shine</th><td>R25</td>' in page and "car-type-0" not in page and "<style>" not in page
    admin = admin_login(app)
    files = zipfile.ZipFile(io.BytesIO(admin.get(f"/admin/tenant/{tid}/website.zip").data))
    index = files.read("index.html").decode()  # prices in files on their hosting would go out of date
    assert '<th scope="row">Wash &amp; Go</th><td></td>' in index and "web-any" not in index


BAY = "bay-crescent-carwash"


def add_prospects(admin, lines, **kw):
    return admin.post("/admin/prospects", {"lines": lines}, **kw)


def test_website_for_a_car_wash_that_has_not_signed_up(app, db):
    from urllib.parse import unquote
    admin = admin_login(app)
    resp = add_prospects(admin, "Bay Crescent Carwash | Lancaster Rd, Gordon's Bay | Gordon's Bay | 082 111 2222\n\n"
                                "Water Worx | | Strand |\n | Main Rd\nBad Number | Main Rd | Strand | 123",
                         follow_redirects=True)
    assert b"Added 2: Bay Crescent Carwash, Water Worx." in resp.data and b"Skipped: Bad Number" in resp.data
    maps = "https://maps.google.com/?cid=4294354752046552542"
    resp = add_prospects(admin, f"Bay Crescent Carwash | Lancaster Rd, Gordons Bay | | | {maps}\n"
                                "Water Worx | | Strand | | https://evil.example.com/maps", follow_redirects=True)
    assert b"Updated 1: Bay Crescent Carwash." in resp.data and b"Skipped: Water Worx (the Google Maps link" in resp.data
    assert db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 2
    t = db.execute("SELECT * FROM tenants WHERE slug = ?", (BAY,)).fetchone()
    assert t["email"] is None and t["address"] == "Lancaster Rd, Gordons Bay" and t["phone"] == "27821112222"
    assert t["maps_url"] == maps
    assert b"https://www.google.com/maps/search/?api=1&amp;query=Water%20Worx%2C%20Strand" in \
        app.test_client().get("/site/water-worx").data
    assert t["city"] == "Gordon's Bay" and ids(db, "services", t["id"])
    assert f"http://localhost/site/{BAY}".encode() in admin.get("/admin/").data

    visitor = Browser(app.test_client())
    page = visitor.get(f"/site/{BAY}")
    assert page.status_code == 200 and b"Made for Bay Crescent Carwash by CarWashEasy" in page.data
    assert b"noindex" in page.data and f'href="/site/{BAY}/claim"'.encode() in page.data
    assert b"Wash &amp; Vac" in page.data and f'href="http://localhost/book/{BAY}"'.encode() in page.data
    assert b"themes/carbon.css" in page.data and b'href="https://wa.me/27821112222?text=' in page.data
    assert f'href="{maps}"'.encode() in page.data
    resp = visitor.post("/theme", {"theme": "volt", "next": f"/site/{BAY}"})
    assert resp.location == f"/site/{BAY}"
    assert b"themes/volt.css" in visitor.get(f"/site/{BAY}").data
    booking = visitor.get(f"/book/{BAY}").data
    assert b"themes/volt.css" in booking and b"Made for Bay Crescent" in booking and b"noindex" in booking
    assert db.execute("SELECT theme FROM tenants WHERE id = ?", (t["id"],)).fetchone()[0] == ""

    signup(Browser(app.test_client()))
    owned = visitor.get(f"/site/{SLUG}")  # a car wash that signed up: its website on our address, in its own look
    assert owned.status_code == 200 and b"themes/carbon.css" in owned.data
    assert b"noindex" not in owned.data and b"Made for" not in owned.data and b"/claim" not in owned.data
    assert b"themes/carbon.css" in visitor.get(f"/book/{SLUG}").data
    assert visitor.get("/site/nobody").status_code == 404

    assert b"Enter the owner" in admin.post(f"/admin/tenant/{t['id']}/owner", {"email": "joe"},
                                            follow_redirects=True).data
    assert b"already has an account" in admin.post(f"/admin/tenant/{t['id']}/owner", {"email": "owner@example.com"},
                                                   follow_redirects=True).data
    resp = admin.post(f"/admin/tenant/{t['id']}/owner", {"email": "Joe@Bay.co.za", "theme": "volt"},
                      follow_redirects=True)
    password = re.search(rb"password: (\S+) ", resp.data).group(1).decode()
    t = db.execute("SELECT * FROM tenants WHERE id = ?", (t["id"],)).fetchone()
    assert t["email"] == "joe@bay.co.za" and t["theme"] == "volt" and t["plan"] == "trial"
    assert t["paid_until"] == (local_today(zone(DEFAULT_TZ)) + timedelta(days=60)).isoformat()
    assert b"Made for" not in visitor.get(f"/site/{BAY}").data
    joe = Browser(app.test_client())
    joe.get("/login")
    assert joe.post("/login", {"email": "joe@bay.co.za", "password": password}).location.endswith("/app/")
    assert b"already has an owner" in admin.post(f"/admin/tenant/{t['id']}/owner", {"email": "x@bay.co.za"},
                                                 follow_redirects=True).data


def test_owner_makes_the_website_theirs(app, db):
    admin = admin_login(app)
    add_prospects(admin, "Bay Crescent Carwash | Lancaster Rd | Gordon's Bay | 082 111 2222")
    joe = Browser(app.test_client())
    joe.post("/theme", {"theme": "volt", "next": f"/site/{BAY}"})
    page = joe.get(f"/site/{BAY}/claim").data
    assert b"Make Bay Crescent Carwash yours" in page and b'value="+27 82 111 2222"' in page
    assert b"themes/volt.css" in page
    signup(Browser(app.test_client()))
    taken = joe.post(f"/site/{BAY}/claim", {"phone": "082 111 2222", "email": "owner@example.com", "password": "secret123"})
    assert b"already has an account" in taken.data
    assert b"at least 8" in joe.post(f"/site/{BAY}/claim", {"phone": "0821112222", "email": "joe@bay.co.za",
                                                              "password": "short"}).data
    resp = joe.post(f"/site/{BAY}/claim", {"phone": "083 999 8888", "email": "Joe@Bay.co.za", "password": "secret123"})
    assert resp.location.endswith("/app/setup")
    assert b"Bay Crescent Carwash is yours" in joe.get(joe.get(resp.location).location).data
    t = db.execute("SELECT * FROM tenants WHERE slug = ?", (BAY,)).fetchone()
    assert t["setup"] == "prices"
    assert t["email"] == "joe@bay.co.za" and t["phone"] == "27839998888" and t["theme"] == "volt"
    assert t["plan"] == "trial" and t["paid_until"] == (local_today(zone(DEFAULT_TZ)) + timedelta(days=60)).isoformat()
    assert t["created_at"] >= ts(now_utc() - timedelta(minutes=1))  # counts as a sign-up in the emails
    assert f"http://localhost/site/{BAY}".encode() in joe.get("/app/bookings").data
    again = Browser(app.test_client()).get(f"/site/{BAY}/claim")
    assert again.status_code == 302 and again.location.endswith("/login")
    from app import notify
    app.config["SITE_URL"] = "https://carwasheasy.co.za"
    assert f"Website: https://carwasheasy.co.za/site/{BAY}" in notify.signup_mail(t, app.config)[1]


def test_see_your_app_opens_a_demo_with_the_car_wash_name(app, db):
    admin = admin_login(app)
    add_prospects(admin, "Bay Crescent Carwash | Lancaster Rd | Gordon's Bay | 082 111 2222")
    joe = Browser(app.test_client())
    joe.get(f"/site/{BAY}")
    assert joe.post("/demo", {"site": BAY}).location.endswith("/app/")
    board = joe.get("/app/").data
    assert b"Bay Crescent Carwash (demo)" in board and f'href="/site/{BAY}/claim"'.encode() in board
    demo_id = db.execute("SELECT id FROM tenants WHERE is_demo = 1").fetchone()[0]
    joe.post("/demo", {"site": BAY})
    assert db.execute("SELECT COUNT(*) FROM tenants WHERE is_demo = 1").fetchone()[0] == 1  # the same demo again
    joe.post("/demo")
    assert b"Sunshine Car Wash (demo)" in joe.get("/app/").data and b"/claim" not in joe.get("/app/").data
    assert db.execute("SELECT id FROM tenants WHERE is_demo = 1 ORDER BY id DESC").fetchone()[0] != demo_id


# --- licence disc scanner -------------------------------------------------------

DISC = ("%MVL1CC09%0154%4024T011%1%40240119RMPX%CA123456%BYJ091C%Hatch back / Luikrug%TOYOTA%COROLLA%"
        "White / Wit%AHTFZ29G309123456%2ZZ1234567%2027-08-31%")


def test_licence_disc_parsing():
    from app import disc
    assert disc.parse(DISC) == {"plate": "CA 123-456", "make": "Toyota Corolla", "description": "Hatch back / Luikrug"}
    bakkie = DISC.replace("CA123456", "CX99XXGP").replace("Hatch back / Luikrug", "Light delivery vehicle / Bakkie")
    found = disc.parse(bakkie.replace("TOYOTA%COROLLA", "MERCEDES-BENZ%X250D"))
    assert found["plate"] == "CX 99 XX GP" and found["make"] == "Mercedes-Benz X250D"
    assert [disc.pretty_plate(p) for p in ("CX99XXGP", "ABC123L", "JOHN1GP", "CA 1")] == [
        "CX 99 XX GP", "ABC 123 L", "JOHN 1 GP", "CA 1"]
    assert disc.parse("VW%POLO VIVO") is None and disc.parse("") is None and disc.parse(None) is None
    assert disc.parse(DISC.replace("TOYOTA%COROLLA", "VOLKSWAGEN%POLO VIVO GTI"))["make"] == "Volkswagen Polo Vivo GTI"
    types = [{"id": 1, "name": "Hatch / Sedan"}, {"id": 2, "name": "SUV"}, {"id": 3, "name": "Bakkie"},
             {"id": 4, "name": "Minibus / Van"}]
    for description, expected in [("Hatch back / Luikrug", 1), ("Sedan (closed top) / Sedan", 1),
                                  ("Station wagon / Stasiewa", 2), ("Light delivery vehicle / Bakkie", 3),
                                  ("Panel van / Paneelwa", 4), ("Minibus / Minibus", 4), ("Motorcycle / Motorfiets", None)]:
        assert disc.car_type(description, types) == expected, description
    assert disc.car_type("Station wagon / Stasiewa", types[:1]) is None


def test_scanned_disc_fills_the_new_car_form(owner, db):
    tid = tenant_id(db)
    page = owner.get("/app/new").data
    assert b'id="scan-disc"' in page and b"vendor/barcode-detector.js" in page
    d = owner.get("/app/disc", query_string={"code": DISC}).json
    assert d == {"found": True, "plate": "CA 123-456", "make": "Toyota Corolla",
                 "car_type_id": ids(db, "car_types", tid)[0]}
    add_car(owner, db, plate="ca 123 456")  # a regular, written his own way
    assert owner.get("/app/disc", query_string={"code": DISC}).json["plate"] == "CA 123 456"
    assert owner.get("/app/disc?code=not-a-disc").json == {"found": False}


def test_landing_has_no_load_shedding_promise(client):
    page = client.get("/").data.lower()
    assert b"load shedding" not in page and b"in the cloud" not in page and b"licence disc" in page


def test_landing_prices(client):
    page = client.get("/").data
    assert b"R349" in page and b"Ready-made website in your look" in page and b"upfront with the ready-made" not in page and b"Price on request" in page
    assert b"R3,990" in page and b"R0" in page and b"/signup?plan=pilot" in page
    assert b'<s class="price-was">R4,188</s>' in page and b"R332.50 a month" in page and b"Save R198 a year" in page
    assert b"Paper works just fine" in page


def test_pilot_signup_gets_longer_free_period(browser, db):
    assert b"Join the pilot" in browser.get("/signup?plan=pilot").data
    resp = browser.post("/signup", {"name": "Pilot Wash", "email": "pilot@example.com", "password": "secret123",
                                    "phone": "082 555 1234", "plan": "pilot"})
    assert resp.status_code == 302
    t = db.execute("SELECT * FROM tenants WHERE email = 'pilot@example.com'").fetchone()
    assert (date.fromisoformat(t["paid_until"]) - date.fromisoformat(t["created_at"][:10])).days in (60, 61)


def test_old_database_drops_washer_pay(tmp_path):
    import sqlite3
    from app.db import init_db
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE washers (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, name TEXT NOT NULL,
            pay_type TEXT NOT NULL DEFAULT 'percent' CHECK (pay_type IN ('percent', 'fixed')),
            pay_value INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE visit_washers (visit_id INTEGER NOT NULL, washer_id INTEGER NOT NULL,
            pay_type TEXT NOT NULL, pay_value INTEGER NOT NULL, PRIMARY KEY (visit_id, washer_id));
        INSERT INTO visit_washers VALUES (1, 1, 'fixed', 2500);
    """)
    conn.close()
    init_db(path)
    conn = sqlite3.connect(path)
    assert [r[1] for r in conn.execute("PRAGMA table_info(visit_washers)")] == ["visit_id", "washer_id"]
    assert "pay_type" not in [r[1] for r in conn.execute("PRAGMA table_info(washers)")]
    assert conn.execute("SELECT * FROM visit_washers").fetchall() == [(1, 1)]


def test_static_files_are_cached_by_content_hash(client):
    page = client.get("/").data.decode()
    css = re.search(r'href="(/static/style\.css\?v=\w+)"', page)[1]
    assert re.search(r'rel="preload" href="/static/fonts/barlow-400\.woff2\?v=\w+"', page)  # Carbon's own font
    resp = client.get(css, headers={"Accept-Encoding": "gzip, deflate, br"})
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert resp.headers["Content-Encoding"] == "br" and resp.headers["Vary"] == "Accept-Encoding"
    fonts = re.findall(r"url\((/static/fonts/[^)]+)\)", brotli.decompress(resp.data).decode())
    assert fonts and all("?v=" in f for f in fonts)
    assert client.get(fonts[0]).headers["Cache-Control"] == "public, max-age=31536000, immutable"
    plain = client.get("/static/style.css?v=old")  # an old page asks for an old hash: no long cache
    assert plain.headers["Cache-Control"] == "no-cache" and "Content-Encoding" not in plain.headers
    assert client.get("/static/style.css", headers={"If-None-Match": plain.headers["ETag"]}).status_code == 304
    for bad in ("/static/../db.py", "/static/fonts", "/static/nope.css"):
        assert client.get(bad).status_code == 404


def test_unchanged_page_comes_back_as_304(owner):
    first = owner.get("/app/settings")
    assert first.headers["Cache-Control"] == "private, no-cache"
    again = owner.get("/app/settings", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304 and again.data == b""
    owner.post("/app/settings/theme", {"theme": "noir"})
    assert owner.get("/app/settings", headers={"If-None-Match": first.headers["ETag"]}).status_code == 200
