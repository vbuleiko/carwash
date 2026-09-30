"""Data access shared by the owner app, reports and seeding."""
import secrets
from datetime import date

from .utils import (
    fill_template,
    format_phone,
    money,
    plate_key,
    ts,
    wa_link,
)

STATUS_FLOW = ["queued", "washing", "ready", "collected"]
STATUS_LABELS = {
    "queued": "Waiting",
    "washing": "Washing",
    "ready": "Ready",
    "collected": "Collected",
    "cancelled": "Cancelled",
}


# --- tenant -------------------------------------------------------------------

def subscription(tenant, today: date) -> dict:
    if tenant["is_demo"]:
        return {"active": True, "demo": True, "days_left": None, "plan": "demo", "until": None}
    until = date.fromisoformat(tenant["paid_until"])
    days_left = (until - today).days
    return {
        "active": days_left >= 0,
        "demo": False,
        "days_left": days_left,
        "plan": tenant["plan"],
        "until": until,
    }


# --- price list ---------------------------------------------------------------

def price_list(db, tenant_id: int, keep_types=(), keep_services=()):
    """Active car types and services (plus any ids in keep_*), and the price matrix."""
    keep_types = tuple(int(i) for i in keep_types if i)
    keep_services = tuple(int(i) for i in keep_services if i)

    def rows(table, keep):
        marks = ",".join("?" * len(keep)) or "NULL"
        return db.execute(
            f"SELECT * FROM {table} WHERE tenant_id = ? AND (active = 1 OR id IN ({marks})) "
            "ORDER BY sort, id",
            (tenant_id, *keep),
        ).fetchall()

    car_types = rows("car_types", keep_types)
    services = rows("services", keep_services)
    matrix: dict[int, dict[int, int]] = {}
    for r in db.execute(
        "SELECT p.* FROM prices p JOIN services s ON s.id = p.service_id WHERE s.tenant_id = ?",
        (tenant_id,),
    ):
        matrix.setdefault(r["service_id"], {})[r["car_type_id"]] = r["price_cents"]
    return car_types, services, matrix


# --- vehicles & visits --------------------------------------------------------

def find_vehicle(db, tenant_id: int, plate: str):
    return db.execute(
        "SELECT * FROM vehicles WHERE tenant_id = ? AND plate_key = ?",
        (tenant_id, plate_key(plate)),
    ).fetchone()


def upsert_vehicle(db, tenant_id: int, plate: str, make: str, car_type_id, phone: str) -> int:
    existing = find_vehicle(db, tenant_id, plate)
    if existing:
        db.execute(
            "UPDATE vehicles SET plate = ?, make = COALESCE(NULLIF(?, ''), make), "
            "car_type_id = COALESCE(?, car_type_id), phone = COALESCE(NULLIF(?, ''), phone) "
            "WHERE id = ?",
            (plate, make, car_type_id, phone, existing["id"]),
        )
        return existing["id"]
    cur = db.execute(
        "INSERT INTO vehicles (tenant_id, plate_key, plate, make, car_type_id, phone, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, plate_key(plate), plate, make, car_type_id, phone, ts()),
    )
    return cur.lastrowid


VISIT_SELECT = """
    SELECT v.*, vh.plate, vh.make, ct.name AS car_type
    FROM visits v
    JOIN vehicles vh ON vh.id = v.vehicle_id
    LEFT JOIN car_types ct ON ct.id = v.car_type_id
"""


def get_visit(db, tenant_id: int, visit_id: int):
    rows = db.execute(VISIT_SELECT + " WHERE v.tenant_id = ? AND v.id = ?", (tenant_id, visit_id)).fetchall()
    return with_details(db, rows)[0] if rows else None


def with_details(db, rows) -> list[dict]:
    """Rows -> dicts with service and washer names attached."""
    visits = [dict(r) for r in rows]
    if not visits:
        return visits
    by_id = {v["id"]: v for v in visits}
    for v in visits:
        v.update(services=[], service_ids=[], washers=[], washer_ids=[])
    marks = ",".join("?" * len(by_id))
    for r in db.execute(
        f"SELECT vs.visit_id, s.id, s.name FROM visit_services vs JOIN services s ON s.id = vs.service_id "
        f"WHERE vs.visit_id IN ({marks}) ORDER BY s.sort, s.id",
        tuple(by_id),
    ):
        by_id[r["visit_id"]]["services"].append(r["name"])
        by_id[r["visit_id"]]["service_ids"].append(r["id"])
    for r in db.execute(
        f"SELECT vw.visit_id, w.id, w.name FROM visit_washers vw JOIN washers w ON w.id = vw.washer_id "
        f"WHERE vw.visit_id IN ({marks}) ORDER BY w.name",
        tuple(by_id),
    ):
        by_id[r["visit_id"]]["washers"].append(r["name"])
        by_id[r["visit_id"]]["washer_ids"].append(r["id"])
    return visits


