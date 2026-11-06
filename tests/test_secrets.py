"""Aucun secret d'administration ne sort : état, logs, journal, logs de serveur (AC52)."""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from itertools import cycle
from pathlib import Path
from traceback import format_exception
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest

from doubles.fake_server import assert_private, on_host
from test_match import Duel
from test_snapshots import ENGINE_READY, engine_rig, free_port  # noqa: F401
from service.api import make_server
from service.engine import Engine
from service.guest_launcher import LauncherRefused, build_launcher
from service.journal import JOURNAL_NAME
from service.preflight import MOD_SRC
from service.servers import REDACTED, launch_command, read_ini

ADMIN_MARKER = "admin-secret-marker"
RCON_MARKER = "rcon-secret-marker"
MARKERS = (ADMIN_MARKER, RCON_MARKER)
# Le double imprime ses arguments et le mot de passe RCON, comme un serveur trop bavard.
LEAKY_LINES = [
    "commande : start-server.sh -adminpassword {adminpassword}",
    f"RCONPassword={RCON_MARKER}",
    *ENGINE_READY,
]
HTTP_TIMEOUT_S = 5


def assert_clean(text: str, markers: tuple[str, ...] = MARKERS) -> None:
    for marker in markers:
        assert marker not in text


def sweep(folder: Path, markers: tuple[str, ...] = MARKERS) -> list[Path]:
    """Relit tout `folder` et rend les fichiers vus : l'appelant dit combien il en attend."""
    files = [path for path in folder.rglob("*") if path.is_file()]
    for path in files:
        assert_clean(path.read_text(encoding="utf-8"), markers)
    return files


@pytest.fixture
def marked_secrets(monkeypatch):
    """Chaque match tire les deux marqueurs, dans l'ordre de `MatchSecrets`."""
    drawn = cycle(MARKERS)
    monkeypatch.setattr("service.engine.gen_secret", lambda: next(drawn))


@contextmanager
def served(engine: Engine) -> Iterator[Callable[[], str]]:
    """Sert `engine` en HTTP et rend de quoi lire le corps de `/api/state`."""
    httpd = make_server(engine)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/state"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def read_state() -> str:
        with urlopen(url, timeout=HTTP_TIMEOUT_S) as response:
            return response.read().decode("utf-8")

    try:
        yield read_state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(HTTP_TIMEOUT_S)


def play_leaky_match(duel: Duel, read_state: Callable[[], str]) -> list[str]:
    """Joue un match où le mod cite les marqueurs, et rend `/api/state` à chaque étape."""
    seen = [read_state()]
    duel.start_match()
    seen.append(read_state())
    mod = duel.mods["B"]
    duel.at_match(1)
    duel.kills("A", 1)
    duel.rig.until(lambda _: len(mod.in_path.read_bytes().splitlines()) == 2)
    command = json.loads(mod.in_path.read_bytes().splitlines()[-1])
    mod.cmd_result(command["seq"], "failed", reason=f"exception {ADMIN_MARKER}")
    # Refusée par le protocole, dont le message cite le `kind` : l'anomalie part au journal.
    mod.emit(f"inconnu {RCON_MARKER}", {})
    duel.kills("A", 1)
    duel.wait_for(lambda s: s["score"]["A"] == 2, expected="second kill au score")
    seen.append(read_state())
    duel.at_match(duel.state()["config"]["duration_s"])
    duel.finish()
    seen.append(read_state())
    return seen


