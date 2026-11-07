"""Ce que l'API locale et l'écouteur de partage ont en commun : ni moteur, ni route.

`api`, `share` et `engine` en dépendent, et ce module ne dépend d'aucun d'eux : le domaine n'a
ainsi pas à passer par la couche HTTP. Stdlib uniquement.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import ThreadingHTTPServer

LAUNCHER_NAME = "pz-duel-join.bat"
LAUNCHER_TYPE = "application/octet-stream"
# Délai de socket de toute connexion, et échéance de la ligne de requête et des en-têtes.
REQUEST_READ_S = 5.0
# Bien plus qu'un navigateur n'en ouvre : au-delà, le refus part sans créer de fil.
CONNECTIONS_MAX = 32
# Ce qu'un client encore en train d'émettre obtient pour finir d'écrire puis lire le refus.
DRAIN_S = 1.0
DRAIN_CHUNK = 1 << 16
# Le même sursis pour un refus de connexion, mais sur le fil d'`accept`, donc plus court. Sur la
# boucle locale, une requête en vol arrive en moins d'une milliseconde : la marge couvre les
# arrêts d'un Windows chargé. Quatre fois moins que la limite d'AC40, car `shutdown` l'attend.
REFUSAL_DRAIN_S = 0.5
LOG = logging.getLogger(__name__)


def drain(conn: socket.socket, budget_s: float) -> None:
    """Émet le FIN, puis jette ce qui arrive jusqu'à l'EOF du client ou la fin du budget.

    Attendre l'EOF, pas seulement lire : une requête arrivée après la fermeture recevrait un
    RST, et Windows jette alors la réponse que le client n'a pas encore lue.
    """
    deadline = time.monotonic() + budget_s
    try:
        conn.shutdown(socket.SHUT_WR)
        while (left := deadline - time.monotonic()) > 0:
            conn.settimeout(left)
            if not conn.recv(DRAIN_CHUNK):
                return
    except OSError:  # le client est parti, ou il se tait : la réponse est déjà sortie
        LOG.debug("fin de lecture après refus", exc_info=True)


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


def turned_away(connections_max: int = CONNECTIONS_MAX) -> bytes:
    """Le refus d'une connexion de trop, entier : il part sans `Handler` ni fil."""
    error = f"trop de connexions : {connections_max} au plus"
    body = json.dumps({"ok": False, "error": error}).encode("ascii")
    head = (
        "HTTP/1.0 503 Service Unavailable\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("ascii") + body


def claim_address(sock: socket.socket) -> None:
    """Sous Windows, interdit de partager l'adresse. La constante n'existe que là."""
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)


class BoundedHTTPServer(ThreadingHTTPServer):
    """Un fil par connexion, `connections_max` au plus ; un refus retient `accept` un temps borné.

    `peer_max`, s'il est donné, borne aussi les connexions d'une même adresse source : sans lui,
    un seul pair lent prendrait toutes les places. Le refus est le même 503, sans fil.
    """

    request_queue_size = CONNECTIONS_MAX  # une rafale se refuse, elle ne se perd pas en SYN
    # `HTTPServer` l'active, et `SO_REUSEADDR` laisserait sous Windows lier un port tenu.
    allow_reuse_address = False
    # Posé par `main` : idempotent, il rend la main tout de suite. Sans lui, la sortie rend 409.
    on_quit = None

    def __init__(
        self,
        *args,
        connections_max: int = CONNECTIONS_MAX,
        peer_max: int | None = None,
        **kwargs,
    ) -> None:
        if peer_max is not None and not 1 <= peer_max <= connections_max:
            raise ValueError(f"peer_max must be from 1 to {connections_max}")
        self.connections_max = connections_max
        self.peer_max = peer_max
        self._slots = threading.BoundedSemaphore(connections_max)
        self._peers: dict[str, int] = {}  # adresse source -> connexions ouvertes, jamais 0
        self._peers_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def server_bind(self) -> None:
        claim_address(self.socket)
        super().server_bind()

    def process_request(self, request, client_address) -> None:
        if not self._take_peer(client_address):
            self._turn_away(request)
            self.shutdown_request(request)
            return
        if not self._slots.acquire(blocking=False):
            self._release_peer(client_address)
            self._turn_away(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:  # le fil n'a pas démarré : personne d'autre ne rendrait la place
            self._slots.release()
            self._release_peer(client_address)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
            self._release_peer(client_address)

    def _take_peer(self, client_address) -> bool:
        """Compte la connexion pour son adresse ; faux si celle-ci a déjà `peer_max` places."""
        if self.peer_max is None:
            return True
        peer = client_address[0]
        with self._peers_lock:
            held = self._peers.get(peer, 0)
            if held >= self.peer_max:
                return False
            self._peers[peer] = held + 1
        return True

    def _release_peer(self, client_address) -> None:
        if self.peer_max is None:
            return
        peer = client_address[0]
        with self._peers_lock:
            held = self._peers.pop(peer) - 1
            if held:
                self._peers[peer] = held

    def _turn_away(self, request: socket.socket) -> None:
        """Tourne sur le fil d'`accept` : l'envoi n'attend pas, la vidange s'arrête au budget."""
        try:
            request.setblocking(False)
            request.send(turned_away(self.connections_max))
        except OSError:  # le client est parti : plus personne à qui répondre
            LOG.debug("fin du refus de connexion", exc_info=True)
            return
        drain(request, REFUSAL_DRAIN_S)
