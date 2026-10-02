# Licence disc decoder for phones without a built-in barcode scanner

Android Chrome reads PDF417 by itself (`BarcodeDetector`). Safari (iPhone) and desktop browsers don't,
so `app.js` loads `barcode-detector.js` instead — only when the owner taps "Scan licence disc".
`barcode-detector.js` fetches `zxing_reader.wasm` from this folder (no CDN).

Versions: barcode-detector 3.2.2, zxing-wasm 3.1.3. Licences in `LICENSES.txt`.

Rebuild:

```bash
npm install barcode-detector@3.2.2 esbuild
cat > entry.js <<'JS'
import { BarcodeDetector, setZXingModuleOverrides } from "barcode-detector/ponyfill";

setZXingModuleOverrides({ locateFile: (path) => new URL(path, import.meta.url).href });

export { BarcodeDetector };
JS
npx esbuild entry.js --bundle --format=esm --minify --legal-comments=eof --outfile=barcode-detector.js
cp node_modules/zxing-wasm/dist/reader/zxing_reader.wasm .
```
