"""Snapshots : restauration exacte, gardes, chemins de `prepare_worlds`, passe de création.

Tout se joue dans `zomboid_home`, sur des ports tirés au hasard : ni les vrais mondes
`duelA` et `duelB`, ni les vrais ports de jeu ne sont atteignables.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import threading
from pathlib import Path

import pytest

from doubles.fake_server import install_fake_server
from service import snapshots
from service.rules import load_runtime
from service.servers import Redactor, ServerPair, ServerProcess, read_ini, write_ini
from service.snapshots import SnapshotError, SnapshotStore, copy_path, prepare_worlds
from service.transport import CommandChannel

SIDES = ("A", "B")
FAST_TIMEOUTS = {
    "server_start_timeout_s": 10,
    "stop_timeout_s": 5,
    "kill_grace_s": 2,
    "rcon_timeout_s": 0.5,
    "tick_ms": 10,
}
CANCEL_DELAY_S = 0.2
READY_LINES = ["*** SERVER STARTED ****", "server is listening on port {port}"]
MANAGED = {"PauseEmpty": "false", "Mods": "duelcore"}
# Le double lancé à la place de PZ relève le disque, puis crée le monde comme le ferait PZ.
RECORDING_SCRIPT = """#!/bin/sh
seen="{launches}/$2"
mkdir -p "$seen"
cp "{zomboid}/Server/$2.ini" "$seen/"
cp -r "{zomboid}/Lua" "$seen/Lua"
{create_world}
exec {inner} "$@"
"""
CREATE_WORLD = 'mkdir -p "{zomboid}/Saves/Multiplayer/$2/map"\n' + (
    'echo created > "{zomboid}/Saves/Multiplayer/$2/map/chunk.bin"\n'
    'echo created > "{zomboid}/db/$2.db"'
)


def free_port(kind: int) -> int:
    """Sur toutes les adresses, comme la garde : libre sur `127.0.0.1` ne suffit pas."""
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def tree(root: Path) -> dict[str, bytes | str | None]:
    """Contenu exact : octets des fichiers, cible des liens, dossiers vides compris."""
    content: dict[str, bytes | str | None] = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            content[name] = f"-> {os.readlink(path)}"
        elif path.is_dir():
            content[name] = None
        else:
            content[name] = path.read_bytes()
    return content


def world_dir(zomboid: Path, side: str) -> Path:
    return zomboid / "Saves" / "Multiplayer" / f"duel{side}"


def world_db(zomboid: Path, side: str) -> Path:
    return zomboid / "db" / f"duel{side}.db"


def make_world(zomboid: Path, side: str, marker: bytes = b"origin") -> None:
    world = world_dir(zomboid, side)
    (world / "map" / "empty").mkdir(parents=True)
    (world / "map" / "chunk.bin").write_bytes(b"\x00\xff\r\n" + marker)
    (world / "players.db").write_bytes(marker * 3)
    (world / "latest").symlink_to("map/chunk.bin")
    world_db(zomboid, side).write_bytes(b"SQLite\x00" + marker)


def alter_world(zomboid: Path, side: str) -> None:
    world = world_dir(zomboid, side)
    (world / "map" / "chunk.bin").write_bytes(b"after the match")
    (world / "players.db").unlink()
    (world / "map" / "new_chunk.bin").write_bytes(b"added during the match")
    world_db(zomboid, side).write_bytes(b"SQLite\x00changed")


def worlds_state(zomboid: Path) -> dict:
    state = {}
    for side in SIDES:
        state[side] = tree(world_dir(zomboid, side)), world_db(zomboid, side).read_bytes()
    return state


def listing(zomboid: Path) -> list[str]:
    """Noms présents à côté des mondes : un dossier temporaire oublié s'y verrait."""
    return sorted(os.listdir(zomboid / "Saves" / "Multiplayer") + os.listdir(zomboid / "db"))


