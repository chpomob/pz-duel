#!/usr/bin/env python3
"""Double du script serveur : sortie scriptée, enfants, refus de quitter, crash, RCON.

Il reçoit les arguments d'un vrai serveur. `install_fake_server` le pose à la place du
script de la plateforme dans un dossier d'installation factice. Le scénario est un objet
JSON :

- `lines` : sortie standard ; `{servername}`, `{port}`, `{udpport}`, `{adminpassword}` et
  `{cwd}` y sont remplacés ; `line_delay_s` les espace ;
- `stderr_lines` : erreur standard, écrite avant `lines` ;
- `args_file` : y écrit en JSON les arguments reçus, tels quels ;
- `announce_pids` : imprime `fake script pid=<parent>` et `fake server pid=<le double>` ;
- `child` : lance un enfant dans l'arbre, dont le pid est imprimé, et qui vit au plus
  `child_life_s` : un test interrompu ne laisse rien derrière lui ;
- `child_depth` : générations de descendants sous cet enfant, 0 par défaut ; chacune imprime
  `fake grandchild pid=<pid>` ;
- `orphan_child` : sort sans tuer l'enfant, comme le script qui précède son `java` ;
- `refuse_quit` : ignore `quit` sur l'entrée standard et par RCON ;
- `ignore_sigterm` : ignore SIGTERM, enfant compris, car l'ignorance se transmet ;
- `exit_code` : crash, sortie immédiate avec ce code après les lignes ;
- `script_tail` : ligne ajoutée au `.bat` après le double, `PAUSE` pour un script qui attend
  une touche ; le script POSIX fait `exec`, il n'a pas de suite ;
- `rcon_password` : ouvre le RCON factice et imprime `fake rcon port=<port>` ; il écoute
  sur `rcon_port` s'il est donné, sinon sur un port libre. En sortant, le double imprime
  `fake rcon commands=<commandes reçues>`.

Le protocole est réécrit ici, sans `service.rcon` : le client est ainsi vérifié contre une
implémentation indépendante, et le script tourne sans le dépôt dans son chemin.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

AUTH = 3
AUTH_RESPONSE = 2
EXEC = 2
RESPONSE = 0
AUTH_REFUSED = -1
DEFAULT_CHILD_LIFE_S = 60
CHILD_FLAG = "--fake-child"
# `--scenario <chemin>` précède les arguments d'un vrai serveur.
SERVER_ARGS_START = 3
# La classe principale du serveur, celle que le JRE reçoit avant les réglages du jeu.
MAIN_CLASS = "zombie/network/GameServer"
REPLIES = {"save": "World saved"}
WINDOWS_HOST = "win32"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
# Le compilateur du .NET Framework, livré avec Windows et présent sur `windows-latest`.
CSC = Path("Microsoft.NET") / "Framework64" / "v4.0.30319" / "csc.exe"
COMPILE_TIMEOUT_S = 120
# Double de `ProjectZomboid64.exe`, lancé détaché comme le vrai : il doit donc être un vrai PE.
# `<exe>.plan` porte le code de sortie, la tenue en ms et le fichier des arguments : un seul
# binaire sert ainsi à tous les tests.
CLIENT_SOURCE = r"""
using System;
using System.IO;
using System.Reflection;
using System.Threading;

