"""Réglages gérés, lancement, expurgation, RCON, double, démarrage et arrêt gradué."""

from __future__ import annotations

import logging
import os
import signal
import socket
import stat
import subprocess
import threading
import time
from functools import partial
from pathlib import Path

import pytest

from doubles.fake_server import FakeRcon, install_fake_server
from service.rcon import MAX_PACKET_SIZE, RconClient, RconError
from service.servers import (
    REDACTED,
    STOP_RCON,
    STOP_SIGKILL,
    STOP_SIGTERM,
    STOP_STDIN,
    Redactor,
    ServerPair,
    ServerProcess,
    apply_status,
    gen_secret,
    launch_command,
    read_ini,
    write_ini,
    write_sandbox,
)
from service.state import MatchState

PROCESS_TIMEOUT_S = 10
RCON_TIMEOUT_S = 2
MUTE_TIMEOUT_S = 0.05
INI_LINES = 200
# Latin-1 égaré dans un commentaire : cet octet n'est pas de l'UTF-8 valide.
INVALID_BYTES = "# Réglé à l'été ".encode() + b"caf\xe9"
MANAGED = {
    "DefaultPort": 17261,
    "UDPPort": 17262,
    "RCONPort": 27015,
    "RCONPassword": "rcon-marker",
    "Password": "player-marker",
    "PauseEmpty": "false",
    "Mods": "duelcore",
    "WorkshopItems": "",
    "UPnP": "false",
    "Public": "false",
    "MaxPlayers": "1",
    "SaveWorldEveryMinutes": "5",
}
SANDBOX = """SandboxVars = {
    VERSION = 6,
    -- DayLength = 7, commentaire : à laisser
    DayLength = 4,
    StartYear = 3,
    -- Mois de départ, l'été
    StartMonth = 1,
    StartDay = 2,
    StartTime = 5, -- le matin
    ZombieConfig = {
        PopulationStartMultiplier = 1.0,
        PopulationPeakDay = 28,
    },
}
"""


def ini_lines() -> list[bytes]:
    """`.ini` de 200 lignes, avec des voisines des clés gérées à ne pas confondre."""
    head = [
        "# Le temps s'arrête lorsqu'il n'y a aucun joueur en ligne".encode(),
        b"PauseEmpty=true",
        INVALID_BYTES,
        "# Mods=commenté, à garder".encode(),
        b"Mods=duelsidea",
        b"PublicName=Mon serveur",
        b"PublicDescription=",
        b"Public=true",
        b"Password=",
        b"RCONPassword=old",
        b"RCONPort=27015",
        b"DefaultPort=16261",
        b"UDPPort=16262",
        b"MaxPlayers=32",
        b"SaveWorldEveryMinutes=0",
        b"WorkshopItems=123;456",
    ]
    filler = [
        (f"Option{index}=é" if index % 2 else f"# Commentaire n°{index}").encode()
        for index in range(INI_LINES - len(head))
    ]
    return head + filler


@pytest.fixture
def sandbox(tmp_path):
    path = tmp_path / "duelA_SandboxVars.lua"
    path.write_text(SANDBOX, encoding="utf-8")
    return path


@pytest.fixture
def launch_cfg(runtime, tmp_path):
    return {**runtime, "pz_dir": tmp_path, "admin_password": "admin-marker"}


