"""Préflight du mode réel et installation du mod `duelcore`.

`run_preflight` ne lance rien et n'écrit rien : il mesure, et rend les valeurs relevées
avec un message français par défaut constaté. `install_mod` est le seul chemin qui écrit
dans `~/Zomboid/mods/`, et seulement dans `duelcore/`.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import socket
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .rules import TableError, load_effects, load_runtime, load_table

MOD_ID = "duelcore"
MOD_SRC = paths.resource_root() / "mods" / MOD_ID
MEMINFO_PATH = Path("/proc/meminfo")
ENV_VAR = "PZ_INSTALL_DIR"
SCRIPT = "start-server.sh"
GAME_SUBDIR = "projectzomboid"
STEAM_GAME_DIR = Path("steamapps") / "common" / "ProjectZomboid"
# Relatifs au dossier personnel : Steam natif, paquet Debian, Flatpak, Snap.
STEAM_ROOTS = (
    Path(".local/share/Steam"),
    Path(".steam/steam"),
    Path(".steam/root"),
    Path(".steam/debian-installation"),
    Path(".var/app/com.valvesoftware.Steam/.local/share/Steam"),
    Path("snap/steam/common/.local/share/Steam"),
)
PRESENT = "présent"
ABSENT = "absent"
# Toutes interfaces, comme le serveur de jeu : l'invité vient d'une autre machine.
BIND_HOST = ""
PORT_KINDS = {"game": socket.SOCK_DGRAM, "udp": socket.SOCK_DGRAM, "rcon": socket.SOCK_STREAM}
PROTOCOL_NAMES = {socket.SOCK_DGRAM: "UDP", socket.SOCK_STREAM: "TCP"}
KIB_PER_MIB = 1024
INSTALL_HINT = "lancez `python3 -m service.main --install-mod`"


@dataclass(frozen=True)
class PzDetection:
    """`path` est le dossier absolu de `start-server.sh`, `None` si `status` vaut « absent »."""

    status: str
    path: Path | None
    message: str


@dataclass(frozen=True)
class PreflightResult:
    """`failures` porte un message par défaut ; `detail` rend les valeurs mesurées."""

    failures: tuple[str, ...]
    values: dict[str, Any]
    detail: str

    @property
    def ok(self) -> bool:
        return not self.failures


def _absent(reason: str) -> PzDetection:
    return PzDetection(ABSENT, None, f"PZ absent : {reason}")


def _accept(script: Path) -> PzDetection:
    if not os.access(script, os.X_OK):
        return _absent(f"{script} n'est pas exécutable ; corrigez avec `chmod +x`.")
    # Absolu : le serveur se lance avec ce dossier pour `cwd` et pour préfixe de son script.
    folder = script.parent.resolve()
    return PzDetection(PRESENT, folder, f"PZ présent : {folder}")


def detect_pz(pz_dir: Any, env: Mapping[str, str], home: Path) -> PzDetection:
    """Cherche `--pz-dir`, puis `PZ_INSTALL_DIR`, puis les chemins Steam connus.

    Un choix explicite invalide répond « absent » sans repli sur Steam (D19). Il peut
    désigner `ProjectZomboid/` comme `ProjectZomboid/projectzomboid/`. Une variable vide
    vaut une variable non définie.
    """
    label = "--pz-dir" if pz_dir is not None else ENV_VAR
    explicit = pz_dir if pz_dir is not None else env.get(ENV_VAR) or None
    if explicit is not None:
        try:
            folder = Path(explicit).expanduser()
        except RuntimeError as exc:
            return _absent(f"{explicit} ne se résout pas ({exc}) ; corrigez {label}.")
        for candidate in (folder / GAME_SUBDIR / SCRIPT, folder / SCRIPT):
            if os.path.isfile(candidate):
                return _accept(candidate)
        return _absent(
            f"{SCRIPT} introuvable dans {folder} ni dans son sous-dossier {GAME_SUBDIR}/ ; "
            f"corrigez {label}."
        )
    for root in STEAM_ROOTS:
        candidate = Path(home) / root / STEAM_GAME_DIR / GAME_SUBDIR / SCRIPT
        if os.path.isfile(candidate):
            return _accept(candidate)
    return _absent(
        f"{SCRIPT} introuvable dans les chemins Steam connus ; "
        f"indiquez le dossier du jeu avec --pz-dir ou {ENV_VAR}."
    )


def _bind(port: int, kind: int) -> None:
    with socket.socket(socket.AF_INET, kind) as sock:
        if kind == socket.SOCK_STREAM:
            # Un RCON tout juste arrêté laisse TIME_WAIT, pas un port occupé. En UDP, cette
            # option laisserait au contraire passer un port réellement lié.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((BIND_HOST, port))


def check_ports(ports: Mapping[str, Mapping[str, int]]) -> tuple[list[str], list[str]]:
    free: list[str] = []
    failures: list[str] = []
    for side, side_ports in ports.items():
        for name, port in side_ports.items():
            kind = PORT_KINDS[name]
            label = f"{port}/{PROTOCOL_NAMES[kind]}"
            try:
                _bind(port, kind)
            except OSError as exc:
                in_use = exc.errno == errno.EADDRINUSE
                state = "déjà lié" if in_use else f"inutilisable ({exc.strerror})"
                failures.append(
                    f"Port {label} ({name} du serveur {side}) {state} : "
                    "arrêtez le processus qui l'occupe."
                )
            else:
                free.append(label)
    return free, failures


def _read_mem_available_mb(meminfo: Path) -> int:
    for line in Path(meminfo).read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // KIB_PER_MIB
    raise ValueError("ligne MemAvailable absente")


def _check_ram(meminfo: Path, minimum_mb: int) -> tuple[int | None, list[str]]:
    try:
        available = _read_mem_available_mb(meminfo)
    except (OSError, ValueError, IndexError) as exc:
        return None, [f"RAM non mesurée : MemAvailable illisible dans {meminfo} ({exc})."]
    if available < minimum_mb:
        return available, [
            f"RAM insuffisante : MemAvailable vaut {available} Mo, il en faut au moins "
            f"{minimum_mb} Mo ; fermez des applications."
        ]
    return available, []


def mod_fingerprint(root: Path) -> str:
    """SHA-256 des chemins relatifs triés et des contenus, indépendant de l'emplacement."""
    root = Path(root)
    digest = hashlib.sha256()
    files = {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}
    for name in sorted(files):
        content = files[name].read_bytes()
        digest.update(f"{name}\0{len(content)}\0".encode("utf-8"))
        digest.update(content)
    return digest.hexdigest()


