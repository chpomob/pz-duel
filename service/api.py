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

# La liaison sur la boucle locale n'arrête pas le navigateur de l'utilisateur : une page web
# peut viser l'API, ou la lire par rebinding DNS. Seuls ces noms désignent ce service.
LOCAL_NAMES = ("127.0.0.1", "localhost")
JSON_TYPE = "application/json"


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

    def _refused(self) -> bool:
        """Répond 403 et rend vrai si la requête ne vient pas de l'interface locale."""
        bound, port = self.server.server_address[:2]
        hosts = {f"{name}:{port}" for name in (bound, *LOCAL_NAMES)}
        sent_hosts = [value.lower() for value in self.headers.get_all("Host", [])]
        sent_origins = [value.lower() for value in self.headers.get_all("Origin", [])]
        if len(sent_hosts) != 1 or sent_hosts[0] not in hosts:
            self._json(403, {"ok": False, "error": "en-tête Host refusé"})
            return True
        if any(origin not in {f"http://{host}" for host in hosts} for origin in sent_origins):
            self._json(403, {"ok": False, "error": "en-tête Origin refusé"})
            return True
        return False

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/") and self._refused():
            return
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
        # Le corps se lit même pour un refus : fermer sur des octets non lus couperait la réponse.
        raw = self.rfile.read(length) if length else b"{}"
        if self._refused():
            return
        if self.headers.get_content_type() != JSON_TYPE:
            self._json(415, {"ok": False, "error": f"Content-Type attendu : {JSON_TYPE}"})
            return
        try:
            payload = json.loads(raw or b"{}")
        except (ValueError, RecursionError):
            self._json(400, {"ok": False, "error": "JSON invalide"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "objet JSON attendu"})
            return
        action = payload.get("action", "")
        ok, msg = self.engine.action(action, payload)
        self._json(200 if ok else 409, {"ok": ok, "message": msg, "state": self.engine.snapshot()})


def make_server(engine, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    return ThreadingHTTPServer((host, port), handler)
