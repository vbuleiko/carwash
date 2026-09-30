#!/usr/bin/env bash
# downloads the self-hosted fonts the theme previews use into app/static/fonts
set -euo pipefail
D="$(cd "$(dirname "$0")/../.." && pwd)/app/static/fonts"
mkdir -p "$D" && cd "$D"
J=https://cdn.jsdelivr.net/npm
curl -sSfo inter.woff2 $J/@fontsource-variable/inter@5/files/inter-latin-wght-normal.woff2
curl -sSfo space-grotesk.woff2 $J/@fontsource-variable/space-grotesk@5/files/space-grotesk-latin-wght-normal.woff2
curl -sSfo archivo.woff2 $J/@fontsource-variable/archivo@5.3.0/files/archivo-latin-standard-normal.woff2
for w in 400 500 600 700; do curl -sSfo barlow-$w.woff2 $J/@fontsource/barlow@5/files/barlow-latin-$w-normal.woff2; done
for w in 500 600 700; do curl -sSfo barlow-condensed-$w.woff2 $J/@fontsource/barlow-condensed@5/files/barlow-condensed-latin-$w-normal.woff2; done
