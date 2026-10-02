"""Online booking: opening hours, free times, and bookings themselves."""
import json
import re
import secrets
from collections import Counter
from datetime import date, datetime, time, timedelta

from .queries import chat_link, subscription
from .utils import fill_template, format_phone, money, now_utc, to_local, ts, utc_bounds, zone

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULT_HOURS = [("08:00", "17:00")] * 5 + [("08:00", "16:00"), ("09:00", "14:00")]
TIMES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]  # a slot every 30 minutes
DAYS_AHEAD = 14
LEAD_MINUTES = 30  # the soonest a customer can book
MAX_CAPACITY = 20
TAKING_A_PLACE = ("new", "confirmed", "arrived")
STATUS_LABELS = {"new": "New", "confirmed": "Confirmed", "arrived": "Arrived", "cancelled": "Cancelled"}
MSG_BOOKING = (
    "Hi {name}! Your booking at {business} is confirmed: {date} at {time}. "
    "{services}, {price}. See you soon!"
)
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]")


# --- opening hours and free times ----------------------------------------------

def hours(tenant) -> list:
    """Opening hours from Monday: ('08:00', '17:00'), or None on a closed day."""
    try:
        week = json.loads(tenant["hours"]) if tenant["hours"] else None
    except ValueError:
        week = None
    if not isinstance(week, list) or len(week) != 7:
        return list(DEFAULT_HOURS)
    return [
        (day[0], day[1]) if isinstance(day, list) and len(day) == 2 and day[0] in TIMES and day[1] in TIMES
        and day[0] < day[1] else None
        for day in week
    ]


def hours_text(span) -> str:
    return f"{span[0]}–{span[1]}" if span else "Closed"


def can_book(tenant, today: date) -> bool:
    return bool(tenant["booking_on"]) and not tenant["is_disabled"] and subscription(tenant, today)["active"]


def day_name(d: date, today: date) -> str:
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return f"{d:%a} {d.day} {d:%b}"


def days(db, tenant, now: datetime | None = None) -> list[dict]:
    """The next two weeks with the times that still have room, in the car wash's time zone."""
    tz = zone(tenant["timezone"])
    now = now or now_utc()
    today = now.astimezone(tz).date()
    earliest = now + timedelta(minutes=LEAD_MINUTES)
    start, end = utc_bounds(today, today + timedelta(days=DAYS_AHEAD), tz)
    marks = ",".join("?" * len(TAKING_A_PLACE))
    taken = Counter(r[0] for r in db.execute(
        f"SELECT slot_at FROM bookings WHERE tenant_id = ? AND status IN ({marks}) AND slot_at >= ? AND slot_at < ?",
        (tenant["id"], *TAKING_A_PLACE, start, end),
    ))
    week = hours(tenant)
    out = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        span = week[d.weekday()]
        slots, ahead = [], 0
        for t in TIMES if span else ():
            at = datetime.combine(d, time.fromisoformat(t), tzinfo=tz)
            if not span[0] <= t < span[1] or at < earliest:
                continue
            ahead += 1
            left = tenant["slot_capacity"] - taken[ts(at)]
            if left > 0:
                slots.append({"time": t, "left": left, "value": f"{d.isoformat()} {t}"})
        out.append({"date": d, "name": day_name(d, today), "full": ahead > 0 and not slots, "slots": slots,
                    "parts": _parts(slots)})
    return out


def _parts(slots) -> list:
    groups: dict[str, list] = {}
    for slot in slots:
        hour = int(slot["time"][:2])
        groups.setdefault("Morning" if hour < 12 else "Afternoon" if hour < 17 else "Evening", []).append(slot)
    return list(groups.items())


def free_slot(db, tenant, value: str | None) -> str | None:
    """'2026-10-03 09:30' (local) -> UTC timestamp, if that time is still open for booking."""
    for day in days(db, tenant):
        for slot in day["slots"]:
            if slot["value"] == value:
                return ts(datetime.combine(day["date"], time.fromisoformat(slot["time"]),
                                           tzinfo=zone(tenant["timezone"])))
    return None


# --- bookings -----------------------------------------------------------------

