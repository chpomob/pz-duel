"""Préflight du mode réel et installation du mod `duelcore`.

`run_preflight` ne lance rien et n'écrit rien : il mesure, et rend les valeurs relevées
avec un message français par défaut constaté, et pour chacun l'action que l'UI propose.
`install_mod` est le seul chemin qui écrit dans `~/Zomboid/mods/`, et seulement dans
`duelcore/`.

D-B : chaque branche Windows s'exécute sous Linux par un `platform` injecté. Les chemins
sondés restent des `Path` de l'hôte : ils désignent de vrais fichiers, pas un texte de
commande. D-C : `winreg` ne s'importe que dans le corps d'une fonction, `ctypes.windll` ne se
lit qu'à l'appel, et registre comme noyau s'injectent.

Un binaire n'a pas de terminal : si `paths.is_frozen()`, aucun message ne cite de commande
(AC29).
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import shutil
import socket
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from . import paths, platforms
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
PROGRAM_FILES_VAR = "ProgramFiles(x86)"
STEAM_DEFAULT_SUBDIR = "Steam"
LIBRARY_FOLDERS = Path("steamapps") / "libraryfolders.vdf"
# Ruche, clé, valeur, dans l'ordre de recherche.
STEAM_REGISTRY = (
    ("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
)
# Le dernier cas ne reconnaît qu'un guillemet sans fin : c'est la marque d'un fichier coupé.
VDF_TOKEN = re.compile(r'''"(?:[^"\\]|\\.)*"|[{}]|"''', re.DOTALL)
VDF_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
VDF_LIBRARY_DEPTH = 2
VDF_LEGACY_LIBRARY_DEPTH = 1
PRESENT = "présent"
ABSENT = "absent"
# Toutes interfaces, comme le serveur de jeu : l'invité vient d'une autre machine.
BIND_HOST = ""
PORT_KINDS = {"game": socket.SOCK_DGRAM, "udp": socket.SOCK_DGRAM, "rcon": socket.SOCK_STREAM}
PROTOCOL_NAMES = {socket.SOCK_DGRAM: "UDP", socket.SOCK_STREAM: "TCP"}
# `errno.EADDRINUSE` n'est pas le code que rend Winsock.
WSAEACCES = 10013
WSAEADDRINUSE = 10048
RESERVED_PORT_HINT = "stop the process holding it, or restart Windows if it reserved this port"
KIB_PER_MIB = 1024
BYTES_PER_MIB = KIB_PER_MIB * KIB_PER_MIB
INSTALL_HINT = "lancez `python3 -m service.main --install-mod`"
INSTALL_HINT_FROZEN = "use the Install mod button"
FOLDER_HINT_FROZEN = "choose the game folder in the app"
EXECUTABLE_HINT_FROZEN = "allow it to run as a program in its file properties"
# Ce que l'UI propose pour un défaut : aucune de ces actions ne passe par un terminal.
INSTALL_MOD = "install_mod"
CHOOSE_FOLDER = "choose_folder"
RETRY = "retry"


class _MemoryStatus(ctypes.Structure):
    """`MEMORYSTATUSEX`, en tailles fixes : `c_ulong` n'a pas la même sous Linux."""

    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


@dataclass(frozen=True)
class PzDetection:
    """`path` est le dossier absolu du script serveur, `None` si `status` vaut « absent »."""

    status: str
    path: Path | None
    message: str


@dataclass(frozen=True)
class Defect:
    """`kind` nomme le contrôle, `action` vaut `INSTALL_MOD`, `CHOOSE_FOLDER` ou `RETRY`."""

    kind: str
    message: str
    action: str


