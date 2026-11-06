"""Invitation (R7) : adresse, portée, lien, validité des `.bat` envoyés et écouteur de partage."""

from __future__ import annotations

import http.client
import itertools
import inspect
import json
import logging
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from test_contract import Lobby
from test_match import Duel
from test_modes import FastClock, settle, wait_phase
from test_snapshots import PLAYER_PASSWORD, engine_rig, free_port  # noqa: F401
from service import engine as engine_module
from service.api import CONNECTIONS_MAX, LAUNCHER_NAME, make_server
from service.engine import Engine
from service.guest_launcher import JOIN_MARK, PASSWORD_REFUSED, LauncherRefused
from service.servers import read_ini
from service.share import ShareHandler, ShareServer

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
# La limite d'AC40.
CLOSE_LIMIT_S = 2
# `close` attend un tour de `serve_forever` : les tests sans moteur ne le paient pas.
POLL_S = 0.01
SHORT_READ_S = 0.2
WAIT_S = 5
STUB_TOKEN = "0123456789abcdef0123456789abcdef"
STUB_PATH = f"/{STUB_TOKEN}/{LAUNCHER_NAME}"
OTHER_METHODS = ("POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH", "TRACE", "CONNECT", "FOO")


@pytest.fixture(autouse=True)
def free_share_port(runtime):
    """Le port livré dépendrait de la machine, et un écouteur resté lié de l'ordre des tests."""
    runtime["share_port"] = free_port(socket.SOCK_STREAM)


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
    # Ce moteur n'a ni match ni fin : un vrai écouteur survivrait au test.
    monkeypatch.setattr(engine_module, "ShareServer", lambda *args: object())
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


# -- écouteur de partage -------------------------------------------------------
class StubEngine:
    """Rend un `.bat` différent à chaque appel : un cache se verrait."""

    def __init__(self) -> None:
        self.calls = 0

    def guest_launcher(self) -> bytes:
        self.calls += 1
        return f"launcher {self.calls}".encode()


@pytest.fixture
def stub_share():
    """Écouteur seul, sur un moteur factice."""
    engine = StubEngine()
    share = ShareServer(engine, STUB_TOKEN, free_port(socket.SOCK_STREAM), POLL_S)
    yield share, engine
    share.close()


def port_of(share: ShareServer) -> int:
    return share._httpd.server_address[1]


def raw(port: int, data: bytes) -> bytes:
    """Rend tout ce que le serveur répond à `data`, jusqu'à ce qu'il ferme."""
    with socket.create_connection((LOOPBACK, port), timeout=HTTP_TIMEOUT_S) as conn:
        conn.sendall(data)
        answer = b""
        while chunk := conn.recv(1 << 16):
            answer += chunk
    return answer


def get_line(path: str, method: str = "GET") -> bytes:
    return f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode("latin-1")


def split_answer(answer: bytes) -> tuple[int, bytes, bytes]:
    head, _, body = answer.partition(b"\r\n\r\n")
    return int(head.split()[1]), head, body


def assert_refused(answer: bytes, *secrets: str) -> None:
    code, _, body = split_answer(answer)
    assert code in (404, 405)
    assert body == b""
    for secret in secrets:
        assert secret.encode() not in answer


def is_closed(port: int) -> bool:
    try:
        socket.create_connection((LOOPBACK, port), timeout=HTTP_TIMEOUT_S).close()
    except OSError:
        return True
    return False


def assert_closed_in_time(port: int, left_at: float) -> None:
    """Date le début de la sonde : sous Windows, un refus de connexion prend lui-même 2 s."""
    while True:
        probed_at = time.monotonic()
        if is_closed(port):
            assert probed_at - left_at < CLOSE_LIMIT_S
            return
        assert probed_at - left_at < CLOSE_LIMIT_S, "écouteur encore ouvert"


def share_path(invite: dict) -> str:
    return urlsplit(invite["link"]).path


def share_port_of(lobby: Lobby) -> int:
    return lobby.rig.runtime["share_port"]


