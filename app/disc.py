"""South African licence disc barcode (PDF417) -> plate, make and car type for the new car form.

The barcode is plain text between '%' signs, e.g.
'%MVL1CC09%0154%4024T011%1%40240119RMPX%CA123456%BYJ091C%Hatch back / Luikrug%TOYOTA%COROLLA%White / Wit%...'
— licence number (the plate), vehicle register number, description, make, model, colour, VIN, engine, expiry.
"""
import re

from .utils import clean_plate, plate_key

MAKES = ["Toyota", "Volkswagen", "Ford", "Hyundai", "Nissan", "Suzuki", "Kia", "Renault", "Isuzu", "Mahindra",
         "Haval", "Chery", "BMW", "Mercedes-Benz", "Audi", "Mazda", "Honda", "Opel", "Datsun", "Jeep",
         "Land Rover", "Mitsubishi", "Peugeot", "GWM", "Omoda", "BAIC", "Proton"]
_MAKES_BY_KEY = {m.upper(): m for m in MAKES}

PROVINCES = ("GP", "MP", "NW", "ZN", "EC", "NC", "FS", "L")

# words in the disc's description -> words in the car wash's own car type names
KINDS = [
    (("bakkie", "pick-up", "pickup", "ldv", "light delivery", "double cab", "single cab"), ("bakkie", "pick", "ldv")),
    (("minibus", "bus", "van", "kombi"), ("minibus", "bus", "van", "kombi")),
    (("suv", "sport utility", "station wagon", "stasiewa", "4x4", "off-road"), ("suv", "4x4")),
    (("hatch", "luikrug", "sedan", "saloon", "coupe", "convertible"), ("hatch", "sedan", "car")),
]


def parse(raw: str | None) -> dict | None:
    fields = [f.strip() for f in (raw or "").split("%")]
    at = _description_at(fields)
    if at is None:
        return None
    plate = clean_plate(fields[at - 2])
    if len(plate_key(plate)) < 2:
        return None
    make, model = _nice(fields[at + 1]), _nice(fields[at + 2])
    if model.upper().startswith(make.upper()):
        make = ""
    return {"plate": pretty_plate(plate), "make": " ".join(p for p in (make, model) if p)[:40],
            "description": fields[at]}


def _description_at(fields: list[str]) -> int | None:
    """Description and colour are in English and Afrikaans ('Hatch back / Luikrug', 'White / Wit')."""
    for i in range(3, len(fields) - 3):
        if " / " in fields[i] and " / " in fields[i + 3]:
            return i
    if len(fields) > 11 and fields[1].upper().startswith("MVL"):
        return 8
    return None


def _nice(text: str) -> str:
    """'TOYOTA' -> 'Toyota', 'POLO VIVO' -> 'Polo Vivo'; short codes such as 'GTI' or 'X5' stay as they are."""
    text = " ".join(text.split()).upper()
    if text in _MAKES_BY_KEY:
        return _MAKES_BY_KEY[text]
    return " ".join(w.capitalize() if w.isalpha() and len(w) > 3 else w for w in text.split())


def pretty_plate(plate: str) -> str:
    """'CX99XXGP' -> 'CX 99 XX GP', 'CA123456' -> 'CA 123-456'. A plate with its own spacing stays."""
    key = plate_key(plate)
    if key != plate:
        return plate
    province = next((p for p in PROVINCES if key.endswith(p) and len(key) > len(p) + 2), "")
    runs = re.findall(r"[A-Z]+|[0-9]+", key[:len(key) - len(province)]) + ([province] if province else [])
    return " ".join(f"{r[:3]}-{r[3:]}" if r.isdigit() and len(r) == 6 else r for r in runs)


def car_type(description: str, car_types) -> int | None:
    text = description.lower()
    for disc_words, type_words in KINDS:
        if any(w in text for w in disc_words):
            return next((t["id"] for t in car_types if any(w in t["name"].lower() for w in type_words)), None)
    return None
