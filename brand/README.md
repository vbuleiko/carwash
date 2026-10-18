# Logo

Same mark as the favicon (`app/static/icon.svg`) + "CARWASHEASY" in Barlow Condensed 700 (the Carbon theme font), letters converted to paths so the SVGs need no font.

| File | Use |
|---|---|
| `logo.svg`, `logo.png` | Dark backgrounds (site, social covers) |
| `logo-light.svg`, `logo-light.png` | White backgrounds (documents, invoices, email) |
| `logo-square.svg`, `logo-square.png` | Profile picture (WhatsApp Business, Facebook); fits the circle crop |

Colours: orange `#ff5a1f`, black `#0a0a0a`, white `#ffffff`.

Rebuild the SVGs (needs `pip install fonttools brotli`):

```bash
python brand/make.py app/static/fonts/barlow-condensed-700.woff2 brand
```

PNGs are the SVGs rendered in Chromium: `logo*.png` 1600px wide, `logo-square.png` 1024px, transparent outside the artwork.
