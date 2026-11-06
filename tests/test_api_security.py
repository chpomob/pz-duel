"""L'API locale ne répond qu'à son interface : ni page web étrangère, ni rebinding DNS."""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from service.api import (
    BODY_MAX,
    IDENTIFIER_FIELDS,
    IDENTIFIER_MAX,
    PROTOCOL_REFUSED,
    PROTOCOL_REFUSALS,
    Handler,
    is_identifier,
    make_server,
)
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


def raw_answer(port: int, request: bytes) -> bytes:
    """Passe par le socket : `http.client` refuserait d'émettre ces requêtes."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(request)
        answer = b""
        while chunk := conn.recv(4096):
            answer += chunk
    return answer


def raw_exchange(port: int, request: bytes) -> tuple[int, dict]:
    head, _, body = raw_answer(port, request).partition(b"\r\n\r\n")
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


def framed(port: int, *framing: str, body: bytes = b"") -> bytes:
    """Une action dont le cadrage est écrit à la main, en latin-1 comme le lit le service."""
    lines = [
        "POST /api/action HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        f"Content-Type: {JSON_TYPE}",
        *framing,
    ]
    return "\r\n".join([*lines, "", ""]).encode("latin-1") + body


# Chaque ligne décrit un cadrage que le service refuse sans lire : aucun octet n'est envoyé.
HOSTILE_FRAMINGS = {
    "py-ssize-t": (413, ["Content-Length: 9223372036854775808"]),
    "digits-beyond-int-limit": (413, [f"Content-Length: {'9' * 5000}"]),
    "just-over-the-cap": (413, [f"Content-Length: {BODY_MAX + 1}"]),
    "negative": (400, ["Content-Length: -1"]),
    "huge-negative": (400, ["Content-Length: -9223372036854775809"]),
    "text": (400, ["Content-Length: abc"]),
    "decimal": (400, ["Content-Length: 1.5"]),
    "plus-sign": (400, ["Content-Length: +5"]),
    "underscore": (400, ["Content-Length: 1_0"]),
    "superscript": (400, ["Content-Length: ²"]),
    "two-values": (400, ["Content-Length: 5, 5"]),
    "zero": (400, ["Content-Length: 0"]),
    "zeros": (400, ["Content-Length: 000"]),
    "absent": (400, []),
    "repeated": (400, ["Content-Length: 2", "Content-Length: 3"]),
    "chunked": (400, ["Transfer-Encoding: chunked"]),
    "chunked-and-length": (400, ["Transfer-Encoding: chunked", "Content-Length: 2"]),
}


class ReadSpy:
    """Double de `rfile` : les en-têtes passent, chaque `read` du corps est noté."""

    def __init__(self, stream, reads: list) -> None:
        self.stream = stream
        self.reads = reads

    def __getattr__(self, name: str):
        return getattr(self.stream, name)

    def read(self, *args):
        self.reads.append(args)
        return self.stream.read(*args)


@pytest.fixture
def body_reads(monkeypatch):
    reads: list = []
    setup = Handler.setup

    def spied_setup(self):
        setup(self)
        self.rfile = ReadSpy(self.rfile, reads)

    monkeypatch.setattr(Handler, "setup", spied_setup)
    return reads


@pytest.mark.parametrize(
    ("expected", "framing"), HOSTILE_FRAMINGS.values(), ids=HOSTILE_FRAMINGS.keys()
)
def test_hostile_framing_is_refused_before_any_read(api, body_reads, expected, framing):
    engine, port = api
    status, answer = raw_exchange(port, framed(port, *framing))
    assert status == expected
    assert answer["ok"] is False and answer["error"]
    assert body_reads == []
    assert engine.actions == []


def test_read_spy_sees_a_valid_body(api, body_reads):
    """Témoin : sans lui, une liste vide ne prouverait rien."""
    engine, port = api
    status, _ = raw_exchange(
        port, framed(port, f"Content-Length: {len(ACTION_BODY)}", body=ACTION_BODY)
    )
    assert status == 200
    assert body_reads == [(len(ACTION_BODY),)]
    assert engine.actions == ["start"]


def test_body_at_the_cap_is_read(api):
    engine, port = api
    body = json.dumps({"action": "start", "pseudo": ""}).encode()
    body = body[:-1] + b" " * (BODY_MAX - len(body)) + b"}"
    assert len(body) == BODY_MAX
    status, answer = action(port, body)
    assert status == 200 and answer["ok"] is True
    assert engine.actions == ["start"]


def test_body_shorter_than_announced_is_refused(api):
    """Le client ferme son sens d'envoi : `{}` seul serait un JSON valide pour le moteur."""
    engine, port = api
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(framed(port, "Content-Length: 10", body=b"{}"))
        conn.shutdown(socket.SHUT_WR)
        answer = conn.makefile("rb").read()
    head, _, body = answer.partition(b"\r\n\r\n")
    assert int(head.split()[1]) == 400
    assert json.loads(body)["ok"] is False
    assert engine.actions == []


