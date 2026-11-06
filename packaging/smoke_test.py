"""Test de fumée du binaire (AC46) : `python packaging/smoke_test.py <commande> [arguments]`.

Deux passages, chacun dans un dossier temporaire qui tient lieu de `LOCALAPPDATA`, de profil et
de `TEMP`, sur un port dédié :

1. La commande démarre avec `--no-browser`. `/` et tous les fichiers de `ui/`, le mode `real`,
   les 4 presets et l'empreinte du mod embarqué sont comparés au dépôt, puis `POST /api/quit`
   doit finir sur le code 0.
2. Un `--onefile` a deux processus, et AC10 ne mesure que depuis les sources. Ici `taskkill /F`
   frappe le seul processus parent : aucun de ses descendants ne doit subsister, et le port
   doit se taire.

Stdlib et `mod_fingerprint` du dépôt : rien à installer. Les limites viennent d'AC46 et d'AC10,
pas de `runtime.json`, dont les valeurs règlent le service que ce script juge.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from service.preflight import mod_fingerprint  # noqa: E402

LOOPBACK = "127.0.0.1"
UI_DIR = REPO / "ui"
MOD_DIR = REPO / "mods" / "duelcore"
PRESETS = {"demo", "duel_rapide", "hardcore", "pression"}
LOG_NAME = "pz-duel.log"
LOG_TAIL_LINES = 20

STARTUP_LIMIT_S = 60  # extraction du `--onefile` et analyse antivirus compris
QUIT_LIMIT_S = 30  # AC46
SURVIVOR_LIMIT_S = 5  # AC10
HTTP_TIMEOUT_S = 10
POLL_S = 0.2

# `TH32CS_SNAPPROCESS`, `INVALID_HANDLE_VALUE` et `MAX_PATH` de l'API Win32.
SNAP_PROCESS = 0x00000002
INVALID_HANDLE = -1
MAX_PATH = 260


class SmokeFailure(Exception):
    """Ce que le binaire n'a pas tenu : le message finit dans la sortie de la CI."""


class Process(NamedTuple):
    parent: int
    image: str


def windows_processes() -> dict[int, Process]:
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel.CreateToolhelp32Snapshot(SNAP_PROCESS, 0)
    if snapshot == ctypes.c_void_p(INVALID_HANDLE).value:
        raise ctypes.WinError(ctypes.get_last_error())
    table = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[entry.th32ProcessID] = Process(entry.th32ParentProcessID, entry.szExeFile)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return table