def _check_mod(zomboid_dir: Path, mod_src: Path, version: str) -> tuple[str | None, list[str]]:
    installed = zomboid_dir / "mods" / MOD_ID
    version_dir = installed / version
    if not version_dir.is_dir():
        return None, [
            f"Mod {MOD_ID} : sous-dossier de version {version} absent de {installed} ; "
            f"{INSTALL_HINT}."
        ]
    mod_info = version_dir / "mod.info"
    if not mod_info.is_file():
        return None, [f"Mod {MOD_ID} : {mod_info} absent ; {INSTALL_HINT}."]
    lua_dir = version_dir / "media" / "lua" / "server"
    if not lua_dir.is_dir():
        return None, [f"Mod {MOD_ID} : {lua_dir} absent ; {INSTALL_HINT}."]
    try:
        measured = mod_fingerprint(installed)
        expected = mod_fingerprint(mod_src)
    except OSError as exc:
        return None, [f"Mod {MOD_ID} : empreinte non calculable ({exc}) ; {INSTALL_HINT}."]
    if measured != expected:
        return measured, [
            f"Mod {MOD_ID} : empreinte {measured} différente de celle du dépôt {expected} ; "
            f"{INSTALL_HINT}."
        ]
    return measured, []


def _check_ini(server_dir: Path, sides: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    readable: list[str] = []
    failures: list[str] = []
    for side in sides:
        path = server_dir / f"duel{side}.ini"
        if not os.path.lexists(path):
            continue
        try:
            path.read_bytes()
        except OSError as exc:
            failures.append(f"Réglages illisibles : {path} ({exc.strerror}) ; corrigez ce fichier.")
        else:
            readable.append(path.name)
    return readable, failures


def _check_table(
    table: Any, effects: Mapping[str, Any], limits: Mapping[str, Any]
) -> tuple[str | None, list[str]]:
    try:
        return load_table(table, effects, limits)["id"], []
    except TableError as exc:
        return None, [f"Table invalide : {exc}"]


def _detail(values: Mapping[str, Any], failures: list[str]) -> str:
    parts = [
        f"PZ : {values['pz_dir'] or ABSENT}",
        f"ports libres : {', '.join(values['free_ports']) or 'aucun'}",
        f"RAM disponible : {values['mem_available_mb']} Mo (seuil {values['min_free_ram_mb']} Mo)",
        f"mod {MOD_ID} : {values['mod_fingerprint'] or 'non vérifié'}",
        f".ini lisibles : {', '.join(values['ini_readable']) or 'aucun présent'}",
        f"table : {values['table_id'] or 'invalide'}",
    ]
    return "\n".join([*failures, " · ".join(parts)])


def run_preflight(
    table: Any,
    *,
    runtime: Mapping[str, Any] | None = None,
    effects: Mapping[str, Any] | None = None,
    pz_dir: Any = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    zomboid_dir: Path | None = None,
    mod_src: Path = MOD_SRC,
    meminfo: Path = MEMINFO_PATH,
) -> PreflightResult:
    """Exécute tous les contrôles, sans s'arrêter au premier défaut."""
    runtime = load_runtime() if runtime is None else runtime
    effects = load_effects() if effects is None else effects
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    zomboid_dir = home / "Zomboid" if zomboid_dir is None else Path(zomboid_dir)

    detection = detect_pz(pz_dir, env, home)
    failures = [] if detection.path else [detection.message]
    free_ports, port_failures = check_ports(runtime["ports"])
    available_mb, ram_failures = _check_ram(meminfo, runtime["min_free_ram_mb"])
    fingerprint, mod_failures = _check_mod(zomboid_dir, Path(mod_src), runtime["mod_version_dir"])
    ini_readable, ini_failures = _check_ini(zomboid_dir / "Server", runtime["ports"])
    table_id, table_failures = _check_table(table, effects, runtime["limits"])
    failures += port_failures + ram_failures + mod_failures + ini_failures + table_failures

    values = {
        "pz_dir": str(detection.path) if detection.path else None,
        "free_ports": free_ports,
        "mem_available_mb": available_mb,
        "min_free_ram_mb": runtime["min_free_ram_mb"],
        "mod_fingerprint": fingerprint,
        "ini_readable": ini_readable,
        "table_id": table_id,
    }
    return PreflightResult(tuple(failures), values, _detail(values, failures))


def install_mod(src: Path, zomboid_dir: Path) -> Path:
    """Remplace `<zomboid_dir>/mods/duelcore/` par une copie de `src` et rend sa destination.

    La copie se prépare à côté de `mods/`, sur le même système de fichiers : rien d'autre
    que `duelcore/` n'apparaît dans `mods/`, et un échec de copie laisse l'ancien en place.
    """
    zomboid_dir = Path(zomboid_dir)
    destination = zomboid_dir / "mods" / MOD_ID
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{MOD_ID}-", dir=zomboid_dir) as stage:
        staged, previous = Path(stage) / "new", Path(stage) / "previous"
        shutil.copytree(src, staged)
        replaced = os.path.lexists(destination)
        if replaced:
            os.rename(destination, previous)
        try:
            os.rename(staged, destination)
        except OSError:
            if replaced:
                os.rename(previous, destination)
            raise
    return destination
