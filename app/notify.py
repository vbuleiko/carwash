"""Emails to you (NOTIFY_EMAIL): each new sign-up within a minute and a daily report at 22:00.

`flask --app app notify` runs the loop (the `notify` service in docker-compose.yml).
`flask --app app report` sends today's report right away, to check the email settings.
"""
import logging
import smtplib
import ssl
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

import click
from flask import current_app, g, request
from flask.cli import with_appcontext

from . import queries
from .db import connect, get_db
from .utils import DEFAULT_TZ, format_phone, money, now_utc, to_local, ts, wa_link, zone

log = logging.getLogger(__name__)
TZ = zone(DEFAULT_TZ)
REPORT_AT = dtime(22, 0)
KEEP_DAYS = 30
MAX_ERRORS_PER_DAY = 1000
APP_DIR = Path(__file__).resolve().parent


# --- server errors --------------------------------------------------------------

def record_error(sender, exception, **_):
    """got_request_exception: keep the error for the daily report."""
    try:
        frames = [f for f in traceback.extract_tb(exception.__traceback__)
                  if Path(f.filename).resolve().is_relative_to(APP_DIR)]
        place = f"{Path(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}" if frames else ""
        tenant = g.get("tenant") or g.get("site")
        row = (ts(), tenant["id"] if tenant else None, request.method, request.path[:300],
               traceback.format_exception_only(exception)[-1].strip()[:300], place,
               "".join(traceback.format_exception(exception))[-4000:])
        conn = connect(sender.config["DATABASE"])
        try:
            with conn:
                day_ago = ts(now_utc() - timedelta(days=1))
                if conn.execute("SELECT COUNT(*) FROM errors WHERE at >= ?", (day_ago,)).fetchone()[0] < MAX_ERRORS_PER_DAY:
                    conn.execute("INSERT INTO errors (at, tenant_id, method, path, error, place, trace) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)", row)
        finally:
            conn.close()
    except Exception:
        log.exception("could not record the error")


# --- sending --------------------------------------------------------------------

def configured(config) -> bool:
    return all(config[k] for k in ("SMTP_HOST", "SMTP_PASSWORD", "NOTIFY_EMAIL", "MAIL_FROM"))


def send(config, subject: str, body: str):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Date"] = config["MAIL_FROM"], config["NOTIFY_EMAIL"], formatdate(usegmt=True)
    msg["Subject"] = " ".join(subject.split())
    msg.set_content(body)
    port, tls = config["SMTP_PORT"], ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(config["SMTP_HOST"], port, timeout=30, context=tls)
    else:
        smtp = smtplib.SMTP(config["SMTP_HOST"], port, timeout=30)
    with smtp:
        if port != 465:
            smtp.starttls(context=tls)
        smtp.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        smtp.send_message(msg)


def _sent(db, key: str) -> bool:
    return db.execute("SELECT 1 FROM mails_sent WHERE key = ?", (key,)).fetchone() is not None


def _mark(db, key: str):
    with db:
        db.execute("INSERT OR IGNORE INTO mails_sent (key, sent_at) VALUES (?, ?)", (key, ts()))


def tick(db, config, now: datetime | None = None):
    """Once a minute: mail new sign-ups, and the report once it's 22:00."""
    now = now or now_utc()
    for t in db.execute("SELECT * FROM tenants WHERE is_demo = 0 AND created_at >= ? ORDER BY id",
                        (ts(now - timedelta(days=1)),)).fetchall():
        key = f"signup:{t['id']}:{t['created_at']}"
        if not _sent(db, key):
            send(config, *signup_mail(t, config))
            _mark(db, key)
    local = now.astimezone(TZ)
    key = f"report:{local.date()}"
    if local.time() >= REPORT_AT and not _sent(db, key):
        send(config, *daily_report(db, config, local.date()))
        _mark(db, key)
        with db:
            old = ts(now - timedelta(days=KEEP_DAYS))
            db.execute("DELETE FROM errors WHERE at < ?", (old,))
            db.execute("DELETE FROM mails_sent WHERE sent_at < ?", (old,))


# --- what the emails say --------------------------------------------------------

def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def signup_mail(t, config) -> tuple[str, str]:
    at = to_local(t["created_at"], TZ)
    lines = [
        f"{t['name']} signed up on {at:%d %b %Y} at {at:%H:%M} (Johannesburg).",
        "",
        f"Email: {t['email']}",
        f"WhatsApp: {format_phone(t['phone'])} · {wa_link(t['phone'])}",
        f"City: {t['city'] or '-'}",
        f"Trial until: {date.fromisoformat(t['paid_until']):%d %b %Y}",
    ]
    if config["SITE_URL"]:
        lines += ["", f"All car washes: {config['SITE_URL']}/admin"]
    return f"New sign-up: {t['name']}", "\n".join(lines)


def daily_report(db, config, day: date) -> tuple[str, str]:
    try:
        return _report(db, config, day)
    except Exception:
        log.exception("daily report failed")
        return f"{config['APP_NAME']} · {day:%d %b} · the report failed", traceback.format_exc()


