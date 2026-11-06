"""Ce qui diffère entre Windows et Linux, décrit par un `Platform` figé.

D-B : `LINUX`, `WINDOWS` et `current()`. Une fonction touchée prend un paramètre `platform`
dont le défaut est `current()` : sous Linux, tout appel existant garde son comportement, et
une branche Windows s'exécute sous Linux par injection. Un chemin de commande se compose
avec `join()`, donc `ntpath` pour Windows : le texte rendu ne dépend pas du système hôte.

D-C : aucun module réservé à Windows ne s'importe au chargement de ce module (AC6).
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from . import paths

MS_PER_S = 1000


class ReplaceError(OSError):
    """Cible restée verrouillée jusqu'à l'échéance : un `except OSError` la reçoit aussi."""


@dataclass(frozen=True)
class Platform:
    """`name` vaut `sys.platform` : c'est ce que `paths` attend pour son `platform`.

    `game_subdir` sépare l'installation du dossier qui porte `server_script` ; il est vide
    quand c'est le même. `stop_signals` porte des noms : `SIGBREAK` n'existe que sous Windows.
    """

    name: str
    server_script: str
    client_program: str
    client_in_game_dir: bool
    game_subdir: str
    posix_modes: bool
    stop_signals: tuple[str, ...]
    path: ModuleType = field(repr=False)

    def join(self, *parts: str | os.PathLike[str]) -> str:
        return self.path.join(*map(os.fspath, parts))

    def client_path(self, game_dir: str | os.PathLike[str]) -> str:
        """Rend le programme client de `game_dir`, le dossier de `server_script`."""
        folder = os.fspath(game_dir)
        if not self.client_in_game_dir:
            folder = self.path.dirname(folder)
        return self.join(folder, self.client_program)


LINUX = Platform(
    name="linux",
    server_script="start-server.sh",
    client_program="projectzomboid.sh",
    client_in_game_dir=False,
    game_subdir="projectzomboid",
    posix_modes=True,
    stop_signals=("SIGINT", "SIGTERM"),
    path=posixpath,
)
WINDOWS = Platform(
    name=paths.WINDOWS,
    server_script="StartServer64.bat",
    client_program="ProjectZomboid64.exe",
    client_in_game_dir=True,
    game_subdir="",
    posix_modes=False,
    stop_signals=("SIGINT", "SIGBREAK"),
    path=ntpath,
)


def current() -> Platform:
    return WINDOWS if sys.platform == WINDOWS.name else LINUX


def replace_file(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    policy: Mapping[str, Any] | None = None,
    platform: Platform | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
) -> None:
    """Remplace `dst` par `src` d'un seul `rename`, sans jamais effacer `dst`.

    Sous Windows, un autre processus qui tient `dst` ouvert fait échouer le remplacement :
    il est réessayé sur `PermissionError` jusqu'à `replace_timeout_s`, tous les
    `replace_retry_ms`, puis `ReplaceError` nomme le fichier. La dernière attente est tronquée
    à l'échéance, où a lieu un dernier essai. `policy` vaut `load_runtime()` par défaut.

    `src` reste en place sur un échec : le temporaire appartient à l'appelant, qui le retire.
    """
    platform = current() if platform is None else platform
    if platform.name != WINDOWS.name:
        os.replace(src, dst)
        return
    if policy is None:
        from .rules import load_runtime

        policy = load_runtime()
    timeout_s = policy["replace_timeout_s"]
    retry_s = policy["replace_retry_ms"] / MS_PER_S
    deadline = clock() + timeout_s
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            remaining_s = deadline - clock()
            if remaining_s <= 0:
                raise ReplaceError(
                    f"Could not replace {os.fspath(dst)}: "
                    f"file still in use after {timeout_s} s."
                ) from exc
        sleep(min(retry_s, remaining_s))