def test_managed_keys_only(tmp_path):
    path = tmp_path / "duelA.ini"
    before = ini_lines()
    assert len(before) == INI_LINES
    path.write_bytes(b"\n".join(before) + b"\n")
    path.chmod(0o644)

    write_ini(path, MANAGED)

    after = path.read_bytes().split(b"\n")
    assert after[-1] == b""
    after = after[:-1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    added = after[len(before) :]
    assert added == [b"UPnP=false"]
    for old, new in zip(before, after):
        key = old.split(b"=")[0].decode(errors="replace")
        if key in MANAGED:
            assert new == f"{key}={MANAGED[key]}".encode()
        else:
            assert new == old

    assert INVALID_BYTES in after
    assert read_ini(path)["PublicName"] == "Mon serveur"
    assert not [name for name in os.listdir(tmp_path) if name != path.name]


def test_write_ini_is_idempotent(tmp_path):
    path = tmp_path / "duelA.ini"
    path.write_bytes(b"\n".join(ini_lines()))
    write_ini(path, MANAGED)
    first = path.read_bytes()
    write_ini(path, MANAGED)
    assert path.read_bytes() == first


def test_write_ini_appends_after_unterminated_last_line(tmp_path):
    path = tmp_path / "duelA.ini"
    path.write_bytes(b"Foo=1")
    write_ini(path, {"Mods": "duelcore"})
    assert path.read_bytes() == b"Foo=1\nMods=duelcore\n"


def test_write_ini_keeps_crlf(tmp_path):
    path = tmp_path / "duelA.ini"
    path.write_bytes(b"# \xc3\xa9\r\nMods=old\r\nFoo=1\r\n")
    write_ini(path, {"Mods": "duelcore", "UPnP": "false"})
    assert path.read_bytes() == b"# \xc3\xa9\r\nMods=duelcore\r\nFoo=1\r\nUPnP=false\r\n"
    assert read_ini(path)["Mods"] == "duelcore"


def test_write_ini_rewrites_every_occurrence(tmp_path):
    path = tmp_path / "duelA.ini"
    path.write_bytes(b"Mods=a\n  Mods = b\n")
    write_ini(path, {"Mods": "duelcore"})
    assert path.read_bytes() == b"Mods=duelcore\nMods=duelcore\n"


def test_write_ini_creates_partial_file(tmp_path):
    path = tmp_path / "Server" / "duelB.ini"
    write_ini(path, {"Mods": "duelcore"})
    assert path.read_bytes() == b"Mods=duelcore\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("value", ["x\nPublic=true", "x\rPublic=true"])
def test_write_ini_refuses_line_break(tmp_path, value):
    path = tmp_path / "duelA.ini"
    path.write_bytes(b"Password=\n")
    with pytest.raises(ValueError) as caught:
        write_ini(path, {"Password": value})
    assert "Password" in str(caught.value)
    assert value not in str(caught.value)
    assert path.read_bytes() == b"Password=\n"


def test_failed_write_leaves_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "duelA.ini"
    path.write_bytes(b"Mods=old\n")

    def fail(*args):
        raise OSError("disque plein")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_ini(path, {"Mods": "duelcore"})
    assert path.read_bytes() == b"Mods=old\n"
    assert os.listdir(tmp_path) == [path.name]


def test_read_ini(tmp_path):
    path = tmp_path / "duelA.ini"
    path.write_bytes(b"# Mods=no\nMods=a\nEmpty=\nOpts=x=y\nnot a key\nMods=duelcore \n")
    assert read_ini(path) == {"Mods": "duelcore", "Empty": "", "Opts": "x=y"}


def test_population_start_multiplier_untouched(sandbox, runtime):
    write_sandbox(sandbox, 1, runtime["sandbox_start"])
    text = sandbox.read_text(encoding="utf-8")
    assert "        PopulationStartMultiplier = 1.0,\n" in text


def test_write_sandbox_exact_keys(sandbox):
    sandbox.chmod(0o664)
    start = {"StartYear": 1, "StartMonth": 7, "StartDay": 9, "StartTime": 2}
    write_sandbox(sandbox, 1, start)
    expected = (
        SANDBOX.replace("    DayLength = 4,", "    DayLength = 1,")
        .replace("StartYear = 3", "StartYear = 1")
        .replace("StartMonth = 1", "StartMonth = 7")
        .replace("StartDay = 2", "StartDay = 9")
        .replace("StartTime = 5, -- le matin", "StartTime = 2, -- le matin")
    )
    assert sandbox.read_text(encoding="utf-8") == expected
    assert stat.S_IMODE(sandbox.stat().st_mode) == 0o664


def test_write_sandbox_refuses_missing_key(sandbox, runtime):
    text = SANDBOX.replace("    StartDay = 2,\n", "")
    sandbox.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="StartDay"):
        write_sandbox(sandbox, 1, runtime["sandbox_start"])
    assert sandbox.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    ("side", "name", "game", "udp"),
    [("A", "duelA", "17261", "17262"), ("B", "duelB", "17271", "17272")],
)
def test_launch_arguments(launch_cfg, tmp_path, side, name, game, udp):
    command = launch_command(side, launch_cfg)
    assert command[0] == str(tmp_path / "start-server.sh")
    options = dict(zip(command[1::2], command[2::2]))
    assert options == {
        "-servername": name,
        "-port": game,
        "-udpport": udp,
        "-adminpassword": "admin-marker",
    }