def explode(*_):
    raise RuntimeError(SECRET)


@pytest.mark.parametrize(
    ("method", "path"),
    [("snapshot", "/api/state"), ("presets", "/api/presets"), ("action", "/api/action")],
)
def test_unexpected_failure_gets_a_json_answer_without_trace(api, method, path):
    engine, port = api
    setattr(engine, method, explode)
    headers = {"Host": f"127.0.0.1:{port}", "Content-Type": JSON_TYPE}
    status, body = send(port, path, headers, ACTION_BODY if method == "action" else None)
    assert status == 500
    assert json.loads(body) == {"ok": False, "error": "erreur interne"}
    assert SECRET.encode() not in body and b"Traceback" not in body


def test_answer_that_cannot_be_serialized_gets_a_json_answer(api):
    engine, port = api
    engine.snapshot = lambda: {"objet": object()}
    status, body = get_state(port)
    assert status == 500
    assert json.loads(body)["ok"] is False


class Departed(Sink):
    """Le client est parti : toute réponse lève."""

    answered = False

    def _fail(self, code: int, error: str) -> None:
        raise ConnectionResetError


def test_barrier_survives_a_departed_client():
    Handler._last_word(Departed())


def test_barrier_never_answers_twice():
    sink = Sink()
    sink.answered = True
    sink._fail = lambda *_: pytest.fail("seconde réponse")
    Handler._last_word(sink)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unparsable_target_gets_a_json_refusal(api, method):
    engine, port = api
    request = f"{method} http://[ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
    status, answer = raw_exchange(port, request.encode())
    assert status == 400
    assert answer["ok"] is False and answer["error"]
    assert engine.actions == []


LINE_MAX = 65536
LONG_LINE = b"X-Long: " + b"a" * (LINE_MAX + 1) + b"\r\n"
# Chaque ligne est un refus du parseur de la bibliothèque, avant nos routes.
PROTOCOL_ABUSES = {
    "header-line-too-long": (431, b"GET / HTTP/1.1\r\n" + LONG_LINE + b"\r\n"),
    "too-many-headers": (431, b"GET / HTTP/1.1\r\n" + b"X: y\r\n" * 101 + b"\r\n"),
    "request-line-too-long": (414, b"GET /" + b"a" * LINE_MAX + b" HTTP/1.1\r\n\r\n"),
    "unknown-method": (501, b"BREW / HTTP/1.1\r\nHost: x\r\n\r\n"),
    "method-without-route": (501, b"DELETE /api/action HTTP/1.1\r\nHost: x\r\n\r\n"),
    "method-with-body": (501, b"PUT / HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"),
    "invalid-version": (400, b"GET / HTTP/x.y\r\n\r\n"),
    "not-a-version": (400, b"GET / FTP/1.1\r\n\r\n"),
    "unsupported-version": (505, b"GET / HTTP/2.0\r\n\r\n"),
    "too-many-words": (400, b"GET / extra HTTP/1.1 more\r\n\r\n"),
    "single-word": (400, b"GET\r\n"),
    "two-words-not-get": (400, b"POST /api/action\r\n"),
    "blank-line": (400, b"\r\n"),
    "spaces-only": (400, b"   \r\n"),
    "http-0.9-page": (400, b"GET /\r\n"),
    "http-0.9-api": (400, b"GET /api/state\r\n"),
    "http-0.9-latin-1-space": (400, b"GET\xa0/\r\n"),
    "http-0.9-spelled-out": (400, b"GET /api/state HTTP/0.9\r\nHost: x\r\n\r\n"),
    "http-0.9-long-header": (431, b"GET / HTTP/0.9\r\n" + LONG_LINE + b"\r\n"),
}
abuses = pytest.mark.parametrize(
    ("status", "raw"), PROTOCOL_ABUSES.values(), ids=PROTOCOL_ABUSES.keys()
)
LIBRARY_MARKS = (b"<html", b"<!doctype", b"basehttprequesthandler", b"basehttp/", b"python/")