@dataclass(frozen=True)
class PreflightResult:
    """`failures` porte un message par défaut, `defects` les mêmes avec leur action ;
    `detail` rend les valeurs mesurées.
    """

    failures: tuple[str, ...]
    values: dict[str, Any]
    detail: str
    defects: tuple[Defect, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def _absent(reason: str) -> PzDetection:
    return PzDetection(ABSENT, None, f"PZ absent : {reason}")


def _install_hint() -> str:
    return INSTALL_HINT_FROZEN if paths.is_frozen() else INSTALL_HINT


def _folder_hint(from_terminal: str) -> str:
    return FOLDER_HINT_FROZEN if paths.is_frozen() else from_terminal


def _accept(folder: Path, platform: platforms.Platform) -> PzDetection:
    """Le dossier accepté est celui du jeu : il porte le programme client et de quoi servir."""
    if platform.posix_modes:
        script = folder / platform.server_script
        if not os.access(script, os.X_OK):
            hint = EXECUTABLE_HINT_FROZEN if paths.is_frozen() else "corrigez avec `chmod +x`"
            return _absent(f"{script} n'est pas exécutable ; {hint}.")
    # Absolu : le serveur se lance avec ce dossier pour `cwd` et pour racine de ses fichiers.
    resolved = folder.resolve()
    return PzDetection(PRESENT, resolved, f"PZ présent : {resolved}")


def parse_library_folders(text: str) -> tuple[str, ...]:
    """Rend les bibliothèques d'un `libraryfolders.vdf`, sans lever sur un texte abîmé.

    Une bibliothèque est la valeur `path` d'un bloc, ou, dans l'ancien format, une valeur à
    clé numérique. Un texte coupé ne rend rien : il ne dit plus quelles bibliothèques existent.
    """
    folders: list[str] = []
    depth = 0
    key = None
    for token in VDF_TOKEN.findall(text):
        if token == '"':
            return ()
        if token in ("{", "}"):
            depth += 1 if token == "{" else -1
            key = None
            if depth < 0:
                return ()
        elif key is None:
            key = token[1:-1]
        else:
            value = VDF_ESCAPE.sub(r"\1", token[1:-1])
            is_library = (depth == VDF_LIBRARY_DEPTH and key.lower() == "path") or (
                depth == VDF_LEGACY_LIBRARY_DEPTH and key.isdigit()
            )
            if is_library and value:
                folders.append(value)
            key = None
    return tuple(folders) if depth == 0 else ()


def steam_from_registry(registry: Any = None) -> list[str]:
    """Rend les dossiers de Steam enregistrés ; une clé illisible est une clé absente."""
    if registry is None:
        try:
            import winreg as registry
        except ImportError:
            return []
    found: list[str] = []
    for hive, key, name in STEAM_REGISTRY:
        try:
            with registry.OpenKey(getattr(registry, hive), key) as handle:
                value, _ = registry.QueryValueEx(handle, name)
        except OSError:
            continue
        if isinstance(value, str) and value:
            found.append(value)
    return found


def _steam_roots(env: Mapping[str, str], steam_locator: Callable[[], Iterable[Any]]) -> list[Path]:
    roots: list[Path] = []
    program_files = env.get(PROGRAM_FILES_VAR)
    if program_files:
        roots.append(Path(program_files) / STEAM_DEFAULT_SUBDIR)
    try:
        roots += [Path(root) for root in steam_locator()]
    except (OSError, ValueError):
        pass
    return roots


def _read_libraries(root: Path) -> tuple[str, ...]:
    try:
        text = (root / LIBRARY_FOLDERS).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ()
    return parse_library_folders(text)


def _missing_files(folder: Path, platform: platforms.Platform) -> list[str]:
    """Les fichiers qui manquent VRAIMENT, en noms affichables.

    Le client est toujours exigé : l'hôte joue sur la même machine. Le lancement serveur vient
    ensuite de la plateforme : le script fournisseur quand il est là, sinon le JRE que le jeu
    embarque (`server_files`).
    """
    required = ((platform.client_program,), *platform.server_files(folder))
    return [
        platform.path.join(*parts)
        for parts in required
        if not os.path.isfile(folder.joinpath(*parts))
    ]


def _search_windows(
    env: Mapping[str, str],
    steam_locator: Callable[[], Iterable[Any]],
    platform: platforms.Platform,
) -> PzDetection:
    """Sonde Steam, ses bibliothèques ensuite. `os.path.isfile` tient un disque déconnecté."""
    roots = _steam_roots(env, steam_locator)
    libraries = [Path(folder) for root in roots for folder in _read_libraries(root)]
    incomplete = None
    for steam in (*roots, *libraries):
        folder = steam / STEAM_GAME_DIR
        missing = _missing_files(folder, platform)
        if not missing:
            return _accept(folder, platform)
        if incomplete is None and len(missing) == 1:
            incomplete = f"{missing[0]} missing from {folder}"
    required_folder = (roots[0] if roots else Path(".")) / STEAM_GAME_DIR
    names = (*platform.server_requirement_names(required_folder), platform.client_program)
    reason = incomplete or f"{' and '.join(names)} not found in the known Steam folders"
    hint = _folder_hint(f"set the game folder with --pz-dir or {ENV_VAR}")
    return _absent(f"{reason}; {hint}.")


def _check_explicit(folder: Path, label: str, platform: platforms.Platform) -> PzDetection:
    if not platform.posix_modes:
        missing = _missing_files(folder, platform)
        if missing:
            return _absent(
                f"{' and '.join(missing)} missing from {folder}; {_folder_hint(f'fix {label}')}."
            )
        return _accept(folder, platform)
    for candidate in (folder / GAME_SUBDIR / SCRIPT, folder / SCRIPT):
        if os.path.isfile(candidate):
            return _accept(candidate.parent, platform)
    return _absent(
        f"{SCRIPT} introuvable dans {folder} ni dans son sous-dossier {GAME_SUBDIR}/ ; "
        f"{_folder_hint(f'corrigez {label}')}."
    )


def detect_pz(
    pz_dir: Any,
    env: Mapping[str, str],
    home: Path,
    *,
    platform: platforms.Platform | None = None,
    steam_locator: Callable[[], Iterable[Any]] | None = None,
) -> PzDetection:
    """Cherche `--pz-dir`, puis `PZ_INSTALL_DIR`, puis les chemins Steam connus.

    Un choix explicite invalide répond « absent » sans repli sur Steam (D19). Sous Linux, il
    peut désigner `ProjectZomboid/` comme `ProjectZomboid/projectzomboid/`. Une variable vide
    vaut une variable non définie.

    Sous Windows, les chemins Steam sont le dossier par défaut, les dossiers que rend
    `steam_locator` (le registre par défaut), puis leurs bibliothèques ; un dossier valide
    porte le script serveur et le programme client.
    """
    platform = platforms.current() if platform is None else platform
    label = "--pz-dir" if pz_dir is not None else ENV_VAR
    explicit = pz_dir if pz_dir is not None else env.get(ENV_VAR) or None
    if explicit is not None:
        try:
            folder = Path(explicit).expanduser()
        except RuntimeError as exc:
            hint = _folder_hint(f"corrigez {label}")
            return _absent(f"{explicit} ne se résout pas ({exc}) ; {hint}.")
        return _check_explicit(folder, label, platform)
    if not platform.posix_modes:
        return _search_windows(env, steam_locator or steam_from_registry, platform)
    for root in STEAM_ROOTS:
        candidate = Path(home) / root / STEAM_GAME_DIR / GAME_SUBDIR / SCRIPT
        if os.path.isfile(candidate):
            return _accept(candidate.parent, platform)
    hint = _folder_hint(f"indiquez le dossier du jeu avec --pz-dir ou {ENV_VAR}")
    return _absent(f"{SCRIPT} introuvable dans les chemins Steam connus ; {hint}.")


def _bind(port: int, kind: int, platform: platforms.Platform) -> None:
    with socket.socket(socket.AF_INET, kind) as sock:
        if platform.name == platforms.WINDOWS.name:
            # `SO_REUSEADDR` y laisserait lier un port tenu. La constante n'existe que sous
            # Windows : sous Linux, une plateforme injectée garde un `bind` nu.
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        elif kind == socket.SOCK_STREAM:
            # Un RCON tout juste arrêté laisse TIME_WAIT, pas un port occupé. En UDP, cette
            # option laisserait au contraire passer un port réellement lié.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((BIND_HOST, port))


def _port_defect(exc: OSError) -> str:
    """Rend l'état et le conseil. Python range 10013 en `errno` 13 : `winerror` prime."""
    code = getattr(exc, "winerror", None) or exc.errno
    if code in (errno.EADDRINUSE, WSAEADDRINUSE):
        return "déjà lié : arrêtez le processus qui l'occupe."
    advice = RESERVED_PORT_HINT if code == WSAEACCES else "arrêtez le processus qui l'occupe"
    return f"inutilisable ({exc.strerror}) : {advice}."


def check_ports(
    ports: Mapping[str, Mapping[str, int]], *, platform: platforms.Platform | None = None
) -> tuple[list[str], list[str]]:
    platform = platforms.current() if platform is None else platform
    free: list[str] = []
    failures: list[str] = []
    for side, side_ports in ports.items():
        for name, port in side_ports.items():
            kind = PORT_KINDS[name]
            label = f"{port}/{PROTOCOL_NAMES[kind]}"
            try:
                _bind(port, kind, platform)
            except OSError as exc:
                failures.append(f"Port {label} ({name} du serveur {side}) {_port_defect(exc)}")
            else:
                free.append(label)
    return free, failures


def _read_mem_available_mb(meminfo: Path) -> int:
    for line in Path(meminfo).read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // KIB_PER_MIB
    raise ValueError("ligne MemAvailable absente")


def read_windows_ram_mb(kernel: Any = None) -> int:
    kernel = ctypes.windll.kernel32 if kernel is None else kernel
    status = _MemoryStatus(dwLength=ctypes.sizeof(_MemoryStatus))
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx returned 0")
    return status.ullAvailPhys // BYTES_PER_MIB


def _check_ram(
    read: Callable[[], int], unreadable: str, minimum_mb: int
) -> tuple[int | None, list[str]]:
    try:
        available = read()
    # `AttributeError` : `ctypes.windll` manque à une plateforme injectée sans `ram_reader`.
    except (OSError, ValueError, IndexError, AttributeError) as exc:
        return None, [f"{unreadable} ({exc})."]
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
            f"{_install_hint()}."
        ]
    mod_info = version_dir / "mod.info"
    if not mod_info.is_file():
        return None, [f"Mod {MOD_ID} : {mod_info} absent ; {_install_hint()}."]
    lua_dir = version_dir / "media" / "lua" / "server"
    if not lua_dir.is_dir():
        return None, [f"Mod {MOD_ID} : {lua_dir} absent ; {_install_hint()}."]
    try:
        measured = mod_fingerprint(installed)
        expected = mod_fingerprint(mod_src)
    except OSError as exc:
        return None, [f"Mod {MOD_ID} : empreinte non calculable ({exc}) ; {_install_hint()}."]
    if measured != expected:
        return measured, [
            f"Mod {MOD_ID} : empreinte {measured} différente de celle du dépôt {expected} ; "
            f"{_install_hint()}."
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
    ram_reader: Callable[[], int] | None = None,
    platform: platforms.Platform | None = None,
    steam_locator: Callable[[], Iterable[Any]] | None = None,
) -> PreflightResult:
    """Exécute tous les contrôles, sans s'arrêter au premier défaut.

    `ram_reader` rend les Mo disponibles et lève `OSError` ou `ValueError` s'il échoue ; par
    défaut, c'est le système de `platform` qui répond.
    """
    runtime = load_runtime() if runtime is None else runtime
    effects = load_effects() if effects is None else effects
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    zomboid_dir = home / "Zomboid" if zomboid_dir is None else Path(zomboid_dir)
    platform = platforms.current() if platform is None else platform
    if platform.name == platforms.WINDOWS.name:
        default_reader = read_windows_ram_mb
        unreadable = "RAM not measured: GlobalMemoryStatusEx failed"
    else:
        default_reader = partial(_read_mem_available_mb, meminfo)
        unreadable = f"RAM non mesurée : MemAvailable illisible dans {meminfo}"

    detection = detect_pz(pz_dir, env, home, platform=platform, steam_locator=steam_locator)
    free_ports, port_failures = check_ports(runtime["ports"], platform=platform)
    available_mb, ram_failures = _check_ram(
        ram_reader or default_reader, unreadable, runtime["min_free_ram_mb"]
    )
    fingerprint, mod_failures = _check_mod(zomboid_dir, Path(mod_src), runtime["mod_version_dir"])
    ini_readable, ini_failures = _check_ini(zomboid_dir / "Server", runtime["ports"])
    table_id, table_failures = _check_table(table, effects, runtime["limits"])
    defects = tuple(
        Defect(kind, message, action)
        for kind, action, messages in (
            ("pz", CHOOSE_FOLDER, [] if detection.path else [detection.message]),
            ("port", RETRY, port_failures),
            ("ram", RETRY, ram_failures),
            ("mod", INSTALL_MOD, mod_failures),
            ("ini", RETRY, ini_failures),
            ("table", RETRY, table_failures),
        )
        for message in messages
    )
    failures = [defect.message for defect in defects]

    values = {
        "pz_dir": str(detection.path) if detection.path else None,
        "free_ports": free_ports,
        "mem_available_mb": available_mb,
        "min_free_ram_mb": runtime["min_free_ram_mb"],
        "mod_fingerprint": fingerprint,
        "ini_readable": ini_readable,
        "table_id": table_id,
    }
    return PreflightResult(tuple(failures), values, _detail(values, failures), defects)


