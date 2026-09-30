"""Default price list for new car washes, and generated data for demo sandboxes."""
import random
from datetime import datetime, time, timedelta

from . import queries
from .utils import DEFAULT_TZ, local_today, now_utc, plate_key, ts, zone

CAR_TYPES = ["Hatch / Sedan", "SUV", "Bakkie", "Minibus / Van"]
SERVICES = [
    # name, prices in rand per car type (same order as CAR_TYPES)
    ("Wash & Go", [70, 90, 90, 120]),
    ("Wash & Vac", [100, 130, 130, 170]),
    ("Full Valet", [250, 320, 320, 400]),
    ("Engine Wash", [80, 100, 100, 120]),
    ("Tyre Shine", [20, 25, 25, 30]),
]
MSG_READY = (
    "Hi! Your {car} ({plate}) is ready for collection at {business}. "
    "Total: {price}. Thank you!"
)
MSG_REVIEW = (
    "Thank you for choosing {business}! If you were happy with your wash, "
    "would you mind leaving us a quick review? It really helps: {review_link}"
)


def create_tenant(db, *, name, email=None, password_hash=None, phone="", city="",
                  trial_days=30, is_demo=False, theme="") -> int:
    today = local_today(zone(DEFAULT_TZ))
    cur = db.execute(
        "INSERT INTO tenants (name, email, password_hash, phone, city, msg_ready, msg_review, "
        "loyalty_every, paid_until, is_demo, theme, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name, email, password_hash, phone, city, MSG_READY, MSG_REVIEW,
            6 if is_demo else 0,
            (today + timedelta(days=trial_days)).isoformat(),
            1 if is_demo else 0,
            theme,
            ts(),
        ),
    )
    tenant_id = cur.lastrowid
    seed_price_list(db, tenant_id)
    return tenant_id


def seed_price_list(db, tenant_id: int):
    type_ids = []
    for i, name in enumerate(CAR_TYPES):
        cur = db.execute("INSERT INTO car_types (tenant_id, name, sort) VALUES (?, ?, ?)", (tenant_id, name, i))
        type_ids.append(cur.lastrowid)
    for i, (name, prices) in enumerate(SERVICES):
        cur = db.execute("INSERT INTO services (tenant_id, name, sort) VALUES (?, ?, ?)", (tenant_id, name, i))
        for type_id, rand in zip(type_ids, prices):
            db.execute(
                "INSERT INTO prices (service_id, car_type_id, price_cents) VALUES (?, ?, ?)",
                (cur.lastrowid, type_id, rand * 100),
            )


# --- demo ---------------------------------------------------------------------

DEMO_WASHERS = [  # name, pay type, pay value, speed factor
    ("Sipho", "percent", 30, 0.85),
    ("Thabo", "percent", 30, 1.0),
    ("Lerato", "percent", 30, 1.1),
    ("Bongani", "fixed", 2500, 1.25),
]
DEMO_CARS = [  # make, car type index
    ("Toyota Corolla", 0), ("VW Polo", 0), ("VW Polo Vivo", 0), ("Hyundai i20", 0), ("Suzuki Swift", 0),
    ("Kia Picanto", 0), ("Renault Kwid", 0), ("BMW 3 Series", 0), ("Mercedes-Benz C-Class", 0),
    ("Toyota Fortuner", 1), ("Haval H6", 1), ("Chery Tiggo 4", 1), ("Toyota RAV4", 1),
    ("Toyota Hilux", 2), ("Ford Ranger", 2), ("Nissan NP200", 2), ("Isuzu D-Max", 2),
    ("Toyota Quantum", 3),
]
# main service weights; minutes of work for one washer on a sedan
MAIN_SERVICES = {"Wash & Go": (45, 20), "Wash & Vac": (35, 32), "Full Valet": (8, 95), "Engine Wash": (12, 25)}
TYPE_FACTOR = [1.0, 1.15, 1.15, 1.45]


def _plate(rng: random.Random) -> str:
    letters = "BCDFGHJKLMNPRSTVWXYZ"
    if rng.random() < 0.55:
        return (f"{rng.choice(letters)}{rng.choice(letters)} {rng.randint(10, 99)} "
                f"{rng.choice(letters)}{rng.choice(letters)} GP")
    prefix = rng.choice(["CA", "CY", "CF", "CAW"])
    return f"{prefix} {rng.randint(100, 999)}-{rng.randint(100, 999)}"