static class FakeClient
{
    static int Main(string[] args)
    {
        string[] plan = File.ReadAllLines(Assembly.GetEntryAssembly().Location + ".plan");
        File.WriteAllText(plan[2], string.Join(" ", args) + "\n");
        Thread.Sleep(int.Parse(plan[1]));
        return int.Parse(plan[0]);
    }
}
"""


def send_packet(conn: socket.socket, packet_id: int, kind: int, body: str) -> None:
    payload = struct.pack("<ii", packet_id, kind) + body.encode() + b"\0\0"
    conn.sendall(struct.pack("<i", len(payload)) + payload)


def read_exact(conn: socket.socket, count: int) -> bytes | None:
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_packet(conn: socket.socket) -> tuple[int, int, str] | None:
    size = read_exact(conn, 4)
    data = size and read_exact(conn, struct.unpack("<i", size)[0])
    if not data:
        return None
    packet_id, kind = struct.unpack_from("<ii", data)
    return packet_id, kind, data[8:-2].decode()


class FakeRcon:
    """Serveur RCON local : `quit` coupe sans répondre, `mute` ne répond à aucune commande."""

    def __init__(
        self,
        password: str,
        on_quit: Callable[[], None] = lambda: None,
        mute: bool = False,
        port: int = 0,
    ) -> None:
        self.password = password
        self.on_quit = on_quit
        self.mute = mute
        self.commands: list[str] = []
        self._listener = socket.create_server(("127.0.0.1", port))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def __enter__(self) -> FakeRcon:
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Elle réveille `accept` sous Linux ; Windows la refuse sur un socket d'écoute.
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        self._listener.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with conn:
                try:
                    self._handle(conn)
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        authenticated = False
        while packet := read_packet(conn):
            packet_id, kind, body = packet
            if kind == AUTH:
                authenticated = body == self.password
                send_packet(conn, packet_id, RESPONSE, "")
                reply_id = packet_id if authenticated else AUTH_REFUSED
                send_packet(conn, reply_id, AUTH_RESPONSE, "")
            elif kind == EXEC and authenticated:
                self.commands.append(body)
                if self.mute:
                    continue
                if body == "quit":
                    self.on_quit()
                    return
                send_packet(conn, packet_id, RESPONSE, REPLIES.get(body, ""))


def install_fake_server(
    pz_dir: Path, scenario: dict[str, Any], platform: Any = None
) -> Path:
    """Pose le script de `platform`, la plateforme de l'hôte par défaut, sur ce double.

    L'import reste ici : le double lui-même tourne sans le dépôt dans son chemin.
    """
    from service.platforms import current

    platform = current() if platform is None else platform
    scenario_path = pz_dir / "fake_scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    command = [sys.executable, str(Path(__file__).resolve()), "--scenario", str(scenario_path)]
    script = pz_dir / platform.server_script
    if platform.posix_modes:
        script.write_text(f'#!/bin/sh\nexec {shlex.join(command)} "$@"\n', encoding="utf-8")
        script.chmod(0o755)
        return script
    lines = ["@echo off", f"{subprocess.list2cmdline(command)} %*"]
    if "script_tail" in scenario:
        lines.append(scenario["script_tail"])
    script.write_text("".join(f"{line}\r\n" for line in lines), encoding="utf-8", newline="")
    return script


def on_host(sh: str, bat: str) -> str:
    """Rend celui des deux textes que le système hôte exécute."""
    return bat if sys.platform == WINDOWS_HOST else sh


def write_script(path: Path, sh_body: str, bat_body: str) -> Path:
    """Pose un script lançable sur l'hôte et rend son chemin, en `.bat` sous Windows."""
    if sys.platform == WINDOWS_HOST:
        script = path.with_suffix(".bat")
        lines = ["@echo off", *bat_body.splitlines()]
        script.write_text("".join(f"{line}\r\n" for line in lines), encoding="utf-8", newline="")
        return script
    path.write_text(f"#!/bin/sh\n{sh_body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def kill_tree(server: Any) -> None:
    """Met fin à tout l'arbre de `server`, sans qu'il l'ait demandé : c'est une panne.

    `signal.SIGKILL` et `os.killpg` restent dans le corps : ils n'existent pas sous Windows.
    """
    if sys.platform == WINDOWS_HOST:
        server._tree.kill()
        return
    os.killpg(server._process.pid, signal.SIGKILL)


def assert_private(path: Path) -> None:
    """Le fichier porte un secret : `0600` sous POSIX. Windows n'a pas ces droits (AC16)."""
    if sys.platform == WINDOWS_HOST:
        assert path.is_file()
        return
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def windows_pid_alive(pid: int) -> bool:
    """Sonde sans effet : sous Windows, `os.kill(pid, 0)` tuerait le processus.

    Le code de sortie décide, pas le signal de l'objet processus : celui-ci suit le démontage de
    la mémoire, quelques millisecondes après que le Job s'est vidé (mesuré à 1,5 ms sur
    `windows-latest`), alors que plus rien ne tourne et que le code de sortie est déjà posé.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def compile_windows_client(target: Path) -> Path:
    """Compile le double du client en un vrai PE. Sans `csc.exe`, c'est une erreur, pas un skip."""
    compiler = Path(os.environ["WINDIR"]) / CSC
    if not compiler.is_file():
        raise FileNotFoundError(f"{compiler} is required to build the client double")
    source = target.with_suffix(".cs")
    source.write_text(CLIENT_SOURCE, encoding="utf-8")
    done = subprocess.run(
        [str(compiler), "/nologo", f"/out:{target}", str(source)],
        capture_output=True,
        text=True,
        timeout=COMPILE_TIMEOUT_S,
    )
    if done.returncode:
        raise RuntimeError(f"{compiler} failed: {done.stdout}{done.stderr}")
    return target


def plan_windows_client(client: Path, args_file: Path, code: int = 0, stay_ms: int = 0) -> None:
    """Dit à `client`, compilé par `compile_windows_client`, comment se comporter."""
    plan = Path(f"{client}.plan")
    plan.write_text(f"{code}\n{stay_ms}\n{args_file}\n", encoding="utf-8")


class WindowsLaunchOnPosix:
    """Tient lieu de `subprocess.Popen` quand la plateforme Windows est injectée sous Linux.

    `calls` garde chaque commande et ses options. Un script Windows part par le script POSIX
    du même dossier, que le test a posé : le double joue ainsi le vrai processus.
    `refuse(flags)` peut lever à la place du lancement.
    """

    def __init__(self, refuse: Callable[[int], None] = lambda flags: None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._real = subprocess.Popen
        self._refuse = refuse

    def __call__(self, command: list[str], **options: Any) -> subprocess.Popen:
        from service.platforms import LINUX, WINDOWS

        self.calls.append((list(command), dict(options)))
        self._refuse(options.pop("creationflags"))
        folder, _, name = command[0].rpartition("\\")
        program = command[0]
        if name == WINDOWS.server_script:
            program = str(Path(folder) / LINUX.server_script)
        elif name == "java.exe":
            # Le JRE du jeu est le programme du serveur : le double POSIX tient sa place dans
            # le dossier du jeu, deux niveaux au-dessus de `jre64\bin`.
            game_dir = Path(folder.replace("\\", "/")).parent.parent
            program = str(game_dir / LINUX.server_script)
        return self._real([program, *command[1:]], start_new_session=True, **options)


class PosixKernel:
    """La couture `kernel` jouée sur l'hôte POSIX : un Job y est le groupe de son processus.

    `calls` garde chaque appel dans l'ordre, `times` l'instant des deux fins forcées, et
    `failing` nomme les appels qui lèvent `OSError`. Aucun processus n'est vraiment suspendu.
    """

    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self.times: dict[str, float] = {}
        self.failing = failing
        self.handles: set[int] = set()
        self._next_handle = 1
        self._pids: dict[int, int] = {}

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.failing:
            raise OSError(f"{name} refused")

    def _handle(self, pid: int | None = None) -> int:
        handle = self._next_handle
        self._next_handle += 1
        self.handles.add(handle)
        if pid is not None:
            self._pids[handle] = pid
        return handle

    def open_process(self, pid: int) -> int:
        self._call("open_process")
        return self._handle(pid)

    def create_job(self) -> int:
        self._call("create_job")
        return self._handle()

    def assign(self, job: int, process: int) -> None:
        self._call("assign")
        self._pids[job] = self._pids[process]

    def resume(self, process: int, pid: int) -> None:
        self._call("resume")

    def active_processes(self, job: int) -> int:
        self._call("active_processes")
        try:
            os.killpg(self._pids[job], 0)
        except ProcessLookupError:
            return 0
        return 1

    def terminate_process(self, process: int, code: int) -> None:
        self.times["terminate_process"] = time.monotonic()
        self._call("terminate_process")
        os.kill(self._pids[process], signal.SIGKILL)

    def terminate_job(self, job: int, code: int) -> None:
        self.times["terminate_job"] = time.monotonic()
        self._call("terminate_job")
        os.killpg(self._pids[job], signal.SIGKILL)

    def close(self, handle: int) -> None:
        self.calls.append("close")
        self.handles.remove(handle)


def print_line(text: str) -> None:
    print(text, flush=True)


def spawn_child(life_s: float, depth: int) -> subprocess.Popen:
    """Python, et non `sleep` : le double doit aussi tourner sous Windows.

    `stdout` est explicite : sans lui, `close_fds` n'y transmet aucun handle à l'enfant.
    `stdin` est coupé, car seul le serveur lit `quit` : sous Windows, une lecture en attente sur
    un tube verrouille son objet fichier, et l'enfant qui hérite de ce tube resterait bloqué
    dans le démarrage de Python, à l'examen de son entrée standard, sans jamais rien imprimer.
    """
    script = str(Path(__file__).resolve())
    command = [sys.executable, script, CHILD_FLAG, str(life_s), str(depth)]
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=sys.stdout)


def run_child(life_s: str, depth: str) -> int:
    if int(depth):
        grandchild = spawn_child(float(life_s), int(depth) - 1)
        print_line(f"fake grandchild pid={grandchild.pid}")
    time.sleep(float(life_s))
    return 0


def server_arguments() -> list[str]:
    """Les arguments d'un vrai serveur, ceux qui suivent `--scenario <chemin>`.

    Un lancement par le JRE ajoute ses options et sa classe principale devant : elles ne font
    pas partie de ce que le serveur reçoit comme réglages.
    """
    arguments = sys.argv[1:]
    if MAIN_CLASS in arguments:
        return arguments[arguments.index(MAIN_CLASS) + 1 :]
    return sys.argv[SERVER_ARGS_START:]


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--scenario", required=True)
    for name in ("-servername", "-port", "-udpport", "-adminpassword"):
        parser.add_argument(name, required=True)
    # Le vrai serveur tolère des options de JVM avant sa classe principale : le double aussi.
    args, _ = parser.parse_known_args()
    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    if "args_file" in scenario:
        received = json.dumps(server_arguments())
        Path(scenario["args_file"]).write_text(received, encoding="utf-8")

    if scenario.get("announce_pids"):
        print_line(f"fake script pid={os.getppid()}")
        print_line(f"fake server pid={os.getpid()}")

    if scenario.get("ignore_sigterm"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = None
    if scenario.get("child"):
        life_s = scenario.get("child_life_s", DEFAULT_CHILD_LIFE_S)
        child = spawn_child(life_s, scenario.get("child_depth", 0))
        print_line(f"fake child pid={child.pid}")

    stop = threading.Event()

    def request_stop() -> None:
        if not scenario.get("refuse_quit"):
            stop.set()

    rcon = None
    if "rcon_password" in scenario:
        rcon = FakeRcon(
            scenario["rcon_password"],
            on_quit=request_stop,
            port=scenario.get("rcon_port", 0),
        )
        print_line(f"fake rcon port={rcon.port}")

    for line in scenario.get("stderr_lines", ()):
        print(line, file=sys.stderr, flush=True)
    for line in scenario.get("lines", ()):
        print_line(line.format(cwd=os.getcwd(), **vars(args)))
        time.sleep(scenario.get("line_delay_s", 0))

    def read_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == "quit":
                request_stop()

    if "exit_code" not in scenario:
        threading.Thread(target=read_stdin, daemon=True).start()
        stop.wait()
    if child and not scenario.get("orphan_child"):
        child.kill()
        child.wait()
    if rcon:
        print_line(f"fake rcon commands={','.join(rcon.commands)}")
    # Sans attendre les fils encore bloqués sur l'entrée standard ou sur RCON.
    sys.stdout.flush()
    os._exit(scenario.get("exit_code", 0))


if __name__ == "__main__":
    if sys.argv[1:2] == [CHILD_FLAG]:
        sys.exit(run_child(*sys.argv[2:]))
    sys.exit(main())
