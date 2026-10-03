"""Static files under content-hash URLs, so a phone downloads each file once.

url_for("static", ...) adds ?v=<hash>: the browser keeps that URL for a year without asking again,
and a deploy only reloads the files that really changed. Text goes out brotli-compressed.
"""
import hashlib
import mimetypes
import re
from pathlib import Path

import brotli
from flask import Response, abort, current_app, request
from werkzeug.security import safe_join

YEAR = 365 * 86400
COMPRESS = {".css", ".js", ".svg", ".wasm", ".webmanifest"}
CSS_URL = re.compile(rb"url\(/static/([^)?]+)\)")  # the fonts in style.css get a hash too

mimetypes.add_type("font/woff2", ".woff2")  # slim Python images have no system mime table


class Asset:
    def __init__(self, name: str, body: bytes):
        self.body = body
        self.hash = hashlib.sha256(body).hexdigest()[:12]
        self.mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.compress = Path(name).suffix in COMPRESS
        self._br = None

    def br(self) -> bytes:
        if self._br is None:  # once per worker; the 1 MB disc decoder gets a faster level
            self._br = brotli.compress(self.body, quality=11 if len(self.body) < 300_000 else 9)
        return self._br


class Static:
    def __init__(self, folder: Path):
        self.folder = folder
        self.files: dict[str, Asset] = {}

    def get(self, name: str) -> Asset | None:
        path = safe_join(str(self.folder), name)  # normalised, so "a/../x.css" is one more name for x.css
        if path is None:
            return None
        if path in self.files and not current_app.debug:  # debug: edits show up without a restart
            return self.files[path]
        if not Path(path).is_file():
            return None
        body = Path(path).read_bytes()
        if path.endswith(".css"):
            body = CSS_URL.sub(self._versioned, body)
        self.files[path] = asset = Asset(path, body)
        return asset

    def _versioned(self, m: re.Match) -> bytes:
        asset = self.get(m[1].decode())
        return b"url(/static/" + m[1] + (b"?v=" + asset.hash.encode() if asset else b"") + b")"

    def add_version(self, endpoint: str, values: dict):
        if endpoint == "static" and "v" not in values:
            asset = self.get(values.get("filename", ""))
            if asset:
                values["v"] = asset.hash

    def send(self, filename: str):
        asset = self.get(filename)
        if asset is None:
            abort(404)
        br = asset.compress and request.accept_encodings["br"] > 0
        resp = Response(asset.br() if br else asset.body, mimetype=asset.mimetype)
        if asset.compress:
            resp.vary.add("Accept-Encoding")
        if br:
            resp.content_encoding = "br"
        resp.set_etag(asset.hash + ("-br" if br else ""))
        if request.args.get("v") == asset.hash:
            resp.cache_control.public = True
            resp.cache_control.max_age = YEAR
            resp.cache_control.immutable = True
        else:  # an old or missing hash: check back next time
            resp.cache_control.no_cache = True
        return resp.make_conditional(request)
