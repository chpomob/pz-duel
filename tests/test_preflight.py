"""Préflight réel : chaque défaut nommé, valeurs mesurées, installation du mod.

Les branches Windows s'exécutent ici par injection de la plateforme (D-B) ; leur pendant sur
le vrai système est dans `tests/test_platform_windows.py`.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from service import paths, preflight
from service.platforms import LINUX, WINDOWS, current
from service.preflight import (
    ABSENT,
    ENV_VAR,
    MOD_ID,
    MOD_SRC,
    PRESENT,
    PROGRAM_FILES_VAR,
    STEAM_REGISTRY,
    WSAEACCES,
    WSAEADDRINUSE,
    check_ports,
    detect_pz,
    install_mod,
    mod_fingerprint,
    parse_library_folders,
    read_windows_ram_mb,
    run_preflight,
    steam_from_registry,
)

ROOT = Path(__file__).resolve().parent.parent
PRESET = ROOT / "service" / "presets" / "duel_rapide.json"
LOOPBACK = "127.0.0.1"
STEAM_GAME_DIR = Path("steamapps") / "common" / "ProjectZomboid"
RAM_THRESHOLD_MB = 6144
MIB = 1024 * 1024
TERMINAL_WORDS = ("python", "--pz-dir", "--install-mod", "chmod")


@pytest.fixture
def zomboid_home(zomboid_home, monkeypatch):
    """`Path.home()` lit `USERPROFILE` sous Windows : `HOME` seul laisserait le vrai profil.

    `tests/conftest.py` reste tel quel : `test_contract`, `test_snapshots` et `test_modes`
    importent cette surcharge.
    """
    monkeypatch.setenv("USERPROFILE", str(zomboid_home.parent))
    return zomboid_home


def make_install(folder: Path, mode: int = 0o755, platform=None) -> Path:
    """Installation de `platform`, celle de l'hôte par défaut : son script s'y exécute."""
    platform = current() if platform is None else platform
    folder.mkdir(parents=True)
    script = folder / platform.server_script
    script.write_text("#!/bin/sh\n" if platform.posix_modes else "@echo off\r\n", encoding="utf-8")
    script.chmod(mode)
    if platform.client_in_game_dir:
        (folder / platform.client_program).write_bytes(b"MZ")
    return folder


def detect_on_linux(pz_dir, env, home) -> preflight.PzDetection:
    """Ces parcours décrivent la disposition de Steam sous Linux, quel que soit l'hôte."""
    return detect_pz(pz_dir, env, home, platform=LINUX)


def windows_env(tmp_path: Path) -> dict:
    return {PROGRAM_FILES_VAR: str(tmp_path / "Program Files (x86)")}


def default_steam(tmp_path: Path) -> Path:
    return tmp_path / "Program Files (x86)" / "Steam"


def detect_on_windows(tmp_path, pz_dir=None, env=None, steam_locator=list):
    env = windows_env(tmp_path) if env is None else env
    return detect_pz(pz_dir, env, tmp_path, platform=WINDOWS, steam_locator=steam_locator)


def library_vdf(*folders: Path) -> str:
    """Écrit le fichier comme Steam : blocs numérotés, `\\` échappés, sous-bloc `apps`."""
    blocks = "".join(
        f'''\t"{index}"
\t{{
\t\t"path"\t\t"{str(folder).replace(chr(92), chr(92) * 2)}"
\t\t"label"\t\t""
\t\t"apps"
\t\t{{
\t\t\t"108600"\t\t"1234"
\t\t}}
\t}}
'''
        for index, folder in enumerate(folders)
    )
    return f'"libraryfolders"\n{{\n{blocks}}}\n'


def write_vdf(steam: Path, content: str) -> None:
    target = steam / "steamapps" / "libraryfolders.vdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def free_ports(runtime: dict) -> dict:
    """Ports libres de cette machine, à la forme de `runtime["ports"]`.

    Les sockets restent ouverts jusqu'à la fin de l'attribution : deux entrées ne peuvent
    pas recevoir le même port.
    """
    kinds = {"game": socket.SOCK_DGRAM, "udp": socket.SOCK_DGRAM, "rcon": socket.SOCK_STREAM}
    ports: dict = {}
    with ExitStack() as stack:
        for side, side_ports in runtime["ports"].items():
            ports[side] = {}
            for name in side_ports:
                sock = stack.enter_context(socket.socket(socket.AF_INET, kinds[name]))
                sock.bind(("", 0))
                ports[side][name] = sock.getsockname()[1]
    return ports


