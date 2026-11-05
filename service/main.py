"""Point d'entrée : `python3 -m service.main` (depuis /home/chpo/pz-duel)."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from .api import make_server
from .engine import Engine
from .preflight import MOD_SRC, detect_pz, install_mod, mod_fingerprint


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


def main() -> None:
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
    args = parser.parse_args()

    if args.install_mod:
        run_install_mod()
        return

    if args.pz_dir is not None:
        # Un choix explicite invalide échoue tout de suite, sans repli (D19).
        detection = detect_pz(args.pz_dir, os.environ, Path.home())
        if detection.path is None:
            sys.exit(detection.message)
        print(detection.message, flush=True)

    engine = Engine(mode=args.mode)
    port = free_port(args.port)
    httpd = make_server(engine, port=port)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"PZ Duel — interface : {url}", flush=True)

    # fichier de découverte : le mod in-game et les outils lisent l'URL ici
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    info_path = os.path.join(root, ".duel-service.json")
    with open(info_path, "w", encoding="utf-8") as fh:
        json.dump({"url": url, "port": port, "pid": os.getpid()}, fh)
    atexit.register(lambda: os.path.exists(info_path) and os.unlink(info_path))

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
