"""Serveur HTTP local : UI + API JSON. Dépendances : stdlib uniquement."""

from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    engine = None  # injecté par make_server
    server_version = "PZduel/0.1"

    # -- utilitaires -----------------------------------------------------
    def log_message(self, fmt, *args):  # silence : l'UI est la source de vérité
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(UI_DIR, rel))
        if not path.startswith(UI_DIR + os.sep) or not os.path.isfile(path):
            self._json(404, {"error": "not found", "path": rel})
            return
        ext = os.path.splitext(path)[1]
        ctype = TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(200, self.engine.snapshot())
            return
        if path == "/api/presets":
            self._json(200, {"presets": self.engine.presets()})
            return
        if path in ("/", ""):
            self._static("index.html")
            return
        self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/action":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "JSON invalide"})
            return
        action = payload.get("action", "")
        ok, msg = self.engine.action(action, payload)
        self._json(200 if ok else 409, {"ok": ok, "message": msg, "state": self.engine.snapshot()})


def make_server(engine, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    return ThreadingHTTPServer((host, port), handler)