# -- AC37 --------------------------------------------------------------------
def test_ac37_link_gives_the_bytes_of_the_save(lobby, api_port):
    invite = lobby.state()["invite"]
    assert invite["share_available"] is True
    assert invite["link"] == invite["qr_text"]
    assert invite["link"].startswith("http://")

    status, headers, shared = request(share_port_of(lobby), "GET", share_path(invite))

    assert status == 200
    assert shared == request(api_port(lobby.engine), "GET", LAUNCHER_ROUTE)[2]
    assert join_line(shared)[0] == LAN_ADDRESS
    assert headers["Content-Disposition"] == f'attachment; filename="{LAUNCHER_NAME}"'
    assert headers["Cache-Control"] == "no-store"
    assert "Server" not in headers


def test_ac37_address_entered_later_reaches_the_next_download(lobby):
    path = share_path(lobby.state()["invite"])
    assert lobby.engine.set_invite_address(PUBLIC_ADDRESS)[0]

    invite = lobby.state()["invite"]
    assert share_path(invite) == path
    shared = request(share_port_of(lobby), "GET", path)[2]
    assert join_line(shared)[0] == PUBLIC_ADDRESS


def test_each_request_asks_the_engine(stub_share):
    share, engine = stub_share

    first = split_answer(raw(port_of(share), get_line(STUB_PATH)))
    second = split_answer(raw(port_of(share), get_line(STUB_PATH)))

    assert (first[0], first[2]) == (200, b"launcher 1")
    assert (second[0], second[2]) == (200, b"launcher 2")


def test_listener_serves_all_interfaces_and_the_api_only_the_loopback(stub_share, lobby):
    assert stub_share[0]._httpd.server_address[0] == "0.0.0.0"
    assert inspect.signature(make_server).parameters["host"].default == LOOPBACK
    server = make_server(lobby.engine)
    try:
        assert server.server_address[0] == LOOPBACK
    finally:
        server.server_close()


def test_binding_asks_no_name_resolution(monkeypatch):
    """`HTTPServer.server_bind` ferait ici un `getfqdn`, sous le verrou du moteur."""

    def refuse(*args):
        raise AssertionError("résolution de nom à la liaison")

    monkeypatch.setattr(socket, "getfqdn", refuse)
    ShareServer(StubEngine(), STUB_TOKEN, free_port(socket.SOCK_STREAM), POLL_S).close()


# -- AC39 --------------------------------------------------------------------
def test_ac39_everything_else_is_refused(lobby):
    invite = lobby.state()["invite"]
    port = share_port_of(lobby)
    token = token_of(invite)
    right = share_path(invite)
    secrets = (token, invite["password"], "<html", "<!doctype")

    for path in (
        "/",
        "/api/state",
        "/api/action",
        "/index.html",
        f"/{'0' * len(token)}/{LAUNCHER_NAME}",
        f"/{token}/other.bat",
        f"/{token}/",
        f"/{token}",
    ):
        answer = raw(port, get_line(path))
        assert split_answer(answer)[0] == 404
        assert_refused(answer, *secrets)
    for method in OTHER_METHODS:
        answer = raw(port, get_line(right, method))
        assert split_answer(answer)[0] == 405
        assert b"Allow: GET" in answer
        assert_refused(answer, *secrets)
    assert request(port, "GET", right)[0] == 200


@pytest.mark.parametrize(
    "request_bytes",
    [
        get_line(STUB_PATH + "?x=1"),
        get_line(STUB_PATH + "/"),
        get_line(STUB_PATH.upper()),
        get_line(f"/../{STUB_TOKEN}/{LAUNCHER_NAME}"),
        get_line("http://x" + STUB_PATH),
        get_line("/é" + STUB_PATH),
        f"GET {STUB_PATH}\r\n".encode(),
        f"GET {STUB_PATH} HTTP/0.9\r\n\r\n".encode(),
        f"GET {STUB_PATH} HTTP/9.9\r\n\r\n".encode(),
        b"GET\r\n\r\n",
        b"\r\n",
        b"\xff\xfe \xff\r\n\r\n",
        get_line("/" + "a" * 70000),
        f"GET /x HTTP/1.1\r\nA: {'b' * 70000}\r\n\r\n".encode(),
    ],
    ids=lambda value: repr(value[:24]),
)
def test_hostile_request_gets_an_empty_refusal(stub_share, request_bytes):
    share, engine = stub_share

    answer = raw(port_of(share), request_bytes)

    assert_refused(answer, STUB_TOKEN, "Python", "a" * 32)
    assert engine.calls == 0