def posix_processes() -> dict[int, Process]:
    """Lit `/proc`, sans les zombies : ils ne tournent plus, seul leur parent les retient."""
    table = {}
    for folder in Path("/proc").iterdir():
        if not folder.name.isdigit():
            continue
        try:
            stat = (folder / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        image, _, rest = stat.partition(" (")[2].rpartition(") ")
        state, parent = rest.split()[:2]
        if state != "Z":
            table[int(folder.name)] = Process(int(parent), image)
    return table


def processes() -> dict[int, Process]:
    return windows_processes() if sys.platform == "win32" else posix_processes()


def descendants(root: int) -> dict[int, Process]:
    table = processes()
    found: dict[int, Process] = {}
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        for pid, process in table.items():
            if process.parent == parent and pid != root and pid not in found:
                found[pid] = process
                frontier.append(pid)
    return found


def still_alive(known: dict[int, Process]) -> dict[int, Process]:
    """Rend ceux de `known` qui tournent : un même pid sous une autre image est un remplaçant."""
    table = processes()
    return {
        pid: process
        for pid, process in known.items()
        if pid in table and table[pid].image == process.image
    }


def force_end(pid: int) -> None:
    """Met fin à ce seul processus, sans ses enfants : `/T` manque exprès."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def sandbox_env(root: Path) -> dict[str, str]:
    """Tient le binaire hors du vrai profil : données, `~/Zomboid` et extraction du `--onefile`."""
    env = {name: value for name, value in os.environ.items() if name != "PZ_INSTALL_DIR"}
    for name in ("LOCALAPPDATA", "XDG_DATA_HOME", "USERPROFILE", "HOME", "TEMP", "TMP", "TMPDIR"):
        folder = root / name
        folder.mkdir()
        env[name] = str(folder)
    return env


def log_tail(root: Path) -> str:
    """Rend la fin du log : sans console, le binaire n'a rien d'autre à montrer."""
    lines: list[str] = []
    for path in root.rglob(LOG_NAME):
        lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "".join(f"\n  log: {line}" for line in lines[-LOG_TAIL_LINES:])


class Session:
    """Un binaire lancé, son port, et les descendants qu'on lui a vus."""

    def __init__(self, process: subprocess.Popen, port: int) -> None:
        self.process = process
        self.port = port
        self.children: dict[int, Process] = {}

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(LOOPBACK, self.port, timeout=HTTP_TIMEOUT_S)
        headers = {} if body is None else {"Content-Type": "application/json"}
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def answers(self) -> bool:
        try:
            return self.request("GET", "/api/state")[0] == http.client.OK
        except (OSError, http.client.HTTPException):
            return False

    def get(self, path: str) -> bytes:
        try:
            status, body = self.request("GET", path)
        except (OSError, http.client.HTTPException) as exc:
            raise SmokeFailure(f"GET {path} failed: {exc}") from exc
        if status != http.client.OK:
            raise SmokeFailure(f"GET {path} returned {status}, expected 200")
        return body

    def get_json(self, path: str) -> dict:
        try:
            return json.loads(self.get(path))
        except ValueError as exc:
            raise SmokeFailure(f"GET {path} did not return JSON") from exc

    def wait_until_up(self) -> None:
        deadline = time.monotonic() + STARTUP_LIMIT_S
        while not self.answers():
            code = self.process.poll()
            if code is not None:
                raise SmokeFailure(f"the binary exited with code {code} before answering")
            if time.monotonic() >= deadline:
                raise SmokeFailure(f"no answer on port {self.port} within {STARTUP_LIMIT_S} s")
            time.sleep(POLL_S)
        if self.process.poll() is not None:
            raise SmokeFailure(f"the binary exited, yet port {self.port} answers: not its service")
        self.children = descendants(self.process.pid)

    def clean_up(self) -> None:
        """Ne laisse rien tourner, quel que soit le verdict."""
        if self.process.poll() is None:
            self.children |= descendants(self.process.pid)
            force_end(self.process.pid)
        for pid in still_alive(self.children):
            force_end(pid)
        self.process.wait()


@contextmanager
def launched(command: list[str]) -> Iterator[Session]:
    with tempfile.TemporaryDirectory(prefix="pz-duel-smoke-", ignore_cleanup_errors=True) as name:
        root = Path(name)
        port = free_port()
        try:
            process = subprocess.Popen(
                [*command, "--no-browser", "--port", str(port)],
                env=sandbox_env(root),
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SmokeFailure(f"could not start {command[0]}: {exc}") from exc
        session = Session(process, port)
        try:
            session.wait_until_up()
            yield session
        except SmokeFailure as exc:
            raise SmokeFailure(f"{exc}{log_tail(root)}") from exc
        finally:
            session.clean_up()


def check_ui(session: Session) -> None:
    if session.get("/") != (UI_DIR / "index.html").read_bytes():
        raise SmokeFailure("/ does not return ui/index.html")
    for path in sorted(UI_DIR.rglob("*")):
        if path.is_file():
            served = f"/{path.relative_to(UI_DIR).as_posix()}"
            if session.get(served) != path.read_bytes():
                raise SmokeFailure(f"{served} differs from the repository")


def check_state(session: Session) -> None:
    mode = session.get_json("/api/state").get("mode")
    if mode != "real":
        raise SmokeFailure(f"/api/state reports mode {mode!r}, expected 'real'")


def check_presets(session: Session) -> None:
    listed = {preset.get("id") for preset in session.get_json("/api/presets").get("presets", [])}
    if listed != PRESETS:
        names = sorted(map(str, listed))
        raise SmokeFailure(f"presets listed: {names}, expected {sorted(PRESETS)}")


def check_mod(session: Session) -> None:
    embedded = session.get_json("/api/setup").get("expected_mod_fingerprint")
    expected = mod_fingerprint(MOD_DIR)
    if embedded != expected:
        raise SmokeFailure(f"embedded mod fingerprint {embedded}, expected {expected}")


def check_quit(session: Session) -> None:
    try:
        status = session.request("POST", "/api/quit", b"{}")[0]
    except (OSError, http.client.HTTPException) as exc:
        raise SmokeFailure(f"POST /api/quit failed: {exc}") from exc
    if status != http.client.OK:
        raise SmokeFailure(f"POST /api/quit returned {status}, expected 200")
    try:
        code = session.process.wait(QUIT_LIMIT_S)
    except subprocess.TimeoutExpired:
        raise SmokeFailure(f"still running {QUIT_LIMIT_S} s after POST /api/quit") from None
    if code != 0:
        raise SmokeFailure(f"exit code {code} after POST /api/quit, expected 0")


def check_forced_end(session: Session) -> None:
    force_end(session.process.pid)
    deadline = time.monotonic() + SURVIVOR_LIMIT_S
    while True:
        survivors = still_alive(session.children)
        answers = session.answers()
        if not survivors and not answers:
            return
        if time.monotonic() >= deadline:
            names = sorted(f"{process.image} ({pid})" for pid, process in survivors.items())
            raise SmokeFailure(
                f"{SURVIVOR_LIMIT_S} s after the forced end of the parent: "
                f"survivors {names}, port answers: {answers}"
            )
        time.sleep(POLL_S)


def run(command: list[str]) -> None:
    with launched(command) as session:
        check_ui(session)
        check_state(session)
        check_presets(session)
        check_mod(session)
        check_quit(session)
    print("Normal run passed: UI, real mode, presets, mod fingerprint, exit code 0")

    with launched(command) as session:
        check_forced_end(session)
    print(f"Forced end passed: {len(session.children)} child process(es) ended with the parent")


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: smoke_test.py <command> [arguments]", file=sys.stderr)
        return 2
    # Le binaire démarre ailleurs que dans le dossier courant.
    command = [str(Path(argv[0]).resolve()) if Path(argv[0]).exists() else argv[0], *argv[1:]]
    try:
        run(command)
    except SmokeFailure as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1
    print("Smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
