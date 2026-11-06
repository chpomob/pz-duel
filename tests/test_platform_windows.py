"""R1, R3 : description des plateformes et remplacement tolérant (AC6, AC15 partie Linux).

D-A : un test qui n'a de sens que sur un seul système est défini dans un bloc `if` de ce
module, il n'est pas sauté. Les branches Windows s'exécutent ici par injection (D-B).
"""

from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from service import platforms, rules
from service.platforms import LINUX, WINDOWS, ReplaceError, replace_file
from service.rules import load_runtime

REPO = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO / "service"
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