def test_method_with_a_body_still_reads_its_refusal(stub_share):
    share, _ = stub_share
    body = b"x" * (1 << 20)
    head = f"POST {STUB_PATH} HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode()

    assert split_answer(raw(port_of(share), head + body))[0] == 405


def test_refused_launcher_is_a_plain_refusal(stub_share):
    share, engine = stub_share
    engine.guest_launcher = lambda: (_ for _ in ()).throw(LauncherRefused("no launcher"))

    assert_refused(raw(port_of(share), get_line(STUB_PATH)), "no launcher")


def test_engine_failure_is_a_plain_refusal(stub_share, caplog):
    caplog.set_level(logging.DEBUG)
    share, engine = stub_share
    engine.guest_launcher = lambda: (_ for _ in ()).throw(RuntimeError(PLAYER_PASSWORD))

    assert_refused(raw(port_of(share), get_line(STUB_PATH)), PLAYER_PASSWORD)
    assert PLAYER_PASSWORD not in caplog.text


# -- AC40 --------------------------------------------------------------------
def leave_by_countdown(lobby: Lobby) -> None:
    lobby.join_and_resolve()


def leave_by_cancel(lobby: Lobby) -> None:
    assert lobby.engine.action("cancel")[0]


def leave_by_failure(lobby: Lobby) -> None:
    lobby.engine._match.pump = lambda now_ms: "panne de test"


def leave_by_exception(lobby: Lobby) -> None:
    def explode(now_ms):
        raise RuntimeError("panne de test")

    lobby.engine._match.pump = explode


def leave_by_shutdown(lobby: Lobby) -> None:
    # L'arrêt des serveurs dure : le refus n'a pas à l'attendre.
    threading.Thread(target=lobby.engine.shutdown, daemon=True).start()


def leave_by_fail(lobby: Lobby) -> None:
    lobby.engine._fail("panne de test")


def leave_by_cancel_state(lobby: Lobby) -> None:
    lobby.engine._cancel_state()


@pytest.mark.parametrize(
    "leave",
    [
        leave_by_countdown,
        leave_by_cancel,
        leave_by_failure,
        leave_by_exception,
        leave_by_shutdown,
        leave_by_fail,
        leave_by_cancel_state,
    ],
)
def test_ac40_every_exit_closes_the_listener(lobby, leave):
    invite = lobby.state()["invite"]
    port = share_port_of(lobby)
    assert request(port, "GET", share_path(invite))[0] == 200

    left_at = time.monotonic()
    leave(lobby)

    assert_closed_in_time(port, left_at)
    state = lobby.rig.until(lambda s: not s["invite"].get("link"), expected="lien retiré")
    assert not state["invite"].get("share_available")
    assert not state["invite"].get("qr_text")


class SpiedShare:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class StoppingMatch:
    """Dit si l'écouteur était fermé quand l'arrêt des serveurs, qui dure, a commencé."""

    result = None

    def __init__(self, share: SpiedShare) -> None:
        self.share = share
        self.closed_first: list[bool] = []

    def enter_phase(self, phase: str) -> None:
        self.closed_first.append(self.share.closed == 1)

    def shutdown(self) -> bool:
        self.closed_first.append(self.share.closed == 1)
        return True

    def record_phase(self, phase: str) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def open_share(runtime, tmp_path):
    """Moteur sans fil de match : `_change_state`, qui ferme aussi, ne peut rien rattraper."""
    engine = Engine("real", runtime=runtime, matches_dir=tmp_path)
    engine.state.phase = "lobby"
    engine._share = SpiedShare()
    return engine, engine._share


@pytest.mark.parametrize("leave", ["_fail", "_cancel_state"])
def test_ac40_each_state_exit_closes_by_itself(open_share, leave):
    engine, share = open_share

    getattr(engine, leave)(*(["panne de test"] if leave == "_fail" else []))

    assert share.closed == 1


def test_ac40_finish_closes_before_stopping_the_servers(open_share):
    engine, share = open_share
    match = StoppingMatch(share)

    engine._finish_match(match, None)

    assert match.closed_first and all(match.closed_first)
    assert share.closed == 1