def test_redactor_masks_every_secret():
    redactor = Redactor(["abc", "abcdef", "xyz", ""])
    assert redactor.redact("a abcdef b abc xyz") == f"a {REDACTED} b {REDACTED} {REDACTED}"
    assert redactor.redact("rien") == "rien"
    message = redactor.redact_exception(RuntimeError("échec avec xyz"))
    assert message == f"RuntimeError: échec avec {REDACTED}"


def test_redactor_masks_logged_command(launch_cfg):
    line = " ".join(launch_command("A", launch_cfg))
    assert "admin-marker" not in Redactor(["admin-marker"]).redact(line)


def test_gen_secret_differs_and_fits_ini():
    first, second = gen_secret(), gen_secret()
    assert first != second
    assert first.isalnum()


def test_rcon_save_quit():
    with FakeRcon("rcon-marker") as server:
        with RconClient(server.port, "rcon-marker", RCON_TIMEOUT_S) as client:
            assert client.save() == "World saved"
            client.quit()
    assert server.commands == ["save", "quit"]


def test_rcon_wrong_password_names_no_secret(caplog):
    caplog.set_level(logging.DEBUG)
    with FakeRcon("rcon-marker") as server:
        client = RconClient(server.port, "wrong-marker", RCON_TIMEOUT_S)
        with pytest.raises(RconError) as caught:
            client.connect()
    assert "refusée" in str(caught.value)
    assert server.commands == []
    for text in (str(caught.value), repr(client), caplog.text):
        assert "wrong-marker" not in text


def test_rcon_short_timeout_on_silent_server():
    with FakeRcon("rcon-marker", mute=True) as server:
        with RconClient(server.port, "rcon-marker", MUTE_TIMEOUT_S) as client:
            with pytest.raises(RconError, match="absente"):
                client.save()
            client.quit()
    assert server.commands == ["save", "quit"]


def test_rcon_deadline_bounds_handshake():
    with socket.create_server(("127.0.0.1", 0)) as silent:
        port = silent.getsockname()[1]
        begin = time.monotonic()
        client = RconClient(port, "rcon-marker", RCON_TIMEOUT_S, deadline=begin + MUTE_TIMEOUT_S)
        with pytest.raises(RconError, match="absente"):
            client.connect()
    assert time.monotonic() - begin < RCON_TIMEOUT_S


def test_rcon_deadline_survives_trickling_server():
    def trickle(listener: socket.socket) -> None:
        conn, _ = listener.accept()
        with conn:
            try:
                conn.sendall(MAX_PACKET_SIZE.to_bytes(4, "little"))
                while True:
                    conn.sendall(b"x")
                    time.sleep(POLL_S)
            except OSError:
                pass

    with socket.create_server(("127.0.0.1", 0)) as listener:
        threading.Thread(target=trickle, args=(listener,), daemon=True).start()
        port = listener.getsockname()[1]
        begin = time.monotonic()
        client = RconClient(port, "rcon-marker", RCON_TIMEOUT_S, deadline=begin + MUTE_TIMEOUT_S)
        with pytest.raises(RconError):
            client.connect()
    assert time.monotonic() - begin < RCON_TIMEOUT_S