class ModRestoreError(OSError):
    """Ancien mod resté hors de `mods/` : le message cite où il est et dit quoi en faire."""


def _swap_mod(src: Path, stage: Path, destination: Path, rename: Callable[..., None]) -> None:
    """Met une copie de `src` à la place de `destination`, ou y remet l'ancien contenu."""
    staged, previous = stage / "new", stage / "previous"
    shutil.copytree(src, staged)
    replaced = os.path.lexists(destination)
    if replaced:
        rename(destination, previous)
    try:
        rename(staged, destination)
    except OSError as exc:
        if replaced:
            try:
                rename(previous, destination)
            except OSError as undo_exc:
                raise ModRestoreError(
                    f"{exc} The previous mod could not be put back either ({undo_exc}). "
                    f"It is safe in {previous}: do not delete that folder. Close Project "
                    f"Zomboid, then move it to {destination}."
                ) from undo_exc
        raise


def install_mod(
    src: Path,
    zomboid_dir: Path,
    *,
    policy: Mapping[str, Any] | None = None,
    platform: platforms.Platform | None = None,
) -> Path:
    """Remplace `<zomboid_dir>/mods/duelcore/` par une copie de `src` et rend sa destination.

    La copie se prépare à côté de `mods/`, sur le même système de fichiers : rien d'autre
    que `duelcore/` n'apparaît dans `mods/`, et un échec de copie laisse l'ancien en place.

    Le dossier d'attente reçoit l'ancien mod : il ne s'efface que là où celui-ci est sûrement
    ailleurs, jamais par défaut. `ModRestoreError` le laisse donc en place, et le cite.
    `policy` et `platform` vont à `replace_file` : voir `service/platforms.py`.
    """
    zomboid_dir = Path(zomboid_dir)
    destination = zomboid_dir / "mods" / MOD_ID
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{MOD_ID}-", dir=zomboid_dir))
    rename = partial(platforms.replace_file, policy=policy, platform=platform)
    try:
        _swap_mod(src, stage, destination, rename)
    except ModRestoreError:
        raise
    except OSError:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    shutil.rmtree(stage, ignore_errors=True)
    return destination
