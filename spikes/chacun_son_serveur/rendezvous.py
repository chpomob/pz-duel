"""Spike S3 : un relais de rendez-vous. Aucun port à ouvrir chez les joueurs.

Les deux arbitres se connectent EN SORTIE au relais et donnent le même code de duel ; le relais
recopie alors les octets de l'un vers l'autre, sans les lire. Une connexion sortante traverse
une box, un double NAT ou un CGNAT : c'est le cas que l'UPnP ne couvre pas.

Le relais ne connaît ni le protocole ni les joueurs. Il voit passer le trafic en clair : la
vraie version chiffrerait de bout en bout avec un secret tiré du code d'invitation.

    python -m spikes.chacun_son_serveur.rendezvous --port 17290      # sur une machine joignable
"""

from __future__ import annotations

import argparse
import socket
import threading

from spikes.chacun_son_serveur.relay import Link

CODE_MAX = 64
HELLO_TIMEOUT_S = 5
BUFFER = 65536


class Rendezvous:
    """Apparie deux connexions qui annoncent le même code (une ligne `code\\n`), puis les relie."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._listener = socket.create_server((host, port))
        self.address = self._listener.getsockname()
        self._waiting: dict[bytes, socket.socket] = {}
        self._lock = threading.Lock()
        self.pairs = 0
        self.relayed_bytes = 0
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._greet, args=(conn,), daemon=True).start()

    def _greet(self, conn: socket.socket) -> None:
        conn.settimeout(HELLO_TIMEOUT_S)
        code = b""
        try:
            while not code.endswith(b"\n") and len(code) <= CODE_MAX:
                byte = conn.recv(1)
                if not byte:
                    raise OSError("fermé avant le code")
                code += byte
        except OSError:
            conn.close()
            return
        if not code.endswith(b"\n"):
            conn.close()
            return
        conn.settimeout(None)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            peer = self._waiting.pop(code, None)
            if peer is None:
                self._waiting[code] = conn
                return
            self.pairs += 1
        for source, target in ((conn, peer), (peer, conn)):
            # L'appariement s'annonce aux deux bouts : aucun n'émet dans le vide avant.
            target.sendall(b"ok\n")
            threading.Thread(target=self._pipe, args=(source, target), daemon=True).start()

    def _pipe(self, source: socket.socket, target: socket.socket) -> None:
        try:
            while data := source.recv(BUFFER):
                target.sendall(data)
                self.relayed_bytes += len(data)
        except OSError:
            pass
        finally:
            for sock in (source, target):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self) -> None:
        self._listener.close()


def join(address: tuple[str, int], code: str, delay_s: float = 0.0, jitter_s: float = 0.0,
         seed: int = 0) -> Link:
    """Connexion SORTANTE au relais ; rend le `Link` une fois l'autre bout apparié."""
    sock = socket.create_connection(address)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(code.encode("ascii") + b"\n")
    ready = b""
    while not ready.endswith(b"\n"):
        byte = sock.recv(1)
        if not byte:
            raise OSError("relais fermé avant l'appariement")
        ready += byte
    return Link(sock, delay_s, jitter_s, seed)


def join_pair(address: tuple[str, int], code: str, delay_s: float = 0.0,
              jitter_s: float = 0.0) -> tuple[Link, Link]:
    """Les deux bouts d'un duel, chacun par sa connexion sortante (spike sur une machine)."""
    links: dict[str, Link] = {}

    def one(name: str, seed: int) -> None:
        links[name] = join(address, code, delay_s, jitter_s, seed)

    threads = [threading.Thread(target=one, args=(name, seed)) for seed, name in enumerate("hg")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(HELLO_TIMEOUT_S * 2)
    return links["h"], links["g"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=17290)
    args = parser.parse_args()
    relay = Rendezvous(args.host, args.port)
    print(f"relais de rendez-vous sur {relay.address[0]}:{relay.address[1]}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
