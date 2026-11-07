"""Serveur HTTP local : UI + API JSON. Dépendances : stdlib uniquement."""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from . import paths
from .guest_launcher import LauncherRefused
from .http_kit import (
    CONNECTIONS_MAX,
    DRAIN_S,
    LAUNCHER_NAME,
    LAUNCHER_TYPE,
    REQUEST_READ_S,
    BoundedHTTPServer,
    HeadReader,
    claim_address,  # noqa: F401 : `main` le lit ici, comme `LOCAL_NAMES` et `SERVER_TOKEN`
    drain,
)

UI_DIR = str(paths.resource_root() / "ui")

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
# Début de l'en-tête `Server` : `main` y reconnaît un service déjà actif.
SERVER_TOKEN = "PZduel/"
JSON_TYPE = "application/json"
# Tout champ qui nomme une chose s'ajoute ici : `test_api_security` balaie le service.
IDENTIFIER_FIELDS = ("action", "preset", "mode", "side")
IDENTIFIER_MAX = 64
IDENTIFIER = re.compile(rf"[A-Za-z0-9_-]{{1,{IDENTIFIER_MAX}}}")
# Une action tient en quelques centaines d'octets : ce plafond borne la lecture, rien de plus.
BODY_MAX = 1 << 20
# Ce qu'un client obtient pour livrer tout son corps : sur la boucle locale, c'est très large.
BODY_READ_S = 5.0
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
LOG = logging.getLogger(__name__)


def is_identifier(value: object) -> bool:
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


class Handler(BaseHTTPRequestHandler):
    engine = None  # injecté par make_server
    # `timeout`, injecté aussi, se pose sur le socket dès `setup`, avant toute lecture.
    server_version = f"{SERVER_TOKEN}0.1"
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
        drain(self.connection, DRAIN_S)

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

    def _send(self, code: int, body: bytes, ctype: str, disposition: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if disposition is not None:
            self.send_header("Content-Disposition", disposition)
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
        if length is None:  # le corps annoncé arrive quand même : le laisser couperait le refus
            return self._hang_up()
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
        if path == "/api/setup":
            self._json(200, self.engine.setup_state())
            return
        if path == "/api/invite/launcher":
            self._get_launcher()
            return
        if path in ("/", ""):
            self._static("index.html")
            return
        self._static(path)

    def _get_launcher(self) -> None:
        try:
            launcher = self.engine.guest_launcher()
        except LauncherRefused as exc:
            self._fail(409, str(exc))
            return
        disposition = f'attachment; filename="{LAUNCHER_NAME}"'
        self._send(200, launcher, LAUNCHER_TYPE, disposition)

    def do_POST(self):
        path = self._path()
        if path is None:
            return
        route = self.POST_ROUTES.get(path)
        if route is None:
            self._json(404, {"error": "not found"})
            return
        # Rien ne se lit pour un inconnu. Fermer sur des octets non lus couperait la réponse :
        # `_hang_up` les jette en temps borné.
        if self._refused():
            self._hang_up()
            return
        payload = self._payload()
        if payload is not None:
            route(self, payload)

    def _payload(self) -> dict | None:
        """Rend l'objet JSON reçu, ses identifiants contrôlés ; sinon répond et rend `None`."""
        raw = self._body()
        if raw is None:
            return None
        if self.headers.get_content_type() != JSON_TYPE:
            self._json(415, {"ok": False, "error": f"Content-Type attendu : {JSON_TYPE}"})
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            self._json(400, {"ok": False, "error": "JSON invalide"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "objet JSON attendu"})
            return None
        field = self._bad_identifier(payload)
        if field is not None:
            error = f"{field} refusé : lettres, chiffres, _ ou -, {IDENTIFIER_MAX} au plus"
            self._json(400, {"ok": False, "error": error})
            return None
        return payload

    def _post_action(self, payload: dict) -> None:
        action = payload.get("action", "")
        self._state_answer(*self.engine.action(action, payload))

    def _state_answer(self, ok: bool, message: str) -> None:
        answer = {"ok": ok, "message": message, "state": self.engine.snapshot()}
        self._json(200 if ok else 409, answer)

    def _post_invite_address(self, payload: dict) -> None:
        self._state_answer(*self.engine.set_invite_address(payload.get("address")))

    def _setup_answer(self, ok: bool, message: str) -> None:
        self._json(200 if ok else 409, {"ok": ok, "message": message})

    def _post_install_mod(self, payload: dict) -> None:
        self._setup_answer(*self.engine.install_mod())

    def _post_pz_dir(self, payload: dict) -> None:
        folder = payload.get("folder")
        # Vide, ce serait le dossier courant du service.
        if not isinstance(folder, str) or not folder:
            self._fail(400, "folder refused: a non-empty text is expected")
            return
        self._setup_answer(*self.engine.set_pz_dir(folder))

    def _post_quit(self, payload: dict) -> None:
        on_quit = self.server.on_quit
        if on_quit is None:
            self._fail(409, "quit unavailable: this server has no stop function")
            return
        # La réponse part d'abord : coupée avant, l'UI croirait à une panne. Un client parti
        # entre-temps n'annule pas la sortie qu'il a demandée.
        try:
            self._json(200, {"ok": True, "message": "PZ Duel is shutting down"})
        finally:
            on_quit()

    # Toute route passe par la même chaîne : `_refused`, lecture bornée, `Content-Type`.
    POST_ROUTES = {
        "/api/action": _post_action,
        "/api/setup/install-mod": _post_install_mod,
        "/api/setup/pz-dir": _post_pz_dir,
        "/api/quit": _post_quit,
        "/api/invite/address": _post_invite_address,
    }


def make_server(engine, host: str = "127.0.0.1", port: int = 0) -> BoundedHTTPServer:
    handler = type("BoundHandler", (Handler,), {"engine": engine, "timeout": REQUEST_READ_S})
    return BoundedHTTPServer((host, port), handler, connections_max=CONNECTIONS_MAX)
