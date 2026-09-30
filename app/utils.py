"""Small helpers: time, money, phone numbers, plates and WhatsApp links."""
import re
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

TS_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_TZ = "Africa/Johannesburg"
TIMEZONES = [
    "Africa/Johannesburg",
    "Africa/Lagos",
    "Africa/Nairobi",
    "Africa/Cairo",
    "Africa/Casablanca",
    "Europe/London",
    "UTC",
]


# --- time -------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ts(dt: datetime | None = None) -> str:
    """UTC timestamp string as stored in the database."""
    return (dt or now_utc()).astimezone(timezone.utc).strftime(TS_FMT)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, TS_FMT).replace(tzinfo=timezone.utc)


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def to_local(value: str | None, tz: ZoneInfo) -> datetime | None:
    dt = parse_ts(value)
    return dt.astimezone(tz) if dt else None


def local_today(tz: ZoneInfo) -> date:
    return now_utc().astimezone(tz).date()


def utc_bounds(start: date, end_exclusive: date, tz: ZoneInfo) -> tuple[str, str]:
    """Local calendar days [start, end) -> UTC timestamp strings."""
    a = datetime.combine(start, time.min, tzinfo=tz)
    b = datetime.combine(end_exclusive, time.min, tzinfo=tz)
    return ts(a), ts(b)


def minutes_between(a: str | None, b: str | None) -> float | None:
    da, db = parse_ts(a), parse_ts(b)
    if not da or not db:
        return None
    return (db - da).total_seconds() / 60


def fmt_minutes(value: float | None) -> str:
    if value is None:
        return "—"
    m = int(round(value))
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


def iso_z(value: str | None) -> str:
    """Timestamp for JavaScript (`data-since`)."""
    return value.replace(" ", "T") + "Z" if value else ""


# --- money ------------------------------------------------------------------

def money(cents) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    rands, c = divmod(abs(cents), 100)
    text = f"{rands:,}"
    if c:
        text += f".{c:02d}"
    return f"{sign}R{text}"


def money_input(cents) -> str:
    """Value for an <input>: 120 or 120.50"""
    cents = int(cents or 0)
    rands, c = divmod(cents, 100)
    return f"{rands}.{c:02d}" if c else str(rands)


def parse_money(value: str | None) -> int | None:
    """'120', '120.50', 'R 1 200,50' -> cents. None if empty or invalid."""
    text = re.sub(r"[Rr\s]", "", value or "")
    if not text:
        return None
    if re.fullmatch(r"\d+,\d{1,2}", text):
        text = text.replace(",", ".")
    text = text.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        return None
    rands, _, c = text.partition(".")
    return int(rands) * 100 + int((c + "00")[:2])


# --- phones, plates, WhatsApp -----------------------------------------------

def normalize_phone(raw: str | None, country_code: str = "27") -> str:
    """'082 123 4567' -> '27821234567' (international digits, no '+')."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    if digits.startswith("0"):
        return country_code + digits[1:]
    if digits.startswith(country_code) and len(digits) >= len(country_code) + 8:
        return digits
    return country_code + digits


def phone_is_valid(digits: str) -> bool:
    return 8 <= len(digits) <= 15


def format_phone(digits: str | None) -> str:
    if not digits:
        return ""
    if digits.startswith("27") and len(digits) == 11:
        return f"+27 {digits[2:4]} {digits[4:7]} {digits[7:]}"
    return "+" + digits


def clean_plate(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").upper()).strip()[:16]


def plate_key(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def fill_template(template: str, values: dict) -> str:
    """Replace {placeholders}; unknown ones are left untouched."""
    return re.sub(
        r"\{(\w+)\}",
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        template or "",
    )


def wa_link(phone: str, text: str = "") -> str:
    link = f"https://wa.me/{phone}"
    return f"{link}?text={quote(text)}" if text else link


def days_ago(n: int) -> datetime:
    return now_utc() - timedelta(days=n)