def set_services(db, visit_id: int, service_ids, matrix, car_type_id):
    db.execute("DELETE FROM visit_services WHERE visit_id = ?", (visit_id,))
    for sid in service_ids:
        db.execute(
            "INSERT INTO visit_services (visit_id, service_id, price_cents) VALUES (?, ?, ?)",
            (visit_id, sid, matrix.get(sid, {}).get(car_type_id, 0)),
        )


def set_washers(db, tenant_id: int, visit_id: int, washer_ids):
    """Replace the team on a visit. Pay rules of washers already on it are kept."""
    washer_ids = [int(w) for w in washer_ids]
    marks = ",".join("?" * len(washer_ids))
    db.execute(
        f"DELETE FROM visit_washers WHERE visit_id = ? AND washer_id NOT IN ({marks})",
        (visit_id, *washer_ids),
    )
    for w in db.execute(
        f"SELECT * FROM washers WHERE tenant_id = ? AND id IN ({marks})", (tenant_id, *washer_ids)
    ).fetchall():
        db.execute(
            "INSERT OR IGNORE INTO visit_washers (visit_id, washer_id, pay_type, pay_value) VALUES (?, ?, ?, ?)",
            (visit_id, w["id"], w["pay_type"], w["pay_value"]),
        )


def status_updates(visit, new_status: str, now: str | None = None) -> dict:
    """Timestamps that go with moving a visit to new_status (forwards or back)."""
    now = now or ts()
    updates = {"status": new_status}
    if new_status == "cancelled":
        return updates
    level = STATUS_FLOW.index(new_status)
    for i, field in enumerate(("started_at", "ready_at", "collected_at"), start=1):
        updates[field] = (visit[field] or now) if level >= i else None
    return updates


def apply_updates(db, visit_id: int, updates: dict):
    cols = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE visits SET {cols} WHERE id = ?", (*updates.values(), visit_id))


# --- loyalty ------------------------------------------------------------------

def loyalty(db, tenant, vehicle_id: int, exclude_visit_id: int | None = None) -> dict:
    """Paid washes since the last free one, and whether the next wash is free."""
    every = tenant["loyalty_every"] or 0
    last_free = db.execute(
        "SELECT MAX(created_at) FROM visits WHERE vehicle_id = ? AND is_free = 1 AND status != 'cancelled' "
        "AND id != ?",
        (vehicle_id, exclude_visit_id or 0),
    ).fetchone()[0]
    count = db.execute(
        "SELECT COUNT(*) FROM visits WHERE vehicle_id = ? AND is_free = 0 AND status != 'cancelled' "
        "AND id != ? AND created_at > ?",
        (vehicle_id, exclude_visit_id or 0, last_free or ""),
    ).fetchone()[0]
    return {
        "every": every,
        "count": count,
        "needed": max(every - 1, 0),
        "next_free": every > 1 and count >= every - 1,
    }


# --- WhatsApp messages --------------------------------------------------------

def message_values(tenant, visit) -> dict:
    return {
        "business": tenant["name"],
        "plate": visit["plate"],
        "car": visit.get("make") or visit.get("car_type") or "car",
        "price": "FREE (loyalty reward)" if visit["is_free"] else money(visit["price_cents"]),
        "review_link": tenant["review_url"],
    }


def ready_message(tenant, visit) -> str:
    return fill_template(tenant["msg_ready"], message_values(tenant, visit))


def review_message(tenant, visit) -> str:
    return fill_template(tenant["msg_review"], message_values(tenant, visit))


def chat_link(tenant, phone: str, text: str = "") -> str:
    """wa.me link; demo phone numbers are random but real-looking, so demos never link out."""
    return "#" if tenant["is_demo"] else wa_link(phone, text)


def add_links(tenant, visit: dict) -> dict:
    """Attach WhatsApp texts/links used by the board and visit pages."""
    visit["phone_display"] = format_phone(visit["phone"])
    visit["msg_ready"] = ready_message(tenant, visit)
    visit["msg_review"] = review_message(tenant, visit)
    if visit["phone"]:
        visit["wa_ready"] = chat_link(tenant, visit["phone"], visit["msg_ready"])
        visit["wa_review"] = chat_link(tenant, visit["phone"], visit["msg_review"]) if tenant["review_url"] else ""
    else:
        visit["wa_ready"] = visit["wa_review"] = ""
    return visit


def session_key(db, tenant_id: int, rotate: bool = False) -> str:
    """Random key stored in every login cookie of this car wash; rotating it logs out all devices."""
    key = db.execute("SELECT session_key FROM tenants WHERE id = ?", (tenant_id,)).fetchone()[0]
    if rotate or not key:
        key = secrets.token_urlsafe(16)
        db.execute("UPDATE tenants SET session_key = ? WHERE id = ?", (key, tenant_id))
        db.commit()
    return key