def test_no_secret_leak(engine_rig, marked_secrets, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(engine_rig, "build", partial(engine_rig.build, lines=LEAKY_LINES))
    duel = Duel(engine_rig)
    engine = duel.engine
    ini_path = engine_rig.zomboid / "Server" / "duelA.ini"
    ini = read_ini(ini_path)
    assert ini["RCONPassword"] == RCON_MARKER
    assert_private(ini_path)
    assert engine_rig.configs[0]["admin_password"] == ADMIN_MARKER

    def explode(side, cfg, match_dir, redactor):
        raise RuntimeError(f"{launch_command(side, cfg)} : {cfg['rcon_password']} refusé")

    with served(engine) as read_state:
        seen = play_leaky_match(duel, read_state)
        # La panne la plus probable : une exception citant la commande, avant tout `Redactor`
        # de match, donc rattrapée par le seul garde-fou du moteur.
        monkeypatch.setattr(engine, "_server_factory", explode)
        assert engine.action("rematch")[0]
        failed = engine_rig.wait("error")
        seen.append(read_state())

    assert "erreur interne" in failed["error"]
    assert failed["error"].count(REDACTED) == 2
    assert_clean(json.dumps(engine.state.logs, ensure_ascii=False))
    for body in seen:
        assert_clean(body)
    assert_clean(caplog.text)

    match_dir = engine_rig.matches / duel.match_id
    assert (match_dir / JOURNAL_NAME).is_file()
    server_log = (match_dir / "server_A.log").read_text(encoding="utf-8")
    assert f"-adminpassword {REDACTED}" in server_log
    assert f"RCONPassword={REDACTED}" in server_log
    # Tout `matches/`, au-delà de ce que l'AC cite : `server_B.log` compris.
    assert len(sweep(engine_rig.matches)) >= 3

    records = engine_rig.journal(duel.match_id)
    assert any(r["data"].get("data", {}).get("reason") == f"exception {REDACTED}" for r in records)
    assert any(f"inconnu {REDACTED}" in r["data"].get("detail", "") for r in records)


def test_failed_server_tail_is_redacted(engine_rig, marked_secrets):
    engine_rig.build(
        on_match_launch=on_host(
            'echo "commande : $*"; exit 3',
            "echo commande : %*\nexit /b 3",
        )
    )

    engine_rig.start()
    state = engine_rig.wait("error", "lobby")

    assert state["phase"] == "error"
    assert f"-adminpassword {REDACTED}" in state["error"]
    assert_clean(json.dumps(state, ensure_ascii=False))


@pytest.mark.parametrize(
    "secret",
    ["hors-liste " + ADMIN_MARKER, f"{RCON_MARKER}%", f"{ADMIN_MARKER}\n", f"é{RCON_MARKER}"],
)
@pytest.mark.parametrize("make_args", [lambda s: ("192.0.2.1", s), lambda s: (s, "abc")])
def test_launcher_refusal_never_cites_the_value(runtime, caplog, secret, make_args):
    """Refus du mot de passe comme de l'adresse : ni message, ni chaîne d'exceptions, ni log."""
    caplog.set_level(logging.DEBUG)
    address, password = make_args(secret)

    with pytest.raises(LauncherRefused) as refused:
        build_launcher(address, runtime["ports"]["B"]["game"], password, MOD_SRC)

    assert_clean("".join(format_exception(refused.value)))
    assert_clean(repr(refused.value))
    assert_clean(caplog.text)


def share_answer(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=HTTP_TIMEOUT_S) as conn:
        conn.sendall(request)
        answer = b""
        while chunk := conn.recv(1 << 16):
            answer += chunk
    return answer


def test_share_listener_leaks_no_secret(engine_rig, marked_secrets, monkeypatch, caplog):
    """Refus comme téléchargement : aucun secret d'administration, et le jeton nulle part."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("service.engine.local_ip", lambda: "192.168.1.20")
    port = free_port(socket.SOCK_STREAM)
    engine_rig.runtime["share_port"] = port
    duel = Duel(engine_rig)
    invite = duel.state()["invite"]
    path = urlsplit(invite["link"]).path
    token = path.split("/")[1]
    password = invite["password"]

    served_bat = share_answer(port, f"GET {path} HTTP/1.1\r\n\r\n".encode())
    assert served_bat.startswith(b"HTTP/1.0 200")
    assert password.encode() in served_bat
    assert_clean(served_bat.decode("latin-1"))
    for request in (
        f"GET /{token}/other HTTP/1.1\r\n\r\n",
        "GET / HTTP/1.1\r\n\r\n",
        "GET /api/state HTTP/1.1\r\n\r\n",
        f"POST {path} HTTP/1.1\r\n\r\n",
        f"BREW {path} HTTP/1.1\r\n\r\n",
        f"GET {path}\r\n",
    ):
        refusal = share_answer(port, request.encode()).decode("latin-1")
        assert_clean(refusal, (*MARKERS, token, password))
        assert refusal.endswith("\r\n\r\n")

    assert engine_rig.engine.action("cancel")[0]
    engine_rig.wait("idle")
    engine_rig.closed()

    state = engine_rig.engine.snapshot()
    assert_clean(json.dumps(state, ensure_ascii=False), (*MARKERS, token))
    assert_clean(caplog.text, (*MARKERS, token, password))
    assert len(sweep(engine_rig.matches, (*MARKERS, token))) >= 3
