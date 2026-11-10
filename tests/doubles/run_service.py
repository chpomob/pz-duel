"""Lance `service.main` avec un `Engine` de test, sans toucher au vrai `~/Zomboid`.

À lancer avec la racine du dépôt dans `PYTHONPATH` : `python run_service.py <config>
<arguments de main>`. `<config>` est un fichier JSON portant `zomboid_dir`, `matches_dir` et
`runtime`, et au besoin `upnp_target` (`[hôte, port]` du faux IGD local) et `local_ip` (adresse
rendue à la place de `service.engine.local_ip`, dont le `connect` vise `8.8.8.8` : un test borné
au loopback la fixe ainsi dans le processus du service).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import service.engine as engine_module
from service.engine import Engine
from service.main import main


def run(config_path: str, argv: list[str]) -> None:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    target = config.get("upnp_target")
    upnp_target = None if target is None else (str(target[0]), int(target[1]))
    if "local_ip" in config:
        address = str(config["local_ip"])
        engine_module.local_ip = lambda: address

    def make_engine(mode: str, pz_dir: str | None) -> Engine:
        return Engine(
            mode,
            zomboid_dir=config["zomboid_dir"],
            pz_dir=pz_dir,
            runtime=config["runtime"],
            matches_dir=config["matches_dir"],
            upnp_target=upnp_target,
        )

    main(argv, make_engine)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2:])