def test_ac40_shutdown_closes_before_stopping_the_servers(open_share):
    engine, share = open_share
    engine._match = match = StoppingMatch(share)

    assert engine.shutdown()

    assert match.closed_first == [True]
    assert share.closed == 1


def test_ac40_closing_twice_closes_once(open_share):
    engine, share = open_share

    engine._close_share()
    engine._close_share()

    assert share.closed == 1


def test_ac40_port_is_not_bound_before_the_lobby(engine_rig, monkeypatch):
    detect(monkeypatch, LAN_ADDRESS)
    rig = engine_rig
    engine = rig.build()
    port = rig.runtime["share_port"]
    verify = engine_module.RealMatch.verify
    seen = []

    def spied(match, caps):
        seen.append((engine.snapshot()["phase"], is_closed(port)))
        return verify(match, caps)

    monkeypatch.setattr(engine_module.RealMatch, "verify", spied)
    assert is_closed(port)

    state = start_lobby(rig)

    assert seen == [("contract", True)]
    assert state["invite"]["share_available"] is True
    assert not is_closed(port)


def test_ac40_mock_lobby_binds_nothing(mock_lobby, runtime):
    assert is_closed(runtime["share_port"])
    assert mock_lobby.snapshot()["invite"]["share_available"] is False


def test_ac40_status_change_before_the_lobby_keeps_the_listener(lobby):
    lobby.engine._change_state(lambda state: state.log("message de test"))

    assert not is_closed(share_port_of(lobby))


def test_ac40_connections_are_capped(stub_share):
    share, engine = stub_share
    port = port_of(share)
    held = [socket.create_connection((LOOPBACK, port)) for _ in range(CONNECTIONS_MAX)]
    slots = share._httpd._slots
    deadline = time.monotonic() + WAIT_S
    try:
        while slots._value and time.monotonic() < deadline:
            time.sleep(POLL_S)
        assert slots._value == 0

        assert split_answer(raw(port, get_line(STUB_PATH)))[0] == 503
        assert engine.calls == 0
    finally:
        for conn in held:
            conn.close()
    while slots._value < CONNECTIONS_MAX and time.monotonic() < deadline:
        time.sleep(POLL_S)
    assert split_answer(raw(port, get_line(STUB_PATH)))[0] == 200


def test_dripped_request_cannot_renew_the_deadline(stub_share, monkeypatch):
    monkeypatch.setattr(ShareHandler, "timeout", SHORT_READ_S)
    share, _ = stub_share
    started = time.monotonic()
    with socket.create_connection((LOOPBACK, port_of(share)), timeout=WAIT_S) as conn:
        conn.sendall(b"GET /")
        try:
            while time.monotonic() - started < WAIT_S:
                conn.sendall(b"a")
                time.sleep(SHORT_READ_S / 4)
        except OSError:
            pass

    assert time.monotonic() - started < WAIT_S


def test_connection_accepted_before_closing_gets_a_refusal(stub_share):
    """Sinon elle survivrait à son jeton : `guest_launcher` répondrait au lobby suivant."""
    share, engine = stub_share
    with socket.create_connection((LOOPBACK, port_of(share)), timeout=WAIT_S) as conn:
        conn.sendall(b"GET ")
        slots = share._httpd._slots
        deadline = time.monotonic() + WAIT_S
        while slots._value == CONNECTIONS_MAX and time.monotonic() < deadline:
            time.sleep(POLL_S)
        share.close()
        conn.sendall(get_line(STUB_PATH)[len(b"GET ") :])
        answer = b""
        while chunk := conn.recv(1 << 16):
            answer += chunk

    assert_refused(answer, STUB_TOKEN)
    assert engine.calls == 0


def test_close_is_idempotent(stub_share):
    share, _ = stub_share
    closers = [threading.Thread(target=share.close) for _ in range(3)]
    for closer in closers:
        closer.start()
    for closer in closers:
        closer.join(WAIT_S)

    share.close()

    assert is_closed(port_of(share))


def test_close_right_after_opening_does_not_hang():
    for _ in range(20):
        share = ShareServer(StubEngine(), STUB_TOKEN, free_port(socket.SOCK_STREAM), POLL_S)
        closer = threading.Thread(target=share.close, daemon=True)
        closer.start()
        closer.join(WAIT_S)
        assert not closer.is_alive()


