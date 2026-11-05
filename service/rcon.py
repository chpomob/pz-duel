"""Client RCON en stdlib (protocole Source), limité au poste local : `save` et `quit`.

Ce module ne journalise rien, et aucun de ses messages ne cite le mot de passe.
"""

from __future__ import annotations

import itertools
import socket
import struct
from types import TracebackType

LOOPBACK = "127.0.0.1"
SIZE_FIELD = struct.Struct("<i")
HEADER = struct.Struct("<ii")
TERMINATOR = b"\0\0"
MIN_PACKET_SIZE = HEADER.size + len(TERMINATOR)
MAX_PACKET_SIZE = 4096
TYPE_RESPONSE = 0
TYPE_EXEC = 2
TYPE_AUTH_RESPONSE = 2
TYPE_AUTH = 3
SAVE = "save"
QUIT = "quit"


class RconError(Exception):
    """Échec RCON : le serveur est injoignable, muet, ou refuse l'authentification."""


class RconClient:
    def __init__(
        self, port: int, password: str, timeout_s: float, host: str = LOOPBACK
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._ids = itertools.count(1)

    def __enter__(self) -> RconClient:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout_s
            )
        except OSError as exc:
            raise RconError(f"RCON injoignable sur le port {self._port}") from exc
        try:
            self._authenticate()
        except RconError:
            self.close()
            raise

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def command(self, text: str) -> str:
        """Envoie une commande et rend le corps de sa réponse."""
        request_id = self._send(TYPE_EXEC, text)
        while True:
            packet_id, kind, body = self._read_packet()
            if packet_id == request_id and kind == TYPE_RESPONSE:
                return body

    def save(self) -> str:
        return self.command(SAVE)

    def quit(self) -> None:
        """Envoie `quit`. Un serveur qui s'arrête peut couper sans répondre.

        Seul l'envoi peut échouer ici : c'est la fin du processus qui prouve l'arrêt.
        """
        self._send(TYPE_EXEC, QUIT)
        try:
            self._read_packet()
        except RconError:
            pass

    def _authenticate(self) -> None:
        request_id = self._send(TYPE_AUTH, self._password)
        while True:
            # Une réponse vide précède celle d'authentification.
            packet_id, kind, _ = self._read_packet()
            if kind != TYPE_AUTH_RESPONSE:
                continue
            if packet_id != request_id:
                raise RconError(f"authentification RCON refusée sur le port {self._port}")
            return

    def _send(self, kind: int, body: str) -> int:
        if self._sock is None:
            raise RconError("RCON non connecté")
        request_id = next(self._ids)
        payload = HEADER.pack(request_id, kind) + body.encode("utf-8") + TERMINATOR
        try:
            self._sock.sendall(SIZE_FIELD.pack(len(payload)) + payload)
        except OSError as exc:
            raise RconError(f"envoi RCON impossible sur le port {self._port}") from exc
        return request_id

    def _read_packet(self) -> tuple[int, int, str]:
        (size,) = SIZE_FIELD.unpack(self._read_exact(SIZE_FIELD.size))
        if not MIN_PACKET_SIZE <= size <= MAX_PACKET_SIZE:
            raise RconError(f"paquet RCON invalide : taille {size}")
        data = self._read_exact(size)
        packet_id, kind = HEADER.unpack_from(data)
        body = data[HEADER.size : -len(TERMINATOR)]
        return packet_id, kind, body.decode("utf-8", errors="replace")

    def _read_exact(self, count: int) -> bytes:
        if self._sock is None:
            raise RconError("RCON non connecté")
        chunks = bytearray()
        while len(chunks) < count:
            try:
                chunk = self._sock.recv(count - len(chunks))
            except OSError as exc:
                raise RconError(f"réponse RCON absente sur le port {self._port}") from exc
            if not chunk:
                raise RconError(f"connexion RCON fermée sur le port {self._port}")
            chunks += chunk
        return bytes(chunks)
