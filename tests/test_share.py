"""Modèle d'invitation (R7) : adresse, portée, lien et validité des `.bat` déjà envoyés."""

from __future__ import annotations

import http.client
import itertools
import json
import re
import socket
import threading

import pytest

from test_contract import Lobby
from test_match import Duel
from test_modes import FastClock, settle, wait_phase
from test_snapshots import PLAYER_PASSWORD, engine_rig  # noqa: F401
from service import engine as engine_module
from service.api import LAUNCHER_NAME, make_server
from service.engine import Engine
from service.guest_launcher import JOIN_MARK, PASSWORD_REFUSED, LauncherRefused
from service.servers import read_ini

PRESET = "demo"
LAN_ADDRESS = "192.168.1.20"
PUBLIC_ADDRESS = "82.64.1.2"
PUBLIC_NAME = "duel.example.net"
# Réservée à la documentation : aucune machine derrière (AC58).
NO_MACHINE = "192.0.2.1"
LOOPBACK = "127.0.0.1"
UNUSABLE_DETECTIONS = (LOOPBACK, "0.0.0.0", "169.254.1.1")
# Des labels valides : seule la longueur totale, 256, doit faire refuser.
TOO_LONG = "abc." * 63 + "abc" + ".net"
REFUSED_ADDRESSES = (
    "",
    "a b",
    "a&b",
    "a%b",
    "a/b",
    "a:b",
    'a"b',
    TOO_LONG,
    LOOPBACK,
    "127.1.2.3",
    "localhost",
    "LOCALHOST",
    "localhost.",
    "0.0.0.0",
    "169.254.1.1",
    "127.1",
    "2130706433",
    None,
    5,
)
TOKEN = re.compile(r"[0-9a-f]{32}")
LAUNCHER_ROUTE = "/api/invite/launcher"
ADDRESS_ROUTE = "/api/invite/address"
HTTP_TIMEOUT_S = 5
UNLISTED_PASSWORD = "two words"


def detect(monkeypatch, address: str) -> list[str]:
    """Remplace `local_ip` et rend la liste de ses appels."""
    calls = []

    def fake() -> str:
        calls.append(address)
        return address

    monkeypatch.setattr(engine_module, "local_ip", fake)
    return calls


@pytest.fixture
def lobby(engine_rig, monkeypatch):
    detect(monkeypatch, LAN_ADDRESS)
    return Lobby(engine_rig, PRESET)


@pytest.fixture
def mock_lobby(runtime, tmp_path, monkeypatch):
    detect(monkeypatch, LAN_ADDRESS)
    engine = Engine("mock", runtime=runtime, clock=FastClock(), matches_dir=tmp_path / "matches")
    assert engine.action("start", {"preset": PRESET})[0]
    wait_phase(engine, "lobby")
    yield engine
    # Sous `FastClock`, l'attente de l'adversaire ne relit jamais `_stop` : il faut le faire entrer.
    assert engine.action("simulate_join")[0]
    wait_phase(engine, "over")
    settle(engine)


@pytest.fixture
def api_port():
    """Rend de quoi servir un moteur : le serveur se ferme avec le test."""
    servers = []

    def serve(engine: Engine) -> int:
        server = make_server(engine)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


