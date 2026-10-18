import ssl
from datetime import date, datetime, timedelta

import pytest

from app import notify, reports
from app.utils import now_utc

from .conftest import Browser, signup

MAIL = {"SMTP_HOST": "smtp.test", "SMTP_PORT": 587, "SMTP_USER": "resend", "SMTP_PASSWORD": "key",
        "NOTIFY_EMAIL": "admin@carwasheasy.co.za", "MAIL_FROM": "CarWashEasy <noreply@carwasheasy.co.za>",
        "SITE_URL": "https://carwasheasy.co.za"}


@pytest.fixture
def mails(app, monkeypatch):
    app.config.update(MAIL)
    sent = []
    monkeypatch.setattr(notify, "send", lambda config, subject, body: sent.append((subject, body)))
    return sent


def next_report_time():
    """The next 22:00 in Johannesburg: its report covers everything done right now."""
    local = now_utc().astimezone(notify.TZ)
    day = local.date() + timedelta(days=local.time() >= notify.REPORT_AT)
    return datetime.combine(day, notify.REPORT_AT, notify.TZ)


def test_new_sign_up_is_mailed_once(app, browser, db, mails):
    signup(browser, name="Joe's Wash\nBcc: someone@example.com")
    Browser(app.test_client()).post("/demo")
    notify.tick(db, app.config)
    notify.tick(db, app.config)
    assert len(mails) == 1
    subject, body = mails[0]
    assert subject.startswith("New sign-up: Joe's Wash")
    assert "owner@example.com" in body and "+27 82 555 1234" in body and "https://wa.me/27825551234" in body
    assert "https://carwasheasy.co.za/admin" in body


def test_websites_for_car_washes_without_an_owner_stay_out_of_the_mails(app, db, mails):
    admin = Browser(app.test_client())
    admin.post("/admin/login", {"password": "admin-pass"})
    admin.post("/admin/prospects", {"lines": "Bay Crescent Carwash | Lancaster Rd | Gordon's Bay | 082 111 2222\n"
                                             "Water Worx | | Strand |"})
    notify.tick(db, app.config, next_report_time())
    assert [s for s, _ in mails] == [f"CarWashEasy · {next_report_time():%d %b} · 0 sign-ups · 0 errors · 0 cars"]
    body = mails[0][1]
    assert "NEW SIGN-UPS: 0" in body and "CAR WASHES: 0" in body and "WEBSITE PREVIEWS, no owner yet: 2" in body


def test_daily_report_goes_once_at_22(app, owner, db, mails):
    tid = db.execute("SELECT id FROM tenants WHERE is_demo = 0").fetchone()[0]
    types = [r[0] for r in db.execute("SELECT id FROM car_types WHERE tenant_id = ? ORDER BY sort", (tid,))]
    svc = db.execute("SELECT id FROM services WHERE tenant_id = ? ORDER BY sort", (tid,)).fetchone()[0]
    owner.post("/app/new", {"plate": "CA 123-456", "phone": "082 111 2222", "car_type_id": types[0],
                            "service_id": [svc]})
    Browser(owner.client.application.test_client()).post("/demo")
    at = next_report_time()

    notify.tick(db, app.config, at - timedelta(minutes=1))
    assert [s for s, _ in mails] == ["New sign-up: Bubbles Car Wash"]
    notify.tick(db, app.config, at)
    notify.tick(db, app.config, at + timedelta(minutes=5))
    report = [(s, b) for s, b in mails if s.startswith("CarWashEasy ·")]
    assert len(report) == 1
    subject, body = report[0]
    assert subject == f"CarWashEasy · {at:%d %b} · 1 sign-up · 0 errors · 1 car"
    assert "NEW SIGN-UPS: 1\n  Bubbles Car Wash · owner@example.com" in body
    assert "CAR WASHES: 1 · active today: 1" in body
    days_left = (date.fromisoformat(db.execute("SELECT paid_until FROM tenants WHERE id = ?", (tid,)).fetchone()[0])
                 - at.date()).days
    assert f"  Bubbles Car Wash · trial, {days_left} days left · last seen" in body
    assert "    1 car · R70 · 0 ready · 0 collected" in body
    assert "DEMOS: 1 started" in body


def test_server_error_is_kept_and_reported(app, owner, db, mails, monkeypatch):
    def broken(*args, **kw):
        raise ValueError("boom")

    monkeypatch.setattr(reports, "build", broken)
    with pytest.raises(ValueError):
        owner.get("/app/reports")
    error = db.execute("SELECT * FROM errors").fetchone()
    assert error["tenant_id"] == db.execute("SELECT id FROM tenants WHERE is_demo = 0").fetchone()[0]
    assert (error["method"], error["path"], error["error"]) == ("GET", "/app/reports", "ValueError: boom")
    assert error["place"].startswith("owner.py:") and error["place"].endswith(" in report")

    notify.tick(db, app.config, next_report_time())
    subject, body = mails[-1]
    assert "1 error" in subject
    assert "SERVER ERRORS: 1\n\n  1× ValueError: boom — owner.py:" in body
    assert "· GET /app/reports · Bubbles Car Wash" in body


def test_send_uses_smtp_with_starttls(app, monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            calls.append(("starttls", context.verify_mode == ssl.CERT_REQUIRED))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, msg):
            calls.append(("send", msg["From"], msg["To"], msg["Subject"], msg.get_content()))

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    notify.send(MAIL, "Hello\r\nthere", "Body")
    assert calls == [("connect", "smtp.test", 587), ("starttls", True), ("login", "resend", "key"),
                     ("send", "CarWashEasy <noreply@carwasheasy.co.za>", "admin@carwasheasy.co.za",
                      "Hello there", "Body\n")]


def test_no_emails_without_settings(app):
    assert not notify.configured(app.config)


def test_mail_goes_from_the_gmail_that_logs_in():
    from app import _mail_from
    assert _mail_from("CarWashEasy", "notify@gmail.com", "carwasheasy.co.za") == "CarWashEasy <notify@gmail.com>"
    assert _mail_from("CarWashEasy", "resend", "carwasheasy.co.za") == "CarWashEasy <noreply@carwasheasy.co.za>"
    assert _mail_from("CarWashEasy", "", "") == ""
