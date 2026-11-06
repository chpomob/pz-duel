"""R1 à R3 : plateformes, écrivains, préflight, processus (AC4 à AC17).

D-A : un test qui n'a de sens que sur un seul système est défini dans un bloc `if` de ce
module, il n'est pas sauté. Les branches Windows s'exécutent ici par injection (D-B).
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import hashlib
import io
import json
import logging
import math
import ntpath
import os
import re
import socket
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from functools import partial
from itertools import cycle
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple
from urllib.request import Request, urlopen

import pytest

from conftest import ZOMBOID_SUBDIRS
from doubles import fake_server
from doubles.fake_server import (
    PosixKernel,
    WindowsLaunchOnPosix,
    compile_windows_client,
    install_fake_server,
    plan_windows_client,
    windows_pid_alive,
)
from test_contract import Lobby
from test_share import (  # noqa: F401
    LAN_ADDRESS,
    LAUNCHER_ROUTE,
    PRESET,
    api_port,
    detect,
    request,
)
from test_snapshots import (  # noqa: F401
    ENGINE_READY,
    PLAYER_PASSWORD,
    READY_LINES,
    engine_rig,
    free_ports,
)
from service import engine as engine_module
from service import platforms, preflight, replay, rules, servers, transport
from service import proofs as proofs_module
from service.engine import (
    MODE_REAL,
    ClientLaunchError,
    Engine,
    MatchSecrets,
    RealMatch,
    launch_client,
)
from service.journal import read_journal
from service.platforms import (
    CREATE_BREAKAWAY_FROM_JOB,
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    CREATE_SUSPENDED,
    DETACHED_PROCESS,
    LINUX,
    WINDOWS,
    PosixProcessTree,
    ReplaceError,
    UnsafeArgument,
    WindowsKernel,
    WindowsProcessTree,
    replace_attempts,
    replace_file,
)
from service.proofs import PROOFS_NAME, HookProofs
from service.protocol import PROTOCOL_VERSION, SIDES, dump_line
from service.rules import load_effects, load_runtime
from service.rcon import RconError
from service.servers import (
    RCON_STOP_SHARE,
    REDACTED,
    STARTED_MARKER,
    STOP_RCON,
    STOP_SIGKILL,
    STOP_SIGTERM,
    STOP_STDIN,
    Redactor,
    ServerProcess,
    launch_command,
    read_ini,
    server_name,
    write_ini,
    write_sandbox,
)
from service.transport import CommandChannel

REPO = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO / "service"
FIXTURES = REPO / "tests" / "fixtures"
RESERVED = {"winreg", "msvcrt", "_winapi"}
SUBPROCESS_TIMEOUT_S = 60
TOLERANCE = 1.1
POLICY = {"replace_timeout_s": 2, "replace_retry_ms": 300}

IMPORT_ALL = """
import importlib, json, pkgutil, sys
import service

names = [m.name for m in pkgutil.walk_packages(service.__path__, "service.")]
for name in names:
    importlib.import_module(name)
print(json.dumps({"names": names, "loaded": sorted(sys.modules)}))
"""


def load_time_imports(tree: ast.AST) -> set[str]:
    """Rend les modules racines importés hors du corps d'une fonction."""
    if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return set()
    roots: set[str] = set()
    if isinstance(tree, ast.Import):
        roots |= {alias.name.split(".")[0] for alias in tree.names}
    elif isinstance(tree, ast.ImportFrom) and tree.level == 0:
        roots.add(tree.module.split(".")[0])
    for child in ast.iter_child_nodes(tree):
        roots |= load_time_imports(child)
    return roots


class FakeTime:
    """Horloge en millisecondes entières : les comptes de tentatives restent exacts."""

    def __init__(self):
        self.now_ms = 0
        self.sleeps = []

    def clock(self) -> float:
        return self.now_ms / 1000

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_ms += round(seconds * 1000)


class HeldTarget:
    """Remplace `os.replace` : refuse `failures` fois, puis laisse passer le vrai appel."""

    def __init__(self, time_source: FakeTime, failures=math.inf, attempt_ms=0):
        self.time = time_source
        self.failures = failures
        self.attempt_ms = attempt_ms
        self.attempts = 0
        self.real = os.replace

    def __call__(self, src, dst):
        self.attempts += 1
        self.time.now_ms += self.attempt_ms
        if self.attempts <= self.failures:
            raise PermissionError(13, "held by another process")
        self.real(src, dst)


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "target.tmp"
    dst = tmp_path / "target"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    return src, dst


@pytest.fixture
def fake_time():
    return FakeTime()


def replace_on_windows(files, fake_time, policy=POLICY):
    replace_file(
        *files, policy=policy, platform=WINDOWS, sleep=fake_time.sleep, clock=fake_time.clock
    )


# AC6 ---------------------------------------------------------------------------------------