def test_rcon_expired_deadline_refuses_connection():
    client = RconClient(1, "rcon-marker", RCON_TIMEOUT_S, deadline=time.monotonic())
    with pytest.raises(RconError, match="dépassé"):
        client.connect()


def test_rcon_unreachable():
    with FakeRcon("rcon-marker") as server:
        port = server.port
    with pytest.raises(RconError, match="injoignable"):
        RconClient(port, "rcon-marker", RCON_TIMEOUT_S).connect()


def test_rcon_requires_connection():
    with pytest.raises(RconError, match="non connecté"):
        RconClient(1, "rcon-marker", RCON_TIMEOUT_S).save()


def start_double(pz_dir: Path, launch_cfg: dict, scenario: dict) -> subprocess.Popen:
    install_fake_server(pz_dir, scenario)
    return subprocess.Popen(
        launch_command("A", launch_cfg),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


@pytest.fixture
def double(tmp_path, launch_cfg):
    """Lance le double par la vraie commande, et tue son groupe en fin de test."""
    started = []

    def start(**scenario):
        started.append(start_double(tmp_path, launch_cfg, scenario))
        return started[-1]

    yield start
    for process in started:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def value_after(process: subprocess.Popen, prefix: str) -> int:
    line = process.stdout.readline()
    assert line.startswith(prefix)
    return int(line.removeprefix(prefix))


def test_double_prints_script_and_masks_through_redactor(double):
    process = double(
        lines=["{servername} listening on port {port}", "adminpassword={adminpassword}"],
        exit_code=0,
    )
    output, _ = process.communicate(timeout=PROCESS_TIMEOUT_S)
    assert output.splitlines()[0] == "duelA listening on port 17261"
    assert "admin-marker" in output
    assert "admin-marker" not in Redactor(["admin-marker"]).redact(output)


def test_double_stops_on_rcon_quit(double):
    process = double(rcon_password="rcon-marker")
    port = value_after(process, "fake rcon port=")
    with RconClient(port, "rcon-marker", RCON_TIMEOUT_S) as client:
        assert client.save() == "World saved"
        client.quit()
    assert process.wait(timeout=PROCESS_TIMEOUT_S) == 0


def test_double_stops_on_stdin_quit(double):
    process = double()
    process.stdin.write("quit\n")
    process.stdin.flush()
    assert process.wait(timeout=PROCESS_TIMEOUT_S) == 0


def test_double_crash_exit_code(double):
    assert double(exit_code=3).wait(timeout=PROCESS_TIMEOUT_S) == 3


def test_double_refuses_quit_and_sigterm(double):
    process = double(
        child=True, refuse_quit=True, ignore_sigterm=True, rcon_password="rcon-marker"
    )
    child = value_after(process, "fake child pid=")
    port = value_after(process, "fake rcon port=")
    with RconClient(port, "rcon-marker", RCON_TIMEOUT_S) as client:
        client.quit()
    process.stdin.write("quit\n")
    process.stdin.flush()
    os.killpg(process.pid, signal.SIGTERM)
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=MUTE_TIMEOUT_S)
    os.kill(child, 0)

    os.killpg(process.pid, signal.SIGKILL)
    assert process.wait(timeout=PROCESS_TIMEOUT_S) == -signal.SIGKILL


def test_double_child_outlives_script(double):
    process = double(child=True, orphan_child=True, exit_code=0)
    child = value_after(process, "fake child pid=")
    assert process.wait(timeout=PROCESS_TIMEOUT_S) == 0
    os.killpg(process.pid, 0)
    os.kill(child, signal.SIGKILL)