class Rig:
    """Vraie `ServerPair` sur le double : `start-server.sh` relève le disque à chaque lancement."""

    def __init__(self, tmp_path: Path, zomboid: Path, runtime: dict) -> None:
        self.root = tmp_path
        self.zomboid = zomboid
        self.launches = tmp_path / "launches"
        self.runtime = {**runtime, **FAST_TIMEOUTS}
        self.runtime["ports"] = {
            side: {
                "game": free_port(socket.SOCK_DGRAM),
                "udp": free_port(socket.SOCK_DGRAM),
                "rcon": free_port(socket.SOCK_STREAM),
            }
            for side in SIDES
        }
        self.events: list[object] = []
        self.servers: dict[str, ServerProcess] = {}
        self.channels = {side: CommandChannel(side, zomboid / "Lua") for side in SIDES}

    def pair(self, scenarios: dict | None = None, create_world: str = "") -> ServerPair:
        scenarios = scenarios or {}
        redactor = Redactor(["admin-marker", "rcon-marker"])
        for side in SIDES:
            pz_dir = self.root / f"pz{side}"
            pz_dir.mkdir(exist_ok=True)
            script = install_fake_server(pz_dir, scenarios.get(side, {"lines": READY_LINES}))
            inner = script.rename(pz_dir / "inner-server.sh")
            script.write_text(
                RECORDING_SCRIPT.format(
                    launches=self.launches,
                    zomboid=self.zomboid,
                    inner=shlex.quote(str(inner)),
                    create_world=create_world.format(zomboid=self.zomboid),
                ),
                encoding="utf-8",
            )
            script.chmod(0o755)
            cfg = {
                **self.runtime,
                "pz_dir": pz_dir,
                "zomboid_dir": self.zomboid,
                "admin_password": "admin-marker",
                "rcon_password": "rcon-marker",
            }
            self.servers[side] = ServerProcess(side, cfg, self.root / "match", redactor)
        return ServerPair(self.servers, self.runtime, self.on_status)

    def store(self, copy=copy_path) -> SnapshotStore:
        def recording_copy(src: Path, dst: Path) -> None:
            if src.is_dir():
                self.events.append(("capture", src.name))
            copy(src, dst)

        return SnapshotStore(self.zomboid, ports=self.runtime["ports"], copy=recording_copy)

    def on_status(self, side: str, status: str, detail: str) -> None:
        self.events.append((status, side))

    def prepare_launch(self) -> None:
        """Ce que `RealMatch` fera en P18, avec les vraies fonctions."""
        self.events.append("prepare_launch")
        for side, channel in self.channels.items():
            ports = self.runtime["ports"][side]
            managed = {**MANAGED, "DefaultPort": ports["game"], "RCONPort": ports["rcon"]}
            write_ini(self.zomboid / "Server" / f"duel{side}.ini", managed)
            channel.write_session("m1", 1, 0)
            channel.reset()

    def cleanup(self) -> None:
        for server in self.servers.values():
            if server._process is not None:
                try:
                    os.killpg(server._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                server._process.wait()


@pytest.fixture
def rig(tmp_path, zomboid_home, runtime):
    built = Rig(tmp_path, zomboid_home, runtime)
    yield built
    built.cleanup()


@pytest.fixture
def captured(rig):
    """Deux mondes capturés, puis modifiés par un match."""
    for side in SIDES:
        make_world(rig.zomboid, side, side.encode())
    pair = rig.pair()
    store = rig.store()
    store.capture(pair)
    origin = worlds_state(rig.zomboid)
    for side in SIDES:
        alter_world(rig.zomboid, side)
    rig.events.clear()
    return pair, store, origin


def test_restore_exact(rig, captured):
    pair, store, origin = captured
    altered = worlds_state(rig.zomboid)
    assert altered != origin
    assert (rig.zomboid / "duel-snapshots" / "duelA").is_dir()
    before = listing(rig.zomboid)

    store.restore(pair)

    assert worlds_state(rig.zomboid) == origin
    assert world_db(rig.zomboid, "A").read_bytes() == b"SQLite\x00A"
    assert "map/empty" in origin["A"][0]
    assert os.readlink(world_dir(rig.zomboid, "A") / "latest") == "map/chunk.bin"
    assert listing(rig.zomboid) == before

    # Le snapshot n'est pas consommé : la revanche repart du même monde.
    alter_world(rig.zomboid, "A")
    store.restore(pair)
    assert worlds_state(rig.zomboid) == origin


def test_restore_rebuilds_a_missing_world(rig, captured):
    pair, store, origin = captured
    shutil.rmtree(world_dir(rig.zomboid, "A"))
    world_db(rig.zomboid, "B").unlink()

    store.restore(pair)

    assert worlds_state(rig.zomboid) == origin


def test_restore_guards(rig, captured):
    pair, store, _ = captured
    altered = worlds_state(rig.zomboid)
    before = listing(rig.zomboid)

    def assert_refused(refused_store: SnapshotStore, *needles: str) -> None:
        with pytest.raises(SnapshotError) as refusal:
            prepare_worlds(pair, refused_store, prepare_launch=rig.prepare_launch)
        for needle in needles:
            assert needle in str(refusal.value)
        assert worlds_state(rig.zomboid) == altered
        assert listing(rig.zomboid) == before

    # Cas 1 : un serveur tourne.
    rig.servers["B"].start()
    assert_refused(store, "restauration refusée", "serveur B en marche")
    assert rig.servers["B"].stop()

    # Cas 2 : un port de jeu est occupé.
    game_port = rig.runtime["ports"]["B"]["game"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupant:
        occupant.bind(("", game_port))
        assert_refused(store, "restauration refusée", f"{game_port}/UDP")

    # Cas 3 : la copie échoue à mi-chemin, sur le dernier monde copié.
    def failing_copy(src: Path, dst: Path) -> None:
        if src.parent.name == "duelB" and src.is_dir():
            (dst / "map").mkdir(parents=True)
            (dst / "map" / "chunk.bin").write_bytes(b"partial")
            raise OSError(28, "No space left on device")
        copy_path(src, dst)

    assert_refused(rig.store(failing_copy), "restauration impossible", "No space left")

    # Cas 4 : un snapshot d'un seul côté arrête le lancement.
    shutil.rmtree(rig.zomboid / "duel-snapshots" / "duelB")
    assert_refused(store, "lancement refusé", "seulement pour A")

    assert "prepare_launch" not in rig.events


def test_restore_refuses_a_truncated_snapshot(rig, captured):
    pair, store, _ = captured
    altered = worlds_state(rig.zomboid)
    (rig.zomboid / "duel-snapshots" / "duelB" / "duelB.db").unlink()

    with pytest.raises(SnapshotError, match="restauration impossible"):
        store.restore(pair)

    assert worlds_state(rig.zomboid) == altered


@pytest.mark.parametrize("failing_rename", range(1, 9))
def test_switch_failure_rolls_back(rig, captured, monkeypatch, failing_rename):
    """Les 8 `rename` de la bascule : une panne sur l'un d'eux rend les mondes d'avant."""
    pair, store, _ = captured
    altered = worlds_state(rig.zomboid)
    before = listing(rig.zomboid)
    real_rename = os.rename
    calls = []

    def rename(src, dst):
        calls.append(src)
        if len(calls) == failing_rename:
            raise OSError(5, "Input/output error")
        real_rename(src, dst)

    monkeypatch.setattr(snapshots.os, "rename", rename)

    with pytest.raises(SnapshotError, match="monde intact"):
        store.restore(pair)

    assert worlds_state(rig.zomboid) == altered
    assert listing(rig.zomboid) == before


def test_failed_rollback_keeps_the_old_world(rig, captured, monkeypatch):
    pair, store, _ = captured
    real_rename = os.rename
    calls = []

    def rename(src, dst):
        calls.append(src)
        if len(calls) >= 2:
            raise OSError(5, "Input/output error")
        real_rename(src, dst)

    monkeypatch.setattr(snapshots.os, "rename", rename)

    with pytest.raises(SnapshotError, match="non défaite"):
        store.restore(pair)

    (kept,) = (rig.zomboid / "Saves" / "Multiplayer").glob(".duelA.*/old")
    assert (kept / "map" / "chunk.bin").read_bytes() == b"after the match"


def test_capture_guards(rig):
    for side in SIDES:
        make_world(rig.zomboid, side)
    pair = rig.pair()

    rig.servers["A"].start()
    with pytest.raises(SnapshotError, match="capture refusée : serveur A en marche"):
        rig.store().capture(pair)
    assert rig.servers["A"].stop()
    assert not rig.store().has("A")

    def failing_copy(src: Path, dst: Path) -> None:
        if src.name == "duelB.db":
            raise OSError(28, "No space left on device")
        copy_path(src, dst)

    # Une panne sur B ne laisse pas de snapshot de A : il bloquerait les lancements suivants.
    with pytest.raises(SnapshotError, match="capture impossible"):
        rig.store(failing_copy).capture(pair)
    assert not os.listdir(rig.zomboid / "duel-snapshots")

    store = rig.store()
    store.capture(pair)
    snapshot = tree(rig.zomboid / "duel-snapshots")
    alter_world(rig.zomboid, "A")
    with pytest.raises(SnapshotError, match="un snapshot de A existe déjà"):
        store.capture(pair)
    assert tree(rig.zomboid / "duel-snapshots") == snapshot


def test_capture_refuses_half_a_world(rig):
    make_world(rig.zomboid, "A")
    make_world(rig.zomboid, "B")
    world_db(rig.zomboid, "B").unlink()
    pair = rig.pair()

    with pytest.raises(SnapshotError, match=r"monde B incomplet, manque duelB\.db"):
        prepare_worlds(pair, rig.store(), prepare_launch=rig.prepare_launch)

    assert not rig.store().has("A")


def test_default_ports_come_from_runtime(zomboid_home):
    assert SnapshotStore(zomboid_home)._ports == load_runtime()["ports"]


def test_prepare_worlds_restores_without_prepare_launch(rig, captured):
    pair, store, origin = captured

    prepare_worlds(pair, store, prepare_launch=rig.prepare_launch)

    assert worlds_state(rig.zomboid) == origin
    assert "prepare_launch" not in rig.events
    assert not rig.launches.exists()


def test_prepare_worlds_captures_without_prepare_launch(rig):
    for side in SIDES:
        make_world(rig.zomboid, side, side.encode())
    pair = rig.pair()
    store = rig.store()
    origin = worlds_state(rig.zomboid)

    prepare_worlds(pair, store, prepare_launch=rig.prepare_launch)

    assert store.has("A") and store.has("B")
    assert worlds_state(rig.zomboid) == origin
    assert "prepare_launch" not in rig.events
    assert not rig.launches.exists()


def test_prepare_launch_is_mandatory(rig):
    with pytest.raises(TypeError):
        prepare_worlds(rig.pair(), rig.store())


def test_single_world_is_refused(rig):
    """D16, y compris après une passe interrompue qui n'aurait créé qu'un monde."""
    make_world(rig.zomboid, "B")
    before = worlds_state_of(rig.zomboid, "B")

    with pytest.raises(SnapshotError) as refusal:
        prepare_worlds(rig.pair(), rig.store(), prepare_launch=rig.prepare_launch)

    assert "monde présent seulement pour B" in str(refusal.value)
    assert "passe de création interrompue" in str(refusal.value)
    assert worlds_state_of(rig.zomboid, "B") == before
    assert "prepare_launch" not in rig.events
    assert not rig.launches.exists()
    assert not rig.store().has("B")


def worlds_state_of(zomboid: Path, side: str) -> tuple:
    return tree(world_dir(zomboid, side)), world_db(zomboid, side).read_bytes()


def test_first_run_creation_pass(rig):
    for channel in rig.channels.values():
        for path in (channel.in_path, channel.out_path, channel.ack_path):
            path.write_bytes(b"left by an earlier run\n")
    pair = rig.pair(create_world=CREATE_WORLD)
    store = rig.store()

    prepare_worlds(pair, store, prepare_launch=rig.prepare_launch)

    # Les deux serveurs deviennent disponibles indépendamment : leur ordre n'est pas fixé.
    assert set(rig.events[3:5]) == {("up", "A"), ("up", "B")}
    assert rig.events[:3] + rig.events[5:] == [
        "prepare_launch",
        ("starting", "A"),
        ("starting", "B"),
        ("down", "A"),
        ("down", "B"),
        ("capture", "duelA"),
        ("capture", "duelB"),
    ]
    for side in SIDES:
        assert store.has(side)
        assert world_db(rig.zomboid, side).read_bytes() == b"created\n"
        snapshot = rig.zomboid / "duel-snapshots" / f"duel{side}"
        assert tree(snapshot / "world") == tree(world_dir(rig.zomboid, side))

        seen = rig.launches / f"duel{side}"
        ini = read_ini(seen / f"duel{side}.ini")
        assert ini["Mods"] == "duelcore"
        assert ini["DefaultPort"] == str(rig.runtime["ports"][side]["game"])
        session = json.loads((seen / "Lua" / f"duel_{side}_session.txt").read_bytes())
        assert (session["side"], session["boot"], session["cmd_floor"]) == (side, 1, 0)
        for name in ("in", "out", "ack"):
            assert (seen / "Lua" / f"duel_{side}_{name}.txt").read_bytes() == b""

    # Une fois la passe faite, le lancement suivant restaure, sans nouvelle passe.
    rig.events.clear()
    prepare_worlds(pair, store, prepare_launch=rig.prepare_launch)
    assert "prepare_launch" not in rig.events


def test_failed_creation_pass_captures_nothing(rig):
    crash = {"lines": ["boom"], "exit_code": 3}
    pair = rig.pair({"B": crash}, create_world=CREATE_WORLD)
    store = rig.store()

    with pytest.raises(SnapshotError, match="passe de création en échec : serveur B"):
        prepare_worlds(pair, store, prepare_launch=rig.prepare_launch)

    assert not store.has("A") and not store.has("B")
    assert not any(event[0] == "capture" for event in rig.events if isinstance(event, tuple))
    assert not any(snapshots._is_running(server) for server in rig.servers.values())


def test_cancelled_creation_pass_captures_nothing(rig):
    pair = rig.pair({side: {"lines": []} for side in SIDES}, create_world=CREATE_WORLD)
    store = rig.store()
    cancel = threading.Timer(CANCEL_DELAY_S, pair.stop)
    cancel.start()

    with pytest.raises(SnapshotError, match="passe de création en échec : interrompue"):
        prepare_worlds(pair, store, prepare_launch=rig.prepare_launch)

    cancel.join()
    assert not store.has("A") and not store.has("B")
    assert not any(snapshots._is_running(server) for server in rig.servers.values())


def test_creation_pass_missing_world_captures_nothing(rig):
    """Une disponibilité sans monde sur le disque ne fabrique pas de snapshot partiel."""
    pair = rig.pair()
    store = rig.store()

    with pytest.raises(SnapshotError, match="monde A incomplet"):
        prepare_worlds(pair, store, prepare_launch=rig.prepare_launch)

    assert not store.has("A") and not store.has("B")


def test_failed_prepare_launch_starts_nothing(rig):
    def failing_prepare() -> None:
        raise OSError("disque plein")

    with pytest.raises(OSError, match="disque plein"):
        prepare_worlds(rig.pair(), rig.store(), prepare_launch=failing_prepare)

    assert not rig.launches.exists()
