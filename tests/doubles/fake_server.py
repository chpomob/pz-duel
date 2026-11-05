#!/usr/bin/env python3
"""Double de `start-server.sh` : sortie scriptée, enfant, refus de quitter, crash, RCON.

Il reçoit les arguments d'un vrai serveur. `install_fake_server` le pose à la place du
script dans un dossier d'installation factice. Le scénario est un objet JSON :

- `lines` : sortie standard ; `{servername}`, `{port}`, `{udpport}` et `{adminpassword}`
  y sont remplacés ; `line_delay_s` les espace ;
- `child` : lance un enfant dans le groupe, dont le pid est imprimé, et qui vit au plus
  `child_life_s` : un test interrompu ne laisse rien derrière lui ;
- `orphan_child` : sort sans tuer l'enfant, comme le script qui précède son `java` ;
- `refuse_quit` : ignore `quit` sur l'entrée standard et par RCON ;
- `ignore_sigterm` : ignore SIGTERM, enfant compris, car l'ignorance se transmet ;
- `exit_code` : crash, sortie immédiate avec ce code après les lignes ;
- `rcon_password` : ouvre le RCON factice et imprime `fake rcon port=<port>`.

Le protocole est réécrit ici, sans `service.rcon` : le client est ainsi vérifié contre une
implémentation indépendante, et le script tourne sans le dépôt dans son chemin.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

AUTH = 3
AUTH_RESPONSE = 2
EXEC = 2
RESPONSE = 0
AUTH_REFUSED = -1
DEFAULT_CHILD_LIFE_S = 60
REPLIES = {"save": "World saved"}


def send_packet(conn: socket.socket, packet_id: int, kind: int, body: str) -> None:
    payload = struct.pack("<ii", packet_id, kind) + body.encode() + b"\0\0"
    conn.sendall(struct.pack("<i", len(payload)) + payload)


def read_exact(conn: socket.socket, count: int) -> bytes | None:
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_packet(conn: socket.socket) -> tuple[int, int, str] | None:
    size = read_exact(conn, 4)
    data = size and read_exact(conn, struct.unpack("<i", size)[0])
    if not data:
        return None
    packet_id, kind = struct.unpack_from("<ii", data)
    return packet_id, kind, data[8:-2].decode()


class FakeRcon:
    """Serveur RCON local : `quit` coupe sans répondre, `mute` ne répond à aucune commande."""

    def __init__(
        self,
        password: str,
        on_quit: Callable[[], None] = lambda: None,
        mute: bool = False,
    ) -> None:
        self.password = password
        self.on_quit = on_quit
        self.mute = mute
        self.commands: list[str] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def __enter__(self) -> FakeRcon:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._listener.shutdown(socket.SHUT_RDWR)
        self._listener.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with conn:
                try:
                    self._handle(conn)
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        authenticated = False
        while packet := read_packet(conn):
            packet_id, kind, body = packet
            if kind == AUTH:
                authenticated = body == self.password
                send_packet(conn, packet_id, RESPONSE, "")
                reply_id = packet_id if authenticated else AUTH_REFUSED
                send_packet(conn, reply_id, AUTH_RESPONSE, "")
            elif kind == EXEC and authenticated:
                self.commands.append(body)
                if self.mute:
                    continue
                if body == "quit":
                    self.on_quit()
                    return
                send_packet(conn, packet_id, RESPONSE, REPLIES.get(body, ""))


def install_fake_server(pz_dir: Path, scenario: dict[str, Any]) -> Path:
    """Pose un `start-server.sh` qui exécute ce double avec `scenario`."""
    scenario_path = pz_dir / "fake_scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    command = [sys.executable, str(Path(__file__).resolve()), "--scenario", str(scenario_path)]
    script = pz_dir / "start-server.sh"
    script.write_text(f'#!/bin/sh\nexec {shlex.join(command)} "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    return script


def print_line(text: str) -> None:
    print(text, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--scenario", required=True)
    for name in ("-servername", "-port", "-udpport", "-adminpassword"):
        parser.add_argument(name, required=True)
    args = parser.parse_args()
    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))

    if scenario.get("ignore_sigterm"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = None
    if scenario.get("child"):
        life_s = scenario.get("child_life_s", DEFAULT_CHILD_LIFE_S)
        child = subprocess.Popen(["sleep", str(life_s)])
        print_line(f"fake child pid={child.pid}")

    stop = threading.Event()

    def request_stop() -> None:
        if not scenario.get("refuse_quit"):
            stop.set()

    if "rcon_password" in scenario:
        rcon = FakeRcon(scenario["rcon_password"], on_quit=request_stop)
        print_line(f"fake rcon port={rcon.port}")

    for line in scenario.get("lines", ()):
        print_line(line.format(**vars(args)))
        time.sleep(scenario.get("line_delay_s", 0))

    def read_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == "quit":
                request_stop()

    if "exit_code" not in scenario:
        threading.Thread(target=read_stdin, daemon=True).start()
        stop.wait()
    if child and not scenario.get("orphan_child"):
        child.kill()
        child.wait()
    # Sans attendre les fils encore bloqués sur l'entrée standard ou sur RCON.
    sys.stdout.flush()
    os._exit(scenario.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
