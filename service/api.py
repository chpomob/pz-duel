"""Serveur HTTP local : UI + API JSON. Dépendances : stdlib uniquement."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
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
# Tout champ qui nomme une chose s'ajoute ici : `test_api_security` balaie le service.
IDENTIFIER_FIELDS = ("action", "preset", "mode", "side")
IDENTIFIER_MAX = 64
IDENTIFIER = re.compile(rf"[A-Za-z0-9_-]{{1,{IDENTIFIER_MAX}}}")
# Une action tient en quelques centaines d'octets : ce plafond borne la lecture, rien de plus.
BODY_MAX = 1 << 20
# Ni `int` ni `str.isdigit` : ils admettent `+5`, `1_0` ou `²`, qu'aucun client honnête n'envoie.
DIGITS = re.compile(r"[0-9]+")
LOG = logging.getLogger(__name__)


def is_identifier(value: object) -> bool:
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


class Handler(BaseHTTPRequestHandler):
    engine = None  # injecté par make_server
    server_version = "PZduel/0.1"
    answered = False  # vrai dès que l'en-tête de réponse part : plus rien ne peut le remplacer

    # -- utilitaires -----------------------------------------------------
    def log_message(self, fmt, *args):  # silence : l'UI est la source de vérité
        return

    def handle_one_request(self):
        """Dernière barrière : l'imprévu devient une réponse JSON, sa trace reste au log."""
        try:
            super().handle_one_request()
        except Exception:
            LOG.exception("requête interrompue par une exception")
            self._last_word()

    def _last_word(self) -> None:
        if self.answered:
            return
        try:
            self._fail(500, "erreur interne")
        except Exception:  # le client est parti, ou la requête n'a jamais été lisible
            LOG.debug("réponse d'erreur impossible", exc_info=True)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.answered = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, code: int, payload) -> None:
        # Tout en ASCII : un surrogate isolé sort échappé, là où UTF-8 lèverait sans répondre.
        self._send(code, json.dumps(payload).encode("ascii"), "application/json; charset=utf-8")

    def _fail(self, code: int, error: str) -> None:
        self._json(code, {"ok": False, "error": error})

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

    def _path(self) -> str | None:
        """Rend le chemin demandé ; sinon répond 400 et rend `None`."""
        try:
            return urlparse(self.path).path
        except ValueError:
            self._json(400, {"ok": False, "error": "chemin invalide"})
            return None

    def _length(self) -> int | None:
        """Rend une longueur de 1 à `BODY_MAX` ; sinon répond 400 ou 413 et rend `None`."""
        lengths = self.headers.get_all("Content-Length", [])
        if "Transfer-Encoding" in self.headers or len(lengths) > 1:
            return self._fail(400, "cadrage du corps refusé")
        sent = lengths[0] if lengths else "0"
        if DIGITS.fullmatch(sent) is None:
            return self._fail(400, "Content-Length invalide")
        digits = sent.lstrip("0")
        if not digits:
            return self._fail(400, "corps vide")
        # L'écriture se mesure d'abord : au-delà de 4300 chiffres, `int` lèverait.
        if len(digits) > len(str(BODY_MAX)) or int(digits) > BODY_MAX:
            return self._fail(413, f"corps trop long : {BODY_MAX} octets au plus")
        return int(digits)

    def _body(self) -> bytes | None:
        """Rend le corps, lu sur une longueur validée ; sinon répond et rend `None`."""
        length = self._length()
        if length is None:
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            return self._fail(400, "corps tronqué")
        return raw

    def _bad_identifier(self, payload: dict) -> str | None:
        """Nomme le premier champ refusé, sans citer sa valeur : elle peut tout contenir."""
        for field in IDENTIFIER_FIELDS:
            if field in payload and not is_identifier(payload[field]):
                return field
        return None

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = self._path()
        if path is None:
            return
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
        path = self._path()
        if path is None:
            return
        if path != "/api/action":
            self._json(404, {"error": "not found"})
            return
        # Un corps de longueur valide se lit même pour un refus : fermer sur des octets non lus
        # couperait la réponse. Une longueur refusée ne se lit pas : le cadrage ne vaut plus rien.
        raw = self._body()
        if raw is None or self._refused():
            return
        if self.headers.get_content_type() != JSON_TYPE:
            self._json(415, {"ok": False, "error": f"Content-Type attendu : {JSON_TYPE}"})
            return
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            self._json(400, {"ok": False, "error": "JSON invalide"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "objet JSON attendu"})
            return
        field = self._bad_identifier(payload)
        if field is not None:
            error = f"{field} refusé : lettres, chiffres, _ ou -, {IDENTIFIER_MAX} au plus"
            self._json(400, {"ok": False, "error": error})
            return
        action = payload.get("action", "")
        ok, msg = self.engine.action(action, payload)
        self._json(200 if ok else 409, {"ok": ok, "message": msg, "state": self.engine.snapshot()})


def make_server(engine, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    return ThreadingHTTPServer((host, port), handler)
