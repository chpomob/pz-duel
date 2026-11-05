"""L'API locale ne répond qu'à son interface : ni page web étrangère, ni rebinding DNS."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from service.api import make_server

SECRET = "mot-de-passe-joueur"
JSON_TYPE = "application/json"
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
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