@pytest.fixture
def ready(zomboid_home, runtime, tmp_path):
    """Machine sans défaut, ports libres et seuil de RAM pris dans le `runtime` de test."""
    runtime["ports"] = free_ports(runtime)
    runtime["min_free_ram_mb"] = 1
    host = current()
    guest = WINDOWS if host is LINUX else LINUX
    installs = {
        host: make_install(tmp_path / "pz", platform=host),
        guest: make_install(tmp_path / "pz-guest", platform=guest),
    }
    install_mod(MOD_SRC, zomboid_home)
    table = json.loads(PRESET.read_text(encoding="utf-8"))

    def preflight(table=table, **overrides):
        options = {
            "runtime": runtime,
            "pz_dir": installs[overrides.get("platform", host)],
            "env": {},
            "home": tmp_path,
            "zomboid_dir": zomboid_home,
            **overrides,
        }
        return run_preflight(table, **options)

    return preflight


def test_each_failure_is_named(ready, zomboid_home, runtime, tmp_path):
    nominal = ready()
    assert nominal.ok and nominal.failures == ()
    values = nominal.values
    assert values["pz_dir"] == str(tmp_path / "pz")
    assert len(values["free_ports"]) == 6
    assert values["mem_available_mb"] >= runtime["min_free_ram_mb"]
    assert values["mod_fingerprint"] == mod_fingerprint(MOD_SRC)
    for shown in (
        values["pz_dir"],
        *values["free_ports"],
        f"{values['mem_available_mb']} Mo",
        values["mod_fingerprint"],
    ):
        assert shown in nominal.detail

    no_script = tmp_path / "sans_script"
    no_script.mkdir()
    (failure,) = ready(pz_dir=no_script).failures
    assert current().server_script in failure and str(no_script) in failure

    port = runtime["ports"]["A"]["game"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as held:
        held.bind((LOOPBACK, port))
        result = ready()
    (failure,) = result.failures
    assert f"Port {port}/UDP" in failure and "déjà lié" in failure
    assert not result.ok

    above = nominal.values["mem_available_mb"] * 1000
    (failure,) = ready(runtime={**runtime, "min_free_ram_mb": above}).failures
    assert "MemAvailable" in failure and f"{above} Mo" in failure

    version = runtime["mod_version_dir"]
    shutil.rmtree(zomboid_home / "mods" / MOD_ID / version)
    (failure,) = ready().failures
    assert f"sous-dossier de version {version}" in failure and "--install-mod" in failure

    install_mod(MOD_SRC, zomboid_home)
    assert ready().ok


def test_rcon_port_is_bound_in_tcp(ready, runtime):
    port = runtime["ports"]["B"]["rcon"]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((LOOPBACK, port))
        held.listen()
        (failure,) = ready().failures
    assert f"Port {port}/TCP" in failure and "serveur B" in failure


def test_ram_read_from_meminfo(ready, runtime, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 9999999 kB\nMemAvailable: 3145728 kB\n", encoding="ascii")
    assert ready(meminfo=meminfo, platform=LINUX).values["mem_available_mb"] == 3072

    meminfo.write_text("MemTotal: 9999999 kB\n", encoding="ascii")
    result = ready(meminfo=meminfo, platform=LINUX)
    assert result.values["mem_available_mb"] is None
    assert "MemAvailable" in result.failures[0]


def test_mod_content_is_checked(ready, zomboid_home, runtime):
    installed = zomboid_home / "mods" / MOD_ID
    version_dir = installed / runtime["mod_version_dir"]

    lua = version_dir / "media" / "lua" / "server" / "DuelCore.lua"
    lua.write_text(lua.read_text(encoding="utf-8") + "-- retouche\n", encoding="utf-8")
    (failure,) = ready().failures
    assert "empreinte" in failure and "--install-mod" in failure

    install_mod(MOD_SRC, zomboid_home)
    (installed / "extra.txt").write_text("", encoding="utf-8")
    assert "empreinte" in ready().failures[0]

    install_mod(MOD_SRC, zomboid_home)
    (version_dir / "mod.info").unlink()
    (failure,) = ready().failures
    assert "mod.info" in failure and "--install-mod" in failure

    install_mod(MOD_SRC, zomboid_home)
    shutil.rmtree(version_dir / "media" / "lua" / "server")
    (failure,) = ready().failures
    assert str(Path("media/lua/server")) in failure and "--install-mod" in failure


def test_fingerprint_covers_paths_and_contents(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    (first / "a.txt").write_text("x", encoding="utf-8")
    moved = tmp_path / "elsewhere" / "copy"
    shutil.copytree(first, moved)
    assert mod_fingerprint(first) == mod_fingerprint(moved)

    (moved / "a.txt").rename(moved / "b.txt")
    assert mod_fingerprint(first) != mod_fingerprint(moved)


def test_present_ini_must_be_readable(ready, zomboid_home):
    assert ready().values["ini_readable"] == []

    (zomboid_home / "Server" / "duelA.ini").write_text("Public=false\n", encoding="utf-8")
    assert ready().values["ini_readable"] == ["duelA.ini"]

    (zomboid_home / "Server" / "duelB.ini").mkdir()
    (failure,) = ready().failures
    assert "duelB.ini" in failure


def test_invalid_table_is_named(ready):
    table = json.loads(PRESET.read_text(encoding="utf-8"))
    table["mods"] = ["autre"]
    result = ready(table=table)
    (failure,) = result.failures
    assert failure.startswith("Table invalide") and "mods" in failure
    assert result.values["table_id"] is None


def test_all_failures_are_reported_together(ready, tmp_path, zomboid_home):
    shutil.rmtree(zomboid_home / "mods" / MOD_ID)
    result = ready(pz_dir=tmp_path / "nulle_part", table={})
    assert len(result.failures) == 3


def test_detection_order(tmp_path):
    steam = tmp_path / ".local/share/Steam/steamapps/common/ProjectZomboid/projectzomboid"
    make_install(steam, platform=LINUX)
    from_env = make_install(tmp_path / "env", platform=LINUX)
    from_flag = make_install(tmp_path / "flag", platform=LINUX)
    env = {ENV_VAR: str(from_env)}

    assert detect_on_linux(from_flag, env, tmp_path).path == from_flag
    assert detect_on_linux(None, env, tmp_path).path == from_env
    found = detect_on_linux(None, {}, tmp_path)
    assert (found.status, found.path) == (PRESENT, steam)
    assert detect_on_linux(None, {ENV_VAR: ""}, tmp_path).path == steam


def test_game_subdir_is_followed(tmp_path):
    game = make_install(tmp_path / "ProjectZomboid" / "projectzomboid", platform=LINUX)
    assert detect_on_linux(tmp_path / "ProjectZomboid", {}, tmp_path).path == game
    assert detect_on_linux(game, {}, tmp_path).path == game


def test_steam_root_without_subdir_is_absent(tmp_path):
    make_install(tmp_path / ".local/share/Steam/steamapps/common/ProjectZomboid")
    assert detect_pz(None, {}, tmp_path).status == ABSENT


def test_invalid_explicit_choice_has_no_fallback(tmp_path):
    make_install(tmp_path / ".local/share/Steam/steamapps/common/ProjectZomboid/projectzomboid")
    empty = tmp_path / "empty"
    empty.mkdir()

    from_flag = detect_pz(empty, {}, tmp_path)
    assert from_flag.status == ABSENT and "--pz-dir" in from_flag.message

    from_env = detect_pz(None, {ENV_VAR: str(empty)}, tmp_path)
    assert from_env.status == ABSENT and ENV_VAR in from_env.message


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="no execute bit; replaced by test_windows_files_need_no_execute_bit",
)
def test_script_must_be_executable(tmp_path):
    folder = make_install(tmp_path / "pz", mode=0o644)
    detection = detect_pz(folder, {}, tmp_path)
    assert detection.status == ABSENT and "exécutable" in detection.message


def snapshot(root: Path) -> dict:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def test_install_touches_only_duelcore(zomboid_home):
    mods = zomboid_home / "mods"
    (mods / "duelsidea" / "42").mkdir(parents=True)
    (mods / "duelsidea" / "42" / "mod.info").write_text("id=duelsidea\n", encoding="utf-8")
    (mods / "default.txt").write_text("mods\n", encoding="utf-8")
    stale = mods / MOD_ID / "obsolete.lua"
    stale.parent.mkdir()
    stale.write_text("-- ancien\n", encoding="utf-8")
    before = {k: v for k, v in snapshot(mods).items() if not k.startswith(MOD_ID)}
    zomboid_before = sorted(path.name for path in zomboid_home.iterdir())

    destination = install_mod(MOD_SRC, zomboid_home)

    assert destination == mods / MOD_ID
    assert not stale.exists()
    assert mod_fingerprint(destination) == mod_fingerprint(MOD_SRC)
    assert {k: v for k, v in snapshot(mods).items() if not k.startswith(MOD_ID)} == before
    assert sorted(path.name for path in zomboid_home.iterdir()) == zomboid_before


def test_failed_copy_keeps_previous_mod(zomboid_home, tmp_path):
    install_mod(MOD_SRC, zomboid_home)
    before = snapshot(zomboid_home)
    with pytest.raises(OSError):
        install_mod(tmp_path / "sans_source", zomboid_home)
    assert snapshot(zomboid_home) == before


def run_main(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "service.main", *args],
        cwd=ROOT,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
        capture_output=True,
        text=True,
    )


def test_install_mod_option_installs_and_exits(tmp_path):
    done = run_main(tmp_path, "--install-mod")
    assert done.returncode == 0, done.stderr
    destination = tmp_path / "Zomboid" / "mods" / MOD_ID
    assert str(destination) in done.stdout
    assert mod_fingerprint(destination) in done.stdout


@pytest.mark.parametrize("through", ["option", "variable"])
def test_relative_install_is_launchable(tmp_path, monkeypatch, through):
    """Relatif, le chemin se résoudrait deux fois : `cwd`, puis le script sous ce `cwd`."""
    install = make_install(tmp_path / "jeu" / "pz")
    monkeypatch.chdir(tmp_path)
    if through == "option":
        detection = detect_pz("jeu/pz", {}, tmp_path)
    else:
        detection = detect_pz(None, {ENV_VAR: "jeu/pz"}, tmp_path)

    assert detection.status == PRESENT
    assert detection.path == install and detection.path.is_absolute()
    script = detection.path / current().server_script
    done = subprocess.run([str(script)], cwd=detection.path, check=False)
    assert done.returncode == 0


def test_preflight_returns_an_absolute_install(ready, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ready(pz_dir="pz").values["pz_dir"] == str(tmp_path / "pz")
    assert ready(pz_dir=None, env={ENV_VAR: "pz"}).values["pz_dir"] == str(tmp_path / "pz")


def test_unresolvable_install_fails_the_preflight(ready):
    (failure,) = ready(pz_dir="~utilisateur-inconnu-du-systeme/pz").failures
    assert "PZ absent" in failure and "--pz-dir" in failure


def test_invalid_pz_dir_option_stops_the_service(tmp_path):
    done = run_main(tmp_path, "--pz-dir", str(tmp_path / "nulle_part"), "--no-browser")
    assert done.returncode != 0
    assert "--pz-dir" in done.stderr and "PZ absent" in done.stderr


# R1 : PZ sous Windows, plateforme injectée --------------------------------------------------


def test_windows_default_steam_folder_is_found(tmp_path):
    game = make_install(default_steam(tmp_path) / STEAM_GAME_DIR, platform=WINDOWS)
    found = detect_on_windows(tmp_path)
    assert (found.status, found.path) == (PRESENT, game)


@pytest.mark.parametrize("removed", [WINDOWS.client_program, WINDOWS.server_script])
def test_windows_folder_needs_both_files(tmp_path, removed):
    game = make_install(default_steam(tmp_path) / STEAM_GAME_DIR, platform=WINDOWS)
    (game / removed).unlink()
    detection = detect_on_windows(tmp_path)
    assert (detection.status, detection.path) == (ABSENT, None)
    assert removed in detection.message


def test_windows_explicit_folder_names_the_missing_file(tmp_path):
    game = make_install(tmp_path / "pz", platform=WINDOWS)
    (game / WINDOWS.client_program).unlink()
    detection = detect_on_windows(tmp_path, pz_dir=game)
    assert detection.status == ABSENT
    assert WINDOWS.client_program in detection.message and "--pz-dir" in detection.message


def test_windows_files_need_no_execute_bit(tmp_path):
    game = make_install(tmp_path / "pz", mode=0o644, platform=WINDOWS)
    assert detect_on_windows(tmp_path, pz_dir=game).path == game


def test_windows_linux_layout_is_not_an_install(tmp_path):
    make_install(tmp_path / "pz" / "projectzomboid")
    assert detect_on_windows(tmp_path, pz_dir=tmp_path / "pz").status == ABSENT


def test_windows_third_library_holds_the_game(tmp_path):
    steam = default_steam(tmp_path)
    (steam / STEAM_GAME_DIR).mkdir(parents=True)
    libraries = [tmp_path / "lib1", tmp_path / "lib2", tmp_path / "lib3"]
    for library in libraries[:2]:
        (library / STEAM_GAME_DIR).mkdir(parents=True)
    game = make_install(libraries[2] / STEAM_GAME_DIR, platform=WINDOWS)
    write_vdf(steam, library_vdf(*libraries))

    found = detect_on_windows(tmp_path)

    assert (found.status, found.path) == (PRESENT, game)


def test_windows_incomplete_folder_does_not_stop_the_search(tmp_path):
    steam = default_steam(tmp_path)
    partial = make_install(steam / STEAM_GAME_DIR, platform=WINDOWS)
    (partial / WINDOWS.client_program).unlink()
    game = make_install(tmp_path / "lib" / STEAM_GAME_DIR, platform=WINDOWS)
    write_vdf(steam, library_vdf(tmp_path / "lib"))
    assert detect_on_windows(tmp_path).path == game


def test_windows_disconnected_library_is_skipped(tmp_path):
    steam = default_steam(tmp_path)
    game = make_install(tmp_path / "lib" / STEAM_GAME_DIR, platform=WINDOWS)
    write_vdf(steam, library_vdf(tmp_path / "disque-debranche", tmp_path / "lib"))
    assert detect_on_windows(tmp_path).path == game


def test_windows_search_order(tmp_path):
    """Dossier par défaut, puis Steam enregistré, puis bibliothèques."""
    default = make_install(default_steam(tmp_path) / STEAM_GAME_DIR, platform=WINDOWS)
    registered = tmp_path / "registered"
    from_registry = make_install(registered / STEAM_GAME_DIR, platform=WINDOWS)
    from_library = make_install(tmp_path / "lib" / STEAM_GAME_DIR, platform=WINDOWS)
    write_vdf(default_steam(tmp_path), library_vdf(tmp_path / "lib"))

    def locate():
        return [str(registered)]

    assert detect_on_windows(tmp_path, steam_locator=locate).path == default
    shutil.rmtree(default)
    assert detect_on_windows(tmp_path, steam_locator=locate).path == from_registry
    shutil.rmtree(from_registry)
    assert detect_on_windows(tmp_path, steam_locator=locate).path == from_library


def test_windows_registered_steam_declares_libraries(tmp_path):
    registered = tmp_path / "registered"
    game = make_install(tmp_path / "lib" / STEAM_GAME_DIR, platform=WINDOWS)
    write_vdf(registered, library_vdf(tmp_path / "lib"))
    found = detect_on_windows(tmp_path, env={}, steam_locator=lambda: [str(registered)])
    assert found.path == game


def test_windows_without_program_files_variable_is_absent(tmp_path):
    assert detect_on_windows(tmp_path, env={}).status == ABSENT


def test_windows_default_locator_is_silent_without_winreg(tmp_path):
    """Sous Linux, `winreg` manque : la plateforme injectée reste sans exception."""
    game = make_install(default_steam(tmp_path) / STEAM_GAME_DIR, platform=WINDOWS)
    found = detect_pz(None, windows_env(tmp_path), tmp_path, platform=WINDOWS)
    assert found.path == game


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(lambda text: "", id="vide"),
        pytest.param(lambda text: text[: len(text) // 2], id="coupe-au-milieu"),
        pytest.param(lambda text: text[: text.index("lib3")], id="coupe-dans-un-chemin"),
        pytest.param(lambda text: text.rstrip()[:-1], id="sans-derniere-accolade"),
        pytest.param(lambda text: text + "}", id="accolade-en-trop"),
        pytest.param(lambda text: "\x00\xff binaire", id="binaire"),
    ],
)
def test_windows_damaged_vdf_gives_absent(tmp_path, broken):
    steam = default_steam(tmp_path)
    libraries = [tmp_path / "lib1", tmp_path / "lib2", tmp_path / "lib3"]
    for library in libraries:
        make_install(library / STEAM_GAME_DIR, platform=WINDOWS)
    write_vdf(steam, broken(library_vdf(*libraries)))

    detection = detect_on_windows(tmp_path)

    assert (detection.status, detection.path) == (ABSENT, None)


def test_windows_missing_vdf_gives_absent(tmp_path):
    default_steam(tmp_path).mkdir(parents=True)
    assert detect_on_windows(tmp_path).status == ABSENT


def test_windows_undecodable_vdf_gives_absent(tmp_path):
    target = default_steam(tmp_path) / "steamapps" / "libraryfolders.vdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xfe\x00")
    assert detect_on_windows(tmp_path).status == ABSENT


@pytest.mark.parametrize("error", [PermissionError(13, "denied"), OSError(5, "unreadable")])
def test_windows_unreadable_registry_gives_absent(tmp_path, error):
    def locate():
        raise error

    assert detect_on_windows(tmp_path, steam_locator=locate).status == ABSENT


def test_vdf_escapes_are_undone():
    text = r'''"libraryfolders" { "0" { "path" "D:\\Steam Library\\\"x\"" } }'''
    assert parse_library_folders(text) == ('D:\\Steam Library\\"x"',)


def test_vdf_reads_the_former_format():
    text = r'''"LibraryFolders" { "TimeNextStatsReport" "1" "1" "D:\\Games" "2" "E:\\Mo" }'''
    assert parse_library_folders(text) == ("D:\\Games", "E:\\Mo")


def test_vdf_ignores_what_is_not_a_library():
    text = '''"libraryfolders" {
        "contentstatsid" "123"
        "0" { "label" "path" "path" "" "apps" { "path" "/nested" "1" "/numbered" } }
    }'''
    assert parse_library_folders(text) == ()


class FakeRegistry:
    """Double de `winreg` : `values` associe (ruche, clé, valeur) à une donnée ou à une erreur."""

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, values: dict):
        self.values = values

    @contextmanager
    def OpenKey(self, hive, key):
        if not any(known[:2] == (hive, key) for known in self.values):
            raise FileNotFoundError(2, "no such key")
        yield (hive, key)

    def QueryValueEx(self, handle, name):
        value = self.values[(*handle, name)]
        if isinstance(value, OSError):
            raise value
        return value, 1