def request(port: int, method: str, path: str, body: dict | None = None):
    conn = http.client.HTTPConnection(LOOPBACK, port, timeout=HTTP_TIMEOUT_S)
    try:
        data = None if body is None else json.dumps(body).encode()
        headers = {} if body is None else {"Content-Type": "application/json"}
        conn.request(method, path, data, headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def join_line(launcher: bytes) -> tuple[str, int, str]:
    """Rend ce que le `.bat` donne à `+connect` et à `+password`."""
    mark = JOIN_MARK.encode()
    line = next(item for item in launcher.split(b"\r\n") if item.startswith(mark + b" "))
    _, address, port, password = line.decode("ascii").split(" ")
    return address, int(port), password


def link_of(rig, address: str, token: str) -> str:
    return f"http://{address}:{rig.runtime['share_port']}/{token}/{LAUNCHER_NAME}"


def token_of(invite: dict) -> str:
    return invite["link"].split("/")[3]


def start_lobby(rig, **payload) -> dict:
    assert rig.engine.action("start", {"preset": PRESET, **payload})[0]
    state = rig.wait("lobby", "error")
    assert state["phase"] == "lobby", state["error"]
    return state


def leave_lobby(rig) -> None:
    assert rig.engine.action("cancel")[0]
    rig.wait("idle")
    rig.closed()


def b_password(rig) -> str:
    return read_ini(rig.zomboid / "Server" / "duelB.ini")["Password"]


# -- AC43 --------------------------------------------------------------------
@pytest.mark.parametrize("address", [PUBLIC_ADDRESS, PUBLIC_NAME])
def test_ac43_entered_address_reaches_link_qr_and_launcher(lobby, address):
    engine = lobby.engine

    assert engine.set_invite_address(address)[0]

    invite = lobby.state()["invite"]
    assert invite["address"] == address
    assert invite["link"] == link_of(lobby.rig, address, token_of(invite))
    assert invite["qr_text"] == invite["link"]
    assert join_line(engine.guest_launcher())[0] == address


def test_ac43_refused_address_keeps_the_previous_one(lobby):
    engine = lobby.engine
    assert engine.set_invite_address(PUBLIC_ADDRESS)[0]
    invite = lobby.state()["invite"]
    launcher = engine.guest_launcher()

    for address in REFUSED_ADDRESSES:
        ok, message = engine.set_invite_address(address)
        assert not ok, address
        if address:
            assert str(address) not in message
        assert lobby.state()["invite"] == invite
        assert engine.guest_launcher() == launcher


def test_ac43_address_route_answers_like_the_engine(lobby, api_port):
    port = api_port(lobby.engine)

    status, _, body = request(port, "POST", ADDRESS_ROUTE, {"address": PUBLIC_NAME})
    assert status == 200
    assert json.loads(body)["state"]["invite"]["address"] == PUBLIC_NAME

    status, _, body = request(port, "POST", ADDRESS_ROUTE, {"address": LOOPBACK})
    assert status == 409
    answer = json.loads(body)
    assert answer["ok"] is False
    assert LOOPBACK not in answer["message"]
    assert answer["state"]["invite"]["address"] == PUBLIC_NAME

    assert request(port, "POST", ADDRESS_ROUTE, {})[0] == 409


def test_address_route_is_refused_outside_a_real_lobby(engine_rig, mock_lobby, api_port):
    idle = engine_rig.build()
    for engine in (idle, mock_lobby):
        before = engine.snapshot()["invite"]
        status, _, _ = request(api_port(engine), "POST", ADDRESS_ROUTE, {"address": PUBLIC_NAME})
        assert status == 409
        assert engine.snapshot()["invite"] == before


# -- AC44 --------------------------------------------------------------------
@pytest.mark.parametrize("detected", UNUSABLE_DETECTIONS)
def test_ac44_no_usable_address_then_entry(engine_rig, monkeypatch, api_port, detected):
    detect(monkeypatch, detected)
    lobby = Lobby(engine_rig, PRESET)
    engine = lobby.engine
    port = api_port(engine)

    state = lobby.state()
    assert state["phase"] == "lobby"
    invite = state["invite"]
    assert invite["needs_address"] is True
    assert invite["address"] is None and invite["link"] is None and invite["qr_text"] is None
    assert "enter" in invite["scope_text"]
    assert request(port, "GET", LAUNCHER_ROUTE)[0] == 409

    assert engine.set_invite_address(PUBLIC_ADDRESS)[0]

    invite = lobby.state()["invite"]
    assert invite["needs_address"] is False
    assert PUBLIC_ADDRESS in invite["link"]
    status, _, launcher = request(port, "GET", LAUNCHER_ROUTE)
    assert status == 200
    assert join_line(launcher)[0] == PUBLIC_ADDRESS


# -- AC57 --------------------------------------------------------------------
def test_ac57_detected_address_is_announced_as_local(lobby):
    invite = lobby.state()["invite"]

    assert invite["address"] == LAN_ADDRESS
    assert invite["link"] == link_of(lobby.rig, LAN_ADDRESS, token_of(invite))
    assert join_line(lobby.engine.guest_launcher())[0] == LAN_ADDRESS
    assert invite["scope"] == "lan"
    assert "local network" in invite["scope_text"]
    assert "public address" in invite["scope_text"]


def test_ac57_entered_address_names_the_shipped_ports(tmp_path, runtime, monkeypatch):
    """Sans le banc : il tire des ports libres, et lier ceux livrés dépendrait de la machine."""
    detect(monkeypatch, LAN_ADDRESS)
    engine = Engine("real", runtime=runtime, matches_dir=tmp_path)
    engine.state.config = {"password": PLAYER_PASSWORD, "code": "ABCDE"}
    engine.state.phase = "lobby"
    engine._open_invite()

    assert engine.set_invite_address(PUBLIC_ADDRESS)[0]

    invite = engine.snapshot()["invite"]
    assert invite["scope"] == "unverified"
    text = invite["scope_text"]
    assert "Unverified" in text
    assert "UDP ports 17271 and 17272" in text
    assert f"TCP port {runtime['share_port']}" in text


def test_ac57_ports_come_from_the_runtime(engine_rig, monkeypatch):
    detect(monkeypatch, LAN_ADDRESS)
    rig = engine_rig
    rig.runtime["share_port"] = 17999
    ports = rig.runtime["ports"]["B"]
    lobby = Lobby(rig, PRESET)

    assert lobby.engine.set_invite_address(PUBLIC_ADDRESS)[0]

    invite = lobby.state()["invite"]
    assert f"UDP ports {ports['game']} and {ports['udp']}" in invite["scope_text"]
    assert "TCP port 17999" in invite["scope_text"]
    assert ":17999/" in invite["link"]
    assert "17271" not in invite["scope_text"]
    assert join_line(lobby.engine.guest_launcher())[1] == ports["game"]


# -- AC58 --------------------------------------------------------------------
class SocketSpy:
    """Note toute résolution et toute connexion ; la boucle locale reste au banc d'essai."""

    def __init__(self, monkeypatch) -> None:
        self.seen: list[object] = []
        for name in ("connect", "connect_ex", "sendto"):
            # Après `self` ; `sendto` finit par sa cible.
            self._wrap(monkeypatch, socket.socket, name, lambda args: args[-1])
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            self._wrap(monkeypatch, socket, name, lambda args: args[0])

    def _wrap(self, monkeypatch, owner, name, target) -> None:
        original = getattr(owner, name)

        def spied(*args, **kwargs):
            self._note(target(args))
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, spied)

    def _note(self, target: object) -> None:
        host = target[0] if isinstance(target, tuple) else target
        if host != LOOPBACK:
            self.seen.append(target)


