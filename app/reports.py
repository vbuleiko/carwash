"""Numbers for the owner: revenue, cars, washer speed and pay."""
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean

from .utils import local_today, minutes_between, to_local, utc_bounds

PERIODS = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("7d", "7 days"),
    ("30d", "30 days"),
    ("month", "Month"),
]
# wash times outside this range are forgotten taps, not real washes
MIN_WASH, MAX_WASH = 3, 300
MIN_CARS_FOR_SPEED = 3


def period_dates(key: str, today: date) -> tuple[date, date]:
    """Local [start, end) dates for a period key."""
    tomorrow = today + timedelta(days=1)
    if key == "yesterday":
        return today - timedelta(days=1), today
    if key == "7d":
        return today - timedelta(days=6), tomorrow
    if key == "30d":
        return today - timedelta(days=29), tomorrow
    if key == "month":
        return today.replace(day=1), tomorrow
    return today, tomorrow


def build(db, tenant, tz, key: str) -> dict:
    start, end = period_dates(key, local_today(tz))
    a, b = utc_bounds(start, end, tz)
    in_period = (
        "SELECT id FROM visits WHERE tenant_id = ? AND status != 'cancelled' "
        "AND created_at >= ? AND created_at < ?"
    )
    args = (tenant["id"], a, b)

    visits = {
        r["id"]: dict(r, services=[], team=[])
        for r in db.execute(
            "SELECT id, car_type_id, price_cents, is_free, created_at, started_at, ready_at "
            f"FROM visits WHERE id IN ({in_period})",
            args,
        )
    }
    for r in db.execute(
        "SELECT vs.visit_id, vs.service_id, vs.price_cents, s.name FROM visit_services vs "
        f"JOIN services s ON s.id = vs.service_id WHERE vs.visit_id IN ({in_period})",
        args,
    ):
        if r["visit_id"] in visits:
            visits[r["visit_id"]]["services"].append(dict(r))
    for r in db.execute(
        "SELECT vw.visit_id, vw.washer_id, vw.pay_type, vw.pay_value FROM visit_washers vw "
        f"WHERE vw.visit_id IN ({in_period})",
        args,
    ):
        if r["visit_id"] in visits:
            visits[r["visit_id"]]["team"].append(dict(r))

    # wash time per visit, and the typical time for the same job (services + car type + team size)
    by_job = defaultdict(list)
    for v in visits.values():
        m = minutes_between(v["started_at"], v["ready_at"])
        v["minutes"] = m if m is not None and MIN_WASH <= m <= MAX_WASH else None
        v["job"] = (tuple(sorted(s["service_id"] for s in v["services"])), v["car_type_id"], len(v["team"]))
        if v["minutes"] is not None:
            by_job[v["job"]].append(v["minutes"])
    typical = {job: mean(ms) for job, ms in by_job.items()}

    cars = len(visits)
    revenue = sum(v["price_cents"] for v in visits.values())
    free = sum(1 for v in visits.values() if v["is_free"])
    timed = [v["minutes"] for v in visits.values() if v["minutes"] is not None]

    return {
        "key": key,
        "start": start,
        "end": end - timedelta(days=1),
        "cars": cars,
        "revenue": revenue,
        "free": free,
        "avg_ticket": round(revenue / (cars - free) / 100) * 100 if cars - free else 0,
        "avg_minutes": mean(timed) if timed else None,
        "staff": _staff(db, tenant, visits, typical),
        "services": _services(visits),
        "hours": _hours(visits, tz),
        "days": _days(visits, tz, start, end) if (end - start).days > 1 else [],
    }


def _staff(db, tenant, visits, typical):
    rows = db.execute(
        "SELECT id, name, active FROM washers WHERE tenant_id = ? ORDER BY name", (tenant["id"],)
    ).fetchall()
    stats = {
        r["id"]: {"name": r["name"], "active": r["active"], "cars": 0, "earned": 0.0, "minutes": [], "ratios": []}
        for r in rows
    }
    for v in visits.values():
        team = len(v["team"])
        for member in v["team"]:
            s = stats[member["washer_id"]]
            s["cars"] += 1
            if member["pay_type"] == "percent":
                s["earned"] += v["price_cents"] * member["pay_value"] / 100 / team
            else:
                s["earned"] += member["pay_value"] / team
            if v["minutes"] is not None:
                s["minutes"].append(v["minutes"])
                s["ratios"].append(v["minutes"] / typical[v["job"]])

    result = []
    for s in stats.values():
        if not s["cars"] and not s["active"]:
            continue
        enough = len(s["ratios"]) >= MIN_CARS_FOR_SPEED
        result.append({
            "name": s["name"],
            "cars": s["cars"],
            "earned": round(s["earned"]),
            "avg_minutes": mean(s["minutes"]) if s["minutes"] else None,
            # negative = faster than the typical time for the same jobs
            "speed": round((mean(s["ratios"]) - 1) * 100) if enough else None,
        })
    result.sort(key=lambda s: (-s["cars"], s["name"]))
    return result


def _services(visits):
    stats = defaultdict(lambda: {"count": 0, "revenue": 0.0})
    for v in visits.values():
        list_total = sum(s["price_cents"] for s in v["services"])
        for s in v["services"]:
            share = s["price_cents"] / list_total if list_total else 1 / len(v["services"])
            item = stats[s["name"]]
            item["count"] += 1
            item["revenue"] += v["price_cents"] * share
    rows = [{"name": k, "count": x["count"], "revenue": round(x["revenue"])} for k, x in stats.items()]
    return sorted(rows, key=lambda r: -r["count"])


def _hours(visits, tz):
    counts = defaultdict(int)
    for v in visits.values():
        counts[to_local(v["created_at"], tz).hour] += 1
    if not counts:
        return []
    top = max(counts.values())
    return [
        {"hour": h, "count": counts[h], "pct": round(counts[h] * 100 / top)}
        for h in range(min(counts), max(counts) + 1)
    ]


def _days(visits, tz, start, end):
    per_day = defaultdict(lambda: {"cars": 0, "revenue": 0})
    for v in visits.values():
        d = to_local(v["created_at"], tz).date()
        per_day[d]["cars"] += 1
        per_day[d]["revenue"] += v["price_cents"]
    top = max((x["revenue"] for x in per_day.values()), default=0) or 1
    days = []
    d = start
    while d < end:
        x = per_day[d]
        days.append({"date": d, "cars": x["cars"], "revenue": x["revenue"], "pct": round(x["revenue"] * 100 / top)})
        d += timedelta(days=1)
    return list(reversed(days))