# Délais de `runtime.json` raccourcis : seule leur valeur change, pas le chemin suivi.
FAST_TIMEOUTS = {
    "tick_ms": 10,
    "stop_timeout_s": 0.3,
    "kill_grace_s": 0.3,
    "rcon_timeout_s": 1,
    "server_start_timeout_s": PROCESS_TIMEOUT_S,
}
POLL_S = 0.01
SHORT_START_TIMEOUT_S = 0.4
# Assez pour qu'un `stop` que rien ne retient aille à son terme.
RACE_WINDOW_S = 0.5
STARTED = "LOG  : Network      f:0 st:2 470 625 176> *** SERVER STARTED ****"
LISTENING = "LOG  : General      f:0 st:2 470 625 176> server is listening on port {port}"
BUILD = "LOG  : General      f:0 st:2 470 606 920> version=42.21.0 4a0e9546ec demo=false"
READY_LINES = [STARTED, LISTENING]
# Plus que la capacité d'un tube : sans lecteur, le double se bloquerait avant d'être prêt.
FLOOD_LINES = ["x" * 1024] * 256


def free_tcp_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        return listener.getsockname()[1]


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def until(condition) -> None:
    deadline = time.monotonic() + PROCESS_TIMEOUT_S
    while not condition():
        assert time.monotonic() < deadline, "condition jamais atteinte"
        time.sleep(POLL_S)


def pid_after(server: ServerProcess, prefix: str) -> int:
    lines: list[str] = []

    def found() -> bool:
        lines[:] = [line for line in server.log_tail() if line.startswith(prefix)]
        return bool(lines)

    until(found)
    return int(lines[0].removeprefix(prefix))


