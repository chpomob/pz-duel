"""Où le service lit ses ressources et écrit ses données, depuis les sources ou figé.

Seul module qui distingue les deux : le reste de `service/` passe par lui. Il n'importe
rien de `service`, car `rules.py` en dépend.

`platform` prend les valeurs de `sys.platform`, qu'il lit par défaut. Un chemin Windows se
compose avec `ntpath` : sous Linux, une plateforme injectée rend ainsi le même texte.
"""

from __future__ import annotations

import ntpath
import os
import sys
from pathlib import Path

WINDOWS = "win32"
APP_DIR_WINDOWS = "PZDuel"
APP_DIR_POSIX = "pz-duel"
MATCHES_NAME = "matches"
LOG_NAME = "pz-duel.log"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Rend le dossier d'extraction du binaire, sinon la racine du dépôt. Lecture seule."""
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _in_data_dir(platform: str | None, *parts: str) -> Path:
    if (sys.platform if platform is None else platform) == WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or ntpath.join(
            ntpath.expanduser("~"), "AppData", "Local"
        )
        return Path(ntpath.join(base, APP_DIR_WINDOWS, *parts))
    # La spécification XDG demande d'ignorer une valeur relative.
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".local" / "share"
    return base.joinpath(APP_DIR_POSIX, *parts)


def data_dir(platform: str | None = None) -> Path:
    """Rend le dossier de données de l'utilisateur, qui survit à la sortie du binaire."""
    return _in_data_dir(platform)


def matches_dir(platform: str | None = None) -> Path:
    if is_frozen():
        return _in_data_dir(platform, MATCHES_NAME)
    return resource_root() / MATCHES_NAME


def log_file(platform: str | None = None) -> Path:
    return _in_data_dir(platform, LOG_NAME)