@pytest.mark.parametrize("entered", [None, PUBLIC_ADDRESS])
def test_rematch_rebinds_right_after_a_download(engine_rig, monkeypatch, entered):
    """Un téléchargement laisse une connexion en TIME_WAIT sur le port."""
    detect(monkeypatch, LAN_ADDRESS)
    rig = engine_rig
    duel = Duel(rig)
    engine = duel.engine
    port = rig.runtime["share_port"]
    if entered:
        assert engine.set_invite_address(entered)[0]
    first = share_path(duel.state()["invite"])
    assert request(port, "GET", first)[0] == 200
    duel.start_match()
    assert is_closed(port)
    duel.at_match(duel.state()["config"]["duration_s"])
    duel.finish()
    for mod in duel.mods.values():
        mod.restart(1)
        mod.seq = 1

    assert engine.action("rematch")[0]
    state = rig.wait("lobby", "error")

    assert state["phase"] == "lobby", state["error"]
    assert state["invite"]["share_available"] is True
    second = share_path(state["invite"])
    assert second != first
    assert_refused(raw(port, get_line(first)), token_of(state["invite"]))
    assert request(port, "GET", second)[0] == 200


# -- AC41 --------------------------------------------------------------------
def test_ac41_token_stays_out_of_every_record(lobby, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    invite = lobby.state()["invite"]
    token = token_of(invite)
    port = share_port_of(lobby)
    rig = lobby.rig

    assert request(port, "GET", share_path(invite))[0] == 200
    for path in (f"/{token}/other.bat", f"/{token}", "/x"):
        assert_refused(raw(port, get_line(path)), token)
    assert_refused(raw(port, get_line(share_path(invite), "POST")), token)
    match_id = lobby.match_id
    leave_lobby(rig)

    records = json.dumps(rig.journal(match_id))
    files = [path for path in rig.matches.rglob("*") if path.is_file()]
    captured = capfd.readouterr()
    assert files
    assert token not in records
    assert token not in json.dumps(lobby.state()["logs"])
    assert token not in caplog.text
    assert token not in captured.out + captured.err
    for path in files:
        assert token.encode() not in path.read_bytes()


# -- AC42 --------------------------------------------------------------------
@pytest.fixture
def taken_port(runtime):
    with socket.socket() as holder:
        holder.bind(("", runtime["share_port"]))
        holder.listen()
        yield holder


def test_ac42_taken_port_keeps_the_lobby_and_the_save(lobby_on_taken_port, api_port):
    lobby = lobby_on_taken_port
    invite = lobby.state()["invite"]

    assert lobby.state()["phase"] == "lobby"
    assert invite["share_available"] is False
    assert invite["link"] is None and invite["qr_text"] is None
    text = invite["share_text"]
    assert "unavailable" in text and text.isascii()
    assert str(share_port_of(lobby)) in text
    status, _, launcher = request(api_port(lobby.engine), "GET", LAUNCHER_ROUTE)
    assert status == 200
    assert join_line(launcher)[0] == LAN_ADDRESS


@pytest.fixture
def lobby_on_taken_port(engine_rig, monkeypatch, taken_port):
    detect(monkeypatch, LAN_ADDRESS)
    return Lobby(engine_rig, PRESET)


def test_ac42_entered_address_keeps_the_notice(lobby_on_taken_port):
    lobby = lobby_on_taken_port

    assert lobby.engine.set_invite_address(PUBLIC_ADDRESS)[0]

    invite = lobby.state()["invite"]
    assert invite["address"] == PUBLIC_ADDRESS
    assert invite["share_available"] is False
    assert "share_text" in invite
    assert join_line(lobby.engine.guest_launcher())[0] == PUBLIC_ADDRESS


def test_ac42_notice_lives_in_the_lobby_only(lobby_on_taken_port):
    lobby = lobby_on_taken_port

    assert lobby.engine.action("cancel")[0]
    lobby.rig.wait("idle")

    assert "share_text" not in lobby.state()["invite"]


def test_ac42_notice_is_absent_when_the_listener_runs(lobby):
    assert "share_text" not in lobby.state()["invite"]


def test_ac42_notice_is_absent_in_mock(mock_lobby):
    assert "share_text" not in mock_lobby.snapshot()["invite"]
