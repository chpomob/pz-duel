"""Réglages gérés, commande de lancement, expurgation, RCON et double de serveur."""

from __future__ import annotations

import logging
import os
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from doubles.fake_server import FakeRcon, install_fake_server
from service.rcon import RconClient, RconError
from service.servers import (
    REDACTED,
    Redactor,
    gen_secret,
    launch_command,
    read_ini,
    write_ini,
    write_sandbox,
)

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
