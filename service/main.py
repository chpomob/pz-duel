"""Point d'entrée : `python3 -m service.main` (depuis /home/chpo/pz-duel)."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable, Sequence
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import FrameType

from .api import make_server
from .engine import STOP_INCOMPLETE, Engine
from .preflight import MOD_SRC, detect_pz, install_mod, mod_fingerprint

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def free_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_install_mod() -> None:
    try:
        destination = install_mod(MOD_SRC, Path.home() / "Zomboid")
    except OSError as exc:
        sys.exit(f"Installation du mod impossible : {exc}")
    print(f"Mod installé : {destination} (empreinte {mod_fingerprint(destination)})", flush=True)


def stop_on_signal(engine: Engine, httpd: ThreadingHTTPServer) -> None:
    """Sur SIGINT ou SIGTERM, arrête le moteur puis le serveur HTTP, une seule fois.

    Tout se passe sur un fil dédié : le gestionnaire s'exécute sur le fil de `serve_forever`,
    et `httpd.shutdown()`, qui attend la fin de cette boucle, s'y bloquerait. Il ne prend
    aucun verrou : un signal peut en interrompre un autre sur ce même fil.
    """
    requested = False

    def stop() -> None:
        print("Arrêt demandé : fermeture des serveurs", flush=True)
        try:
            engine.shutdown()
        finally:
            httpd.shutdown()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal requested
        if not requested:
            requested = True
            threading.Thread(target=stop, name="duel-stop", daemon=True).start()

    for signum in STOP_SIGNALS:
        signal.signal(signum, on_signal)


def main(
    argv: Sequence[str] | None = None, make_engine: Callable[..., Engine] | None = None
) -> None:
    """`argv` et `make_engine` servent aux tests, qui posent un `Engine` hors du vrai `~`."""
    parser = argparse.ArgumentParser(description="PZ Duel — service local")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
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

    if args.pz_dir is not None:
        # Un choix explicite invalide échoue tout de suite, sans repli (D19).
        detection = detect_pz(args.pz_dir, os.environ, Path.home())
        if detection.path is None:
            sys.exit(detection.message)
        print(detection.message, flush=True)

    engine = (Engine if make_engine is None else make_engine)(mode=args.mode, pz_dir=args.pz_dir)
    port = free_port(args.port)
    httpd = make_server(engine, port=port)
    # Avant toute requête : aucun serveur ne peut partir sans que le signal l'arrête.
    stop_on_signal(engine, httpd)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"PZ Duel — interface : {url}", flush=True)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    finally:
        # Sur signal, attend le fil d'arrêt ; sur panne de la boucle, c'est l'arrêt lui-même.
        stopped = engine.shutdown()
        httpd.server_close()
    if not stopped:
        sys.exit(STOP_INCOMPLETE)


if __name__ == "__main__":
    main()
