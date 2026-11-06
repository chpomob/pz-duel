"""R1, R3 : description des plateformes, remplacement tolérant, écrivains (AC6, AC13 à AC17).

D-A : un test qui n'a de sens que sur un seul système est défini dans un bloc `if` de ce
module, il n'est pas sauté. Les branches Windows s'exécutent ici par injection (D-B).
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from service import platforms, replay, rules, servers, transport
from service import proofs as proofs_module
from service.engine import MODE_REAL, Engine, MatchSecrets, RealMatch
from service.journal import read_journal
from service.platforms import LINUX, WINDOWS, ReplaceError, replace_file
from service.proofs import PROOFS_NAME, HookProofs
from service.protocol import PROTOCOL_VERSION, SIDES, dump_line
from service.rules import load_effects, load_runtime
from service.servers import (
    STARTED_MARKER,
    Redactor,
    ServerProcess,
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


@pytest.mark.parametrize(
    "make_writer", [session_writer, ini_writer, sandbox_writer], ids=lambda make: make.__name__
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