USER_KEY = ("HKCU", STEAM_REGISTRY[0][1], "SteamPath")
MACHINE_KEY = ("HKLM", STEAM_REGISTRY[1][1], "InstallPath")


def test_registry_keys_are_the_steam_ones():
    assert STEAM_REGISTRY == (
        ("HKEY_CURRENT_USER", "Software\\Valve\\Steam", "SteamPath"),
        ("HKEY_LOCAL_MACHINE", "SOFTWARE\\WOW6432Node\\Valve\\Steam", "InstallPath"),
    )


def test_registry_user_key_comes_first():
    registry = FakeRegistry({MACHINE_KEY: "C:\\machine", USER_KEY: "c:/user"})
    assert steam_from_registry(registry) == ["c:/user", "C:\\machine"]


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({}, id="sans-cle"),
        pytest.param({USER_KEY: PermissionError(13, "denied")}, id="refusee"),
        pytest.param({USER_KEY: 7}, id="pas-un-texte"),
        pytest.param({USER_KEY: ""}, id="vide"),
    ],
)
def test_registry_unreadable_user_key_leaves_the_machine_key(values):
    assert steam_from_registry(FakeRegistry(values)) == []
    registry = FakeRegistry({**values, MACHINE_KEY: "C:\\machine"})
    assert steam_from_registry(registry) == ["C:\\machine"]


