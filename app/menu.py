"""Reads a car wash's printed price list (photos or a PDF) with a vision model on Qubrid."""
import base64
import io
import json
import re
import urllib.error
import urllib.request

import pypdfium2
from flask import current_app
from PIL import Image, ImageOps

API_URL = "https://platform.qubrid.com/v1/chat/completions"
MAX_PICTURES = 10  # photos and PDF pages together
MAX_UPLOAD = 40 * 1024 * 1024
MAX_SERVICES = 60
SIDE = 1600  # longest side of a picture sent to the model, px
PROMPT = """You read car wash price lists from photos. Reply with JSON only:
{"services": [{"name": "Wash & Vac", "minutes": 45, "prices": [100, 130, 130, 170]}]}

- One entry per service or package, in the order of the list. "name": as written, short (up to 40 characters), \
without the price. Skip what isn't a service: opening hours, phone numbers, slogans.
- "prices": one number per car type, in the order of the car types given, in rand, without "R".
- Fit the list's vehicle groups to the given car types: "Small / Medium / Large", "Cars / SUVs / Bakkies", \
"Light / Heavy" and so on go to the closest car type. A service with one price gets it for every car type. \
null only where the list says the service isn't done for that car type.
- A range or "from R80": the lowest price.
- "minutes": how long the service takes, from the list if it says, otherwise a realistic guess for a hand car wash \
(a quick wash about 30, a full valet about 120), a multiple of 5.
- No price list on the pictures: {"services": []}."""


class Unreadable(Exception):
    """Says what went wrong in words for the owner."""


def enabled() -> bool:
    return bool(current_app.config.get("QUBRID_API_KEY"))


def pictures(files) -> list[bytes]:
    """Uploaded photos and PDFs -> JPEGs small enough for the model."""
    out = []
    for f in files:
        data = f.read()
        if data.startswith(b"%PDF"):
            out += _pdf_pages(data, MAX_PICTURES - len(out))
        else:
            out.append(_jpeg(_open(data)))
        if len(out) > MAX_PICTURES:
            break
    if len(out) > MAX_PICTURES:
        raise Unreadable(f"Up to {MAX_PICTURES} photos or PDF pages at a time.")
    if not out:
        raise Unreadable("Choose a photo or a PDF of your price list.")
    return out


def _open(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (SIDE, SIDE))
        img.load()
        return ImageOps.exif_transpose(img)
    except Exception:
        raise Unreadable("Couldn't open one of the files. Use photos (JPG, PNG) or a PDF.")


def _pdf_pages(data: bytes, room: int) -> list[bytes]:
    try:
        pdf = pypdfium2.PdfDocument(data)
    except Exception:
        raise Unreadable("Couldn't open the PDF.")
    try:
        if len(pdf) > room:
            raise Unreadable(f"Up to {MAX_PICTURES} photos or PDF pages at a time.")
        return [_jpeg(page.render(scale=SIDE / max(page.get_size())).to_pil()) for page in pdf]
    finally:
        pdf.close()


def _jpeg(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((SIDE, SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def read(images: list[bytes], car_types: list[str]) -> list[dict]:
    """[{name, minutes, prices: [cents or None per car type]}]"""
    content = [{"type": "text", "text": "Car types, in this order: " + json.dumps(car_types)}]
    content += [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(i).decode()}}
                for i in images]
    reply = ask([{"role": "system", "content": PROMPT}, {"role": "user", "content": content}])
    rows = clean(reply, len(car_types))
    if not rows:
        raise Unreadable("Couldn't find services and prices on these pictures. Try a sharper photo.")
    return rows


def ask(messages: list) -> str:
    body = json.dumps({"model": current_app.config["QUBRID_MODEL"], "messages": messages,
                       "temperature": 0, "max_tokens": 6000}).encode()
    req = urllib.request.Request(API_URL, body, {
        "Authorization": "Bearer " + current_app.config["QUBRID_API_KEY"],
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            return json.load(resp)["choices"][0]["message"]["content"] or ""
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as e:
        detail = e.read()[:500] if isinstance(e, urllib.error.HTTPError) else ""
        current_app.logger.warning("price list reader: %r %s", e, detail)
        raise Unreadable("Couldn't read the price list right now. Try again in a minute or fill it in by hand.")


def clean(reply: str, n_types: int) -> list[dict]:
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        services = json.loads(m.group(0))["services"] if m else []
    except (ValueError, KeyError, TypeError):
        services = []
    rows, seen = [], set()
    for s in services if isinstance(services, list) else []:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str):
            continue
        name = " ".join(s["name"].split())[:40]
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        prices = s.get("prices") if isinstance(s.get("prices"), list) else []
        prices = [_cents(p) for p in prices[:n_types]]
        rows.append({"name": name, "minutes": _minutes(s.get("minutes")),
                     "prices": prices + [None] * (n_types - len(prices))})
    return rows[:MAX_SERVICES]


def _cents(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = re.sub(r"[^\d.]", "", value.replace(",", ""))
    try:
        cents = round(float(value) * 100)
    except (TypeError, ValueError, OverflowError):
        return None
    return cents if 0 < cents <= 999_999_999 else None


def _minutes(value) -> int:
    try:
        minutes = int(round(float(value) / 5) * 5)
    except (TypeError, ValueError, OverflowError):
        return 30
    return minutes if 5 <= minutes <= 600 else 30
