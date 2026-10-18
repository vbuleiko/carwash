import sys
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

FONT = TTFont(sys.argv[1])
OUT = sys.argv[2]
ORANGE, BLACK, WHITE = "#ff5a1f", "#0a0a0a", "#ffffff"
MARK = ('<rect width="64" height="64" rx="14" fill="{o}"/>'
        '<path d="M32 11c7 9.5 13 16.6 13 24a13 13 0 0 1-26 0c0-7.4 6-14.5 13-24z" fill="{b}"/>'
        '<path d="M25.5 36.5l4.5 4.5 9-9" fill="none" stroke="{o}" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/>')


def text_path(text, size, x, baseline, spacing=0.04):
    gs, cmap, hmtx = FONT.getGlyphSet(), FONT.getBestCmap(), FONT["hmtx"]
    scale = size / FONT["head"].unitsPerEm
    pen = SVGPathPen(gs, lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
    for ch in text:
        name = cmap[ord(ch)]
        gs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, x, baseline)))
        x += hmtx[name][0] * scale + spacing * size
    return pen.getCommands(), x - spacing * size


def cap_height():
    return FONT["OS/2"].sCapHeight / FONT["head"].unitsPerEm


def wordmark(size, x, baseline, ink):
    a, x2 = text_path("CARWASH", size, x, baseline)
    b, x3 = text_path("EASY", size, x2 + 0.04 * size, baseline)
    return f'<path d="{a}" fill="{ink}"/><path d="{b}" fill="{ORANGE}"/>', x3


def horizontal(ink, bg=None):
    size = 64 / cap_height() * 0.62
    base = 32 + cap_height() * size / 2
    words, end = wordmark(size, 64 + 18, base, ink)
    w, pad = round(end), 0
    back = ""
    if bg:
        pad = 24
        back = f'<rect x="{-pad}" y="{-pad}" width="{w + 2 * pad}" height="{64 + 2 * pad}" fill="{bg}"/>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad} {-pad} {w + 2 * pad} {64 + 2 * pad}">'
            f'{back}{MARK.format(o=ORANGE, b=BLACK)}{words}</svg>\n')


def square():
    _, end = wordmark(100, 0, 0, WHITE)
    size = 100 * 330 / end
    x = (512 - 330) / 2
    capped = cap_height() * size
    mark_size, gap = 170, 30
    top = (512 - mark_size - gap - capped) / 2
    words, _ = wordmark(size, x, top + mark_size + gap + capped, WHITE)
    k = mark_size / 64
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            f'<rect width="512" height="512" fill="{BLACK}"/>'
            f'<g transform="translate({(512 - mark_size) / 2} {top}) scale({k})">{MARK.format(o=ORANGE, b=BLACK)}</g>'
            f'{words}</svg>\n')


for name, svg in {"logo.svg": horizontal(WHITE), "logo-light.svg": horizontal(BLACK),
                  "logo-square.svg": square()}.items():
    open(f"{OUT}/{name}", "w").write(svg)
