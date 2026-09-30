# Screenshots every theme at 390px: run the app on :5055, then `python design/themes/shoot.py [theme ...]`
import base64, json, pathlib, sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5055"
HERE = pathlib.Path(__file__).parent
OUT = HERE / "shots"
OUT.mkdir(exist_ok=True)
VARIANTS = sys.argv[1:] or ["current", "carbon", "volt", "midnight", "noir"]
VIEW = {"width": 390, "height": 844}


def init_script(v):
    css_file, svg_file = HERE / f"{v}.css", HERE / f"{v}.svg"
    css = css_file.read_text() if css_file.exists() else ""
    logo = ("data:image/svg+xml;base64," + base64.b64encode(svg_file.read_bytes()).decode()) if svg_file.exists() else ""
    return f"""document.addEventListener('DOMContentLoaded', () => {{
      const s = document.createElement('style'); s.textContent = {json.dumps(css)}; document.head.appendChild(s);
      const logo = {json.dumps(logo)}; if (logo) document.querySelectorAll('.logo img').forEach(i => i.src = logo);
    }});"""


def snap(page, name):
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(250)
    sw = page.evaluate("document.documentElement.scrollWidth")
    if sw > VIEW["width"]:
        print(f"  !! horizontal scroll on {name}: {sw}px")
    page.screenshot(path=str(OUT / f"{name}.jpg"), type="jpeg", quality=90)


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    state = HERE / "state.json"
    if not state.exists():
        ctx = browser.new_context(viewport=VIEW)
        page = ctx.new_page()
        page.goto(BASE + "/")
        page.click("text=Try the live demo")
        page.wait_for_url("**/app**")
        ctx.storage_state(path=str(state))
        ctx.close()

    for v in VARIANTS:
        print(v)
        opts = dict(viewport=VIEW, device_scale_factor=2, color_scheme="dark")
        ctx = browser.new_context(**opts)
        ctx.add_init_script(init_script(v))
        page = ctx.new_page()
        page.goto(BASE + "/")
        snap(page, f"{v}-landing")
        ctx.close()

        ctx = browser.new_context(storage_state=str(state), **opts)
        ctx.add_init_script(init_script(v))
        page = ctx.new_page()
        page.goto(BASE + "/app/")
        snap(page, f"{v}-board")
        page.goto(BASE + "/app/new")
        page.fill("#plate", "CA 482-117")
        page.fill("#make", "Toyota Corolla")
        page.fill("#phone", "082 123 4567")
        page.locator(".chip span", has_text="SUV").first.click()
        page.locator("input[name=service_id] + span").first.click()
        page.locator("input[name=service_id] + span").nth(2).click()
        page.evaluate("document.activeElement.blur()")
        snap(page, f"{v}-new")
        page.goto(BASE + "/app/reports?p=7d")
        snap(page, f"{v}-reports")
        ctx.close()
    browser.close()