def create_demo(db, now=None, theme="") -> int:
    """A sandbox car wash with two weeks of history and a few cars on the board."""
    tenant_id = create_tenant(db, name="Sunshine Car Wash (demo)", is_demo=True, theme=theme)
    db.execute(
        "UPDATE tenants SET review_url = ?, city = ? WHERE id = ?",
        ("https://g.page/r/your-car-wash/review", "Johannesburg", tenant_id),
    )
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    rng = random.Random(tenant_id)
    tz = zone(tenant["timezone"])
    now = now or now_utc()

    types = [r["id"] for r in db.execute("SELECT id FROM car_types WHERE tenant_id = ? ORDER BY sort", (tenant_id,))]
    services = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM services WHERE tenant_id = ?", (tenant_id,))}
    _, _, matrix = queries.price_list(db, tenant_id)

    washers = []
    for name, pay_type, pay_value, speed in DEMO_WASHERS:
        cur = db.execute(
            "INSERT INTO washers (tenant_id, name, pay_type, pay_value, created_at) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, name, pay_type, pay_value, ts(now - timedelta(days=30))),
        )
        washers.append((cur.lastrowid, speed))

    vehicles = []
    seen = set()
    for i in range(60):
        make, type_index = rng.choice(DEMO_CARS)
        plate = _plate(rng)
        if plate_key(plate) in seen:
            continue
        seen.add(plate_key(plate))
        phone = f"27{rng.choice(['60', '61', '71', '72', '73', '79', '82', '83'])}{rng.randint(1000000, 9999999)}"
        cur = db.execute(
            "INSERT INTO vehicles (tenant_id, plate_key, plate, make, car_type_id, phone, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, plate_key(plate), plate, make, types[type_index], phone, ts(now - timedelta(days=30))),
        )
        # a third of customers are regulars and come back often
        vehicles.append((cur.lastrowid, type_index, 6 if i < 20 else 1))

    # visit start times: 14 days of history + today up to an hour ago
    today = local_today(tz)
    arrivals = []
    for d in range(14, -1, -1):
        day = today - timedelta(days=d)
        count = rng.randint(24, 38) if day.weekday() >= 5 else rng.randint(14, 26)
        for _ in range(count):
            hour = rng.choices(range(7, 18), weights=[2, 5, 7, 8, 7, 6, 6, 7, 8, 7, 4])[0]
            local = datetime.combine(day, time(hour, rng.randint(0, 59)), tzinfo=tz)
            created = local.astimezone(now.tzinfo)
            if created < now - timedelta(hours=1):
                arrivals.append(created)
    arrivals.sort()

    loyalty_count: dict[int, int] = {}
    service_names = list(MAIN_SERVICES)
    for created in arrivals:
        vehicle_id, type_index, _ = rng.choices(vehicles, weights=[v[2] for v in vehicles])[0]
        main = rng.choices(service_names, weights=[MAIN_SERVICES[s][0] for s in service_names])[0]
        chosen = [services[main]] + ([services["Tyre Shine"]] if rng.random() < 0.3 else [])
        type_id = types[type_index]
        price = sum(matrix[s][type_id] for s in chosen)

        team = rng.sample(washers, 2 if rng.random() < 0.3 else 1)
        speed = sum(w[1] for w in team) / len(team)
        work = MAIN_SERVICES[main][1] + (5 if len(chosen) > 1 else 0)
        minutes = work * TYPE_FACTOR[type_index] * speed * (0.7 if len(team) > 1 else 1) * rng.uniform(0.85, 1.15)
        started = created + timedelta(minutes=rng.randint(0, 25))
        ready = started + timedelta(minutes=minutes)
        collected = ready + timedelta(minutes=rng.randint(3, 40))
        if collected > now:
            continue
        review = collected + timedelta(minutes=1) if rng.random() < 0.4 else None

        paid_so_far = loyalty_count.get(vehicle_id, 0)
        is_free = paid_so_far >= 5
        loyalty_count[vehicle_id] = 0 if is_free else paid_so_far + 1

        cur = db.execute(
            "INSERT INTO visits (tenant_id, vehicle_id, car_type_id, phone, price_cents, is_free, status, "
            "created_at, started_at, ready_at, collected_at, notified_at, review_requested_at) "
            "SELECT ?, id, ?, phone, ?, ?, 'collected', ?, ?, ?, ?, ?, ? FROM vehicles WHERE id = ?",
            (tenant_id, type_id, 0 if is_free else price, int(is_free), ts(created), ts(started),
             ts(ready), ts(collected), ts(ready), ts(review) if review else None, vehicle_id),
        )
        queries.set_services(db, cur.lastrowid, chosen, matrix, type_id)
        queries.set_washers(db, tenant_id, cur.lastrowid, [w[0] for w in team])

    # cars on the board right now
    board = [("queued", 9), ("queued", 3), ("washing", 24), ("washing", 12), ("ready", 41)]
    for status, age in board:
        vehicle_id, type_index, _ = rng.choice(vehicles)
        main = rng.choice(["Wash & Go", "Wash & Vac", "Wash & Vac", "Full Valet"])
        type_id = types[type_index]
        created = now - timedelta(minutes=age)
        visit = {"started_at": None, "ready_at": None, "collected_at": None}
        cur = db.execute(
            "INSERT INTO visits (tenant_id, vehicle_id, car_type_id, phone, price_cents, status, created_at) "
            "SELECT ?, id, ?, phone, ?, 'queued', ? FROM vehicles WHERE id = ?",
            (tenant_id, type_id, matrix[services[main]][type_id], ts(created), vehicle_id),
        )
        visit_id = cur.lastrowid
        queries.set_services(db, visit_id, [services[main]], matrix, type_id)
        if status != "queued":
            queries.set_washers(db, tenant_id, visit_id, [w[0] for w in rng.sample(washers, 1)])
            updates = queries.status_updates(visit, status, ts(created + timedelta(minutes=2)))
            if status == "ready":
                updates["ready_at"] = updates["notified_at"] = ts(now - timedelta(minutes=4))
            queries.apply_updates(db, visit_id, updates)
    return tenant_id


def cleanup_demos(db, max_age_hours=24, keep_at_most=300):
    cutoff = ts(now_utc() - timedelta(hours=max_age_hours))
    db.execute("DELETE FROM tenants WHERE is_demo = 1 AND created_at < ?", (cutoff,))
    db.execute(
        "DELETE FROM tenants WHERE is_demo = 1 AND id NOT IN "
        "(SELECT id FROM tenants WHERE is_demo = 1 ORDER BY id DESC LIMIT ?)",
        (keep_at_most,),
    )

