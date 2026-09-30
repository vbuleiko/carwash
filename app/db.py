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
    active    INTEGER NOT NULL DEFAULT 1
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
    pay_type   TEXT NOT NULL DEFAULT 'percent' CHECK (pay_type IN ('percent', 'fixed')),
    pay_value  INTEGER NOT NULL DEFAULT 0,         -- percent, or cents per car
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

-- pay rule is copied at assignment time so later rate changes don't rewrite history
CREATE TABLE IF NOT EXISTS visit_washers (
    visit_id  INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
    washer_id INTEGER NOT NULL REFERENCES washers(id) ON DELETE CASCADE,
    pay_type  TEXT NOT NULL,
    pay_value INTEGER NOT NULL,
    PRIMARY KEY (visit_id, washer_id)
);
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
    conn.commit()
    conn.close()


def init_app(app):
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)