@pytest.mark.parametrize("through", ["option", "variable"])
def test_windows_invalid_explicit_choice_has_no_fallback(tmp_path, through):
    steam_game = make_install(default_steam(tmp_path) / STEAM_GAME_DIR, platform=WINDOWS)
    empty = tmp_path / "empty"
    empty.mkdir()
    env = windows_env(tmp_path)
    if through == "variable":
        env[ENV_VAR] = str(empty)

    detection = detect_on_windows(tmp_path, pz_dir=empty if through == "option" else None, env=env)

    assert (detection.status, detection.path) == (ABSENT, None)
    assert str(steam_game) not in detection.message


def test_windows_option_wins_over_variable(tmp_path):
    from_flag = make_install(tmp_path / "flag", platform=WINDOWS)
    from_env = make_install(tmp_path / "env", platform=WINDOWS)
    env = {**windows_env(tmp_path), ENV_VAR: str(from_env)}
    assert detect_on_windows(tmp_path, pz_dir=from_flag, env=env).path == from_flag
    assert detect_on_windows(tmp_path, env=env).path == from_env


def test_preflight_gives_platform_and_locator_to_the_detection(ready, tmp_path):
    registered = tmp_path / "registered"
    game = make_install(registered / STEAM_GAME_DIR, platform=WINDOWS)
    result = ready(
        platform=WINDOWS,
        pz_dir=None,
        steam_locator=lambda: [str(registered)],
        ram_reader=lambda: 1,
    )
    assert result.ok and result.values["pz_dir"] == str(game)


