"""L'API locale ne répond qu'à son interface : ni page web étrangère, ni rebinding DNS."""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from pathlib import Path

import pytest

from service.api import IDENTIFIER_FIELDS, IDENTIFIER_MAX, Handler, is_identifier, make_server
from service.engine import ACTIONS, Engine, load_presets

SECRET = "mot-de-passe-joueur"
JSON_TYPE = "application/json"
# `shutdown` attend un tour de `serve_forever` : la valeur par défaut coûte 0,5 s par test.
POLL_S = 0.01
ACTION_BODY = json.dumps({"action": "start", "preset": "demo"}).encode()
OTHER_PORT = 1
LOCAL_NAMES = ("127.0.0.1", "localhost")
FOREIGN_ORIGINS = (
    "http://evil.example",
    "http://evil.example:{port}",
    f"http://127.0.0.1:{OTHER_PORT}",
    "https://127.0.0.1:{port}",
    "null",
)
FOREIGN_HOSTS = ("evil.example:{port}", f"127.0.0.1:{OTHER_PORT}", "127.0.0.1", "localhost")
FOREIGN_TYPES = (
    "text/plain",
    "text/plain; charset=utf-8",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=x",
    None,
)


class RecordingEngine:
    """Tient lieu de moteur : une action qui l'atteint lancerait de vrais processus."""

    def __init__(self) -> None:
        self.actions: list[str] = []

    def snapshot(self) -> dict:
        return {"config": {"password": SECRET}}

    def presets(self) -> list:
        return []

    def action(self, action: str, payload: dict) -> tuple[bool, str]:
        self.actions.append(action)
        return True, "ok"


@pytest.fixture
def api():
    engine = RecordingEngine()
    server = make_server(engine)
    threading.Thread(target=server.serve_forever, args=(POLL_S,), daemon=True).start()
    yield engine, server.server_address[1]
    server.shutdown()
    server.server_close()