def _report(db, config, day: date) -> tuple[str, str]:
    a = ts(datetime.combine(day - timedelta(days=1), REPORT_AT, TZ))
    b = ts(datetime.combine(day, REPORT_AT, TZ))
    real = "AND tenant_id IN (SELECT id FROM tenants WHERE is_demo = 0)"
    tenants = db.execute("SELECT * FROM tenants WHERE is_demo = 0 ORDER BY name COLLATE NOCASE, id").fetchall()
    visits = {r["tenant_id"]: r for r in db.execute(
        "SELECT tenant_id, SUM(status != 'cancelled') AS cars, SUM(status = 'cancelled') AS cancelled, "
        "SUM(CASE WHEN status != 'cancelled' THEN price_cents ELSE 0 END) AS revenue "
        f"FROM visits WHERE created_at >= ? AND created_at < ? {real} GROUP BY tenant_id", (a, b))}
    ready, collected = (dict(db.execute(
        f"SELECT tenant_id, COUNT(*) FROM visits WHERE {col} >= ? AND {col} < ? {real} GROUP BY tenant_id", (a, b)
    ).fetchall()) for col in ("ready_at", "collected_at"))
    booked = {r["tenant_id"]: r for r in db.execute(
        "SELECT tenant_id, COUNT(*) AS made, SUM(status = 'cancelled') AS cancelled FROM bookings "
        f"WHERE created_at >= ? AND created_at < ? {real} GROUP BY tenant_id", (a, b))}
    errors = db.execute(
        "SELECT e.*, t.name, t.is_demo FROM errors e LEFT JOIN tenants t ON t.id = e.tenant_id "
        "WHERE e.at >= ? AND e.at < ? ORDER BY e.at", (a, b)).fetchall()
    demos = db.execute("SELECT COUNT(*) FROM tenants WHERE is_demo = 1 AND created_at >= ? AND created_at < ?",
                       (a, b)).fetchone()[0]
    signups = [t for t in tenants if a <= t["created_at"] < b]
    active = set(visits) | set(ready) | set(collected) | set(booked)
    cars = sum(v["cars"] for v in visits.values())

    lines = [f"{config['APP_NAME']} — {day:%A %d %B %Y}", "22:00 yesterday to 22:00 today, Johannesburg time.",
             "", f"NEW SIGN-UPS: {len(signups)}"]
    lines += [f"  {t['name']} · {t['email']} · {format_phone(t['phone'])} · {t['city'] or '-'} · "
              f"at {to_local(t['created_at'], TZ):%H:%M}" for t in signups]

    lines += ["", f"SERVER ERRORS: {len(errors)}"]
    groups = defaultdict(list)
    for e in errors:
        groups[(e["error"], e["place"])].append(e)
    for (error, place), same in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        last = same[-1]
        lines += ["", f"  {len(same)}× {error}" + (f" — {place}" if place else ""),
                  f"     last at {to_local(last['at'], TZ):%H:%M} · {last['method']} {last['path']} · "
                  + ", ".join(sorted({_who(e) for e in same}))]
        trace = [s for s in last["trace"].splitlines() if s.strip(" ~^")]  # without Python's ~~~^^^ underlines
        lines += ["     " + s for s in trace[-8:]]

    lines += ["", f"CAR WASHES: {len(tenants)} · active today: {len(active)}"]
    for t in sorted(tenants, key=lambda t: (t["id"] not in active, -visits[t["id"]]["cars"] if t["id"] in visits else 0)):
        head = f"  {t['name']} · {_plan(t, day)} · last seen {_seen(t, day)}"
        if t["id"] not in active:
            lines.append(head + " · nothing today")
            continue
        v, bk = visits.get(t["id"]), booked.get(t["id"])
        parts = [_n(v["cars"] if v else 0, "car"), money(v["revenue"] if v else 0),
                 f"{ready.get(t['id'], 0)} ready", f"{collected.get(t['id'], 0)} collected"]
        if v and v["cancelled"]:
            parts.append(f"{v['cancelled']} cancelled")
        if bk:
            parts.append(_n(bk["made"], "booking") + (f" ({bk['cancelled']} cancelled)" if bk["cancelled"] else ""))
        lines += [head, "    " + " · ".join(parts)]

    lines += ["", f"DEMOS: {demos} started"]
    subject = (f"{config['APP_NAME']} · {day:%d %b} · {_n(len(signups), 'sign-up')} · "
               f"{_n(len(errors), 'error')} · {_n(cars, 'car')}")
    return subject, "\n".join(lines)


def _who(e) -> str:
    if e["tenant_id"] is None:
        return "no car wash"
    if e["name"] is None:
        return f"deleted #{e['tenant_id']}"
    return "a demo" if e["is_demo"] else e["name"]


def _plan(t, day: date) -> str:
    if t["is_disabled"]:
        return "paused"
    sub = queries.subscription(t, day)
    plan = "trial" if sub["plan"] == "trial" else "paid"
    if sub["days_left"] < 0:
        return f"{plan} ended {_n(-sub['days_left'], 'day')} ago"
    return f"{plan}, {_n(sub['days_left'], 'day')} left"


def _seen(t, day: date) -> str:
    seen = to_local(t["last_seen_at"], TZ)
    if not seen:
        return "never"
    return f"{seen:%H:%M}" if seen.date() == day else f"{seen:%d %b %H:%M}"


# --- commands -------------------------------------------------------------------

@click.command("notify")
@with_appcontext
def notify_command():
    """Mail new sign-ups and the daily report (runs until stopped)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = current_app._get_current_object()
    if not configured(app.config):
        log.warning("Emails are off: set SMTP_HOST, SMTP_PASSWORD and NOTIFY_EMAIL in .env")
    while True:
        if configured(app.config):
            with app.app_context():
                try:
                    tick(get_db(), app.config)
                except Exception:
                    log.exception("notify failed")
        time.sleep(60)


@click.command("report")
@with_appcontext
def report_command():
    """Send today's report now, to check the email settings."""
    config = current_app.config
    if not configured(config):
        raise click.ClickException("Set SMTP_HOST, SMTP_PASSWORD and NOTIFY_EMAIL in .env")
    send(config, *daily_report(get_db(), config, now_utc().astimezone(TZ).date()))
    click.echo(f"Sent to {config['NOTIFY_EMAIL']}.")
