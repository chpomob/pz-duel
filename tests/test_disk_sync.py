"""P18F1 : `sync_disk`, seule synchronisation disque du service, sur les deux systèmes.

`os.sync` n'existe pas sous Windows. Le premier test tourne sans injection, donc pour de bon sur
`windows-latest` ; les suivants retirent `os.sync` pour rejouer sous Linux la panne de ce système.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from test_snapshots import (  # noqa: F401
    SIDES,
    captured,
    make_world,
    rig,
    world_db,
    world_dir,
    worlds_state,
)
from service import platforms
from service.platforms import LINUX, WINDOWS, sync_disk
from service.snapshots import SnapshotError, SnapshotStore

SERVICE_DIR = Path(__file__).resolve().parent.parent / "service"
PRIMITIVE_MODULE = "platforms.py"
PRIMITIVE = "sync_disk"
MARKER_NAME = ".creation-en-cours"


class FlushLog:
    """Ce que la branche Windows ouvre, synchronise et ferme, sur les vrais appels."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fsync: Callable[[int], None] = os.fsync):
        self.opened: list[str] = []
        self.flushed: list[int] = []
        self.closed: list[int] = []
        self._open, self._fsync, self._close = os.open, fsync, os.close
        monkeypatch.setattr(platforms.os, "open", self.open)
        monkeypatch.setattr(platforms.os, "fsync", self.fsync)
        monkeypatch.setattr(platforms.os, "close", self.close)

    def open(self, path, flags, *args, **kwargs) -> int:
        descriptor = self._open(path, flags, *args, **kwargs)
        if flags == os.O_RDWR:
            self.opened.append(os.fspath(path))
        return descriptor

    def fsync(self, descriptor: int) -> None:
        self.flushed.append(descriptor)
        self._fsync(descriptor)

    def close(self, descriptor: int) -> None:
        self.closed.append(descriptor)
        self._close(descriptor)


@pytest.fixture
def no_os_sync(monkeypatch):
    """L'état réel de Windows : tout appel direct lèverait `AttributeError`."""
    monkeypatch.delattr(os, "sync", raising=False)


@pytest.fixture
def world(tmp_path) -> Path:
    root = tmp_path / "world"
    (root / "map" / "empty").mkdir(parents=True)
    (root / "map" / "chunk.bin").write_bytes(b"chunk")
    (root / "players.db").write_bytes(b"players")
    return root


