"""Five dark looks. The owner picks one for the app, a visitor picks one on the landing page."""
from flask import current_app, g, request

THEMES = {
    "carbon": {"name": "Carbon", "about": "Motorsport. Signal orange on carbon black.",
               "bg": "#0a0a0a", "card": "#1c1c1c", "accent": "#ff5a1f", "ink": "#0a0a0a"},
    "volt": {"name": "Volt", "about": "Electric. Lime on graphite, rounded and friendly.",
             "bg": "#0b0c0e", "card": "#1d2024", "accent": "#d4ff3a", "ink": "#0b0c0e"},
    "midnight": {"name": "Midnight", "about": "Premium tech. Deep blue with a soft glow.",
                 "bg": "#05070d", "card": "#121a2a", "accent": "#3d8bff", "ink": "#ffffff"},
    "noir": {"name": "Noir", "about": "Detailing studio. Champagne gold, fine lines.",
             "bg": "#0b0a09", "card": "#1a1814", "accent": "#c9a56b", "ink": "#0b0a09"},
    "classic": {"name": "Classic", "about": "The original. Calm teal on slate.",
                "bg": "#0b1016", "card": "#141b23", "accent": "#2dd4bf", "ink": "#04201d"},
}
COOKIE = "theme"


def default() -> str:
    name = current_app.config["THEME"]
    return name if name in THEMES else "carbon"


def picked() -> str:
    """The look a visitor chose on this device, or '' if they never did."""
    name = request.cookies.get(COOKIE, "")
    return name if name in THEMES else ""


def current() -> str:
    tenant = g.get("tenant") or g.get("site")  # the owner's car wash, or the one whose page this is
    if tenant is not None:
        return tenant["theme"] if tenant["theme"] in THEMES else default()
    return picked() or default()


def remember(resp, name: str):
    resp.set_cookie(COOKIE, name, max_age=365 * 86400, httponly=True, samesite="Lax",
                    secure=current_app.config["SESSION_COOKIE_SECURE"])
    return resp