def send(port: int, path: str, headers: dict, body: bytes | None = None) -> tuple[int, bytes]:
    """Envoie exactement `headers` : `http.client` n'ajoute ni Host ni Accept-Encoding."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET" if body is None else "POST", path, skip_host=True,
                        skip_accept_encoding=True)  # fmt: skip
        for name, value in headers.items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def post_action(port: int, **headers: str) -> tuple[int, bytes]:
    base = {"Host": f"127.0.0.1:{port}", "Content-Type": JSON_TYPE}
    merged = base | {name.replace("_", "-"): value for name, value in headers.items()}
    return send(port, "/api/action", merged, ACTION_BODY)


def get_state(port: int, **headers: str) -> tuple[int, bytes]:
    return send(port, "/api/state", {"Host": f"127.0.0.1:{port}"} | headers)


@pytest.mark.parametrize("origin", FOREIGN_ORIGINS)
def test_foreign_origin_refused(api, origin):
    engine, port = api
    origin = origin.format(port=port)

    status, body = post_action(port, Origin=origin)
    assert status == 403
    assert engine.actions == []

    status, body = get_state(port, Origin=origin)
    assert status == 403
    assert SECRET.encode() not in body


@pytest.mark.parametrize("host", FOREIGN_HOSTS)
def test_foreign_host_refused(api, host):
    engine, port = api
    host = host.format(port=port)

    status, body = post_action(port, Host=host)
    assert status == 403
    assert engine.actions == []

    status, body = get_state(port, Host=host)
    assert status == 403
    assert SECRET.encode() not in body


def test_missing_host_refused(api):
    engine, port = api
    status, body = send(port, "/api/state", {})
    assert status == 403
    assert SECRET.encode() not in body

    status, _ = send(port, "/api/action", {"Content-Type": JSON_TYPE}, ACTION_BODY)
    assert status == 403
    assert engine.actions == []


@pytest.mark.parametrize("content_type", FOREIGN_TYPES)
def test_action_requires_json_content_type(api, content_type):
    engine, port = api
    headers = {"Host": f"127.0.0.1:{port}"}
    if content_type is not None:
        headers["Content-Type"] = content_type

    status, _ = send(port, "/api/action", headers, ACTION_BODY)

    assert status == 415
    assert engine.actions == []


@pytest.mark.parametrize("name", LOCAL_NAMES)
@pytest.mark.parametrize("with_origin", [False, True])
def test_local_interface_still_works(api, name, with_origin):
    engine, port = api
    local = f"{name}:{port}"
    origin = {"Origin": f"http://{local}"} if with_origin else {}

    status, body = get_state(port, Host=local, **origin)
    assert status == 200
    assert json.loads(body)["config"]["password"] == SECRET

    status, _ = send(port, "/api/presets", {"Host": local} | origin)
    assert status == 200

    headers = {"Host": local, "Content-Type": f"{JSON_TYPE}; charset=utf-8"} | origin
    status, body = send(port, "/api/action", headers, ACTION_BODY)
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert engine.actions == ["start"]


def action(port: int, body: bytes) -> tuple[int, dict]:
    headers = {"Host": f"127.0.0.1:{port}", "Content-Type": JSON_TYPE}
    status, answer = send(port, "/api/action", headers, body)
    return status, json.loads(answer)


@pytest.mark.parametrize(
    "body",
    [b"[1, 2]", b'"start"', b"5", b"null", b"\xff", b"[" * 200_000],
    ids=["array", "text", "number", "null", "bad-utf8", "deep"],
)
def test_body_that_is_no_object_gets_a_json_refusal(api, body):
    engine, port = api
    status, answer = action(port, body)
    assert status == 400
    assert answer["ok"] is False and answer["error"]
    assert engine.actions == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "start", "day_length": "abc"},
        {"action": "start", "day_length": 8},
        {"action": "start", "duration_s": "abc"},
        {"action": "start", "pseudo": 5},
    ],
    ids=["day-length-text", "day-length-unknown", "duration-text", "pseudo"],
)
def test_bad_payload_keeps_the_connection_and_answers_json(payload):
    engine = Engine("mock")
    server = make_server(engine)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, answer = action(server.server_address[1], json.dumps(payload).encode())
    finally:
        server.shutdown()
        server.server_close()
    assert status == 409
    assert answer["ok"] is False and answer["message"]
    assert answer["state"]["phase"] == "idle"


SURROGATE = "\ud800"
# Un champ tenu hors de `IDENTIFIER_FIELDS` doit être classé ici, en connaissance de cause.
NOT_IDENTIFIERS = {"pseudo", "password", "duration_s", "day_length"}
SERVICE = Path(__file__).resolve().parent.parent / "service"
PAYLOAD_KEY = re.compile(r"""(?:payload\.get\(|payload\[|_text_option\(payload, )"(\w+)\"""")
BAD_IDENTIFIERS = (
    SURROGATE,
    "<script>",
    "a b",
    "préset",
    "a\n",
    "",
    "x" * (IDENTIFIER_MAX + 1),
    5,
    None,
    True,
    ["start"],
    {"a": 1},
)


def serve(engine):
    server = make_server(engine)
    threading.Thread(target=server.serve_forever, args=(POLL_S,), daemon=True).start()
    return server


def raw_exchange(port: int, request: bytes) -> tuple[int, dict]:
    """Passe par le socket : `http.client` refuserait d'émettre ces requêtes."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(request)
        answer = b""
        while chunk := conn.recv(4096):
            answer += chunk
    head, _, body = answer.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


def test_real_mode_start_with_a_lone_surrogate_preset_gets_a_json_refusal(tmp_path):
    engine = Engine("real", zomboid_dir=tmp_path, matches_dir=tmp_path)
    server = serve(engine)
    try:
        body = json.dumps({"action": "start", "preset": SURROGATE}).encode()
        status, answer = action(server.server_address[1], body)
    finally:
        server.shutdown()
        server.server_close()
    assert status == 400
    assert answer["ok"] is False and answer["error"]
    assert engine.snapshot()["phase"] == "idle"


@pytest.mark.parametrize("field", IDENTIFIER_FIELDS)
@pytest.mark.parametrize("value", BAD_IDENTIFIERS, ids=repr)
def test_bad_identifier_is_refused_before_the_engine(api, field, value):
    engine, port = api
    payload = {"action": "start"} | {field: value}
    status, answer = action(port, json.dumps(payload).encode())
    assert status == 400
    assert answer["ok"] is False
    assert engine.actions == []
    if isinstance(value, str) and value:
        assert value not in answer["error"]


def test_refusal_is_the_same_whatever_the_value(api):
    _, port = api
    answers = [
        action(port, json.dumps({"action": "start", "preset": value}).encode())
        for value in BAD_IDENTIFIERS
    ]
    assert len({answer["error"] for _, answer in answers}) == 1


def test_every_shipped_identifier_passes_the_boundary():
    names = [*ACTIONS, *(preset["id"] for preset in load_presets())]
    assert names and all(is_identifier(name) for name in names)


def test_longest_identifier_passes_the_boundary(api):
    engine, port = api
    name = "x" * IDENTIFIER_MAX
    status, _ = action(port, json.dumps({"action": name}).encode())
    assert status == 200
    assert engine.actions == [name]


def test_every_field_read_from_the_payload_is_classified():
    """Le champ ajouté demain doit choisir son camp : sinon il échapperait à la frontière."""
    source = "".join(path.read_text(encoding="utf-8") for path in SERVICE.glob("*.py"))
    found = set(PAYLOAD_KEY.findall(source))
    assert {"action", "preset", "pseudo"} <= found, "le balayage ne voit plus le code"
    assert found <= set(IDENTIFIER_FIELDS) | NOT_IDENTIFIERS


class Sink:
    """Reçoit ce que `_json` enverrait, sans socket."""

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.sent = (code, body, ctype)


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False, "message": f"preset inconnu : {SURROGATE}"},
        {f"clé {SURROGATE}": "valeur"},
        {"state": {"logs": [{"msg": f"a{SURROGATE}é\\"}]}},
        {"low": "\udc80", "emoji": "\U0001f600", "nul": "\x00"},
    ],
    ids=["message", "key", "nested", "others"],
)
def test_json_never_raises_and_keeps_the_text(payload):
    sink = Sink()
    Handler._json(sink, 409, payload)
    code, body, ctype = sink.sent
    assert code == 409
    assert ctype == "application/json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == payload


def test_engine_text_with_a_surrogate_still_reaches_the_client(api):
    """Second verrou seul : le champ libre passe la frontière, la réponse ne doit pas lever."""
    engine, port = api
    engine.action = lambda *_: (False, f"refus {SURROGATE}")
    status, answer = action(port, ACTION_BODY)
    assert status == 409
    assert answer["message"] == f"refus {SURROGATE}"


@pytest.mark.parametrize("field", ["pseudo", "password"])
def test_free_text_with_a_surrogate_keeps_the_connection(field):
    engine = Engine("mock")
    server = serve(engine)
    port = server.server_address[1]
    try:
        body = json.dumps({"action": "start", "preset": "demo", field: SURROGATE}).encode()
        status, answer = action(port, body)
        state_status, _ = get_state(port, Host=f"127.0.0.1:{port}")
    finally:
        engine.shutdown()
        server.shutdown()
        server.server_close()
    assert status == 200 and answer["ok"] is True
    assert state_status == 200


@pytest.mark.parametrize("length", ["abc", "-1", "1.5"])
def test_bad_content_length_gets_a_json_refusal(api, length):
    engine, port = api
    request = (
        f"POST /api/action HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Content-Type: {JSON_TYPE}\r\nContent-Length: {length}\r\n\r\n"
    )
    status, answer = raw_exchange(port, request.encode())
    assert status == 400
    assert answer["ok"] is False and answer["error"]
    assert engine.actions == []


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unparsable_target_gets_a_json_refusal(api, method):
    engine, port = api
    request = f"{method} http://[ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
    status, answer = raw_exchange(port, request.encode())
    assert status == 400
    assert answer["ok"] is False and answer["error"]
    assert engine.actions == []