# R1 : RAM -----------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", [LINUX, WINDOWS], ids=["linux", "windows"])
@pytest.mark.parametrize(
    ("measured", "failures"), [(RAM_THRESHOLD_MB - 1, 1), (RAM_THRESHOLD_MB, 0)]
)
def test_ram_threshold_is_exact(ready, runtime, platform, measured, failures):
    limited = {**runtime, "min_free_ram_mb": RAM_THRESHOLD_MB}
    result = ready(runtime=limited, platform=platform, ram_reader=lambda: measured)
    assert len(result.failures) == failures
    assert result.values["mem_available_mb"] == measured
    if failures:
        assert result.failures[0].startswith("RAM insuffisante")
        assert f"{RAM_THRESHOLD_MB} Mo" in result.failures[0]


@pytest.mark.parametrize("platform", [LINUX, WINDOWS], ids=["linux", "windows"])
@pytest.mark.parametrize("error", [OSError("no measure"), ValueError("no measure")])
def test_failed_ram_measure_is_one_failure(ready, platform, error):
    def read():
        raise error

    result = ready(platform=platform, ram_reader=read)
    (failure,) = result.failures
    assert "RAM" in failure and "no measure" in failure
    assert result.values["mem_available_mb"] is None


class FakeKernel:
    """Double de `kernel32` : remplit la structure reçue, comme le vrai appel."""

    def __init__(self, available_bytes: int, succeeds: bool = True):
        self.available_bytes = available_bytes
        self.succeeds = succeeds
        self.length = None

    def GlobalMemoryStatusEx(self, reference) -> int:
        status = reference._obj
        self.length = status.dwLength
        status.ullAvailPhys = self.available_bytes
        return int(self.succeeds)