class Harness:
    """Fabrique des `ServerProcess` sur des doubles, un dossier d'installation par camp."""

    def __init__(self, tmp_path: Path, runtime: dict) -> None:
        self.root = tmp_path
        self.runtime = {**runtime, **FAST_TIMEOUTS}
        self.runtime["ports"] = {
            side: {**ports, "rcon": free_tcp_port()} for side, ports in runtime["ports"].items()
        }
        self.match_dir = tmp_path / "matches" / "m1"
        self.redactor = Redactor(["admin-marker", "rcon-marker"])
        self.servers: list[ServerProcess] = []
        self.state = MatchState()
        self.state.reset_steps()

    def server(self, side: str = "A", **scenario) -> ServerProcess:
        pz_dir = self.root / f"pz{side}"
        pz_dir.mkdir(exist_ok=True)
        if "rcon_password" in scenario:
            scenario["rcon_port"] = self.runtime["ports"][side]["rcon"]
        install_fake_server(pz_dir, scenario)
        cfg = {
            **self.runtime,
            "pz_dir": pz_dir,
            "zomboid_dir": self.root / "Zomboid",
            "admin_password": "admin-marker",
            "rcon_password": "rcon-marker",
        }
        self.servers.append(ServerProcess(side, cfg, self.match_dir, self.redactor))
        return self.servers[-1]

    def pair(self, a: dict, b: dict, **overrides) -> ServerPair:
        servers = {"A": self.server("A", **a), "B": self.server("B", **b)}
        runtime = {**self.runtime, **overrides}
        return ServerPair(servers, runtime, partial(apply_status, self.state))

    def step(self, side: str) -> dict:
        return next(step for step in self.state.steps if step["id"] == f"server{side}")

    def cleanup(self) -> None:
        for server in self.servers:
            if server._process is not None:
                try:
                    os.killpg(server._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                server._process.wait()


@pytest.fixture
def harness(tmp_path, runtime):
    built = Harness(tmp_path, runtime)
    yield built
    built.cleanup()


def start_ready(server: ServerProcess) -> ServerProcess:
    server.start()
    until(lambda: server.ready)
    return server


@pytest.mark.parametrize(
    ("a", "b", "reason"),
    [
        pytest.param({"lines": READY_LINES}, {"lines": READY_LINES}, None, id="up"),
        pytest.param(
            {"lines": [STARTED, "server is listening on port 17262"]},
            {"lines": READY_LINES},
            "pas de disponibilité",
            id="other_port",
        ),
        pytest.param(
            {"lines": [STARTED, "server is listening on port 172610"]},
            {"lines": READY_LINES},
            "pas de disponibilité",
            id="port_prefix",
        ),
        pytest.param(
            {"lines": [LISTENING, STARTED]},
            {"lines": READY_LINES},
            "pas de disponibilité",
            id="wrong_order",
        ),
        pytest.param(
            {"lines": ["erreur fatale"], "exit_code": 1},
            {"lines": READY_LINES, "child": True},
            "sortie anticipée, code 1",
            id="crash",
        ),
        pytest.param({}, {"lines": READY_LINES}, "pas de disponibilité", id="mute"),
    ],
)
def test_readiness_paths(harness, a, b, reason):
    timeout_s = SHORT_START_TIMEOUT_S if reason else PROCESS_TIMEOUT_S
    pair = harness.pair(a, b, server_start_timeout_s=timeout_s)
    begin = time.monotonic()
    assert pair.start()
    groups = [server._process.pid for server in pair.servers.values()]

    ready = pair.wait_ready()

    if reason is None:
        assert ready and pair.failure is None
        assert harness.state.servers == {"A": "up", "B": "up"}
        assert harness.step("A")["state"] == harness.step("B")["state"] == "ok"
        assert pair.stop()
        assert harness.state.servers == {"A": "down", "B": "down"}
    else:
        assert not ready
        assert reason in pair.failure
        assert reason in harness.step("A")["detail"]
        assert harness.step("A")["state"] == harness.step("B")["state"] == "error"
        assert "serveur A" in harness.step("B")["detail"]
        assert harness.state.servers == {"A": "down", "B": "down"}
        assert not pair.servers["B"].exited_unexpectedly()
        if "disponibilité" in reason:
            assert time.monotonic() - begin >= SHORT_START_TIMEOUT_S
    assert not any(group_alive(group) for group in groups)


def test_failure_detail_carries_redacted_tail(harness):
    pair = harness.pair(
        {"lines": ["adminpassword={adminpassword}", "erreur fatale"], "exit_code": 1},
        {"lines": READY_LINES},
    )
    pair.start()
    assert not pair.wait_ready()
    detail = harness.step("A")["detail"]
    assert "erreur fatale" in detail
    assert f"adminpassword={REDACTED}" in detail
    assert "admin-marker" not in detail


def test_launch_failure_stops_other_server(harness):
    pair = harness.pair({"lines": READY_LINES}, {"lines": READY_LINES})
    (harness.root / "pzB" / "start-server.sh").unlink()
    assert not pair.start()
    assert "lancement impossible" in pair.failure
    assert harness.step("B")["state"] == "error"
    assert not pair.wait_ready()
    assert not group_alive(pair.servers["A"]._process.pid)


def test_stop_interrupts_wait_without_error(harness):
    pair = harness.pair({}, {})
    pair.start()
    results = []
    waiter = threading.Thread(target=lambda: results.append(pair.wait_ready()))
    waiter.start()
    assert pair.stop()
    waiter.join(PROCESS_TIMEOUT_S)
    assert results == [False]
    assert pair.failure is None
    assert harness.step("A")["state"] == "running"


def race_stop(pair: ServerPair) -> threading.Thread:
    stopper = threading.Thread(target=pair.stop)
    stopper.start()
    stopper.join(RACE_WINDOW_S)
    return stopper


def test_stop_during_failure_check_reports_nothing(harness):
    pair = harness.pair({}, {})
    pair.start()
    real_failures = pair._failures
    stoppers = []

    def racing() -> dict[str, str]:
        if not stoppers:
            stoppers.append(race_stop(pair))
        return real_failures()

    pair._failures = racing
    assert not pair.wait_ready()
    stoppers[0].join(PROCESS_TIMEOUT_S)
    assert pair.failure is None
    assert harness.step("A")["state"] == harness.step("B")["state"] == "running"
    assert harness.state.servers == {"A": "down", "B": "down"}


def test_stop_after_failure_check_never_follows_with_up(harness):
    pair = harness.pair({"lines": READY_LINES}, {"lines": READY_LINES})
    pair.start()
    until(lambda: all(server.ready for server in pair.servers.values()))
    real_failures = pair._failures
    stoppers = []

    def racing() -> dict[str, str]:
        failures = real_failures()
        stoppers.append(race_stop(pair))
        return failures

    pair._failures = racing
    pair.wait_ready()
    stoppers[0].join(PROCESS_TIMEOUT_S)
    assert pair.failure is None
    assert harness.state.servers == {"A": "down", "B": "down"}


def test_stop_during_start_keeps_next_server_down(harness):
    pair = harness.pair({"lines": READY_LINES}, {"lines": READY_LINES})
    (harness.root / "pzB" / "start-server.sh").unlink()
    first = pair.servers["A"]
    real_start = first.start

    def cancelled_after_launch() -> None:
        real_start()
        pair._stopping.set()

    first.start = cancelled_after_launch
    assert not pair.start()
    assert pair.failure is None
    assert pair.servers["B"]._process is None
    assert pair.stop()
    assert not group_alive(first._process.pid)


def test_pair_restarts_after_stop(harness):
    pair = harness.pair({"lines": READY_LINES}, {"lines": READY_LINES})
    for _ in range(2):
        assert pair.start()
        assert pair.wait_ready()
        assert pair.stop()
    log = pair.servers["A"].log_path.read_text(encoding="utf-8")
    assert log.count("SERVER STARTED") == 2


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param([BUILD, STARTED, LISTENING], id="before"),
        pytest.param([STARTED, BUILD, LISTENING], id="between"),
        pytest.param([STARTED, LISTENING, BUILD], id="after"),
    ],
)
def test_build_read_in_any_order(harness, lines):
    server = start_ready(harness.server(lines=lines))
    until(lambda: server.build is not None)
    assert server.build == "42.21.0"
    assert server.stop()