def test_ac58_entry_and_generation_touch_no_network(engine_rig, monkeypatch):
    calls = detect(monkeypatch, LAN_ADDRESS)
    lobby = Lobby(engine_rig, PRESET)
    assert calls == [LAN_ADDRESS]
    spy = SocketSpy(monkeypatch)

    assert lobby.engine.set_invite_address(NO_MACHINE)[0]
    launcher = lobby.engine.guest_launcher()
    state = lobby.state()

    assert NO_MACHINE in state["invite"]["link"]
    assert join_line(launcher)[0] == NO_MACHINE
    assert spy.seen == []
    assert calls == [LAN_ADDRESS]


def test_socket_spy_sees_what_local_ip_does(monkeypatch):
    """Sans ce témoin, un espion muet ferait passer AC58 pour rien."""
    spy = SocketSpy(monkeypatch)

    engine_module.local_ip()

    assert spy.seen


# -- AC36 --------------------------------------------------------------------
def test_ac36_launcher_is_refused_in_mock(mock_lobby, api_port):
    status, _, body = request(api_port(mock_lobby), "GET", LAUNCHER_ROUTE)
    assert status == 409
    assert json.loads(body)["ok"] is False
    with pytest.raises(LauncherRefused):
        mock_lobby.guest_launcher()


def test_ac36_launcher_is_refused_outside_the_lobby(engine_rig, monkeypatch, api_port):
    detect(monkeypatch, LAN_ADDRESS)
    rig = engine_rig
    rig.build()
    port = api_port(rig.engine)
    assert request(port, "GET", LAUNCHER_ROUTE)[0] == 409

    duel = Duel(rig)
    port = api_port(duel.engine)
    assert request(port, "GET", LAUNCHER_ROUTE)[0] == 200
    duel.start_match()
    status, _, body = request(port, "GET", LAUNCHER_ROUTE)
    assert status == 409
    assert PLAYER_PASSWORD.encode() not in body


def test_ac36_generation_writes_nothing_and_answers_as_an_attachment(
    engine_rig, monkeypatch, tmp_path, api_port
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(data_dir))
    detect(monkeypatch, LAN_ADDRESS)
    lobby = Lobby(engine_rig, PRESET)

    status, headers, launcher = request(api_port(lobby.engine), "GET", LAUNCHER_ROUTE)

    assert status == 200
    assert join_line(launcher)[2] == PLAYER_PASSWORD
    assert headers["Content-Disposition"] == f'attachment; filename="{LAUNCHER_NAME}"'
    assert headers["Cache-Control"] == "no-store"
    assert not list(tmp_path.rglob("*.bat"))
    files = [
        path
        for folder in (engine_rig.matches, data_dir)
        for path in folder.rglob("*")
        if path.is_file()
    ]
    assert files, "le balayage ne voit plus les fichiers de match"
    for path in files:
        assert PLAYER_PASSWORD.encode() not in path.read_bytes(), path


