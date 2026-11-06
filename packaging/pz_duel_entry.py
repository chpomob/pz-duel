"""Point d'entrée du binaire : PyInstaller l'exécute comme un script, hors de tout paquet.

`service/main.py` a des imports relatifs et ne peut pas jouer ce rôle : ce fichier l'importe
en absolu.

Un `--onefile` a deux processus : le chargeur extrait les ressources et relance l'exécutable,
et c'est ce second processus qui porte le service. Le chargeur de PyInstaller 6.22.3 le crée
sans objet Job (`pyi_utils_create_child`) : un `taskkill /F` du chargeur le laisserait vivre,
et avec lui le Job des serveurs, alors que R2 veut leur fin quelle que soit la façon de finir.
Le service attend donc son parent, et meurt avec lui comme s'il avait été tué lui-même : le
noyau ferme le handle du Job et tue l'arbre (D-E). Pas d'arrêt ordonné ici : ses paliers
durent plus que les 5 s d'AC10.

Cette attente vit ici et non dans `service/` : elle ne concerne que le binaire.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

from service import paths
from service.main import log_line, main

FORCED_END_CODE = 1
PARENT_POLL_S = 1


def wait_for_parent_windows(parent: int) -> None:
    """Rend la main à la fin de `parent`. Un parent déjà parti ne s'ouvre plus."""
    import _winapi

    try:
        handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, parent)
    except OSError:
        return
    _winapi.WaitForSingleObject(handle, _winapi.INFINITE)


def wait_for_parent_posix(parent: int) -> None:
    """Rend la main quand `parent` n'est plus le parent : orphelin, on change de parent."""
    while os.getppid() == parent:
        time.sleep(PARENT_POLL_S)


def end_with_parent(parent: int, wait: Callable[[int], None], end: Callable[[int], None]) -> None:
    wait(parent)
    log_line("The launcher process ended: stopping now")
    end(FORCED_END_CODE)


def watch_parent() -> None:
    """Lance l'attente en mode figé. Sans parent lisible, le service tourne quand même."""
    if not paths.is_frozen():
        return
    try:
        parent = os.getppid()
    except OSError as exc:
        log_line(f"Could not read the launcher process: {exc}")
        return
    wait = wait_for_parent_windows if sys.platform == "win32" else wait_for_parent_posix
    threading.Thread(
        target=end_with_parent, args=(parent, wait, os._exit), name="duel-parent", daemon=True
    ).start()


if __name__ == "__main__":
    watch_parent()
    main()