def split_answer(answer: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = answer.partition(b"\r\n\r\n")
    status_line, *lines = head.decode("latin-1").split("\r\n")
    headers = dict(line.lower().split(": ", 1) for line in lines)
    return int(status_line.split()[1]), headers, body


@abuses
def test_protocol_refusal_is_json_with_a_status_line(api, status, raw):
    engine, port = api
    code, headers, body = split_answer(raw_answer(port, raw))
    assert code == status
    assert headers["content-type"].startswith(JSON_TYPE)
    assert int(headers["content-length"]) == len(body)
    assert json.loads(body) == {"ok": False, "error": PROTOCOL_REFUSALS[status]}
    assert engine.actions == []


@abuses
def test_protocol_refusal_carries_no_library_signature(api, status, raw):
    _, port = api
    answer = raw_answer(port, raw).lower()
    assert not [mark for mark in LIBRARY_MARKS if mark in answer]


@pytest.mark.parametrize("raw", [b"GET /\r\n", b"POST /api/action\r\n", b"GET\xa0/\r\n"])
def test_two_word_request_never_reaches_the_library_parser(api, monkeypatch, raw):
    """Seules certaines versions y attendent des en-têtes ; ce garde-fou vaut sur toutes."""
    _, port = api

    def parser_entered(_handler):
        raise AssertionError("parseur atteint")

    monkeypatch.setattr(BaseHTTPRequestHandler, "parse_request", parser_entered)
    assert split_answer(raw_answer(port, raw))[0] == 400


def test_served_answer_carries_no_library_signature(api):
    _, port = api
    request = f"GET /api/presets HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
    code, headers, _ = split_answer(raw_answer(port, request))
    assert code == 200
    assert headers["server"] == Handler.server_version.lower()


def test_protocol_refusal_never_echoes_the_request(api):
    _, port = api
    answer = raw_answer(port, b"<script> / HTTP/1.1\r\n\r\n")
    assert split_answer(answer)[0] == 501
    assert b"script" not in answer


def test_unmapped_code_still_gets_a_json_refusal():
    sink = Sink()
    sink._drain = lambda: None
    sink._fail = lambda code, error: sink.sent.append((code, error))
    sink.sent = []
    Handler.send_error(sink, 418, "cité par le client")
    assert sink.sent == [(418, PROTOCOL_REFUSED)]


def test_http_client_reads_the_refusal_of_a_long_header(api):
    _, port = api
    headers = {"Host": f"127.0.0.1:{port}", "X-Long": "a" * (LINE_MAX + 1)}
    status, body = send(port, "/api/state", headers)
    assert status == 431
    assert json.loads(body)["ok"] is False


def test_head_gets_the_refusal_headers_without_a_body(api):
    _, port = api
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("HEAD", "/")
        response = conn.getresponse()
        assert response.status == 501
        assert response.getheader("Content-Type").startswith(JSON_TYPE)
        assert int(response.getheader("Content-Length")) > 0
    finally:
        conn.close()
    assert raw_answer(port, b"HEAD / HTTP/1.1\r\n\r\n").endswith(b"\r\n\r\n")


def test_client_still_writing_finishes_and_reads_the_refusal(api):
    """Au-delà des tampons du noyau, un refus sans lecture casse l'écriture du client."""
    _, port = api
    request = b"GET / HTTP/1.1\r\nX-Long: " + b"a" * (16 * BODY_MAX) + b"\r\n\r\n"
    code, _, body = split_answer(raw_answer(port, request))
    assert code == 431
    assert json.loads(body)["ok"] is False


class Flood:
    """Un client qui émet sans fin : seule l'échéance arrête la lecture."""

    def shutdown(self, how: int) -> None:
        pass

    def settimeout(self, timeout: float) -> None:
        pass

    def recv(self, size: int) -> bytes:
        return b"x"


class Gone(Flood):
    def shutdown(self, how: int) -> None:
        raise OSError("déconnecté")


def test_drain_stops_at_the_deadline(monkeypatch):
    monkeypatch.setattr("service.api.DRAIN_S", 0.01)
    sink = Sink()
    sink.connection = Flood()
    Handler._drain(sink)


def test_drain_survives_a_departed_client():
    sink = Sink()
    sink.connection = Gone()
    Handler._drain(sink)