def record_os_sync(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    monkeypatch.setattr(os, "sync", lambda: events.append("sync"), raising=False)


def test_sync_disk_runs_on_this_system(world):
    """Aucune injection : sous `windows-latest`, c'est la branche Windows qui s'exécute."""
    single = world / "players.db"

    sync_disk([world, single])

    assert (world / "map" / "chunk.bin").read_bytes() == b"chunk"
    assert single.read_bytes() == b"players"


def test_posix_uses_os_sync_once_and_touches_no_file(monkeypatch, world):
    events: list[str] = []
    record_os_sync(monkeypatch, events)
    log = FlushLog(monkeypatch)

    sync_disk([world, world / "players.db"], platform=LINUX)

    assert events == ["sync"]
    assert log.opened == []


def test_windows_flushes_every_file_without_os_sync(monkeypatch, no_os_sync, world):
    log = FlushLog(monkeypatch)

    sync_disk([world], platform=WINDOWS)

    assert sorted(log.opened) == sorted(
        [str(world / "map" / "chunk.bin"), str(world / "players.db")]
    )
    assert len(log.flushed) == len(log.opened)
    assert log.closed == log.flushed


def test_windows_accepts_a_file_target(monkeypatch, no_os_sync, world):
    log = FlushLog(monkeypatch)

    sync_disk([world / "players.db"], platform=WINDOWS)

    assert log.opened == [str(world / "players.db")]


def test_windows_skips_links_and_absent_targets(monkeypatch, no_os_sync, world, tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (world / "latest").symlink_to(outside)
    (world / "broken").symlink_to(tmp_path / "absent")
    log = FlushLog(monkeypatch)

    sync_disk([world, world / "latest", tmp_path / "absent"], platform=WINDOWS)

    assert sorted(log.opened) == sorted(
        [str(world / "map" / "chunk.bin"), str(world / "players.db")]
    )


def test_windows_skips_a_read_only_file(monkeypatch, no_os_sync, world):
    """`FlushFileBuffers` le refuserait : le monde reste lançable, voir `sync_disk`."""
    read_only = world / "players.db"
    read_only.chmod(0o444)
    log = FlushLog(monkeypatch)

    sync_disk([world], platform=WINDOWS)

    assert log.opened == [str(world / "map" / "chunk.bin")]


def test_windows_failed_flush_raises_and_closes(monkeypatch, no_os_sync, world):
    def refuse(descriptor: int) -> None:
        raise OSError("disque en panne")

    log = FlushLog(monkeypatch, refuse)

    with pytest.raises(OSError, match="disque en panne"):
        sync_disk([world], platform=WINDOWS)

    assert log.closed == log.flushed
    assert len(log.flushed) == 1


def test_creation_marker_is_flushed_without_os_sync(monkeypatch, no_os_sync, rig):
    log = FlushLog(monkeypatch)
    store = SnapshotStore(rig.zomboid, ports=rig.runtime["ports"], platform=WINDOWS)

    store.begin_creation()

    assert store.creation_pending()
    assert [Path(path).name for path in log.opened] == [MARKER_NAME]


def test_capture_and_restore_work_without_os_sync(monkeypatch, no_os_sync, rig):
    """La panne d'origine : `AttributeError` dès la préparation des mondes."""
    for side in SIDES:
        make_world(rig.zomboid, side, side.encode())
    origin = worlds_state(rig.zomboid)
    pair = rig.pair()
    store = SnapshotStore(rig.zomboid, ports=rig.runtime["ports"], platform=WINDOWS)

    store.capture(pair)
    world_db(rig.zomboid, "A").write_bytes(b"altered")
    store.restore(pair)

    assert worlds_state(rig.zomboid) == origin


@pytest.mark.parametrize("platform", [LINUX, WINDOWS], ids=["posix", "windows"])
def test_restore_syncs_before_the_first_rename(monkeypatch, rig, captured, platform):
    """Sans cet ordre, une coupure pourrait garder le `rename` et perdre les données."""
    pair, _, origin = captured
    events: list[str] = []
    record_os_sync(monkeypatch, events)
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor: int) -> None:
        events.append("sync")
        real_fsync(descriptor)

    def replace(src, dst) -> None:
        events.append("rename")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    store = SnapshotStore(rig.zomboid, ports=rig.runtime["ports"], platform=platform)

    store.restore(pair)

    assert events[0] == "sync"
    assert "sync" not in events[events.index("rename") :]
    assert worlds_state(rig.zomboid) == origin


def test_failed_flush_refuses_the_restore_and_keeps_the_world(monkeypatch, rig, captured):
    pair, _, _ = captured
    altered = worlds_state(rig.zomboid)

    def refuse(descriptor: int) -> None:
        raise OSError("disque en panne")

    monkeypatch.setattr(os, "fsync", refuse)
    store = SnapshotStore(rig.zomboid, ports=rig.runtime["ports"], platform=WINDOWS)

    with pytest.raises(SnapshotError, match="monde intact"):
        store.restore(pair)

    assert worlds_state(rig.zomboid) == altered
    assert world_dir(rig.zomboid, "A").is_dir()


def direct_os_sync(tree: ast.AST) -> list[int]:
    """Lignes où `os.sync` est atteint : attribut, import, alias du module ou `getattr`."""
    modules = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.asname for alias in node.names if alias.name == "os" and alias.asname}
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found = node.attr == "sync" and isinstance(node.value, ast.Name)
            found = found and node.value.id in modules
        elif isinstance(node, ast.ImportFrom):
            found = node.module == "os" and any(alias.name == "sync" for alias in node.names)
        elif isinstance(node, ast.Call):
            found = (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in modules
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "sync"
            )
        else:
            found = False
        if found:
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.sync()",
        "import os\ncall = os.sync",
        "import os as system\nsystem.sync()",
        "from os import sync",
        "import os\ngetattr(os, 'sync')()",
    ],
)
def test_direct_os_sync_is_seen(source):
    assert direct_os_sync(ast.parse(source))


def test_other_sync_is_not_taken_for_os_sync():
    assert direct_os_sync(ast.parse("import os\nchannel.sync()\ngetattr(os, 'path')")) == []


def test_no_service_module_reaches_os_sync_directly():
    """`sync_disk` en est l'unique appelant : c'est lui qui choisit selon la plateforme."""
    paths = sorted(SERVICE_DIR.rglob("*.py"))
    assert paths

    offenders = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = direct_os_sync(tree)
        if path.name == PRIMITIVE_MODULE:
            (primitive,) = [
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == PRIMITIVE
            ]
            assert len(direct_os_sync(primitive)) == 1
            lines = [
                line
                for line in lines
                if not primitive.lineno <= line <= (primitive.end_lineno or primitive.lineno)
            ]
        if lines:
            offenders[path.name] = lines
    assert not offenders
