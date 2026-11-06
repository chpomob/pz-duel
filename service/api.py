"""Serveur HTTP local : UI + API JSON. Dépendances : stdlib uniquement."""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import socket
import threading
import time
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
# Ce qu'un client obtient pour livrer tout son corps : sur la boucle locale, c'est très large.
BODY_READ_S = 5.0
# Délai de socket de toute connexion, et échéance de la ligne de requête et des en-têtes.
REQUEST_READ_S = 5.0
# Bien plus qu'un navigateur n'en ouvre : au-delà, le refus part sans créer de fil.
CONNECTIONS_MAX = 32
# Ni `int` ni `str.isdigit` : ils admettent `+5`, `1_0` ou `²`, qu'aucun client honnête n'envoie.
DIGITS = re.compile(r"[0-9]+")
# Les refus du parseur de `http.server`. Son message cite la requête : il ne sort jamais.
PROTOCOL_REFUSALS = {
    400: "requête malformée",
    414: "ligne de requête trop longue",
    431: "en-têtes trop longs ou trop nombreux",
    501: "méthode non prise en charge",
    505: "version HTTP non prise en charge",
}
PROTOCOL_REFUSED = "requête refusée"
# Ce qu'un client encore en train d'émettre obtient pour finir d'écrire puis lire le refus.
DRAIN_S = 1.0
DRAIN_CHUNK = 1 << 16
LOG = logging.getLogger(__name__)


def is_identifier(value: object) -> bool:
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


class HeadReader(socket.SocketIO):
    """Lit sous une échéance : un délai par `recv` se renouvellerait à chaque octet."""

    def __init__(self, connection: socket.socket) -> None:
        super().__init__(connection, "rb")
        self.connection = connection
        self.deadline: float | None = None

    def readinto(self, buffer) -> int | None:
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self.connection.settimeout(left)
        return super().readinto(buffer)


class Handler(BaseHTTPRequestHandler):
    engine = None  # injecté par make_server
    # `timeout`, injecté aussi, se pose sur le socket dès `setup`, avant toute lecture.
    server_version = "PZduel/0.1"
    answered = False  # vrai dès que l'en-tête de réponse part : plus rien ne peut le remplacer

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.head_reader = HeadReader(self.connection)
        self.rfile = io.BufferedReader(self.head_reader)

    # -- utilitaires -----------------------------------------------------
    def log_message(self, fmt, *args):  # silence : l'UI est la source de vérité
        return

    def version_string(self) -> str:
        return self.server_version  # sans le `Python/x.y` de la bibliothèque

    def parse_request(self) -> bool:
        """Ferme ce que le parseur laisse passer : la ligne vide, sans réponse, et HTTP/0.9."""
        # Deux mots, c'est HTTP/0.9 : sans en-têtes à venir, où certaines versions de la
        # bibliothèque les attendraient sans fin. Le découpage est le sien, en latin-1.
        if len(str(self.raw_requestline, "iso-8859-1").split()) == 2:
            self.command = None  # `_send` et le journal lisent ce que le parseur aurait posé
            self.requestline = ""
            self.send_error(400)
            return False
        if super().parse_request() and self.request_version != "HTTP/0.9":
            self.head_reader.deadline = None
            self.connection.settimeout(self.timeout)
            return True
        if not self.answered:
            self.send_error(400)
        return False

    def send_error(self, code, message=None, explain=None) -> None:
        """Remplace la page HTML du parseur par le refus JSON du service."""
        # En HTTP/0.9, la bibliothèque tairait le statut et les en-têtes.
        self.request_version = ""
        self.close_connection = True
        self._fail(code, PROTOCOL_REFUSALS.get(code, PROTOCOL_REFUSED))
        self._drain()

    def _hang_up(self) -> None:
        """Clôt une connexion dont le corps n'a pas été lu en entier."""
        self.close_connection = True
        self._drain()

    def _drain(self) -> None:
        """Fermer sur des octets non lus couperait la réponse : ils se jettent, en temps borné."""
        deadline = time.monotonic() + DRAIN_S
        try:
            self.connection.shutdown(socket.SHUT_WR)
            while (left := deadline - time.monotonic()) > 0:
                self.connection.settimeout(left)
                if not self.connection.recv(DRAIN_CHUNK):
                    return
        except OSError:  # le client est parti, ou il se tait : la réponse est déjà sortie
            LOG.debug("fin de lecture après refus", exc_info=True)

    def handle_one_request(self):
        """Dernière barrière : l'imprévu devient une réponse JSON, sa trace reste au log.

        L'échéance dépassée, `TimeoutError` ferme la connexion dans la bibliothèque.
        """
        self.head_reader.deadline = time.monotonic() + self.timeout
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
        if self.command == "HEAD":  # le corps est annoncé, jamais envoyé
            return
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
        try:
            raw = self._read_body(length)
        except TimeoutError:
            self._fail(408, f"corps non reçu en {BODY_READ_S} s")
            return self._hang_up()
        if len(raw) != length:
            return self._fail(400, "corps tronqué")
        return raw

    def _read_body(self, length: int) -> bytes:
        """Lit le corps avant l'échéance ; rend moins d'octets si le client ferme.

        L'échéance vaut pour tout le corps : un délai par `recv` se renouvellerait à chaque
        octet d'un client au goutte-à-goutte.
        """
        deadline = time.monotonic() + BODY_READ_S
        raw = bytearray()
        try:
            while len(raw) < length:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(length - len(raw))
                if not chunk:
                    break
                raw += chunk
        finally:
            self.connection.settimeout(self.timeout)
        return bytes(raw)

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
        # Rien ne se lit pour un inconnu. Fermer sur des octets non lus couperait la réponse :
        # `_hang_up` les jette en temps borné.
        if self._refused():
            self._hang_up()
            return
        raw = self._body()
        if raw is None:
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


def turned_away() -> bytes:
    """Le refus d'une connexion de trop, entier : il part sans `Handler` ni fil."""
    error = f"trop de connexions : {CONNECTIONS_MAX} au plus"
    body = json.dumps({"ok": False, "error": error}).encode("ascii")
    head = (
        "HTTP/1.0 503 Service Unavailable\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("ascii") + body


class BoundedHTTPServer(ThreadingHTTPServer):
    """Un fil par connexion, `CONNECTIONS_MAX` au plus ; le refus ne bloque jamais `accept`."""

    request_queue_size = CONNECTIONS_MAX  # une rafale se refuse, elle ne se perd pas en SYN

    def __init__(self, *args, **kwargs) -> None:
        self._slots = threading.BoundedSemaphore(CONNECTIONS_MAX)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self._turn_away(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:  # le fil n'a pas démarré : personne d'autre ne rendrait la place
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _turn_away(self, request: socket.socket) -> None:
        """Tourne sur le fil d'`accept`, sans attente : un refus tient au tampon du socket."""
        try:
            request.setblocking(False)
            request.send(turned_away())
            request.shutdown(socket.SHUT_WR)
            request.recv(DRAIN_CHUNK)  # fermer sur des octets non lus couperait le refus
        except OSError:  # rien à lire encore, ou le client est parti
            LOG.debug("fin du refus de connexion", exc_info=True)


def make_server(engine, host: str = "127.0.0.1", port: int = 0) -> BoundedHTTPServer:
    handler = type("BoundHandler", (Handler,), {"engine": engine, "timeout": REQUEST_READ_S})
    return BoundedHTTPServer((host, port), handler)
