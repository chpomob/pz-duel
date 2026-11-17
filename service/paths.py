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


# -- registre des tenues du jeu (P9F25) --------------------------------------------------------
# Ce module n'importe rien de `service` : les chemins de Steam de `preflight.py` sont redits ici.
GAME_ENV_VAR = "PZ_INSTALL_DIR"
GAME_DIR_OPTION = "--pz-dir"
STEAM_GAME_DIR = ("steamapps", "common", "ProjectZomboid")
# Sous Linux, le jeu est dans un sous-dossier de l'installation ; sous Windows, à sa racine.
GAME_SUBDIRS = ("projectzomboid", "")
CLOTHING_REGISTRY = ("media", "clothing", "clothing.xml")
STEAM_ROOTS_POSIX = (
    ".local/share/Steam",
    ".steam/steam",
    ".steam/root",
    ".steam/debian-installation",
    ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    "snap/steam/common/.local/share/Steam",
)
PROGRAM_FILES_VAR = "ProgramFiles(x86)"
STEAM_DEFAULT_SUBDIR = "Steam"


def _game_dir_option(argv: list[str]) -> str | None:
    """Rend la valeur de `--pz-dir` sur la ligne de commande, sous ses deux écritures."""
    for index, word in enumerate(argv):
        if word == GAME_DIR_OPTION and index + 1 < len(argv):
            return argv[index + 1]
        if word.startswith(GAME_DIR_OPTION + "="):
            return word[len(GAME_DIR_OPTION) + 1 :]
    return None


def clothing_registry(
    platform: str | None = None,
    env: os._Environ | dict[str, str] | None = None,
    home: Path | None = None,
    argv: list[str] | None = None,
) -> Path | None:
    """Rend `media/clothing/clothing.xml` du jeu installé, ou le premier endroit où il manque.

    Ordre de `preflight.detect_pz` : `--pz-dir`, puis `PZ_INSTALL_DIR`, puis les dossiers connus
    de Steam. Un dossier désigné ne se replie pas sur Steam. `None` : aucun endroit où chercher.
    Sert à l'import, avant tout préflight : le dossier choisi dans le lobby, le registre Windows
    et les bibliothèques Steam secondaires se suivent ensuite par `game_clothing_registry`.
    """
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else argv
    explicit = _game_dir_option(argv) or env.get(GAME_ENV_VAR) or None
    if explicit is not None:
        folders = [Path(explicit)]
    elif (sys.platform if platform is None else platform) == WINDOWS:
        program_files = env.get(PROGRAM_FILES_VAR)
        folders = (
            [Path(program_files).joinpath(STEAM_DEFAULT_SUBDIR, *STEAM_GAME_DIR)]
            if program_files
            else []
        )
    else:
        home = Path.home() if home is None else home
        folders = [Path(home).joinpath(root, *STEAM_GAME_DIR) for root in STEAM_ROOTS_POSIX]
    candidates = [
        folder.joinpath(subdir, *CLOTHING_REGISTRY) for folder in folders for subdir in GAME_SUBDIRS
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0] if candidates else None


def game_clothing_registry(game_dir: str | os.PathLike[str]) -> Path:
    """Rend le registre des tenues de `game_dir`, le dossier du jeu que le préflight a accepté.

    Ce dossier porte déjà `media/` : `projectzomboid/` sous Linux, la racine du jeu sous Windows.
    """
    return Path(game_dir).joinpath(*CLOTHING_REGISTRY)
