"""Écouteur de partage (R7) : seul service hors boucle locale, pour un seul téléchargement.

Il ne vit que pendant le lobby. Tout ce qu'il ne sert pas reçoit 404 ou 405 à corps vide, et rien
de la requête ne sort : son chemin porte le jeton, à la ligne de requête comme dans les logs.
"""

from __future__ import annotations

import hmac
import io
import logging
import socket
import socketserver
import threading
import time
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

from .guest_launcher import LauncherRefused
from .http_kit import (
    DRAIN_S,
    LAUNCHER_NAME,
    LAUNCHER_TYPE,
    REQUEST_READ_S,
    BoundedHTTPServer,
    HeadReader,
    claim_address,
    drain,
)

# Toutes les interfaces : l'invité n'est jamais sur la boucle locale (H1).
ALL_INTERFACES = ""
LOG = logging.getLogger(__name__)


class ShareHandler(BaseHTTPRequestHandler):
    """Ni `Server`, ni `Date`, ni journal : `send_response`, qui les pose, n'est jamais appelé."""

    # Posé sur le socket dès `setup`, avant toute lecture.
    timeout = REQUEST_READ_S
    answered = False  # vrai dès que l'en-tête de réponse part : plus rien ne peut le remplacer

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.head_reader = HeadReader(self.connection)
        self.rfile = io.BufferedReader(self.head_reader)

    def _drain(self) -> None:
        """Fermer sur des octets non lus couperait le refus : ils se jettent, en temps borné."""
        drain(self.connection, DRAIN_S)

    def log_message(self, fmt, *args) -> None:  # la bibliothèque y met la ligne de requête
        return

    def handle_one_request(self) -> None:
        # Un délai par `recv` se renouvellerait à chaque octet : un inconnu tiendrait une place.
        self.head_reader.deadline = time.monotonic() + self.timeout
        super().handle_one_request()

    def parse_request(self) -> bool:
        """Ferme ce que le parseur laisse passer : la ligne vide, sans réponse, et HTTP/0.9."""
        if len(str(self.raw_requestline, "iso-8859-1").split()) == 2:
            self.send_error(HTTPStatus.NOT_FOUND)
            return False
        if super().parse_request() and self.request_version != "HTTP/0.9":
            self.head_reader.deadline = None
            self.connection.settimeout(self.timeout)
            return True
        if not self.answered:
            self.send_error(HTTPStatus.NOT_FOUND)
        return False

    def send_error(self, code, message=None, explain=None) -> None:
        """Le message de la bibliothèque cite la requête : seul le statut sort.

        Une méthode inconnue d'elle arrive ici en 501 ; tout le reste vaut 404.
        """
        # En HTTP/0.9, la bibliothèque tairait le statut et les en-têtes.
        self.request_version = ""
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._answer(HTTPStatus.METHOD_NOT_ALLOWED, headers={"Allow": "GET"})
        else:
            self._answer(HTTPStatus.NOT_FOUND)
        self._drain()

    def _answer(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response_only(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.answered = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # Des octets : sur du texte non ASCII, `compare_digest` lèverait.
        asked = self.path.encode("utf-8", "replace")
        # Une connexion acceptée avant la fermeture survit à son écouteur, pas son jeton.
        if self.server.closed.is_set() or not hmac.compare_digest(asked, self.server.route):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            launcher = self.server.engine.guest_launcher()
        except LauncherRefused:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        except Exception as exc:  # le nom seul : un message pourrait citer le mot de passe
            LOG.error("share: launcher failed: %s", type(exc).__name__)
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        disposition = f'attachment; filename="{LAUNCHER_NAME}"'
        headers = {"Content-Type": LAUNCHER_TYPE, "Content-Disposition": disposition}
        self._answer(HTTPStatus.OK, launcher, headers)


class ShareHTTPServer(BoundedHTTPServer):
    # Une revanche relie le port juste après un téléchargement, dont la connexion reste en
    # TIME_WAIT : sous POSIX, sans `SO_REUSEADDR`, la liaison échouerait. Un port tenu par un
    # écouteur y reste refusé. Sous Windows, cette option laisserait lier un port tenu.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def __init__(self, address: tuple[str, int], engine, route: bytes) -> None:
        self.engine = engine
        self.route = route
        self.closed = threading.Event()
        super().__init__(address, ShareHandler)

    def server_bind(self) -> None:
        """Sans `HTTPServer.server_bind` : sur toutes les interfaces, son `getfqdn` interroge le
        DNS, sous le verrou du moteur."""
        claim_address(self.socket)
        socketserver.TCPServer.server_bind(self)

    def handle_error(self, request, client_address) -> None:
        """La bibliothèque écrit la trace sur `stderr`, absent du binaire sans console."""
        LOG.debug("share: connection lost")


class ShareServer:
    """Sert `/<jeton>/pz-duel-join.bat` dès sa création. Lève `OSError` si le port est pris."""

    def __init__(self, engine, token: str, port: int, poll_s: float) -> None:
        route = f"/{token}/{LAUNCHER_NAME}".encode("ascii")
        self._httpd = ShareHTTPServer((ALL_INTERFACES, port), engine, route)
        self._lock = threading.Lock()
        serve = partial(self._httpd.serve_forever, poll_s)
        threading.Thread(target=serve, name="duel-share", daemon=True).start()

    def close(self) -> None:
        """Idempotent. `shutdown` attend le fil de l'écouteur : jamais à appeler depuis lui.

        Les fils en cours sont des démons : `server_close` ne les attend pas.
        """
        with self._lock:
            if self._httpd.closed.is_set():
                return
            self._httpd.closed.set()
            self._httpd.shutdown()
            self._httpd.server_close()