def test_unlisted_password_gives_a_reason_and_no_launcher(engine_rig, monkeypatch, api_port):
    detect(monkeypatch, LAN_ADDRESS)
    rig = engine_rig
    rig.build()
    invite = start_lobby(rig, password=UNLISTED_PASSWORD)["invite"]

    assert invite["launcher_refused"] == PASSWORD_REFUSED
    assert UNLISTED_PASSWORD not in invite["launcher_refused"]
    assert invite["link"] is None and invite["qr_text"] is None
    status, _, body = request(api_port(rig.engine), "GET", LAUNCHER_ROUTE)
    assert status == 409
    assert UNLISTED_PASSWORD.encode() not in body


# -- AC38 --------------------------------------------------------------------
@pytest.mark.parametrize("entered", [None, PUBLIC_ADDRESS])
def test_ac38_rematch_keeps_the_launcher_and_changes_the_token(engine_rig, monkeypatch, entered):
    detect(monkeypatch, LAN_ADDRESS)
    rig = engine_rig
    duel = Duel(rig)
    engine = duel.engine
    if entered:
        assert engine.set_invite_address(entered)[0]
    first = duel.state()["invite"]
    launcher = engine.guest_launcher()
    duel.start_match()
    duel.at_match(duel.state()["config"]["duration_s"])
    duel.finish()
    for mod in duel.mods.values():
        mod.restart(1)
        mod.seq = 1

    assert engine.action("rematch")[0]
    state = rig.wait("lobby", "error")

    assert state["phase"] == "lobby", state["error"]
    second = state["invite"]
    assert TOKEN.fullmatch(token_of(first)) and TOKEN.fullmatch(token_of(second))
    assert token_of(first) != token_of(second)
    assert second["scope"] == first["scope"]
    assert join_line(engine.guest_launcher()) == join_line(launcher)
    assert join_line(launcher)[0] == (entered or LAN_ADDRESS)
    assert join_line(launcher)[2] == b_password(rig)


# -- AC59 --------------------------------------------------------------------
@pytest.fixture
def draws(monkeypatch):
    """Tirages tous distincts : le mot de passe de B prend le premier de chaque `start`."""
    numbers = itertools.count(1)
    monkeypatch.setattr(engine_module, "gen_player_password", lambda: f"draw-{next(numbers)}")


def test_ac59_new_start_makes_the_launcher_stale(engine_rig, monkeypatch, draws):
    detect(monkeypatch, LAN_ADDRESS)
    rig = engine_rig
    engine = rig.build()
    start_lobby(rig)
    stale = join_line(engine.guest_launcher())[2]
    assert stale == b_password(rig)
    leave_lobby(rig)

    start_lobby(rig)

    assert b_password(rig) != stale
    assert join_line(engine.guest_launcher())[2] == b_password(rig)


def test_ac59_start_with_the_previous_password_keeps_it(engine_rig, monkeypatch, draws):
    detect(monkeypatch, LAN_ADDRESS)
    rig = engine_rig
    engine = rig.build()
    start_lobby(rig)
    kept = join_line(engine.guest_launcher())[2]
    leave_lobby(rig)

    start_lobby(rig, password=kept)

    assert b_password(rig) == kept
    assert join_line(engine.guest_launcher())[2] == kept


# -- AC60 --------------------------------------------------------------------
def test_ac60_note_lives_in_the_real_lobby_only(engine_rig, monkeypatch):
    detect(monkeypatch, LAN_ADDRESS)
    duel = Duel(engine_rig)

    note = duel.state()["invite"]["rematch_note"]
    assert "rematch" in note and "stale" in note
    assert note.isascii()

    duel.start_match()
    state = duel.state()
    assert state["phase"] == "running"
    assert state["invite"]["port"] == engine_rig.runtime["ports"]["B"]["game"]
    assert "rematch_note" not in state["invite"]


def test_ac60_note_leaves_with_a_cancelled_lobby(lobby):
    assert "rematch_note" in lobby.state()["invite"]
    leave_lobby(lobby.rig)
    assert "rematch_note" not in lobby.state()["invite"]


def test_ac60_mock_has_neither_note_nor_link(mock_lobby, runtime):
    invite = mock_lobby.snapshot()["invite"]

    assert "rematch_note" not in invite
    assert invite["link"] is None and invite["qr_text"] is None
    assert invite["needs_address"] is False
    assert invite["port"] == runtime["ports"]["A"]["game"]


# -- forme de l'invitation -----------------------------------------------------
def test_invite_never_carries_the_host_password(lobby):
    assert lobby.engine.set_invite_address(PUBLIC_NAME)[0]
    state = lobby.state()
    assert state["config"]["host_password"] not in json.dumps(state["invite"])


def test_invitation_never_reaches_the_journal(lobby):
    assert lobby.engine.set_invite_address(PUBLIC_NAME)[0]
    invite = lobby.state()["invite"]

    journal = json.dumps(lobby.rig.journal(lobby.match_id))

    assert token_of(invite) not in journal
    assert PUBLIC_NAME not in journal