def test_ready_on_a_single_line(harness):
    server = start_ready(harness.server(lines=[f"{STARTED}  {LISTENING}"]))
    assert server.stop()


def test_output_is_merged_redacted_and_drained(harness):
    server = harness.server(
        stderr_lines=["sur stderr"],
        lines=[
            "dossier={cwd}",
            "adminpassword={adminpassword} rcon=rcon-marker",
            *FLOOD_LINES,
            *READY_LINES,
        ],
    )
    start_ready(server)
    assert server.stop()
    log = server.log_path.read_text(encoding="utf-8")
    assert "sur stderr" in log
    assert f"dossier={harness.root / 'pzA'}" in log
    assert f"adminpassword={REDACTED} rcon={REDACTED}" in log
    assert "marker" not in log
    assert len(server.log_tail()) == harness.runtime["log_tail_lines"]
    assert not any("marker" in line for line in server.log_tail())


def test_log_failure_keeps_draining(harness):
    harness.match_dir.mkdir(parents=True)
    (harness.match_dir / "server_A.log").symlink_to("/dev/full")
    server = start_ready(harness.server(lines=[*FLOOD_LINES, *READY_LINES]))
    assert "OSError" in server.log_error
    assert server.stop()


def test_ini_mode_restored_once_ready(harness):
    ini = harness.root / "Zomboid" / "Server" / "duelA.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text("RCONPassword=rcon-marker\n", encoding="utf-8")
    ini.chmod(0o644)
    server = start_ready(harness.server(lines=READY_LINES))
    assert stat.S_IMODE(ini.stat().st_mode) == 0o600
    assert server.ini_failure is None
    assert server.stop()


