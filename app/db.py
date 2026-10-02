"""SQLite storage. One file, one schema, every row belongs to a tenant (car wash)."""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT UNIQUE,
    password_hash TEXT,
    phone         TEXT NOT NULL DEFAULT '',
    city          TEXT NOT NULL DEFAULT '',
    country_code  TEXT NOT NULL DEFAULT '27',
    timezone      TEXT NOT NULL DEFAULT 'Africa/Johannesburg',
    review_url    TEXT NOT NULL DEFAULT '',
    msg_ready     TEXT NOT NULL DEFAULT '',
    msg_review    TEXT NOT NULL DEFAULT '',
    loyalty_every INTEGER NOT NULL DEFAULT 0,      -- 0 = off, 6 = every 6th wash free
    theme         TEXT NOT NULL DEFAULT '',        -- '' = the site default
    session_key   TEXT NOT NULL DEFAULT '',        -- in every login cookie; a new one logs out all devices
    slug          TEXT NOT NULL DEFAULT '',        -- booking page address: /book/<slug>
    address       TEXT NOT NULL DEFAULT '',
    hours         TEXT NOT NULL DEFAULT '',        -- JSON, 7 days from Monday: ["08:00", "17:00"] or null; '' = default
    slot_capacity INTEGER NOT NULL DEFAULT 2,      -- cars that can be booked for the same time
    booking_on    INTEGER NOT NULL DEFAULT 1,
    msg_booking   TEXT NOT NULL DEFAULT '',
    msg_cancel    TEXT NOT NULL DEFAULT '',
    plan          TEXT NOT NULL DEFAULT 'trial',   -- trial | paid
    paid_until    TEXT NOT NULL,                   -- YYYY-MM-DD, inclusive
    is_demo       INTEGER NOT NULL DEFAULT 0,
    is_disabled   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS car_types (
    id        INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    sort      INTEGER NOT NULL DEFAULT 0,
    active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS services (
    id        INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    sort      INTEGER NOT NULL DEFAULT 0,
    active    INTEGER NOT NULL DEFAULT 1,
    minutes   INTEGER NOT NULL DEFAULT 30          -- how long a booking for it holds a place
);

CREATE TABLE IF NOT EXISTS prices (
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    car_type_id INTEGER NOT NULL REFERENCES car_types(id) ON DELETE CASCADE,
    price_cents INTEGER NOT NULL,
    PRIMARY KEY (service_id, car_type_id)
);

CREATE TABLE IF NOT EXISTS washers (
    id         INTEGER PRIMARY KEY,
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles (
    id          INTEGER PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plate_key   TEXT NOT NULL,
    plate       TEXT NOT NULL,
    make        TEXT NOT NULL DEFAULT '',
    car_type_id INTEGER REFERENCES car_types(id) ON DELETE SET NULL,
    phone       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE (tenant_id, plate_key)
);

CREATE TABLE IF NOT EXISTS visits (
    id                  INTEGER PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vehicle_id          INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    car_type_id         INTEGER REFERENCES car_types(id) ON DELETE SET NULL,
    phone               TEXT NOT NULL DEFAULT '',
    price_cents         INTEGER NOT NULL DEFAULT 0,
    is_free             INTEGER NOT NULL DEFAULT 0,
    note                TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'washing', 'ready', 'collected', 'cancelled')),
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    ready_at            TEXT,
    collected_at        TEXT,
    notified_at         TEXT,
    review_requested_at TEXT
);
CREATE INDEX IF NOT EXISTS visits_by_tenant_created ON visits (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS visits_by_tenant_status ON visits (tenant_id, status);
CREATE INDEX IF NOT EXISTS visits_by_vehicle ON visits (vehicle_id);

CREATE TABLE IF NOT EXISTS visit_services (
    visit_id    INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    price_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (visit_id, service_id)
);

CREATE TABLE IF NOT EXISTS visit_washers (
    visit_id  INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
    washer_id INTEGER NOT NULL REFERENCES washers(id) ON DELETE CASCADE,
    PRIMARY KEY (visit_id, washer_id)
);

-- deleting a car wash cascades through these
CREATE INDEX IF NOT EXISTS car_types_by_tenant ON car_types (tenant_id);
CREATE INDEX IF NOT EXISTS services_by_tenant ON services (tenant_id);
CREATE INDEX IF NOT EXISTS washers_by_tenant ON washers (tenant_id);
CREATE INDEX IF NOT EXISTS prices_by_car_type ON prices (car_type_id);
CREATE INDEX IF NOT EXISTS vehicles_by_car_type ON vehicles (car_type_id);
CREATE INDEX IF NOT EXISTS visits_by_car_type ON visits (car_type_id);
CREATE INDEX IF NOT EXISTS visit_services_by_service ON visit_services (service_id);
CREATE INDEX IF NOT EXISTS visit_washers_by_washer ON visit_washers (washer_id);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY,
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code         TEXT NOT NULL,                    -- in the customer's link to their booking
    slot_at      TEXT NOT NULL,
    minutes      INTEGER NOT NULL DEFAULT 30,      -- copied from its services when booked
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL,
    plate        TEXT NOT NULL DEFAULT '',
    car_type_id  INTEGER REFERENCES car_types(id) ON DELETE SET NULL,
    price_cents  INTEGER NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new', 'confirmed', 'arrived', 'cancelled')),
    visit_id     INTEGER REFERENCES visits(id) ON DELETE SET NULL,
    cancelled_by TEXT NOT NULL DEFAULT '',         -- owner | customer
    created_at   TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS bookings_by_tenant_slot ON bookings (tenant_id, slot_at);
CREATE INDEX IF NOT EXISTS bookings_by_car_type ON bookings (car_type_id);
CREATE INDEX IF NOT EXISTS bookings_by_visit ON bookings (visit_id);

CREATE TABLE IF NOT EXISTS booking_services (
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    PRIMARY KEY (booking_id, service_id)
);
CREATE INDEX IF NOT EXISTS booking_services_by_service ON booking_services (service_id);

CREATE TABLE IF NOT EXISTS rate_hits (
    key TEXT NOT NULL,
    at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_hits_by_key ON rate_hits (key, at);
"""

# columns added after the first release: CREATE TABLE has them, older databases get an ALTER
ADDED_COLUMNS = [
    ("tenants", "theme", "TEXT NOT NULL DEFAULT ''"),
    ("tenants", "session_key", "TEXT NOT NULL DEFAULT ''"),
    ("tenants", "slug", "TEXT NOT NULL DEFAULT ''"),
    ("tenants", "address", "TEXT NOT NULL DEFAULT ''"),
    ("tenants", "hours", "TEXT NOT NULL DEFAULT ''"),
    ("tenants", "slot_capacity", "INTEGER NOT NULL DEFAULT 2"),
    ("tenants", "booking_on", "INTEGER NOT NULL DEFAULT 1"),
    ("tenants", "msg_booking", "TEXT NOT NULL DEFAULT ''"),
    ("tenants", "msg_cancel", "TEXT NOT NULL DEFAULT ''"),
    ("services", "minutes", "INTEGER NOT NULL DEFAULT 30"),
    ("bookings", "minutes", "INTEGER NOT NULL DEFAULT 30"),
    ("bookings", "cancelled_by", "TEXT NOT NULL DEFAULT ''"),
]
# columns no longer used: older databases drop them
DROPPED_COLUMNS = [
    ("washers", "pay_type"),
    ("washers", "pay_value"),
    ("visit_washers", "pay_type"),
    ("visit_washers", "pay_value"),
]
# indexes on added columns: created after the ALTERs
LATE_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS tenants_by_slug ON tenants (slug) WHERE slug != '';
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path: str):
    conn = connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for table, column, decl in ADDED_COLUMNS:
        if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    for table, column in DROPPED_COLUMNS:
        if column in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.executescript(LATE_SCHEMA)
    conn.commit()
    conn.close()


def init_app(app):
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)