def create(db, tenant_id: int, *, slot_at, name, phone, plate, car_type_id, service_ids, price_cents, note) -> str:
    code = secrets.token_urlsafe(9)
    cur = db.execute(
        "INSERT INTO bookings (tenant_id, code, slot_at, name, phone, plate, car_type_id, price_cents, note, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, code, slot_at, name, phone, plate, car_type_id, price_cents, note, ts()),
    )
    db.executemany("INSERT INTO booking_services (booking_id, service_id) VALUES (?, ?)",
                   [(cur.lastrowid, s) for s in service_ids])
    return code


SELECT = """
    SELECT b.*, ct.name AS car_type FROM bookings b
    LEFT JOIN car_types ct ON ct.id = b.car_type_id
"""


def with_services(db, rows) -> list[dict]:
    found = [dict(r, services=[], service_ids=[]) for r in rows]
    by_id = {b["id"]: b for b in found}
    if by_id:
        marks = ",".join("?" * len(by_id))
        for r in db.execute(
            f"SELECT bs.booking_id, s.id, s.name FROM booking_services bs JOIN services s ON s.id = bs.service_id "
            f"WHERE bs.booking_id IN ({marks}) ORDER BY s.sort, s.id",
            tuple(by_id),
        ):
            by_id[r["booking_id"]]["services"].append(r["name"])
            by_id[r["booking_id"]]["service_ids"].append(r["id"])
    return found


def get(db, tenant_id: int, booking_id: int):
    rows = db.execute(SELECT + " WHERE b.tenant_id = ? AND b.id = ?", (tenant_id, booking_id)).fetchall()
    return with_services(db, rows)[0] if rows else None


def between(db, tenant_id: int, start: str, end: str | None = None, open_only=False) -> list[dict]:
    sql = SELECT + " WHERE b.tenant_id = ? AND b.slot_at >= ? AND b.slot_at < ?"
    if open_only:
        sql += " AND b.status IN ('new', 'confirmed')"
    rows = db.execute(sql + " ORDER BY b.slot_at, b.id", (tenant_id, start, end or "9999")).fetchall()
    return with_services(db, rows)


def count_new(db, tenant_id: int, since: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM bookings WHERE tenant_id = ? AND status = 'new' AND slot_at >= ?", (tenant_id, since)
    ).fetchone()[0]


# --- WhatsApp -----------------------------------------------------------------

def message_values(tenant, b) -> dict:
    local = to_local(b["slot_at"], zone(tenant["timezone"]))
    return {
        "name": b["name"],
        "business": tenant["name"],
        "date": f"{local:%a} {local.day} {local:%b}",
        "time": f"{local:%H:%M}",
        "services": ", ".join(b["services"]),
        "price": money(b["price_cents"]),
        "address": tenant["address"] or tenant["city"],
    }


def confirm_message(tenant, b) -> str:
    return fill_template(tenant["msg_booking"] or MSG_BOOKING, message_values(tenant, b))


def add_links(tenant, b: dict) -> dict:
    b["phone_display"] = format_phone(b["phone"])
    b["msg_confirm"] = confirm_message(tenant, b)
    b["wa_confirm"] = chat_link(tenant, b["phone"], b["msg_confirm"])
    b["wa_chat"] = chat_link(tenant, b["phone"])
    return b


# --- booking page address -------------------------------------------------------

def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-")
    return slug if SLUG_RE.fullmatch(slug) else "car-wash"


def unique_slug(db, name: str, tenant_id: int = 0) -> str:
    base = slugify(name)
    slug, n = base, 2
    while db.execute("SELECT 1 FROM tenants WHERE slug = ? AND id != ?", (slug, tenant_id)).fetchone():
        slug, n = f"{base[:36]}-{n}", n + 1
    return slug


def fill_slugs(db):
    """Car washes from before online booking get their page address."""
    for t in db.execute("SELECT id, name FROM tenants WHERE slug = ''").fetchall():
        db.execute("UPDATE tenants SET slug = ? WHERE id = ?", (unique_slug(db, t["name"], t["id"]), t["id"]))