# Sous Windows, la stdlib charge elle-même `msvcrt` et `_winapi` : la mesure n'y dirait rien.
if sys.platform != "win32":

    def test_service_loads_no_windows_module():
        done = subprocess.run(
            [sys.executable, "-c", IMPORT_ALL],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        assert done.returncode == 0, done.stderr
        report = json.loads(done.stdout)

        on_disk = {f"service.{path.stem}" for path in SERVICE_DIR.glob("*.py")}
        assert on_disk - {"service.__init__"} <= set(report["names"])
        assert "service.platforms" in report["names"]
        assert not RESERVED & set(report["loaded"])


def test_no_windows_module_is_imported_at_load_time():
    """Un `try: import winreg` échouerait sans bruit sous Linux : les sources le disent."""
    sample = "import json\ntry:\n    import winreg\nexcept ImportError:\n    pass\n"
    assert "winreg" in load_time_imports(ast.parse(sample))
    assert not load_time_imports(ast.parse("def f():\n    import msvcrt\n"))

    offenders = {
        path.name: found
        for path in SERVICE_DIR.rglob("*.py")
        if (found := RESERVED & load_time_imports(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert not offenders


# Platform ----------------------------------------------------------------------------------


def test_current_follows_the_host(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert platforms.current() is WINDOWS
    monkeypatch.setattr(sys, "platform", "linux")
    assert platforms.current() is LINUX


def test_linux_description_is_todays_layout():
    assert LINUX.server_script == "start-server.sh"
    assert LINUX.game_subdir == "projectzomboid"
    assert LINUX.posix_modes
    assert LINUX.stop_signals == ("SIGINT", "SIGTERM")
    assert LINUX.join("/opt/pz", LINUX.server_script) == "/opt/pz/start-server.sh"
    assert LINUX.client_path("/opt/pz/projectzomboid") == "/opt/pz/projectzomboid.sh"


def test_windows_description_composes_with_ntpath():
    folder = r"C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid"
    assert WINDOWS.game_subdir == ""
    assert not WINDOWS.posix_modes
    assert WINDOWS.stop_signals == ("SIGINT", "SIGBREAK")
    assert WINDOWS.join(folder, WINDOWS.server_script) == folder + r"\StartServer64.bat"
    assert WINDOWS.client_path(folder) == folder + r"\ProjectZomboid64.exe"


def test_platform_is_frozen():
    with pytest.raises(AttributeError):
        LINUX.posix_modes = False


# runtime.json ------------------------------------------------------------------------------


def test_runtime_declares_the_new_values():
    runtime = load_runtime()
    taken = {port for side in runtime["ports"].values() for port in side.values()}
    assert runtime["share_port"] not in taken
    assert runtime["instance_wait_s"] > 0
    # AC14 : un fichier tenu 500 ms ne doit pas épuiser le délai.
    assert runtime["replace_timeout_s"] > 0.5
    assert 0 < runtime["replace_retry_ms"] < runtime["replace_timeout_s"] * 1000


# AC15, partie Linux ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    [
        load_runtime(),
        POLICY,
        # L'attente dépasse le délai : tronquée, elle ne doit pas le faire déborder.
        {"replace_timeout_s": 1, "replace_retry_ms": 60_000},
    ],
    ids=["runtime", "uneven", "retry_beyond_timeout"],
)
def test_held_target_fails_within_the_configured_delay(files, fake_time, monkeypatch, policy):
    held = HeldTarget(fake_time)
    monkeypatch.setattr(os, "replace", held)

    with pytest.raises(ReplaceError) as caught:
        replace_on_windows(files, fake_time, policy)

    src, dst = files
    timeout_s = policy["replace_timeout_s"]
    assert str(dst) in str(caught.value)
    assert isinstance(caught.value, OSError)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert held.attempts <= math.ceil(timeout_s * 1000 / policy["replace_retry_ms"]) + 1
    assert held.attempts >= 2
    assert fake_time.clock() <= timeout_s * TOLERANCE
    assert dst.read_text(encoding="utf-8") == "old"
    assert src.read_text(encoding="utf-8") == "new"


def test_slow_attempts_shorten_the_retries(files, fake_time, monkeypatch):
    held = HeldTarget(fake_time, attempt_ms=50)
    monkeypatch.setattr(os, "replace", held)

    with pytest.raises(ReplaceError):
        replace_on_windows(files, fake_time)

    assert fake_time.clock() <= POLICY["replace_timeout_s"] * TOLERANCE


def test_transient_hold_ends_in_the_new_content(files, fake_time, monkeypatch):
    held = HeldTarget(fake_time, failures=2)
    monkeypatch.setattr(os, "replace", held)

    replace_on_windows(files, fake_time)

    src, dst = files
    assert held.attempts == 3
    assert fake_time.sleeps == [0.3, 0.3]
    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_other_errors_are_not_retried(tmp_path, fake_time):
    with pytest.raises(FileNotFoundError):
        replace_on_windows((tmp_path / "absent", tmp_path / "target"), fake_time)
    assert not fake_time.sleeps


def test_policy_defaults_to_the_runtime_file(files, fake_time, monkeypatch):
    monkeypatch.setattr(os, "replace", HeldTarget(fake_time))
    monkeypatch.setattr(rules, "load_runtime", lambda: POLICY)

    with pytest.raises(ReplaceError):
        replace_on_windows(files, fake_time, policy=None)

    assert fake_time.clock() == POLICY["replace_timeout_s"]


def test_linux_is_a_bare_replace(files, fake_time, monkeypatch):
    held = HeldTarget(fake_time)
    monkeypatch.setattr(os, "replace", held)

    def no_runtime():
        raise AssertionError("runtime read on Linux")

    monkeypatch.setattr(rules, "load_runtime", no_runtime)

    with pytest.raises(PermissionError) as caught:
        replace_file(*files, platform=LINUX, sleep=fake_time.sleep, clock=fake_time.clock)

    assert not isinstance(caught.value, ReplaceError)
    assert held.attempts == 1
    assert not fake_time.sleeps


def test_default_platform_replaces_the_file(files):
    replace_file(*files)

    src, dst = files
    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


# AC13 à AC17 : les écrivains ---------------------------------------------------------------

# Tenue d'AC14, et délai court pour que le test d'AC15 n'attende pas celui de `runtime.json`.
HOLD_S = 0.5
SHORT_POLICY = {"replace_timeout_s": 0.2, "replace_retry_ms": 20}
CLOCK_GRAIN_MS = 16
SANDBOX_TEXT = "SandboxVars = {\n    DayLength = 1,\n}\n"
HOOK = "hook"

# Empreinte de ce que produit le rejeu de chaque fixture, relevée sous Linux (AC17).
REPLAY_DIGESTS = {
    "replay_1000": "608f15f956fb2cf5cc1e0da618532020358c16419ce08019bdeb54878b7b7503",
    "replay_clock": "e2949c613b7156e551d5536342938aae4e2b6575ead176740261f7f58827403d",
    "restart_records": "83a56e543fca6a8e60040d19b1f97ff5d04aa8e8c11bca5267a904b36c8d76b2",
    "summary_known": "d56605b218876995ad97c9b9572b38fb6e8b695b861cf67130a5946219e3b904",
}


class Writer(NamedTuple):
    """Un écrivain du service : `write(n)` remplace `target`, `holds(n)` lit le résultat."""

    target: Path
    write: Callable[[int], None]
    holds: Callable[[int], bool]


def session_writer(folder: Path, **options) -> Writer:
    channel = CommandChannel("A", folder, **options)

    def write(boot: int) -> None:
        channel.write_session("m1", boot, 0)

    def holds(boot: int) -> bool:
        return json.loads(channel.session_path.read_bytes())["boot"] == boot

    return Writer(channel.session_path, write, holds)


def ini_writer(folder: Path, **options) -> Writer:
    path = folder / "duelA.ini"

    def write(value: int) -> None:
        write_ini(path, {"Mods": f"mod{value}"}, **options)

    return Writer(path, write, lambda value: read_ini(path)["Mods"] == f"mod{value}")


def sandbox_writer(folder: Path, **options) -> Writer:
    path = folder / "duelA_SandboxVars.lua"
    path.write_text(SANDBOX_TEXT, encoding="utf-8")

    def write(value: int) -> None:
        write_sandbox(path, value, {}, **options)

    return Writer(path, write, lambda value: f"DayLength = {value}," in path.read_text("utf-8"))


def proofs_writer(folder: Path, **options) -> Writer:
    """Les preuves rapportent leur échec sans lever : `write` le relève pour la même mesure."""
    path = folder / PROOFS_NAME
    reports: list[str] = []
    proofs = HookProofs(path, reports.append, **options)

    def write(value: int) -> None:
        proofs.prove(f"build{value}|mod", HOOK)
        if reports:
            raise ReplaceError(reports[-1])

    return Writer(path, write, lambda value: f"build{value}|mod" in json.loads(path.read_bytes()))


WRITERS = [session_writer, ini_writer, sandbox_writer, proofs_writer]
each_writer = pytest.mark.parametrize("make_writer", WRITERS, ids=lambda make: make.__name__)


def folder_of(writer: Writer) -> list[str]:
    return os.listdir(writer.target.parent)


# AC13 : ces tests valent sur les deux systèmes, ils tournent donc aussi sur le vrai Windows.


def spawn_command(seq: int) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "seq": seq,
        "kind": "spawn_pop",
        "n": 2,
        "distance": [30, 60],
        "warn_s": 6,
        "origin": {"rule": "kill_baseline", "boot": 1, "event_seq": 42},
    }


def test_in_holds_exactly_the_protocol_bytes(tmp_path):
    channel = CommandChannel("A", tmp_path)
    channel.write_session("m1", 1, 0)
    lines = []
    for seq in (1, 2, 3):
        channel.write(spawn_command(seq))
        lines.append(dump_line(spawn_command(seq)))

    content = channel.in_path.read_bytes()
    assert content == b"".join(lines)
    assert len(content) == sum(map(len, lines))
    assert b"\r" not in content


# `os.O_NONBLOCK` n'existe pas sous Windows, où `test_windows_in_has_no_carriage_return` mesure.
if sys.platform != "win32":

    def test_in_is_opened_in_binary_mode(tmp_path, monkeypatch):
        """`os.O_BINARY` n'existe pas ici : un drapeau marqué dit qu'il est bien transmis."""
        marker = os.O_NONBLOCK
        opened = []
        real_open = os.open

        def spy(path, flags, *args):
            opened.append(flags)
            return real_open(path, flags, *args)

        monkeypatch.setattr(transport, "O_BINARY", marker)
        monkeypatch.setattr(os, "open", spy)
        channel = CommandChannel("A", tmp_path)
        channel.write_session("m1", 1, 0)
        channel.write(spawn_command(1))

        assert opened[-1] & marker


def test_o_binary_follows_the_os_module():
    assert transport.O_BINARY == getattr(os, "O_BINARY", 0)


# AC14 et AC15 par injection : plateforme Windows, `os.replace` refusé.


@each_writer
def test_transient_hold_does_not_fail_a_writer(tmp_path, monkeypatch, make_writer):
    writer = make_writer(tmp_path, policy=SHORT_POLICY, platform=WINDOWS)
    writer.write(1)
    held = HeldTarget(FakeTime(), failures=2)
    monkeypatch.setattr(os, "replace", held)

    writer.write(2)

    assert held.attempts == 3
    assert writer.holds(2)
    assert folder_of(writer) == [writer.target.name]


@each_writer
def test_lasting_hold_keeps_the_old_content_and_no_temporary(tmp_path, monkeypatch, make_writer):
    writer = make_writer(tmp_path, policy=SHORT_POLICY, platform=WINDOWS)
    writer.write(1)
    held = HeldTarget(FakeTime())
    monkeypatch.setattr(os, "replace", held)
    started = time.monotonic()

    with pytest.raises(ReplaceError) as caught:
        writer.write(2)

    elapsed = time.monotonic() - started
    assert writer.target.name in str(caught.value)
    budget_s = SHORT_POLICY["replace_timeout_s"]
    assert held.attempts <= math.ceil(budget_s * 1000 / SHORT_POLICY["replace_retry_ms"]) + 1
    assert elapsed >= budget_s
    monkeypatch.undo()
    assert writer.holds(1)
    assert folder_of(writer) == [writer.target.name]


class CoarseTime(FakeTime):
    """Lit l'heure par pas de `CLOCK_GRAIN_MS` : c'est `time.monotonic` sous Windows 3.12."""

    def clock(self) -> float:
        return self.now_ms // CLOCK_GRAIN_MS * CLOCK_GRAIN_MS / 1000


def test_coarse_clock_bounds_the_attempts_and_still_spends_the_budget(files):
    """La CI réelle a compté 12 essais pour 11 permis : l'horloge retarde sur les veilles."""
    coarse = CoarseTime()
    held = HeldTarget(coarse)
    budget_ms = SHORT_POLICY["replace_timeout_s"] * 1000

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", held)
        with pytest.raises(ReplaceError):
            replace_on_windows(files, coarse, SHORT_POLICY)

    assert held.attempts == replace_attempts(SHORT_POLICY)
    assert coarse.clock() * 1000 >= budget_ms


def test_attempt_bound_comes_from_the_budget_and_the_step():
    for policy in (POLICY, SHORT_POLICY, {"replace_timeout_s": 5, "replace_retry_ms": 100}):
        steps = policy["replace_timeout_s"] * 1000 / policy["replace_retry_ms"]
        assert replace_attempts(policy) == math.ceil(steps) + 1


# Le chemin POSIX de ces deux écrivains passe par `os.fchmod`, absent sous Windows.
NO_FCHMOD = pytest.mark.skipif(
    sys.platform == "win32",
    reason="no os.fchmod; replaced by test_windows_writer_gives_up_after_the_delay",
)


@pytest.mark.parametrize(
    "make_writer",
    [
        session_writer,
        pytest.param(ini_writer, marks=NO_FCHMOD),
        pytest.param(sandbox_writer, marks=NO_FCHMOD),
    ],
    ids=lambda make: make.__name__,
)
def test_linux_failure_leaves_no_temporary(tmp_path, monkeypatch, make_writer):
    writer = make_writer(tmp_path, platform=LINUX)
    writer.write(1)

    def refuse(src, dst):
        raise PermissionError(13, "refused")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError):
        writer.write(2)
    monkeypatch.undo()

    assert writer.holds(1)
    assert folder_of(writer) == [writer.target.name]


def test_failed_session_keeps_the_channel_state(tmp_path, monkeypatch):
    channel = CommandChannel("A", tmp_path, policy=SHORT_POLICY, platform=WINDOWS)
    channel.write_session("m1", 1, 0)
    channel.write(spawn_command(1))
    monkeypatch.setattr(os, "replace", HeldTarget(FakeTime()))

    with pytest.raises(ReplaceError):
        channel.write_session("m1", 2, 1)

    assert (channel.boot, channel.last_seq) == (1, 1)


def test_session_write_failure_before_the_replace_is_not_masked(tmp_path):
    channel = CommandChannel("A", tmp_path / "absent")

    with pytest.raises(FileNotFoundError) as caught:
        channel.write_session("m1", 1, 0)

    assert "session.txt.tmp" in str(caught.value)


def test_held_proofs_report_names_the_cause(tmp_path, monkeypatch):
    """`ReplaceError` n'a pas de `strerror` : le rapport ne doit pas dire `None`."""
    path = tmp_path / PROOFS_NAME
    reports = []
    proofs = HookProofs(path, reports.append, policy=SHORT_POLICY, platform=WINDOWS)
    monkeypatch.setattr(os, "replace", HeldTarget(FakeTime()))

    proofs.prove("build|mod", HOOK)

    (report,) = reports
    assert PROOFS_NAME in report
    assert "still in use" in report
    assert "None" not in report
    assert os.listdir(tmp_path) == []


def test_refused_proofs_report_keeps_the_system_reason(tmp_path, monkeypatch):
    reports = []
    proofs = HookProofs(tmp_path / PROOFS_NAME, reports.append, platform=LINUX)

    def refuse(src, dst):
        raise PermissionError(13, "refused")

    monkeypatch.setattr(os, "replace", refuse)
    proofs.prove("build|mod", HOOK)

    assert reports == [f"preuve de hook non enregistrée, {PROOFS_NAME} : refused"]
    assert os.listdir(tmp_path) == []


def test_proofs_drop_the_temporary_on_any_failure(tmp_path, monkeypatch):
    proofs = HookProofs(tmp_path / PROOFS_NAME, [].append)

    def explode(*args, **kwargs):
        raise TypeError("not serializable")

    monkeypatch.setattr(json, "dump", explode)
    with pytest.raises(TypeError):
        proofs.prove("build|mod", HOOK)

    assert os.listdir(tmp_path) == []


# AC16 par injection.


def test_windows_ini_needs_no_posix_mode(tmp_path, monkeypatch):
    def refuse(*args):
        raise AssertionError("POSIX mode set on Windows")

    monkeypatch.setattr(os, "fchmod", refuse, raising=False)
    for make_writer in (ini_writer, sandbox_writer):
        writer = make_writer(tmp_path, policy=SHORT_POLICY, platform=WINDOWS)
        writer.write(1)
        assert writer.holds(1)


# Le délai est celui du `runtime` de l'`Engine`.


def test_engine_gives_its_runtime_to_the_proofs(tmp_path, runtime, monkeypatch):
    seen = []
    monkeypatch.setattr(proofs_module, "replace_file", lambda *a, **kw: seen.append(kw["policy"]))
    runtime["replace_timeout_s"] = SHORT_POLICY["replace_timeout_s"]

    engine = Engine(mode=MODE_REAL, runtime=runtime, matches_dir=tmp_path)
    engine._proofs.prove("build|mod", HOOK)

    assert seen == [runtime]


def test_match_gives_its_runtime_to_every_writer(tmp_path, runtime, monkeypatch):
    seen = []

    def spy(src, dst, *, policy, platform):
        seen.append((Path(dst).name, policy))
        os.replace(src, dst)

    monkeypatch.setattr(servers, "replace_file", spy)
    monkeypatch.setattr(transport, "replace_file", spy)
    runtime["sandbox_start"] = {}
    zomboid = tmp_path / "Zomboid"
    (zomboid / "Server").mkdir(parents=True)
    for side in SIDES:
        sandbox = zomboid / "Server" / f"{server_name(side)}_SandboxVars.lua"
        sandbox.write_text(SANDBOX_TEXT, encoding="utf-8")
    match = RealMatch(
        {"password": "guest", "host_password": "host", "day_length": 1, "duration_s": 60},
        {"mods": ["duelcore"]},
        secrets=MatchSecrets("admin", "rcon"),
        runtime=runtime,
        effects=load_effects(),
        zomboid_dir=zomboid,
        pz_dir=tmp_path,
        matches_dir=tmp_path / "matches",
        proofs=HookProofs(tmp_path / PROOFS_NAME, [].append),
        clock=time.time,
        stop=threading.Event(),
        server_factory=lambda *args: None,
        on_status=lambda side, status, detail: None,
        on_state=lambda change: None,
    )

    match._prepare_launch()

    expected = {
        name
        for side in SIDES
        for name in (
            f"{server_name(side)}.ini",
            f"{server_name(side)}_SandboxVars.lua",
            f"duel_{side}_session.txt",
        )
    }
    assert {name for name, _ in seen} == expected
    assert all(policy is runtime for _, policy in seen)


# AC17 : ces tests valent sur les deux systèmes, ils tournent donc aussi sur le vrai Windows.


def replay_digest(records) -> str:
    result = [replay.replay_records(records), replay.summarize(records)]
    text = json.dumps(result, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("name", REPLAY_DIGESTS)
def test_fixture_replay_gives_the_recorded_result(name):
    path = FIXTURES / f"{name}.jsonl"
    assert b"\r" not in path.read_bytes()

    records = read_journal(path).records

    assert replay.first_gap(records, replay.replay_records(records)) is None
    assert replay_digest(records) == REPLAY_DIGESTS[name]


def test_every_fixture_has_a_recorded_result():
    assert {path.stem for path in FIXTURES.glob("*.jsonl")} == set(REPLAY_DIGESTS)


# AC13 à AC16 sur le vrai Windows (D-A) -----------------------------------------------------

if sys.platform == "win32":
    # `open()` passe par le CRT, qui partage lecture et écriture, jamais la suppression.
    HOLDER = """
import sys, time
with open(sys.argv[1], "rb"):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""

    @contextlib.contextmanager
    def held_by_another_process(path: Path, hold_s: float) -> Iterator[None]:
        """Rend la main une fois `path` tenu par un autre processus, sans `FILE_SHARE_DELETE`."""
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(path), str(hold_s)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "held"
            yield
        finally:
            holder.kill()
            holder.communicate(timeout=SUBPROCESS_TIMEOUT_S)

    def test_windows_holder_really_refuses_the_replacement(tmp_path):
        """Sans ce refus, les tests suivants passeraient sans rien mesurer."""
        target = tmp_path / "target"
        temporary = tmp_path / "target.tmp"
        target.write_text("old", encoding="utf-8")
        temporary.write_text("new", encoding="utf-8")

        with held_by_another_process(target, SUBPROCESS_TIMEOUT_S):
            with pytest.raises(PermissionError):
                os.replace(temporary, target)

    def test_windows_in_has_no_carriage_return(tmp_path):
        channel = CommandChannel("A", tmp_path)
        channel.write_session("m1", 1, 0)
        line = dump_line(spawn_command(1))

        channel.write(spawn_command(1))

        assert channel.in_path.stat().st_size == len(line)
        assert 0x0D not in channel.in_path.read_bytes()

    @each_writer
    def test_windows_writer_survives_a_short_hold(tmp_path, make_writer):
        writer = make_writer(tmp_path, policy=load_runtime())
        writer.write(1)

        with held_by_another_process(writer.target, HOLD_S):
            writer.write(2)

        assert writer.holds(2)
        assert folder_of(writer) == [writer.target.name]

    @each_writer
    def test_windows_writer_gives_up_after_the_delay(tmp_path, make_writer):
        writer = make_writer(tmp_path, policy=SHORT_POLICY)
        writer.write(1)

        with held_by_another_process(writer.target, SUBPROCESS_TIMEOUT_S):
            with pytest.raises(ReplaceError) as caught:
                writer.write(2)

        assert writer.target.name in str(caught.value)
        assert writer.holds(1)
        assert folder_of(writer) == [writer.target.name]

    def test_windows_ini_write_and_ready_server_report_no_failure(tmp_path):
        writer = ini_writer(tmp_path / "Server")
        writer.target.parent.mkdir()
        writer.write(1)
        runtime = load_runtime()
        cfg = {**runtime, "zomboid_dir": tmp_path}
        server = ServerProcess("A", cfg, tmp_path / "match", Redactor(()))
        port = runtime["ports"]["A"]["game"]

        server._scan(f"{STARTED_MARKER} server is listening on port {port}")

        assert writer.holds(1)
        assert server.ready
        assert server.ini_failure is None


# AC4 et AC5 sur le vrai Windows (D-A) -------------------------------------------------------

if sys.platform == "win32":
    LOOPBACK = "127.0.0.1"

    @contextlib.contextmanager
    def held_port(kind: int, *, reuse: bool = False, host: str = "") -> Iterator[int]:
        """Tient un port libre, avec les options que poserait un autre programme."""
        with socket.socket(socket.AF_INET, kind) as holder:
            if reuse:
                holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind((host, 0))
            if kind == socket.SOCK_STREAM:
                holder.listen()
            yield holder.getsockname()[1]

    def free_port(kind: int) -> int:
        with held_port(kind) as port:
            return port

    def test_windows_real_ram_measure_is_positive():
        measured = preflight.read_windows_ram_mb()
        assert isinstance(measured, int) and measured > 0

    @pytest.mark.parametrize("reuse", [False, True], ids=["exclusive", "reuseaddr"])
    @pytest.mark.parametrize("host", ["", LOOPBACK], ids=["wildcard", "loopback"])
    def test_windows_held_tcp_port_is_one_failure(reuse, host):
        with held_port(socket.SOCK_STREAM, reuse=reuse, host=host) as port:
            free, failures = preflight.check_ports({"A": {"rcon": port}})
        (failure,) = failures
        assert free == [] and f"Port {port}/TCP" in failure

    @pytest.mark.parametrize("host", ["", LOOPBACK], ids=["wildcard", "loopback"])
    def test_windows_bound_udp_port_is_one_failure(host):
        with held_port(socket.SOCK_DGRAM, host=host) as port:
            free, failures = preflight.check_ports({"A": {"game": port}})
        (failure,) = failures
        assert free == [] and f"Port {port}/UDP" in failure

    def test_windows_free_port_gives_no_failure():
        tcp, udp = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_DGRAM)
        free, failures = preflight.check_ports({"A": {"rcon": tcp, "game": udp}})
        assert failures == [] and free == [f"{tcp}/TCP", f"{udp}/UDP"]

    def test_windows_injected_10013_is_unusable(monkeypatch):
        def refuse(port, kind, platform):
            raise OSError(preflight.WSAEACCES, "forbidden")

        monkeypatch.setattr(preflight, "_bind", refuse)
        (failure,) = preflight.check_ports({"A": {"rcon": 27015}})[1]
        assert "Port 27015/TCP" in failure and "inutilisable" in failure


# R2 : commande, drapeaux et contrôle des arguments ------------------------------------------

GAME_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Project Zomboid"
ADMIN_MARKER = "adminsecretmarker"
RCON_MARKER = "rconsecretmarker"
CLIENT_GRACE_S = 0.05
WAIT_CLIENT_EXIT_S = 10
LEAKY_LINE = "commande : -adminpassword {adminpassword}"


def launch_cfg(runtime: dict, pz_dir: str, admin: str = ADMIN_MARKER) -> dict:
    return {**runtime, "pz_dir": pz_dir, "admin_password": admin}


@pytest.mark.parametrize("side", SIDES)
def test_windows_command_keeps_its_eight_arguments(runtime, side):
    ports = runtime["ports"][side]

    command = launch_command(side, launch_cfg(runtime, GAME_DIR), WINDOWS)

    assert command == [
        GAME_DIR + r"\StartServer64.bat",
        "-servername",
        server_name(side),
        "-port",
        str(ports["game"]),
        "-udpport",
        str(ports["udp"]),
        "-adminpassword",
        ADMIN_MARKER,
    ]


def test_linux_command_uses_the_linux_script(runtime):
    command = launch_command("A", launch_cfg(runtime, "/opt/pz"), LINUX)

    assert command[0] == "/opt/pz/start-server.sh"
    assert len(command[1:]) == 8


def test_server_popen_options_follow_the_platform():
    assert LINUX.server_popen_kwargs() == {"start_new_session": True}
    assert WINDOWS.server_popen_kwargs() == {
        "creationflags": CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    }


def test_client_popen_attempts_follow_the_platform():
    assert LINUX.client_popen_attempts() == ({"start_new_session": True},)
    detached = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    assert WINDOWS.client_popen_attempts() == (
        {"creationflags": detached | CREATE_BREAKAWAY_FROM_JOB},
        {"creationflags": detached},
    )


def test_client_file_follows_the_platform(tmp_path):
    game = tmp_path / "ProjectZomboid" / "projectzomboid"

    assert WINDOWS.client_file(game) == game / "ProjectZomboid64.exe"
    assert LINUX.client_file(game) == game.parent / "projectzomboid.sh"


@pytest.mark.parametrize(
    "folder",
    [
        GAME_DIR,
        r"D:\Games(SSD)\Project Zomboid",
        r"D:\Games(SSD)\ProjectZomboid",
        r"D:\Jeux & Co\Project Zomboid",
        r"D:\Jeux^2 <1>\Project Zomboid",
    ],
    ids=["espaces", "parentheses", "parentheses-sans-blanc", "esperluette", "autres-entre-guillemets"],
)
def test_quoted_or_literal_folders_are_accepted(runtime, folder):
    WINDOWS.check_arguments(launch_command("A", launch_cfg(runtime, folder), WINDOWS))


@pytest.mark.parametrize(
    "folder",
    [r"D:\100%\Project Zomboid", r"D:\Wow!\Project Zomboid", r"D:\A&B\PZ", r"D:\A^B\PZ"],
    ids=["pourcent", "exclamation", "esperluette-nue", "circonflexe-nu"],
)
def test_interpreted_folders_are_refused(runtime, folder):
    with pytest.raises(UnsafeArgument, match="argument 0"):
        WINDOWS.check_arguments(launch_command("A", launch_cfg(runtime, folder), WINDOWS))


@pytest.mark.parametrize("character", list('&|<>^%!"\r\n'))
def test_interpreted_password_is_refused_without_being_cited(runtime, character):
    password = f"secret{character}marker"
    command = launch_command("A", launch_cfg(runtime, GAME_DIR, password), WINDOWS)

    with pytest.raises(UnsafeArgument, match="argument 8") as caught:
        WINDOWS.check_arguments(command)

    assert "secret" not in str(caught.value)
    assert "marker" not in str(caught.value)


def test_linux_arguments_are_not_checked_against_cmd():
    LINUX.check_arguments(["/opt/a&b/start-server.sh", "100%"])


def test_refused_argument_never_reaches_popen(tmp_path, runtime, monkeypatch):
    launch = WindowsLaunchOnPosix()
    monkeypatch.setattr(subprocess, "Popen", launch)
    cfg = {
        **launch_cfg(runtime, str(tmp_path), "a&calc"),
        "zomboid_dir": tmp_path,
        "rcon_password": RCON_MARKER,
    }
    server = ServerProcess(
        "A", cfg, tmp_path / "match", Redactor(()), platform=WINDOWS, kernel=PosixKernel()
    )

    with pytest.raises(OSError, match="cmd.exe"):
        server.start()

    assert launch.calls == []


# R2 : le client ------------------------------------------------------------------------------


def test_windows_missing_client_is_not_retried(tmp_path, monkeypatch):
    launch = WindowsLaunchOnPosix()
    monkeypatch.setattr(subprocess, "Popen", launch)

    with pytest.raises(ClientLaunchError, match="ne démarre pas") as caught:
        launch_client(tmp_path / WINDOWS.client_program, ["+password", "marker"], WINDOWS)

    assert "marker" not in str(caught.value)
    assert len(launch.calls) == 1


# R2 : l'arbre, sur un `kernel` scripté -------------------------------------------------------


class Root:
    """Tient lieu de `Popen` pour l'arbre : aucun vrai processus n'est en jeu."""

    pid = 4242

    def __init__(self):
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


class ScriptedKernel:
    def __init__(self, active=(1,)):
        self.calls = []
        self.active = iter(active)

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, *args))
            return len(self.calls)

        return call

    def active_processes(self, job):
        count = next(self.active)
        if isinstance(count, Exception):
            raise count
        return count


def test_tree_terminate_ends_the_root_and_kill_ends_the_job():
    kernel = ScriptedKernel()
    tree = WindowsProcessTree(Root(), kernel)
    kernel.calls.clear()

    tree.terminate()
    tree.kill()

    names = [call[0] for call in kernel.calls]
    assert names == ["terminate_process", "terminate_job"]


def test_tree_counts_the_job_and_closes_it_once():
    kernel = ScriptedKernel(active=[2, 1, 0])
    tree = WindowsProcessTree(Root(), kernel)

    assert tree.alive() and tree.alive()
    assert not [call for call in kernel.calls if call[0] == "close"]
    assert not tree.alive()
    assert not tree.alive()
    assert len([call for call in kernel.calls if call[0] == "close"]) == 2


def test_failed_probe_proves_no_end():
    tree = WindowsProcessTree(Root(), ScriptedKernel(active=[OSError("refused")]))

    assert tree.alive()


@pytest.mark.parametrize("method", ["terminate", "kill"])
def test_refused_end_does_not_raise(method):
    kernel = PosixKernel(failing=("terminate_process", "terminate_job"))
    root = Root()
    tree = WindowsProcessTree(root, kernel)

    getattr(tree, method)()

    assert not root.killed


def test_failed_adoption_kills_the_suspended_process():
    root = Root()

    with pytest.raises(OSError, match="resume"):
        WindowsProcessTree(root, PosixKernel(failing=("resume",)))

    assert root.killed


def test_process_tree_follows_the_platform():
    assert isinstance(LINUX.process_tree(Root()), PosixProcessTree)
    assert isinstance(WINDOWS.process_tree(Root(), ScriptedKernel()), WindowsProcessTree)


# R2 : `WindowsKernel`, sur des bibliothèques simulées ----------------------------------------

PID = 4242
THREADS = [(11, PID), (12, 999), (13, PID)]


def fake_kernel32(calls: list, threads=THREADS, **overrides) -> SimpleNamespace:
    """`kernel32` en simples fonctions : `_declare` peut y poser ses prototypes."""
    walk = iter(())

    def record(name, result):
        def call(*args):
            calls.append((name, *args))
            return result

        return call

    def next_thread(snapshot, entry):
        try:
            thread_id, owner = next(walk)
        except StopIteration:
            return 0
        entry._obj.th32ThreadID = thread_id
        entry._obj.th32OwnerProcessID = owner
        return 1

    def first_thread(snapshot, entry):
        nonlocal walk
        walk = iter(threads)
        return next_thread(snapshot, entry)

    names = {
        "OpenProcess": 7,
        "OpenThread": 8,
        "CloseHandle": 1,
        "CreateJobObjectW": 9,
        "SetInformationJobObject": 1,
        "QueryInformationJobObject": 1,
        "AssignProcessToJobObject": 1,
        "TerminateJobObject": 1,
        "TerminateProcess": 1,
        "CreateToolhelp32Snapshot": 10,
        "ResumeThread": 1,
    }
    functions = {name: record(name, result) for name, result in names.items()}
    functions |= {"Thread32First": first_thread, "Thread32Next": next_thread}
    return SimpleNamespace(**(functions | overrides))


def nt_resume(calls: list, status: int) -> SimpleNamespace:
    def resume(handle):
        calls.append(("NtResumeProcess", handle))
        return status

    return SimpleNamespace(NtResumeProcess=resume)


def test_resume_goes_through_nt_resume_process():
    calls: list = []
    kernel = WindowsKernel(fake_kernel32(calls), nt_resume(calls, 0))

    kernel.resume(7, PID)

    assert calls == [("NtResumeProcess", 7)]


@pytest.mark.parametrize(
    "make_ntdll",
    [lambda calls: SimpleNamespace(), lambda calls: nt_resume(calls, -1)],
    ids=["absent", "refus"],
)
def test_resume_falls_back_on_the_threads_of_the_process(make_ntdll):
    calls: list = []
    kernel = WindowsKernel(fake_kernel32(calls), make_ntdll(calls))

    kernel.resume(7, PID)

    opened = [call[3] for call in calls if call[0] == "OpenThread"]
    assert opened == [11, 13]
    assert [call[0] for call in calls].count("ResumeThread") == 2
    closed = [call[1] for call in calls if call[0] == "CloseHandle"]
    assert closed == [8, 8, 10], "chaque fil, puis le relevé"


def test_resume_without_a_thread_fails():
    calls: list = []
    kernel = WindowsKernel(fake_kernel32(calls, threads=[(12, 999)]), SimpleNamespace())

    with pytest.raises(OSError, match=str(PID)):
        kernel.resume(7, PID)


def test_refused_thread_resume_is_an_error(monkeypatch):
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    calls: list = []
    refused = fake_kernel32(calls, ResumeThread=lambda thread: platforms.RESUME_FAILED)
    kernel = WindowsKernel(refused, SimpleNamespace())

    with pytest.raises(OSError, match="ResumeThread"):
        kernel.resume(7, PID)

    assert [call[1] for call in calls if call[0] == "CloseHandle"] == [8, 10]


def test_job_kills_on_close_and_its_handle_is_not_inheritable():
    limits = []

    def set_limits(job, kind, info, size):
        limits.append((job, kind, info._obj.BasicLimitInformation.LimitFlags, size))
        return 1

    calls: list = []
    kernel = WindowsKernel(
        fake_kernel32(calls, SetInformationJobObject=set_limits), SimpleNamespace()
    )

    assert kernel.create_job() == 9

    assert ("CreateJobObjectW", None, None) in calls, "attributs de sécurité : aucun héritage"
    assert limits == [
        (
            9,
            platforms.JOB_EXTENDED_LIMIT_INFORMATION,
            platforms.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            ctypes.sizeof(platforms._JobExtendedLimits),
        )
    ]


def test_job_without_its_limit_is_closed_and_refused(monkeypatch):
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    calls: list = []
    refused = fake_kernel32(calls, SetInformationJobObject=lambda *args: 0)
    kernel = WindowsKernel(refused, SimpleNamespace())

    with pytest.raises(OSError, match="SetInformationJobObject"):
        kernel.create_job()

    assert ("CloseHandle", 9) in calls


def test_active_processes_reads_the_accounting_of_the_job():
    def query(job, kind, info, size, returned):
        assert kind == platforms.JOB_BASIC_ACCOUNTING_INFORMATION
        info._obj.ActiveProcesses = 3
        return 1

    kernel = WindowsKernel(
        fake_kernel32([], QueryInformationJobObject=query), SimpleNamespace()
    )

    assert kernel.active_processes(9) == 3


def test_win32_structures_have_their_64_bit_sizes():
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(platforms._JobExtendedLimits) == 144
    assert ctypes.sizeof(platforms._JobAccounting) == 48
    assert ctypes.sizeof(platforms._ThreadEntry) == 28


# Le double ------------------------------------------------------------------------------------


def test_windows_double_is_a_batch_that_calls_python(tmp_path):
    script = install_fake_server(tmp_path, {"script_tail": "PAUSE"}, WINDOWS)

    assert script == tmp_path / "StartServer64.bat"
    lines = script.read_bytes().decode().split("\r\n")
    assert lines[0] == "@echo off"
    assert sys.executable in lines[1] and lines[1].endswith(" %*")
    assert lines[2:] == ["PAUSE", ""]


# AC7 et AC12 : le vrai `Engine`, plateforme injectée. Ces tests jouent les processus sur le
# double POSIX ; sur le vrai Windows, le bloc suivant les remplace (D-A).

if sys.platform != "win32":

    def write_client(path: Path, body: str) -> Path:
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path


    def test_windows_client_breaks_away_and_stays_detached(tmp_path, monkeypatch):
        monkeypatch.setattr(engine_module, "CLIENT_START_GRACE_S", CLIENT_GRACE_S)
        launch = WindowsLaunchOnPosix()
        monkeypatch.setattr(subprocess, "Popen", launch)
        client = write_client(tmp_path / WINDOWS.client_program, "exec sleep 1")

        process = launch_client(client, ["-nosteam"], WINDOWS)

        assert process.poll() is None
        process.kill()
        process.wait()
        [(command, options)] = launch.calls
        assert command == [str(client), "-nosteam"]
        assert options["creationflags"] & CREATE_BREAKAWAY_FROM_JOB
        assert options["creationflags"] & DETACHED_PROCESS
        assert options["stdout"] == subprocess.DEVNULL


    def test_windows_client_falls_back_when_the_job_refuses_breakaway(tmp_path, monkeypatch):
        def refuse(flags: int) -> None:
            if flags & CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(engine_module, "CLIENT_START_GRACE_S", CLIENT_GRACE_S)
        launch = WindowsLaunchOnPosix(refuse)
        monkeypatch.setattr(subprocess, "Popen", launch)
        client = write_client(tmp_path / WINDOWS.client_program, "exec sleep 1")

        process = launch_client(client, [], WINDOWS)

        process.kill()
        process.wait()
        first, second = (options["creationflags"] for _, options in launch.calls)
        assert first & CREATE_BREAKAWAY_FROM_JOB
        assert not second & CREATE_BREAKAWAY_FROM_JOB

    def test_double_records_the_arguments_it_receives(tmp_path):
        args_file = tmp_path / "args.json"
        script = install_fake_server(tmp_path, {"args_file": str(args_file), "exit_code": 0}, LINUX)
        arguments = [
            "-servername", "duelA", "-port", "1", "-udpport", "2", "-adminpassword", "a b",
        ]  # fmt: skip

        subprocess.run([script, *arguments], check=True, timeout=SUBPROCESS_TIMEOUT_S)

        assert json.loads(args_file.read_text(encoding="utf-8")) == arguments

    @pytest.fixture
    def marked_secrets(monkeypatch):
        drawn = cycle((ADMIN_MARKER, RCON_MARKER))
        monkeypatch.setattr("service.engine.gen_secret", lambda: next(drawn))


    def engine_on(rig, platform, monkeypatch, **scenario) -> Engine:
        """Moteur du banc `engine_rig` sur `platform`, le double POSIX jouant les processus."""
        rig.build(**scenario)
        if platform is LINUX:
            return rig.engine
        monkeypatch.setattr(subprocess, "Popen", WindowsLaunchOnPosix())
        real_preflight = engine_module.run_preflight
        monkeypatch.setattr(
            engine_module, "run_preflight", partial(real_preflight, ram_reader=lambda: 1)
        )
        (rig.pz_dir / WINDOWS.server_script).write_text("", encoding="utf-8")
        write_client(rig.pz_dir / WINDOWS.client_program, "exec sleep 1")

        def factory(side, cfg, match_dir, redactor) -> ServerProcess:
            server = ServerProcess(
                side, cfg, match_dir, redactor, platform=WINDOWS, kernel=PosixKernel()
            )
            rig.servers.append(server)
            return server

        rig.engine = Engine(
            "real",
            zomboid_dir=rig.zomboid,
            pz_dir=rig.pz_dir,
            runtime=rig.runtime,
            matches_dir=rig.matches,
            server_factory=factory,
            platform=WINDOWS,
        )
        return rig.engine


    def exposed_text(engine: Engine, rig, caplog, *messages: str) -> str:
        """Tout ce qui sort : messages, état, log du service et fichiers du match.

        Au lobby seulement, `public_config` publie `host_password` à l'hôte, pour le repli manuel
        (`service/state.py`) : cette clé de l'état est écartée ici, rien d'autre.
        """
        state = engine.snapshot()
        config = {key: value for key, value in state["config"].items() if key != "host_password"}
        if state["phase"] != "lobby":
            assert config == state["config"]
        files = [path for path in rig.matches.rglob("*") if path.is_file()]
        return "\n".join(
            [
                *messages,
                json.dumps({**state, "config": config}, ensure_ascii=False),
                json.dumps(engine.state.logs, ensure_ascii=False),
                caplog.text,
                *(path.read_text(encoding="utf-8") for path in files),
            ]
        )


    def test_engine_launches_windows_servers_and_client_as_specified(
        engine_rig, marked_secrets, monkeypatch, caplog
    ):
        caplog.set_level(logging.DEBUG)
        monkeypatch.setattr(engine_module, "CLIENT_START_GRACE_S", CLIENT_GRACE_S)
        rig = engine_rig
        engine = engine_on(rig, WINDOWS, monkeypatch, lines=[LEAKY_LINE, *ENGINE_READY])
        launch = subprocess.Popen

        rig.start()
        state = rig.wait("lobby", "error")
        assert state["phase"] == "lobby", state["error"]

        for side in SIDES:
            ports = rig.runtime["ports"][side]
            commands = [c for c, _ in launch.calls if server_name(side) in c]
            assert commands, "le serveur a été lancé"
            assert commands[-1] == [
                ntpath.join(str(rig.pz_dir), "StartServer64.bat"),
                "-servername",
                server_name(side),
                "-port",
                str(ports["game"]),
                "-udpport",
                str(ports["udp"]),
                "-adminpassword",
                ADMIN_MARKER,
            ]
        flags = {options["creationflags"] for _, options in launch.calls}
        assert flags == {WINDOWS.server_popen_kwargs()["creationflags"]}
        for server in rig.servers:
            assert server._cfg["stop_timeout_s"] == rig.runtime["stop_timeout_s"]
            assert server._cfg["kill_grace_s"] == rig.runtime["kill_grace_s"]

        ok, message = engine.action("open_game")
        assert ok, message
        client, _ = launch.calls[-1]
        address, password = state["config"]["host_address"], state["config"]["host_password"]
        assert client == [
            str(rig.pz_dir / "ProjectZomboid64.exe"),
            "-nosteam",
            "+connect",
            address,
            "+password",
            password,
        ]
        engine._client.kill()
        engine._client.wait()

        text = exposed_text(engine, rig, caplog, message)
        assert ADMIN_MARKER not in text and RCON_MARKER not in text
        assert password not in text
        assert f"-adminpassword {REDACTED}" in text


    PLATFORMS = pytest.mark.parametrize("platform", [LINUX, WINDOWS], ids=["linux", "windows"])


    def assert_no_password(engine, rig, caplog, password, *messages) -> None:
        text = exposed_text(engine, rig, caplog, *messages)
        for secret in (ADMIN_MARKER, RCON_MARKER, password):
            assert secret not in text


    @PLATFORMS
    def test_missing_server_script_reveals_no_password(
        engine_rig, marked_secrets, monkeypatch, caplog, platform
    ):
        caplog.set_level(logging.DEBUG)
        rig = engine_rig
        engine = engine_on(rig, platform, monkeypatch)
        real_preflight = engine_module.run_preflight

        def preflight_then_lose_the_script(*args, **kwargs):
            result = real_preflight(*args, **kwargs)
            (rig.pz_dir / LINUX.server_script).unlink()
            return result

        monkeypatch.setattr(engine_module, "run_preflight", preflight_then_lose_the_script)

        rig.start()
        state = rig.wait("error", "lobby")

        assert state["phase"] == "error"
        assert "lancement impossible" in state["error"]
        assert_no_password(engine, rig, caplog, engine.state.config["host_password"], state["error"])


    @PLATFORMS
    def test_immediate_server_exit_reveals_no_password(
        engine_rig, marked_secrets, monkeypatch, caplog, platform
    ):
        caplog.set_level(logging.DEBUG)
        rig = engine_rig
        engine = engine_on(rig, platform, monkeypatch, lines=[LEAKY_LINE], exit_code=3)

        rig.start()
        state = rig.wait("error", "lobby")

        assert state["phase"] == "error"
        assert "sortie anticipée, code 3" in state["error"]
        assert f"-adminpassword {REDACTED}" in state["error"]
        assert_no_password(engine, rig, caplog, engine.state.config["host_password"], state["error"])


    def in_lobby(rig) -> dict:
        rig.start()
        state = rig.wait("lobby", "error")
        assert state["phase"] == "lobby", state["error"]
        return state


    @PLATFORMS
    def test_missing_client_reveals_no_password(
        engine_rig, marked_secrets, monkeypatch, caplog, platform
    ):
        caplog.set_level(logging.DEBUG)
        rig = engine_rig
        engine = engine_on(rig, platform, monkeypatch)
        state = in_lobby(rig)
        client = platform.client_file(rig.pz_dir)
        client.unlink(missing_ok=True)
        if platform is WINDOWS:
            # La détection exige ce fichier : il reste, mais ne s'exécute plus.
            client.write_text("", encoding="utf-8")
            client.chmod(0o600)

        ok, message = engine.action("open_game")

        assert not ok
        assert f"{platform.client_program} absent" in message
        assert_no_password(engine, rig, caplog, state["config"]["host_password"], message)


    @PLATFORMS
    def test_immediate_client_exit_reveals_no_password(
        engine_rig, marked_secrets, monkeypatch, caplog, platform
    ):
        caplog.set_level(logging.DEBUG)
        monkeypatch.setattr(engine_module, "CLIENT_START_GRACE_S", WAIT_CLIENT_EXIT_S)
        rig = engine_rig
        engine = engine_on(rig, platform, monkeypatch)
        state = in_lobby(rig)
        write_client(platform.client_file(rig.pz_dir), "exit 3")

        ok, message = engine.action("open_game")

        assert not ok
        assert "s'est arrêté aussitôt (code 3)" in message
        assert_no_password(engine, rig, caplog, state["config"]["host_password"], message)


# AC8 à AC11 sur le vrai Windows (D-A) --------------------------------------------------------

# R2 : ce que la CI réelle a montré des arrêts et du double, rejoué sur toute plateforme.

STOP_CFG = {"tick_ms": 1, "stop_timeout_s": 0.2, "kill_grace_s": 0.2, "rcon_timeout_s": 1}


class SlowRefusedRcon:
    """RCON absent à la façon de Windows : le `connect` occupe tout le délai qu'on lui donne."""

    deadlines: list[float] = []

    def __init__(self, port, password, timeout_s, deadline):
        self.deadlines.append(deadline)
        self._deadline = deadline

    def __enter__(self):
        time.sleep(max(0, self._deadline - time.monotonic()))
        raise RconError("RCON injoignable")

    def __exit__(self, *exc_info):
        return False


class QuitOnStdin:
    """`stdin` d'un serveur qui part dès qu'il lit `quit`."""

    def __init__(self, tree):
        self._tree = tree

    def write(self, line):
        self._tree.active = 0

    def flush(self):
        pass

    def close(self):
        pass


class ScriptedTree:
    """Arbre sans processus : `can_terminate` dit si le palier `sigterm` a prise."""

    def __init__(self, can_terminate=True):
        self.active = 1
        self.calls = []
        self._can_terminate = can_terminate

    def alive(self):
        return bool(self.active)

    def can_terminate(self):
        return self._can_terminate

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")
        self.active = 0


def stopping_server(tmp_path, runtime, monkeypatch, tree, stdin=None) -> ServerProcess:
    monkeypatch.setattr(SlowRefusedRcon, "deadlines", [])
    monkeypatch.setattr(servers, "RconClient", SlowRefusedRcon)
    cfg = {**runtime, **STOP_CFG, "zomboid_dir": tmp_path, "rcon_password": RCON_MARKER}
    server = ServerProcess("A", cfg, tmp_path, Redactor(()))
    server._tree = tree
    server._process = SimpleNamespace(stdin=stdin or io.BytesIO(), poll=lambda: None)
    return server


def test_slow_rcon_failure_leaves_stdin_its_share_of_the_stop_delay(
    tmp_path, runtime, monkeypatch
):
    """Sous Windows, `connect` échoue en deux secondes : `stdin` doit encore avoir son délai."""
    tree = ScriptedTree()
    server = stopping_server(tmp_path, runtime, monkeypatch, tree, QuitOnStdin(tree))
    begin = time.monotonic()

    assert server.stop()

    assert server.stop_stage == STOP_STDIN
    assert tree.calls == []
    share_s = STOP_CFG["stop_timeout_s"] * RCON_STOP_SHARE
    assert SlowRefusedRcon.deadlines[0] - begin <= share_s * TOLERANCE


def test_rcon_share_does_not_lengthen_the_stop(tmp_path, runtime, monkeypatch):
    tree = ScriptedTree()
    server = stopping_server(tmp_path, runtime, monkeypatch, tree)
    begin = time.monotonic()

    assert server.stop()

    elapsed = time.monotonic() - begin
    assert 0 < RCON_STOP_SHARE < 1
    assert tree.calls == ["terminate", "kill"]
    planned_s = STOP_CFG["stop_timeout_s"] + STOP_CFG["kill_grace_s"]
    assert planned_s <= elapsed < planned_s + STOP_CFG["kill_grace_s"]
    assert server.stop_stage == STOP_SIGKILL


def test_stage_without_a_hold_on_the_tree_is_skipped_not_renamed(tmp_path, runtime, monkeypatch):
    tree = ScriptedTree(can_terminate=False)
    server = stopping_server(tmp_path, runtime, monkeypatch, tree)

    assert server.stop()

    assert tree.calls == ["kill"]
    assert server.stop_stage == STOP_SIGKILL


def test_stop_vocabulary_is_the_one_of_the_plan():
    stages = (STOP_RCON, STOP_STDIN, STOP_SIGTERM, STOP_SIGKILL)
    assert stages == ("rcon", "stdin", "sigterm", "sigkill")


@pytest.mark.parametrize(("code", "expected"), [(None, True), (0, False)])
def test_windows_root_gone_takes_the_hold_of_the_sigterm_stage(code, expected):
    process = SimpleNamespace(pid=1, poll=lambda: code, kill=lambda: None)
    kernel = PosixKernel()
    kernel.open_process = lambda pid: 1
    kernel.assign = lambda job, root: None

    assert WindowsProcessTree(process, kernel).can_terminate() is expected


def test_posix_tree_always_takes_the_sigterm_stage():
    assert PosixProcessTree(SimpleNamespace(pid=1)).can_terminate()


def test_double_child_does_not_share_the_stdin_the_double_reads(monkeypatch):
    """Sous Windows, l'enfant qui en hérite reste bloqué au démarrage de Python : la CI réelle
    n'a jamais vu ses descendants, et wine ne reproduit pas ce verrou."""
    seen = {}

    def spy(command, **options):
        seen.update(options)

    monkeypatch.setattr(fake_server.subprocess, "Popen", spy)

    fake_server.spawn_child(1, 0)

    assert seen["stdin"] == subprocess.DEVNULL


def test_launcher_generation_adds_no_bat_to_the_disk(
    engine_rig, monkeypatch, tmp_path, api_port  # noqa: F811
):
    """Le rig pose déjà ses propres `.bat` sous Windows : seul l'écart compte ici."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(data_dir))
    detect(monkeypatch, LAN_ADDRESS)
    lobby = Lobby(engine_rig, PRESET)
    before = set(tmp_path.rglob("*.bat"))

    status, _, _ = request(api_port(lobby.engine), "GET", LAUNCHER_ROUTE)

    assert status == 200
    assert set(tmp_path.rglob("*.bat")) == before


if sys.platform == "win32":
    REPO_SERVICE = REPO / "tests" / "doubles" / "run_service.py"
    SERVICE_URL_PREFIX = "interface : "
    FAST_STOP = {"tick_ms": 10, "stop_timeout_s": 1, "kill_grace_s": 2, "rcon_timeout_s": 1}
    WAIT_S = 30
    POLL_S = 0.05
    SERVICE_GONE_S = 5
    CREATE_NEW_CONSOLE = 0x00000010
    TREE_SIZE = 4
    # Le script et le double précèdent leurs enfants dans la sortie.
    CHILDREN_START = 2
    # Pas d'ancre : l'invite de `PAUSE` n'a pas de fin de ligne, et un enfant peut la suivre.
    TREE_PID = re.compile(r"fake (?:script|server|child|grandchild) pid=(\d+)")
    TREE = {"child": True, "child_depth": 1, "announce_pids": True}
    # `AttachConsole` veut un processus sans console : ce script tourne donc à part.
    # Une vraie fenêtre reçoit `WM_CLOSE`, et sa console envoie `CTRL_CLOSE_EVENT` par un fil posé
    # sur `CtrlRoutine`. Sur `windows-latest`, la console est une pseudo-console qui ignore
    # ce message, même pour un Python sans gestionnaire : le script y pose lui-même ce fil.
    CLOSE_CONSOLE = """
import ctypes, sys
service_pid = int(sys.argv[1])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.GetConsoleWindow.restype = ctypes.c_void_p
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.CreateRemoteThread.restype = ctypes.c_void_p
kernel32.CreateRemoteThread.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_void_p,
]
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
kernel32.FreeConsole()
if not kernel32.AttachConsole(service_pid):
    sys.exit("AttachConsole failed: " + str(ctypes.get_last_error()))
window = kernel32.GetConsoleWindow()
if not window:
    sys.exit("no console window to close")
name = ctypes.create_unicode_buffer(64)
user32.GetClassNameW(window, name, len(name))
if name.value != "PseudoConsoleWindow":
    if not user32.PostMessageW(window, 0x0010, 0, 0):
        sys.exit("WM_CLOSE refused: " + str(ctypes.get_last_error()))
    sys.exit(0)
routine = ctypes.cast(ctypes.WinDLL("kernelbase").CtrlRoutine, ctypes.c_void_p)
# PROCESS_CREATE_THREAD, VM_OPERATION, VM_READ, VM_WRITE et QUERY_INFORMATION.
process = kernel32.OpenProcess(0x043A, False, service_pid)
if not process:
    sys.exit("OpenProcess failed: " + str(ctypes.get_last_error()))
# 2 vaut CTRL_CLOSE_EVENT.
if not kernel32.CreateRemoteThread(process, None, 0, routine, 2, 0, None):
    sys.exit("CreateRemoteThread failed: " + str(ctypes.get_last_error()))
"""

    def wait_until(condition: Callable[[], bool], timeout_s: float = WAIT_S) -> None:
        deadline = time.monotonic() + timeout_s
        while not condition():
            assert time.monotonic() < deadline, "condition jamais atteinte"
            time.sleep(POLL_S)

    def tree_pids(lines: list[str]) -> list[int]:
        return [int(pid) for line in lines for pid in TREE_PID.findall(line)]

    def real_server(pz_dir: Path, zomboid: Path, scenario: dict, **overrides) -> ServerProcess:
        pz_dir.mkdir(parents=True, exist_ok=True)
        install_fake_server(pz_dir, scenario)
        cfg = {
            **load_runtime(),
            **FAST_STOP,
            **overrides,
            "pz_dir": pz_dir,
            "zomboid_dir": zomboid,
            "admin_password": ADMIN_MARKER,
            "rcon_password": RCON_MARKER,
        }
        return ServerProcess("A", cfg, zomboid / "match", Redactor((ADMIN_MARKER, RCON_MARKER)))

    def ready_tree(server: ServerProcess, count: int) -> list[int]:
        server.start()
        wait_until(lambda: server.ready and len(tree_pids(server.log_tail())) == count)
        return tree_pids(server.log_tail())

    def test_windows_server_starts_from_a_folder_with_spaces_and_parentheses(tmp_path):
        pz_dir = tmp_path / "Program Files (x86)" / "Project Zomboid"
        args_file = tmp_path / "args.json"
        scenario = {"lines": READY_LINES, "args_file": str(args_file)}
        server = real_server(pz_dir, tmp_path, scenario)

        server.start()
        wait_until(lambda: server.ready)

        expected = launch_command("A", server._cfg)
        assert expected[0] == str(pz_dir / "StartServer64.bat")
        assert json.loads(args_file.read_text(encoding="utf-8")) == expected[1:]
        assert server.stop()

    @pytest.mark.parametrize(
        "scenario",
        [
            {"orphan_child": True, "exit_code": 0},
            {"refuse_quit": True},
        ],
        ids=["script-sur-pause", "double-sourd"],
    )
    def test_windows_stop_ends_a_paused_tree_at_the_sigkill_stage(tmp_path, scenario):
        full = {"lines": READY_LINES, "script_tail": "PAUSE", **TREE, **scenario}
        server = real_server(tmp_path / "pz", tmp_path, full)
        pids = ready_tree(server, TREE_SIZE)
        assert all(map(windows_pid_alive, pids[CHILDREN_START:]))

        assert server.stop()

        assert server.stop_stage == STOP_SIGKILL
        assert not server.running
        assert not any(map(windows_pid_alive, pids))

    def test_windows_early_child_cannot_escape_the_job(tmp_path):
        """Le double lance son enfant dès le départ : la suspension l'a déjà mis dans le Job."""
        scenario = {"lines": READY_LINES, "refuse_quit": True, **TREE}
        server = real_server(tmp_path / "pz", tmp_path, scenario)
        pids = ready_tree(server, TREE_SIZE)

        server._tree.kill()

        wait_until(lambda: not any(map(windows_pid_alive, pids)), FAST_STOP["kill_grace_s"])
        assert not server.running

    def test_windows_normal_stop_goes_through_rcon(tmp_path):
        with socket.create_server(("127.0.0.1", 0)) as probe:
            port = probe.getsockname()[1]
        runtime = load_runtime()
        ports = {**runtime["ports"], "A": {**runtime["ports"]["A"], "rcon": port}}
        scenario = {"lines": READY_LINES, "rcon_password": RCON_MARKER, "rcon_port": port}
        server = real_server(tmp_path / "pz", tmp_path, scenario, ports=ports)

        server.start()
        wait_until(lambda: server.ready)

        assert server.stop()
        assert server.stop_stage == STOP_RCON
        assert not server.running

    def test_windows_failed_launches_reveal_no_password(tmp_path):
        missing = real_server(tmp_path / "pz", tmp_path, {})
        (tmp_path / "pz" / "StartServer64.bat").unlink()
        with pytest.raises(OSError) as caught:
            missing.start()
        assert ADMIN_MARKER not in missing.redactor.redact_exception(caught.value)

        leaky = {"lines": [LEAKY_LINE], "exit_code": 3}
        server = real_server(tmp_path / "pz2", tmp_path, leaky)
        server.start()
        wait_until(lambda: server.exit_code == 3)
        assert f"-adminpassword {REDACTED}" in server.log_tail()[-1]
        log = server.log_path.read_text(encoding="utf-8")
        assert ADMIN_MARKER not in log

    def test_windows_log_failure_keeps_draining(tmp_path, monkeypatch):
        """Pendant de `/dev/full` : le journal refuse d'écrire, et le tube reste lu."""

        class FullLog(io.RawIOBase):
            def writable(self) -> bool:
                return True

            def write(self, data) -> int:
                raise OSError(28, "No space left on device")

        monkeypatch.setattr(servers, "open", lambda *args, **kwargs: FullLog(), raising=False)
        flood = ["x" * 1024] * 256
        server = real_server(tmp_path / "pz", tmp_path, {"lines": [*flood, *READY_LINES]})

        server.start()
        wait_until(lambda: server.ready)

        assert "OSError" in server.log_error
        assert server.stop()

    def test_windows_client_double_is_a_real_pe(tmp_path):
        """Sans `csc.exe`, la compilation lève : ce test échoue, il ne saute pas."""
        client = compile_windows_client(tmp_path / "client.exe")
        args_file = tmp_path / "args"
        plan_windows_client(client, args_file, code=3)

        done = subprocess.run([str(client), "-nosteam"], timeout=SUBPROCESS_TIMEOUT_S)

        assert client.read_bytes()[:2] == b"MZ"
        assert done.returncode == 3
        assert args_file.read_text(encoding="utf-8") == "-nosteam\n"

    class WindowsService:
        """`service.main` dans son processus, ses deux doubles lancés et leurs pids relevés."""

        def __init__(self, root: Path, creationflags: int = 0) -> None:
            runtime = load_runtime()
            self.matches = root / "matches"
            zomboid = root / "Zomboid"
            pz_dir = root / "pz"
            pz_dir.mkdir()
            for name in ZOMBOID_SUBDIRS:
                (zomboid / name).mkdir(parents=True)
            preflight.install_mod(preflight.MOD_SRC, zomboid)
            install_fake_server(pz_dir, TREE)
            (pz_dir / WINDOWS.client_program).write_bytes(b"")
            config = root / "service.json"
            config.write_text(
                json.dumps(
                    {
                        "zomboid_dir": str(zomboid),
                        "matches_dir": str(self.matches),
                        "runtime": {
                            **runtime,
                            **FAST_STOP,
                            "min_free_ram_mb": 0,
                            "ports": free_ports(runtime),
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.process = subprocess.Popen(
                [
                    sys.executable, str(REPO_SERVICE), str(config),
                    "--mode", "real", "--no-browser", "--port", "0", "--pz-dir", str(pz_dir),
                ],
                env={**os.environ, "PYTHONPATH": str(REPO)},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                creationflags=creationflags,
            )  # fmt: skip
            self.url = self._read_url()

        def _read_url(self) -> str:
            for line in self.process.stdout:
                if SERVICE_URL_PREFIX in line:
                    return line.split(SERVICE_URL_PREFIX)[1].strip()
            raise AssertionError("le service est sorti sans publier son URL")

        def start_doubles(self) -> list[int]:
            payload = {"action": "start", "preset": "demo", "password": PLAYER_PASSWORD}
            headers = {"Content-Type": "application/json"}
            request = Request(f"{self.url}api/action", json.dumps(payload).encode(), headers)
            urlopen(request, timeout=SUBPROCESS_TIMEOUT_S).close()
            wait_until(self._doubles_up)
            return self.pids()

        def _doubles_up(self) -> bool:
            with urlopen(f"{self.url}api/state", timeout=SUBPROCESS_TIMEOUT_S) as response:
                state = json.load(response)
            assert state["phase"] != "error", state["error"]
            return len(self.pids()) == len(SIDES) * TREE_SIZE

        def pids(self) -> list[int]:
            lines = [
                line
                for path in self.matches.glob("*/server_*.log")
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            return tree_pids(lines)

        def close(self) -> None:
            """Filet d'un test en échec : un survivant ne doit pas bloquer la suite."""
            self.process.kill()
            for pid in filter(windows_pid_alive, self.pids()):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            self.process.communicate(timeout=SUBPROCESS_TIMEOUT_S)

    @contextlib.contextmanager
    def running_service(root: Path, creationflags: int = 0) -> Iterator[WindowsService]:
        service = WindowsService(root, creationflags)
        try:
            yield service
        finally:
            service.close()

    def assert_tree_ends_with_the_service(service: WindowsService, pids: list[int]) -> None:
        service.process.wait(SUBPROCESS_TIMEOUT_S)
        wait_until(lambda: not any(map(windows_pid_alive, pids)), SERVICE_GONE_S)

    def test_windows_forced_end_of_the_service_leaves_no_double(tmp_path):
        with running_service(tmp_path) as service:
            pids = service.start_doubles()
            assert all(map(windows_pid_alive, pids))

            subprocess.run(
                ["taskkill", "/F", "/PID", str(service.process.pid)],
                check=True,
                capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S,
            )

            assert_tree_ends_with_the_service(service, pids)

    def test_windows_closed_console_leaves_no_double(tmp_path):
        with running_service(tmp_path, CREATE_NEW_CONSOLE) as service:
            pids = service.start_doubles()
            assert all(map(windows_pid_alive, pids))

            closer = subprocess.run(
                [sys.executable, "-c", CLOSE_CONSOLE, str(service.process.pid)],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_S,
                creationflags=CREATE_NO_WINDOW,
            )

            assert closer.returncode == 0, closer.stderr

            assert_tree_ends_with_the_service(service, pids)


# AC53 et AC62 : la suite ne perd aucun test, et chaque skip nomme un test qui existe ---------

BASELINE = "1d854d0"
COLLECT_TIMEOUT_S = 180
TESTS_DIR = REPO / "tests"
GATED_MODULES = ("test_snapshots", "test_contract", "test_modes", "test_campaign_gate")
POSIX_NAMES = ("SIGKILL", "killpg")
WINDOWS_ONLY = "sys.platform == 'win32'"
NOT_WINDOWS = "sys.platform != 'win32'"
TEST_NAME = re.compile(r"\btest_\w+")


def extract_baseline(target: Path) -> None:
    done = subprocess.run(
        ["git", "archive", "--format=tar", BASELINE],
        cwd=REPO,
        capture_output=True,
        timeout=COLLECT_TIMEOUT_S,
    )
    assert done.returncode == 0, f"{BASELINE} is unreachable: {done.stderr.decode()}"
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        archive.extractall(target, filter="data")


def collected_functions(root: Path) -> set[str]:
    """Les fonctions de test de `root`, identifiants de paramètres retirés."""
    env = {name: value for name, value in os.environ.items() if name != "PYTEST_ADDOPTS"}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=root,
        env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=COLLECT_TIMEOUT_S,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return {line.split("[", 1)[0] for line in done.stdout.splitlines() if "::" in line}


def test_every_baseline_test_still_exists(tmp_path):
    extract_baseline(tmp_path)

    baseline = collected_functions(tmp_path)

    assert baseline
    assert baseline - collected_functions(REPO) == set()


def test_conftest_is_the_one_of_the_baseline():
    done = subprocess.run(
        ["git", "diff", "--name-only", BASELINE, "--", "tests/conftest.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


def is_windows_skip(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "skipif"
        and "win32" in ast.unparse(node.args[0])
    )


def skip_reason(call: ast.Call) -> str:
    """Littérale, sinon l'analyse statique ne pourrait pas la lire : c'est donc un échec."""
    reason = next((word.value for word in call.keywords if word.arg == "reason"), None)
    assert isinstance(reason, ast.Constant), ast.unparse(call)
    return reason.value


def skip_markers(tree: ast.Module) -> set[str]:
    """Les noms qui portent un skip Windows : `injected = pytest.mark.skipif(...)`."""
    return {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and is_windows_skip(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def windows_tests(body: list[ast.stmt], markers: set[str]) -> Iterator[str]:
    """Les tests qui s'exécutent sous Windows : blocs `if` compris, skips et blocs POSIX exclus."""
    for node in body:
        if isinstance(node, ast.If):
            guard = ast.unparse(node.test)
            if guard != NOT_WINDOWS:
                yield from windows_tests(node.body, markers)
            if guard != WINDOWS_ONLY:
                yield from windows_tests(node.orelse, markers)
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            skipped = any(
                is_windows_skip(mark) or ast.unparse(mark) in markers
                for mark in node.decorator_list
            )
            if not skipped:
                yield node.name


def citation_problems(sources: Mapping[str, str]) -> list[str]:
    """Rend chaque skip Windows sans test cité, ou citant un test absent de Windows (AC62)."""
    trees = {name: ast.parse(source) for name, source in sources.items()}
    existing = {
        test
        for tree in trees.values()
        for test in windows_tests(tree.body, skip_markers(tree))
    }
    problems = []
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if is_windows_skip(node):
                reason = skip_reason(node)
                cited = TEST_NAME.findall(reason)
                problems += [f"{name}: no test named: {reason}"] * (not cited)
                missing = [test for test in cited if test not in existing]
                problems += [f"{name}: no such test: {test}" for test in missing]
    return problems


def test_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in TESTS_DIR.glob("test_*.py")}


test_sources.__test__ = False


def test_windows_skips_name_a_test_that_runs_on_windows():
    sources = test_sources()
    assert any("skipif" in source and "win32" in source for source in sources.values())
    assert citation_problems(sources) == []


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        pytest.param(
            'S = pytest.mark.skipif(sys.platform == "win32", reason="see test_gone")\n',
            "no such test: test_gone",
            id="absent",
        ),
        pytest.param(
            'S = pytest.mark.skipif(sys.platform == "win32", reason="no equivalent")\n',
            "no test named",
            id="sans-nom",
        ),
        pytest.param(
            'if sys.platform != "win32":\n    def test_posix(): pass\n'
            'S = pytest.mark.skipif(sys.platform == "win32", reason="see test_posix")\n',
            "no such test: test_posix",
            id="bloc-posix",
        ),
        pytest.param(
            'S = pytest.mark.skipif(sys.platform == "win32", reason="see test_self")\n'
            "@S\ndef test_self(): pass\n",
            "no such test: test_self",
            id="lui-meme-saute",
        ),
    ],
)
def test_bad_citation_is_reported(source, problem):
    (found,) = citation_problems({"test_x.py": source})
    assert problem in found


def test_windows_block_test_counts_as_a_citation():
    source = (
        'if sys.platform == "win32":\n    def test_real(): pass\n'
        'S = pytest.mark.skipif(sys.platform == "win32", reason="see test_real")\n'
    )
    assert citation_problems({"test_x.py": source}) == []


@pytest.mark.parametrize("module", GATED_MODULES)
def test_gated_modules_leave_posix_stops_to_the_helpers(module):
    """AC62 : plus aucun appel direct, c'est `kill_tree` et `write_script` qui choisissent."""
    source = (TESTS_DIR / f"{module}.py").read_text(encoding="utf-8")
    names = {
        node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)
    }
    assert not names & set(POSIX_NAMES)
    assert "#!/bin/sh" not in source


def test_no_posix_name_is_evaluated_at_collection():
    """`signal.SIGKILL` hors d'une fonction casserait la collecte sous Windows."""

    def outside_functions(node: ast.AST) -> Iterator[ast.AST]:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                yield child
                yield from outside_functions(child)

    paths = [*TESTS_DIR.glob("test_*.py"), *TESTS_DIR.glob("doubles/*.py")]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        evaluated = {n.attr for n in outside_functions(tree) if isinstance(n, ast.Attribute)}
        assert not evaluated & set(POSIX_NAMES), path