def test_windows_ram_is_read_in_mib():
    kernel = FakeKernel(RAM_THRESHOLD_MB * MIB + MIB - 1)
    assert read_windows_ram_mb(kernel) == RAM_THRESHOLD_MB
    # Taille de `MEMORYSTATUSEX` : le vrai appel échoue sur toute autre valeur.
    assert kernel.length == 64


def test_windows_ram_failure_raises():
    with pytest.raises(OSError, match="GlobalMemoryStatusEx"):
        read_windows_ram_mb(FakeKernel(0, succeeds=False))


def test_windows_default_ram_reader_is_the_system_one(ready, monkeypatch):
    monkeypatch.setattr(preflight, "read_windows_ram_mb", lambda: 7000)
    assert ready(platform=WINDOWS).values["mem_available_mb"] == 7000


def test_windows_without_kernel_is_a_failed_measure(ready, monkeypatch):
    monkeypatch.delattr(ctypes, "windll", raising=False)
    result = ready(platform=WINDOWS)
    (failure,) = result.failures
    assert "GlobalMemoryStatusEx" in failure
    assert result.values["mem_available_mb"] is None


# R1 : ports ---------------------------------------------------------------------------------


class RecordingSocket:
    """Double de `socket.socket` : retient les options posées, et peut refuser le `bind`."""

    instances: list = []
    bind_error: OSError | None = None

    def __init__(self, family, kind):
        self.kind = kind
        self.options: list = []
        RecordingSocket.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def setsockopt(self, level, option, value):
        self.options.append(option)

    def bind(self, address):
        if RecordingSocket.bind_error is not None:
            raise RecordingSocket.bind_error