def test_ini_mode_failure_fails_start(harness, monkeypatch):
    def refuse(path, mode):
        raise PermissionError("refusé")

    pair = harness.pair({"lines": READY_LINES}, {"lines": READY_LINES})
    monkeypatch.setattr(os, "chmod", refuse)
    pair.start()
    assert not pair.wait_ready()
    assert "droits de duel" in pair.failure


def test_start_refused_while_running(harness):
    server = start_ready(harness.server(lines=READY_LINES))
    with pytest.raises(RuntimeError, match="déjà lancé"):
        server.start()
    assert server.stop()


def test_stop_never_started_server(harness):
    assert harness.server().stop()


def test_stop_by_rcon_saves_then_quits(harness):
    server = start_ready(harness.server(lines=READY_LINES, rcon_password="rcon-marker"))
    assert server.stop()
    assert server.stop_stage == STOP_RCON
    assert server.exit_code == 0
    assert "fake rcon commands=save,quit" in server.log_tail()


def test_stop_falls_back_to_stdin(harness):
    server = start_ready(harness.server(lines=READY_LINES))
    assert server.stop()
    assert server.stop_stage == STOP_STDIN
    assert server.exit_code == 0


def test_stop_sigterm_after_stop_timeout(harness):
    server = start_ready(harness.server(lines=READY_LINES, refuse_quit=True))
    begin = time.monotonic()
    assert server.stop()
    assert time.monotonic() - begin >= harness.runtime["stop_timeout_s"]
    assert server.stop_stage == STOP_SIGTERM
    assert server.exit_code == -signal.SIGTERM


def test_stop_timeout_bounds_rcon_attempt(harness):
    server = start_ready(harness.server(lines=READY_LINES, refuse_quit=True))
    rcon_port = harness.runtime["ports"]["A"]["rcon"]
    with socket.create_server(("127.0.0.1", rcon_port)):
        begin = time.monotonic()
        assert server.stop()
    assert time.monotonic() - begin < harness.runtime["rcon_timeout_s"]
    assert server.stop_stage == STOP_SIGTERM


def test_stop_waits_for_child_after_script_exit(harness):
    server = start_ready(
        harness.server(lines=READY_LINES, child=True, orphan_child=True)
    )
    child = pid_after(server, "fake child pid=")
    assert server.stop()
    assert server.exit_code == 0
    assert server.stop_stage == STOP_SIGTERM
    assert not pid_alive(child)


def test_graded_stop_kills_group(harness):
    server = start_ready(
        harness.server(
            lines=READY_LINES,
            child=True,
            refuse_quit=True,
            ignore_sigterm=True,
            rcon_password="rcon-marker",
        )
    )
    child = pid_after(server, "fake child pid=")
    pgid = server._process.pid
    begin = time.monotonic()

    assert server.stop()

    runtime = harness.runtime
    assert time.monotonic() - begin >= runtime["stop_timeout_s"] + runtime["kill_grace_s"]
    assert server.stop_stage == STOP_SIGKILL
    assert server.exit_code == -signal.SIGKILL
    assert not group_alive(pgid)
    assert not pid_alive(child)
    assert not server.exited_unexpectedly()


def test_stop_is_bounded_when_group_lingers(harness, monkeypatch):
    server = start_ready(harness.server(lines=READY_LINES))
    real_killpg = os.killpg

    def lingering(pgid: int, signum: int) -> None:
        if signum:
            real_killpg(pgid, signum)

    monkeypatch.setattr(os, "killpg", lingering)
    assert not server.stop()
    assert server.stop_stage == STOP_SIGKILL


def test_exited_unexpectedly_distinguishes_requested_stop(harness):
    crashed = harness.server("A", exit_code=1)
    crashed.start()
    until(lambda: crashed.exit_code is not None)
    assert crashed.exited_unexpectedly()
    assert crashed.stop()
    assert crashed.exited_unexpectedly()

    stopped = start_ready(harness.server("B", lines=READY_LINES))
    assert not stopped.exited_unexpectedly()
    assert stopped.stop()
    assert not stopped.exited_unexpectedly()
