import base64
import io
import json

import pytest
from PIL import Image

from app import menu

from .conftest import signup

REPLY = json.dumps({"services": [
    {"name": "Wash & Vac", "minutes": 40, "prices": [110, 140, "R140", None]},
    {"name": "Full Valet", "minutes": 150, "prices": [300, 380, 380, 450]},
    {"name": "Polish", "prices": [200]},
]})


def photo(name="list.jpg", fmt="JPEG"):
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(buf, fmt)
    return io.BytesIO(buf.getvalue()), name


def pdf(pages):
    buf = io.BytesIO()
    first, *rest = [Image.new("RGB", (600, 800), "white") for _ in range(pages)]
    first.save(buf, "PDF", save_all=True, append_images=rest)
    return io.BytesIO(buf.getvalue()), "list.pdf"


@pytest.fixture
def reader(app, monkeypatch):
    app.config["QUBRID_API_KEY"] = "test-key"
    sent = []

    def ask(messages):
        sent.append(messages)
        return "```json\n" + REPLY + "\n```"

    monkeypatch.setattr(menu, "ask", ask)
    return sent


def services(db):
    return {r["name"]: r for r in db.execute("SELECT * FROM services ORDER BY id")}


def test_without_a_key_there_is_no_upload(owner):
    assert b"/settings/prices/upload" not in owner.get("/app/settings/prices").data
    assert owner.get("/app/settings/prices/upload").status_code == 404


def test_photos_are_read_and_shown_to_check(owner, reader):
    assert b"/settings/prices/upload" in owner.get("/app/settings/prices").data
    page = owner.post("/app/settings/prices/upload", {"files": [photo(), photo("b.png", "PNG")]}).data.decode()
    assert "We found 3 services" in page and 'value="Wash &amp; Vac"' in page and 'value="140"' in page
    user = reader[0][1]["content"]
    assert '"Hatch / Sedan", "SUV", "Bakkie", "Minibus / Van"' in user[0]["text"]
    assert len(user) == 3 and user[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    sent = Image.open(io.BytesIO(base64.b64decode(user[1]["image_url"]["url"].split(",")[1])))
    assert max(sent.size) == menu.SIDE


def test_pdf_pages_are_pictures(owner, reader):
    owner.post("/app/settings/prices/upload", {"files": [pdf(3)]})
    assert len(reader[0][1]["content"]) == 4


def test_up_to_ten_pictures(owner, reader):
    page = owner.post("/app/settings/prices/upload", {"files": [pdf(11)]}).data
    assert b"Up to 10 photos" in page and not reader
    page = owner.post("/app/settings/prices/upload", {"files": [photo() for _ in range(11)]}).data
    assert b"Up to 10 photos" in page and not reader
    assert b"Read my price list" in page


def test_not_a_picture(owner, reader):
    page = owner.post("/app/settings/prices/upload", {"files": [(io.BytesIO(b"hello"), "a.txt")]}).data
    assert b"Couldn&#39;t open one of the files" in page and not reader


def test_no_prices_found(owner, reader, monkeypatch):
    monkeypatch.setattr(menu, "ask", lambda m: '{"services": []}')
    assert b"Couldn&#39;t find services" in owner.post("/app/settings/prices/upload", {"files": [photo()]}).data


def test_service_down(owner, app, monkeypatch):
    app.config["QUBRID_API_KEY"] = "test-key"

    def fail(*a, **kw):
        raise TimeoutError

    monkeypatch.setattr(menu.urllib.request, "urlopen", fail)
    assert b"Try again in a minute" in owner.post("/app/settings/prices/upload", {"files": [photo()]}).data


def test_replace_keeps_history(owner, reader, db):
    old = services(db)
    types = [r["id"] for r in db.execute("SELECT id FROM car_types ORDER BY sort")]
    form = {"name_0": "wash & go", "minutes_0": "25", "name_1": "Ceramic Coat", "minutes_1": "240", "name_2": "",
            "name_3": "Ceramic coat"}
    form.update({f"price_0_{t}": p for t, p in zip(types, ["80", "95", "", "1x"])})
    form.update({f"price_1_{t}": "2500" for t in types})
    resp = owner.post("/app/settings/prices/import", form)
    assert resp.location.endswith("/app/settings/prices")
    now = services(db)
    assert now["wash & go"]["id"] == old["Wash & Go"]["id"] and now["wash & go"]["minutes"] == 25
    assert now["Ceramic Coat"]["active"] and now["Ceramic Coat"]["minutes"] == 240
    assert [n for n, s in now.items() if s["active"]] == ["wash & go", "Ceramic Coat"]
    assert len(now) == len(old) + 1  # hidden, not deleted
    prices = dict(db.execute("SELECT car_type_id, price_cents FROM prices WHERE service_id = ?",
                             (now["wash & go"]["id"],)).fetchall())
    assert prices == {types[0]: 8000, types[1]: 9500}
    assert b"Some numbers were wrong" in owner.get("/app/settings/prices").data


def test_new_owner_reads_the_price_list_first(browser, reader):
    resp = signup(browser)
    assert browser.get(resp.location).location.endswith("/app/settings/prices/upload")
    page = browser.get("/app/settings/prices/upload").data
    assert b"Step 1 of 4" in page and b"Start with your price list" in page and b"Skip" in page
    resp = browser.post("/app/settings/prices/import", {"name_0": "Wash"})
    assert resp.location.endswith("/app/setup") and browser.get(resp.location).location.endswith("/app/setup/hours")


def test_demo_cannot_read(browser, reader):
    browser.post("/demo")
    assert b"/settings/prices/upload" not in browser.get("/app/settings/prices").data
    assert browser.get("/app/settings/prices/upload").status_code == 404
    assert browser.post("/app/settings/prices/import", {"name_0": "X"}).status_code == 404


def test_big_upload_only_there(owner, reader):
    big = (io.BytesIO(b"x" * 300_000), "big.jpg")
    assert owner.post("/app/settings/prices/upload", {"files": [big]}).status_code == 200
    assert owner.post("/app/settings/staff", {"new_name": "x" * 300_000}).status_code == 413


def test_clean():
    rows = menu.clean("Here you go: " + REPLY, 4)
    assert rows[0] == {"name": "Wash & Vac", "minutes": 40, "prices": [11000, 14000, 14000, None]}
    assert rows[2] == {"name": "Polish", "minutes": 30, "prices": [20000, None, None, None]}
    assert menu.clean("no json", 4) == [] and menu.clean('{"services": "x"}', 4) == []
    odd = menu.clean(json.dumps({"services": [
        {"name": "  A  ", "minutes": 1e999, "prices": [True, -5, "R1,250.50", 1e12]}, {"name": "a"}, {"name": 5}]}), 4)
    assert odd == [{"name": "A", "minutes": 30, "prices": [None, None, 125050, None]}]
