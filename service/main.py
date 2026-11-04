"""Point d'entrée : `python3 -m service.main` (depuis /home/chpo/pz-duel)."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import socket
import threading
import webbrowser

from .api import make_server
from .engine import Engine


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


def main() -> None:
    parser = argparse.ArgumentParser(description="PZ Duel — service local")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    args = parser.parse_args()

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
