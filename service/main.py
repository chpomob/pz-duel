"""Point d'entrée : `python3 -m service.main` (depuis /home/chpo/pz-duel)."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import IO, Any

from . import paths, platforms
from .api import BODY_MAX, LOCAL_NAMES, SERVER_TOKEN, claim_address, make_server
from .engine import STOP_INCOMPLETE, Engine
from .preflight import MOD_SRC, detect_pz, install_mod, mod_fingerprint
from .rules import load_runtime

STOP_SIGNALS = tuple(getattr(signal, name) for name in platforms.current().stop_signals)
LOOPBACK = LOCAL_NAMES[0]
PORT_SPAN = 20
INSTANCE_LOCK = "instance.lock"
INSTANCE_PORT = "instance.port"
STATE_PATH = "/api/state"
STATE_KEY = "phase"


def log_line(text: str) -> None:
    """Consigne `text` dans le fichier de log en mode figé : sans console, il n'y a que lui.

    Seuls l'URL et des messages sans secret y viennent. Un disque plein n'arrête pas le service.
    """
    if not paths.is_frozen():
        return
    stamp = datetime.now().isoformat(sep=" ", timespec="seconds")
    try:
        path = paths.log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="backslashreplace") as log:
            log.write(f"{stamp} {text}\n")
    except OSError:
        pass


def report(text: str, stream: IO[str] | None = None) -> None:
    """`print` qui ne lève pas : le flux peut manquer, être fermé ou refuser un caractère."""
    log_line(text)
    stream = sys.stdout if stream is None else stream
    if stream is None:
        return
    try:
        try:
            print(text, file=stream, flush=True)
        except UnicodeEncodeError:
            encoding = stream.encoding or "ascii"
            safe = text.encode(encoding, "backslashreplace").decode(encoding)
            print(safe, file=stream, flush=True)
    except (OSError, ValueError):
        pass


class Parser(argparse.ArgumentParser):
    """Passe l'usage et ses erreurs par `report` : le binaire n'a pas de `stderr` à montrer."""

    def _print_message(self, message: str, file: IO[str] | None = None) -> None:
        if message:
            report(message.rstrip(), file or sys.stderr)


def interface_url(port: int) -> str:
    return f"http://{LOOPBACK}:{port}/"


def open_interface(url: str, browser: Callable[[str], object] | None = None) -> bool:
    """Ouvre `url`. Un échec n'arrête rien : le log garde de quoi l'ouvrir à la main."""
    try:
        opened = bool((webbrowser.open if browser is None else browser)(url))
    except Exception as exc:
        log_line(f"Could not open the browser ({exc}): open {url} by hand")
        return False
    if not opened:
        log_line(f"Could not open the browser: open {url} by hand")
    return opened


def candidate_ports(preferred: int) -> range:
    return range(preferred, preferred + PORT_SPAN)


def free_port(preferred: int = 8765) -> int:
    for port in candidate_ports(preferred):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                # Comme le serveur : un port qu'il ne pourrait pas lier n'est pas libre.
                claim_address(sock)
                sock.bind((LOOPBACK, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


class ProbeSocket(socket.socket):
    """Lit sous une échéance : un délai par `recv` se renouvellerait à chaque octet."""

    deadline = 0.0

    def recv_into(self, *args: Any) -> int:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        self.settimeout(left)
        return super().recv_into(*args)


class ProbeConnection(http.client.HTTPConnection):
    """Tient tout entière dans `timeout_s`, connexion comprise, quoi que l'écouteur envoie."""

    def __init__(self, port: int, timeout_s: float) -> None:
        super().__init__(LOOPBACK, port, timeout=timeout_s)
        self.deadline = time.monotonic() + timeout_s

    def connect(self) -> None:
        super().connect()
        sock = ProbeSocket(fileno=self.sock.detach())
        sock.settimeout(self.timeout)
        sock.deadline = self.deadline
        self.sock = sock