@pytest.fixture
def recording_socket(monkeypatch):
    monkeypatch.setattr(RecordingSocket, "instances", [])
    monkeypatch.setattr(RecordingSocket, "bind_error", None)
    monkeypatch.setattr(socket, "socket", RecordingSocket)
    # Valeur de Winsock : `~SO_REUSEADDR`.
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)
    return RecordingSocket


PORTS = {"A": {"game": 17261, "rcon": 27015}}


def test_windows_bind_is_exclusive_on_both_protocols(recording_socket):
    free, failures = check_ports(PORTS, platform=WINDOWS)
    assert (free, failures) == (["17261/UDP", "27015/TCP"], [])
    assert [sock.options for sock in recording_socket.instances] == [[-5], [-5]]


def test_linux_bind_keeps_its_options(recording_socket):
    check_ports(PORTS, platform=LINUX)
    udp, tcp = recording_socket.instances
    assert (udp.options, tcp.options) == ([], [socket.SO_REUSEADDR])


class WinsockError(OSError):
    """Forme d'une erreur Winsock réelle : `winerror` n'existe sous Linux que par ce double."""

    def __init__(self, code: int, mapped_errno: int, text: str):
        super().__init__(mapped_errno, text)
        self.winerror = code


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(WSAEADDRINUSE, "in use"), id="errno"),
        pytest.param(WinsockError(WSAEADDRINUSE, WSAEADDRINUSE, "in use"), id="winerror"),
        pytest.param(OSError(errno.EADDRINUSE, "in use"), id="posix"),
    ],
)
def test_port_in_use_is_named(recording_socket, error):
    recording_socket.bind_error = error
    free, failures = check_ports({"B": {"rcon": 27016}}, platform=WINDOWS)
    (failure,) = failures
    assert free == []
    assert "Port 27016/TCP" in failure and "serveur B" in failure and "déjà lié" in failure


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(WSAEACCES, "forbidden"), id="errno"),
        # Python range WSAEACCES en `errno` 13 : seul `winerror` le distingue.
        pytest.param(WinsockError(WSAEACCES, errno.EACCES, "forbidden"), id="winerror"),
    ],
)
def test_reserved_port_is_unusable(recording_socket, error):
    recording_socket.bind_error = error
    free, failures = check_ports({"A": {"game": 17261}}, platform=WINDOWS)
    (failure,) = failures
    assert free == []
    assert "Port 17261/UDP" in failure and "inutilisable (forbidden)" in failure
    assert "restart Windows" in failure


