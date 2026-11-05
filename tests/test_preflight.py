"""Préflight réel : chaque défaut nommé, valeurs mesurées, installation du mod."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from service.preflight import (
    ABSENT,
    ENV_VAR,
    MOD_ID,
    MOD_SRC,
    PRESENT,
    SCRIPT,
    detect_pz,
    install_mod,
    mod_fingerprint,
    run_preflight,
)

ROOT = Path(__file__).resolve().parent.parent
PRESET = ROOT / "service" / "presets" / "duel_rapide.json"
LOOPBACK = "127.0.0.1"


def make_install(folder: Path, mode: int = 0o755) -> Path:
    folder.mkdir(parents=True)
    script = folder / SCRIPT
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(mode)
    return folder


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
    install_dir = make_install(tmp_path / "pz")
    install_mod(MOD_SRC, zomboid_home)
    table = json.loads(PRESET.read_text(encoding="utf-8"))

    def preflight(table=table, **overrides):
        options = {
            "runtime": runtime,
            "pz_dir": install_dir,
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
    assert SCRIPT in failure and str(no_script) in failure

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
    assert ready(meminfo=meminfo).values["mem_available_mb"] == 3072

    meminfo.write_text("MemTotal: 9999999 kB\n", encoding="ascii")
    result = ready(meminfo=meminfo)
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
    assert "media/lua/server" in failure and "--install-mod" in failure


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
    make_install(steam)
    from_env = make_install(tmp_path / "env")
    from_flag = make_install(tmp_path / "flag")
    env = {ENV_VAR: str(from_env)}

    assert detect_pz(from_flag, env, tmp_path).path == from_flag
    assert detect_pz(None, env, tmp_path).path == from_env
    found = detect_pz(None, {}, tmp_path)
    assert (found.status, found.path) == (PRESENT, steam)
    assert detect_pz(None, {ENV_VAR: ""}, tmp_path).path == steam


def test_game_subdir_is_followed(tmp_path):
    game = make_install(tmp_path / "ProjectZomboid" / "projectzomboid")
    assert detect_pz(tmp_path / "ProjectZomboid", {}, tmp_path).path == game
    assert detect_pz(game, {}, tmp_path).path == game


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
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )


def test_install_mod_option_installs_and_exits(tmp_path):
    done = run_main(tmp_path, "--install-mod")
    assert done.returncode == 0, done.stderr
    destination = tmp_path / "Zomboid" / "mods" / MOD_ID
    assert str(destination) in done.stdout
    assert mod_fingerprint(destination) in done.stdout


def test_invalid_pz_dir_option_stops_the_service(tmp_path):
    done = run_main(tmp_path, "--pz-dir", str(tmp_path / "nulle_part"), "--no-browser")
    assert done.returncode != 0
    assert "--pz-dir" in done.stderr and "PZ absent" in done.stderr