def is_service(port: int, timeout_s: float) -> bool:
    """Vrai si `port` porte un service PZ Duel : tout le reste est un port occupé."""
    connection = ProbeConnection(port, timeout_s)
    try:
        connection.request("GET", STATE_PATH)
        response = connection.getresponse()
        if response.status != http.client.OK:
            return False
        if not (response.getheader("Server") or "").startswith(SERVER_TOKEN):
            return False
        state = json.loads(response.read(BODY_MAX))
    except (OSError, OverflowError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    return isinstance(state, dict) and STATE_KEY in state


def find_service(preferred: int, timeout_s: float, recorded: int | None = None) -> int | None:
    ports = candidate_ports(preferred)
    for port in ports if recorded is None else (recorded, *ports):
        if is_service(port, timeout_s):
            return port
    return None


def record_port(port: int) -> None:
    """Note le port réel : hors des ports candidats, la sonde ne le trouverait pas autrement."""
    try:
        (paths.data_dir() / INSTANCE_PORT).write_text(str(port), encoding="ascii")
    except OSError as exc:
        log_line(f"Could not record port {port}: {exc}")


def recorded_port() -> int | None:
    """Rend le port noté. Il peut dater d'un service mort : seule la sonde le confirme."""
    try:
        return int((paths.data_dir() / INSTANCE_PORT).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def claim_instance(preferred: int, on_found: Callable[[str], None]) -> int | None:
    """Rend le descripteur du verrou d'instance, ou `None` si un service actif a répondu.

    La sonde seule laisserait passer deux double-clics simultanés : le verrou tranche, et le
    perdant attend que le gagnant réponde. Si le gagnant meurt, le verrou lui revient.
    Le port noté se relit à chaque tour : le gagnant ne le note qu'une fois lié. Un tour de
    sonde, port muet à chaque pas, port noté compris, tient dans `instance_wait_s`.
    """
    wait_s = load_runtime()["instance_wait_s"]
    step_s = wait_s / (PORT_SPAN + 1)
    deadline = time.monotonic() + wait_s
    while True:
        port = find_service(preferred, step_s, recorded_port())
        if port is not None:
            on_found(interface_url(port))
            return None
        lock = platforms.take_instance_lock(paths.data_dir() / INSTANCE_LOCK)
        if lock is not None:
            return lock
        if time.monotonic() >= deadline:
            sys.exit(f"Another PZ Duel instance did not answer within {wait_s} s.")
        time.sleep(step_s)


def run_install_mod() -> None:
    try:
        destination = install_mod(MOD_SRC, Path.home() / "Zomboid")
    except OSError as exc:
        sys.exit(f"Installation du mod impossible : {exc}")
    report(f"Mod installé : {destination} (empreinte {mod_fingerprint(destination)})")


def stop_on_signal(engine: Engine, httpd: ThreadingHTTPServer) -> Callable[[], None]:
    """Sur un signal d'arrêt, arrête le moteur puis le serveur HTTP, une seule fois.

    Rend la fonction qui fait cet arrêt, sans signal : elle rend la main tout de suite et
    s'appelle autant de fois qu'on veut, de n'importe quel fil.

    Tout se passe sur un fil dédié : le gestionnaire s'exécute sur le fil de `serve_forever`,
    et `httpd.shutdown()`, qui attend la fin de cette boucle, s'y bloquerait. Rien n'y attend
    un verrou, car un signal peut en interrompre un autre sur ce même fil : `requested` ne se
    prend qu'à l'essai, et ne se rend jamais.
    """
    requested = threading.Lock()

    def stop() -> None:
        report("Arrêt demandé : fermeture des serveurs")
        try:
            engine.shutdown()
        finally:
            httpd.shutdown()

    def request_stop() -> None:
        if requested.acquire(blocking=False):
            threading.Thread(target=stop, name="duel-stop", daemon=True).start()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        request_stop()

    for signum in STOP_SIGNALS:
        signal.signal(signum, on_signal)
    return request_stop


def serve(
    args: argparse.Namespace,
    make_engine: Callable[..., Engine] | None,
    browser: Callable[[str], object] | None,
    on_bound: Callable[[int], None] | None = None,
) -> None:
    if args.pz_dir is not None:
        # Un choix explicite invalide échoue tout de suite, sans repli (D19).
        detection = detect_pz(args.pz_dir, os.environ, Path.home())
        if detection.path is None:
            sys.exit(detection.message)
        report(detection.message)

    engine = (Engine if make_engine is None else make_engine)(mode=args.mode, pz_dir=args.pz_dir)
    port = free_port(args.port)
    httpd = make_server(engine, port=port)
    # Avant toute requête : aucun serveur ne peut partir sans que le signal l'arrête.
    stop_on_signal(engine, httpd)
    port = httpd.server_address[1]
    if on_bound is not None:
        on_bound(port)
    url = interface_url(port)
    report(f"PZ Duel — interface : {url}")

    if not args.no_browser:
        # Le socket écoute déjà : la requête du navigateur attend `serve_forever`, et un
        # navigateur lent ne le retarde pas.
        threading.Thread(
            target=open_interface, args=(url, browser), name="duel-browser", daemon=True
        ).start()

    try:
        httpd.serve_forever()
    finally:
        # Sur signal, attend le fil d'arrêt ; sur panne de la boucle, c'est l'arrêt lui-même.
        stopped = engine.shutdown()
        httpd.server_close()
    if not stopped:
        sys.exit(STOP_INCOMPLETE)


def run(
    argv: Sequence[str] | None,
    make_engine: Callable[..., Engine] | None,
    browser: Callable[[str], object] | None,
) -> None:
    parser = Parser(description="PZ Duel — service local")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--mode", choices=["mock", "real"], default="real" if paths.is_frozen() else "mock"
    )
    parser.add_argument("--pz-dir", help="dossier du jeu, prioritaire sur PZ_INSTALL_DIR et Steam")
    parser.add_argument(
        "--install-mod",
        action="store_true",
        help="installe mods/duelcore dans ~/Zomboid/mods/ puis quitte",
    )
    args = parser.parse_args(argv)

    if args.install_mod:
        run_install_mod()
        return

    if not paths.is_frozen():
        serve(args, make_engine, browser)
        return

    def on_found(url: str) -> None:
        report(f"PZ Duel is already running: {url}")
        if not args.no_browser:
            open_interface(url, browser)

    lock = claim_instance(args.port, on_found)
    if lock is None:
        return
    try:
        serve(args, make_engine, browser, record_port)
    finally:
        os.close(lock)


def main(
    argv: Sequence[str] | None = None,
    make_engine: Callable[..., Engine] | None = None,
    *,
    browser: Callable[[str], object] | None = None,
) -> None:
    """`argv`, `make_engine` et `browser` servent aux tests, hors du vrai `~` et du vrai bureau.

    Une erreur finit par `SystemExit` : un appelant n'a pas de code de retour à transmettre.
    Figé, son texte n'a que le log pour survivre, et une exception ne doit pas finir dans la
    boîte de dialogue du binaire.
    """
    try:
        run(argv, make_engine, browser)
    except SystemExit as exc:
        if isinstance(exc.code, str):  # les erreurs d'usage, en entier, sont passées par `report`
            log_line(exc.code)
        raise
    except Exception as exc:
        if not paths.is_frozen():
            raise
        message = f"Startup failed: {type(exc).__name__}: {exc}"
        log_line(message)
        sys.exit(message)


if __name__ == "__main__":
    main()