def test_other_port_error_keeps_its_message(recording_socket):
    recording_socket.bind_error = OSError(errno.EACCES, "denied")
    (failure,) = check_ports({"A": {"game": 17261}}, platform=LINUX)[1]
    assert "inutilisable (denied)" in failure and "arrêtez le processus" in failure


# R1 : mod et messages -----------------------------------------------------------------------


def test_install_creates_a_missing_zomboid_folder(tmp_path):
    """Sur une machine où PZ n'a jamais tourné, `Zomboid/` n'existe pas encore."""
    zomboid = tmp_path / "jamais-lance" / "Zomboid"
    destination = install_mod(MOD_SRC, zomboid)
    assert destination == zomboid / "mods" / MOD_ID
    assert mod_fingerprint(destination) == mod_fingerprint(MOD_SRC)


def assert_no_terminal(message: str, tmp_path: Path) -> None:
    shown = message.replace(str(tmp_path), "")
    assert not [word for word in TERMINAL_WORDS if word in shown], message


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(paths, "is_frozen", lambda: True)


@pytest.mark.usefixtures("frozen")
def test_frozen_detection_messages_cite_no_terminal(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    detections = [
        detect_pz(empty, {}, tmp_path),
        detect_pz(None, {ENV_VAR: str(empty)}, tmp_path),
        detect_pz(None, {}, tmp_path),
        detect_pz("~utilisateur-inconnu-du-systeme/pz", {}, tmp_path),
        detect_on_windows(tmp_path, pz_dir=empty),
        detect_on_windows(tmp_path),
    ]
    if current().posix_modes:
        not_executable = make_install(tmp_path / "pz", mode=0o644)
        detections.append(detect_pz(not_executable, {}, tmp_path))
    for detection in detections:
        assert detection.status == ABSENT
        assert_no_terminal(detection.message, tmp_path)


@pytest.mark.usefixtures("frozen")
def test_frozen_mod_messages_cite_no_terminal(ready, zomboid_home, runtime, tmp_path):
    installed = zomboid_home / "mods" / MOD_ID
    version_dir = installed / runtime["mod_version_dir"]

    (installed / "extra.txt").write_text("", encoding="utf-8")
    (version_dir / "mod.info").rename(version_dir / "mod.info.bak")
    (missing_info,) = ready().failures
    (version_dir / "mod.info.bak").rename(version_dir / "mod.info")
    (other_content,) = ready().failures
    shutil.rmtree(version_dir)
    (missing_version,) = ready().failures

    for failure in (missing_info, other_content, missing_version):
        assert_no_terminal(failure, tmp_path)
