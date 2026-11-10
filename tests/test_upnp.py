"""UPnP de la phase 3 : faux IGD local (P2) et client `service/upnp.py` (P3–P6).

Un garde de socket, actif sur chaque test du fichier, refuse et note toute connexion, tout
envoi et toute résolution hors `127.0.0.0/8`, et tout envoi vers le groupe SSDP (AC30) : le
faux IGD local est le seul interlocuteur.
"""

from __future__ import annotations

import ast
import http.client
import ipaddress
import json
import logging
import socket
import threading
import time
from pathlib import Path

import pytest

from doubles.fake_igd import FakeIGD
from service import upnp
from service.rules import load_runtime

SOAP_NS = "urn:schemas-upnp-org:service:WANIPConnection:1"
LOOPBACK_NET = ipaddress.ip_network("127.0.0.0/8")
FOREIGN_NAME = "gateway.invalid"


class SocketGuard:
    """Espion des sockets : note chaque cible, et refuse celles hors de la boucle locale."""

    def __init__(self) -> None:
        self.connects: list[str] = []
        self.sends: list[str] = []
        self.resolutions: list[str] = []
        self.send_targets: list[object] = []
        self.violations: list[str] = []

    def check(self, kind: str, host: object) -> None:
        text = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            address = None
        if address is None or address not in LOOPBACK_NET or text == upnp.SSDP_GROUP:
            self.violations.append(f"{kind} {text}")
            raise AssertionError(f"socket guard: {kind} vers {text} interdit")


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch):
    guard = SocketGuard()
    connect, connect_ex, sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto
    getaddrinfo = socket.getaddrinfo

    def host_of(address: object) -> object:
        return address[0] if isinstance(address, tuple) else address

    def guarded_connect(self, address):
        guard.connects.append(str(host_of(address)))
        guard.check("connect", host_of(address))
        return connect(self, address)

    def guarded_connect_ex(self, address):
        guard.connects.append(str(host_of(address)))
        guard.check("connect_ex", host_of(address))
        return connect_ex(self, address)

    def guarded_sendto(self, data, *args):
        address = args[-1]
        guard.sends.append(str(host_of(address)))
        guard.send_targets.append(address)
        guard.check("sendto", host_of(address))
        return sendto(self, data, *args)

    def guarded_getaddrinfo(host, *args, **kwargs):
        guard.resolutions.append(str(host))
        if host is not None:
            guard.check("getaddrinfo", host)
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield guard
    assert guard.violations == []


RUNTIME = load_runtime()
PORTS = [
    (RUNTIME["ports"]["B"]["game"], "UDP"),
    (RUNTIME["ports"]["B"]["udp"], "UDP"),
    (RUNTIME["share_port"], "TCP"),
]
FORBIDDEN_PORTS = {*RUNTIME["ports"]["A"].values(), RUNTIME["ports"]["B"]["rcon"]}
UPNP_PATH = Path(upnp.__file__)


def ac12_violations(calls) -> list[str]:
    """AC12 : ni port du camp A ni RCON, ni port externe à 0, ni `NewRemoteHost` non vide."""
    bad: list[str] = []
    for call in calls:
        args = call.args
        for name in ("NewExternalPort", "NewInternalPort"):
            if name in args and args[name].strip().isdigit():
                port = int(args[name])
                if port in FORBIDDEN_PORTS:
                    bad.append(f"{call.action} {name}={port}")
                if name == "NewExternalPort" and port == 0:
                    bad.append(f"{call.action} external port 0")
        if call.action in ("AddPortMapping", "DeletePortMapping"):
            if args.get("NewExternalPort", "").strip() == "":
                bad.append(f"{call.action} external port missing")
            if args.get("NewRemoteHost", "") != "":
                bad.append(f"{call.action} NewRemoteHost={args['NewRemoteHost']!r}")
    return bad


@pytest.fixture(autouse=True)
def ac12_audit(monkeypatch):
    """Relève chaque faux IGD démarré dans le fichier et passe AC12 sur tous ses appels."""
    started: list[FakeIGD] = []
    start = FakeIGD.start

    def recording_start(self):
        started.append(self)
        return start(self)

    monkeypatch.setattr(FakeIGD, "start", recording_start)
    yield started
    for igd in started:
        assert ac12_violations(igd.actions()) == []


def _cfg(**overrides: object) -> dict:
    cfg = dict(load_runtime()["upnp"])
    cfg.update(overrides)
    return cfg


def _msearch(target: tuple[str, int]) -> str:
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {target[0]}:{target[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
    ).encode("ascii")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2)
        sock.sendto(request, target)
        data, _ = sock.recvfrom(4096)
    return data.decode("latin-1")


def _soap(igd: FakeIGD, action: str, args: dict[str, str], timeout: float = 2) -> tuple[int, bytes]:
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    body = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:{action} xmlns:u="{SOAP_NS}">{inner}</u:{action}></s:Body></s:Envelope>'
    ).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", igd.http_port, timeout=timeout)
    try:
        conn.request(
            "POST",
            igd.control_path,
            body,
            {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{SOAP_NS}#{action}"'},
        )
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _mapping(port: int, protocol: str) -> dict[str, str]:
    return {
        "NewRemoteHost": "",
        "NewExternalPort": str(port),
        "NewProtocol": protocol,
        "NewInternalPort": str(port),
        "NewInternalClient": "127.0.0.1",
        "NewEnabled": "1",
        "NewPortMappingDescription": "test",
        "NewLeaseDuration": "60",
    }


def test_fake_igd_answers_msearch():
    with FakeIGD() as igd:
        reply = _msearch(igd.ssdp_target)
        assert reply.startswith("HTTP/1.1 200 OK")
        assert f"LOCATION: http://127.0.0.1:{igd.http_port}/desc.xml" in reply
        assert len(igd.msearches) == 1
        conn = http.client.HTTPConnection("127.0.0.1", igd.http_port, timeout=2)
        conn.request("GET", "/desc.xml")
        body = conn.getresponse().read()
        conn.close()
        assert b"WANIPConnection:1" in body
    with FakeIGD(location_host="127.0.0.2") as other:
        assert "LOCATION: http://127.0.0.2:" in _msearch(other.ssdp_target)


def test_fake_igd_records_calls():
    with FakeIGD(errors={"DeletePortMapping": 714, ("AddPortMapping", 2): 718}) as igd:
        assert _soap(igd, "DeletePortMapping", {"NewExternalPort": "1", "NewProtocol": "UDP"})[0] == 500
        assert _soap(igd, "AddPortMapping", _mapping(1, "UDP"))[0] == 200
        status, body = _soap(igd, "AddPortMapping", _mapping(2, "TCP"))
        assert status == 500 and b"<errorCode>718</errorCode>" in body
        status, body = _soap(igd, "GetExternalIPAddress", {})
        assert status == 200 and b"5.6.7.8" in body
        assert [(c.order, c.index, c.action) for c in igd.calls] == [
            (1, 1, "DeletePortMapping"),
            (2, 1, "AddPortMapping"),
            (3, 2, "AddPortMapping"),
            (4, 1, "GetExternalIPAddress"),
        ]
        assert igd.calls[1].args["NewInternalClient"] == "127.0.0.1"
        assert set(igd.mappings) == {(1, "UDP")}


def test_fake_igd_close_releases_silent_handler():
    igd = FakeIGD(silent_from=2).start()
    try:
        assert _soap(igd, "AddPortMapping", _mapping(1, "UDP"))[0] == 200
        outcome: list[object] = []

        def call() -> None:
            try:
                outcome.append(_soap(igd, "AddPortMapping", _mapping(2, "UDP"), timeout=30))
            except (OSError, http.client.HTTPException) as exc:
                outcome.append(exc)

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        while len(igd.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [c.silent for c in igd.calls] == [False, True]
        assert (2, "UDP") not in igd.mappings
    finally:
        started = time.monotonic()
        igd.close()
    thread.join(timeout=1)
    assert time.monotonic() - started < 1
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], Exception)


def test_socket_guard_refuses_foreign_targets(socket_guard):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(AssertionError):
            sock.sendto(b"x", (upnp.SSDP_GROUP, upnp.SSDP_PORT))
    with pytest.raises(AssertionError):
        socket.getaddrinfo(FOREIGN_NAME, 80)
    assert socket_guard.violations == [f"sendto {upnp.SSDP_GROUP}", f"getaddrinfo {FOREIGN_NAME}"]
    socket_guard.violations.clear()


# --- P3 : découverte et description ------------------------------------------------


@pytest.mark.parametrize("service_type", ["WANIPConnection:1", "WANPPPConnection:1"])
def test_ac1_discovery_and_description(service_type, socket_guard):
    with FakeIGD(service_type=service_type) as igd:
        gateway, failure = upnp.find_gateway(_cfg(), igd.ssdp_target)
        assert failure is None
        assert gateway == upnp.Gateway("127.0.0.1", igd.http_port, igd.control_path, service_type)
        assert len(igd.msearches) == 1
        assert igd.fetches == ["/desc.xml"]
        assert igd.calls == []
    assert set(socket_guard.sends) == {"127.0.0.1"}


def test_ac1_discover_returns_responder_and_location():
    with FakeIGD() as igd:
        found = upnp.discover(igd.ssdp_target, timeout_s=1, max_bytes=_cfg()["max_response_bytes"])
        assert found == ("127.0.0.1", igd.location)


def test_ac1_absolute_control_url_on_same_host():
    with FakeIGD() as igd:
        igd.control_url = f"http://127.0.0.1:{igd.http_port}{igd.control_path}"
        gateway, failure = upnp.find_gateway(_cfg(), igd.ssdp_target)
        assert failure is None and gateway.control_path == igd.control_path


@pytest.mark.parametrize(
    "control_url", ["http://127.0.0.2/ctl/wan", f"http://{FOREIGN_NAME}/ctl/wan", "//127.0.0.2/ctl/wan"]
)
def test_ac4_foreign_control_url_is_no_gateway(control_url, socket_guard):
    with FakeIGD(control_url=control_url) as igd:
        gateway, failure = upnp.find_gateway(_cfg(), igd.ssdp_target)
        assert gateway is None and failure == {"status": "no_gateway", "reason": "foreign_control_url"}
        assert igd.calls == []
    assert "127.0.0.2" not in socket_guard.connects
    assert FOREIGN_NAME not in socket_guard.resolutions


def test_ac2_no_reply_is_no_gateway_within_timeout():
    with FakeIGD(ssdp_reply=False) as igd:
        started = time.monotonic()
        gateway, failure = upnp.find_gateway(_cfg(discovery_timeout_s=1), igd.ssdp_target)
        elapsed = time.monotonic() - started
        assert gateway is None and failure == {"status": "no_gateway", "reason": "no_reply"}
        assert elapsed <= 1 + 1
        assert len(igd.msearches) == 1
        assert igd.fetches == [] and igd.calls == []


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"description": "oversized"}, "unreadable"),
        ({"description": "malformed"}, "no_service"),
        ({"service_type": None}, "no_service"),
    ],
)
def test_ac3_bad_description_is_no_gateway(options, reason):
    with FakeIGD(**options) as igd:
        gateway, failure = upnp.find_gateway(_cfg(), igd.ssdp_target)
        assert gateway is None and failure == {"status": "no_gateway", "reason": reason}
        assert igd.fetches == ["/desc.xml"]
        assert igd.calls == []


def test_ac3_description_at_exact_cap_is_read():
    cap = _cfg()["max_response_bytes"]
    with FakeIGD(description="oversized", oversize_bytes=cap) as igd:
        gateway, failure = upnp.find_gateway(_cfg(), igd.ssdp_target)
        assert failure is None and gateway is not None


def test_ac3_fetch_without_content_length_is_capped():
    """Un corps sans `Content-Length` est lu par morceaux et coupé au plafond."""
    cap = _cfg()["max_response_bytes"]
    body = b"x" * (cap + 1)

    def serve(server: socket.socket) -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        thread = threading.Thread(target=serve, args=(server,), daemon=True)
        thread.start()
        reply = upnp._fetch("127.0.0.1", server.getsockname()[1], "/", None, 2, cap)
        thread.join(timeout=2)
    assert reply.failure == "unreadable"
    assert reply.local_address == "127.0.0.1"


def test_ac3_fetch_slow_drip_respects_deadline():
    """Un serveur qui égrène son corps ne retient pas la lecture au-delà de l'échéance."""
    stop = threading.Event()

    def serve(server: socket.socket) -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
            while not stop.wait(0.1):
                try:
                    conn.sendall(b"x")
                except OSError:
                    return

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        thread = threading.Thread(target=serve, args=(server,), daemon=True)
        thread.start()
        started = time.monotonic()
        reply = upnp._fetch("127.0.0.1", server.getsockname()[1], "/", None, 0.5, 4096)
        elapsed = time.monotonic() - started
        stop.set()
        thread.join(timeout=2)
    assert reply.failure == "timeout"
    assert elapsed <= 0.5 + 1


def _drip_server(head: bytes, drip: bytes):
    """Serveur qui envoie `head` puis égrène `drip` octet par octet, toutes les 0,05 s."""
    stop = threading.Event()

    def serve(server: socket.socket) -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(head)
            for byte in drip:
                if stop.wait(0.05):
                    return
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    return
            stop.wait(5)

    return stop, serve


@pytest.mark.parametrize(
    ("head", "drip"),
    [
        # en-têtes égrenés : `getresponse()` lit ligne à ligne
        (b"HTTP/1.1 200 OK\r\n", b"X-Pad: " + b"a" * 400 + b"\r\n\r\n"),
        # entête de morceau égrenée : `read1()` lit la taille du morceau ligne à ligne
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"1" + b";ext=" + b"b" * 400 + b"\r\n",
        ),
    ],
    ids=["headers", "chunk-framing"],
)
def test_ac3_fetch_drip_in_framing_respects_deadline(head, drip):
    """Ni les en-têtes ni l'entête d'un morceau, égrenés, ne retiennent la lecture (R1)."""
    stop, serve = _drip_server(head, drip)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        thread = threading.Thread(target=serve, args=(server,), daemon=True)
        thread.start()
        started = time.monotonic()
        reply = upnp._fetch("127.0.0.1", server.getsockname()[1], "/", None, 0.5, 4096)
        elapsed = time.monotonic() - started
        stop.set()
        thread.join(timeout=2)
    assert reply.failure == "timeout"
    assert elapsed <= 0.5 + 0.5


def test_ac3_fetch_chunked_body_is_read():
    """Un corps en morceaux, servi d'un bloc, se lit à travers la socket sous échéance."""

    def serve(server: socket.socket) -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
            )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        thread = threading.Thread(target=serve, args=(server,), daemon=True)
        thread.start()
        reply = upnp._fetch("127.0.0.1", server.getsockname()[1], "/", None, 2, 4096)
        thread.join(timeout=2)
    assert reply.failure is None and reply.status == 200
    assert reply.body == b"hello world"


DEVICE = "urn:schemas-upnp-org:device:"


def igd_description(service: str = "WANIPConnection:1", control: str = "/ctl", root_type: str = "InternetGatewayDevice:1") -> str:
    """Description minimale d'un IGD : racine, `WANDevice`, `WANConnectionDevice`, service (C3)."""
    return (
        '<root xmlns="urn:schemas-upnp-org:device-1-0">'
        f"<device><deviceType>{DEVICE}{root_type}</deviceType><deviceList>"
        f"<device><deviceType>{DEVICE}WANDevice:1</deviceType><deviceList>"
        f"<device><deviceType>{DEVICE}WANConnectionDevice:1</deviceType><serviceList><service>"
        f"<serviceType>urn:schemas-upnp-org:service:{service}</serviceType>"
        f"<controlURL>{control}</controlURL></service></serviceList></device>"
        "</deviceList></device></deviceList></device></root>"
    )


GOOD_DESCRIPTION = igd_description(control="{control}")


@pytest.mark.parametrize(
    ("codec", "declared", "bom"),
    [
        ("utf-16-le", "UTF-16", b"\xff\xfe"),
        ("utf-16-be", "UTF-16", b"\xfe\xff"),
    ],
)
def test_ac3_find_service_rejects_dtd_in_any_encoding(codec, declared, bom):
    """Une DTD encodée hors ASCII (octets entrecoupés de NUL) est refusée par l'analyseur."""
    prolog = f'<?xml version="1.0" encoding="{declared}"?>'
    dtd = '<!DOCTYPE root [<!ENTITY x "/ctl">]>'
    with_dtd = bom + (prolog + dtd + GOOD_DESCRIPTION.format(control="&x;")).encode(codec)
    assert b"<!doctype" not in with_dtd.lower() and b"<!entity" not in with_dtd.lower()
    assert upnp._find_service(with_dtd) is None
    plain = bom + (prolog + GOOD_DESCRIPTION.format(control="/ctl")).encode(codec)
    assert upnp._find_service(plain) == ("WANIPConnection:1", "/ctl")


def test_ac3_unknown_xml_encoding_is_no_gateway(monkeypatch):
    """Un encodage XML inconnu (`LookupError`) rend `no_gateway`, pas une exception (AC3)."""
    data = b'<?xml version="1.0" encoding="x-unknown"?><root/>'
    assert upnp._find_service(data) is None
    with FakeIGD() as igd:
        monkeypatch.setattr(igd, "description_body", lambda: data)
        gateway, failure = upnp.find_gateway(_cfg(), igd.ssdp_target)
        assert gateway is None and failure == {"status": "no_gateway", "reason": "no_service"}
        assert igd.fetches == ["/desc.xml"]


def test_ac3_find_service_rejects_doctype_and_entities():
    good = igd_description("WANIPConnection:2").encode("utf-8")
    assert upnp._find_service(good) == ("WANIPConnection:2", "/ctl")
    assert upnp._find_service(b'<!DOCTYPE r [<!ENTITY a "b">]>' + good) is None
    assert upnp._find_service(b"<!entity x>" + good) is None
    assert upnp._find_service(good.replace(b"WANIPConnection:2", b"WANIPConnection:3")) is None
    assert upnp._find_service(b"\xff\xfe<not xml") is None


@pytest.mark.parametrize("location_host", ["127.0.0.2", FOREIGN_NAME])
def test_ac4_foreign_location_is_no_gateway(location_host, socket_guard):
    with FakeIGD(location_host=location_host) as igd:
        gateway, failure = upnp.find_gateway(_cfg(), igd.ssdp_target)
        assert gateway is None and failure == {"status": "no_gateway", "reason": "foreign_location"}
        assert len(igd.msearches) == 1
        assert igd.fetches == [] and igd.calls == []
    assert location_host not in socket_guard.connects
    assert location_host not in socket_guard.resolutions


@pytest.mark.parametrize(
    ("location", "responder", "expected"),
    [
        ("http://127.0.0.1:5000/desc.xml", "127.0.0.1", ("127.0.0.1", 5000, "/desc.xml")),
        ("http://127.0.0.1/d?x=1", "127.0.0.1", ("127.0.0.1", 80, "/d?x=1")),
        ("https://127.0.0.1:5000/desc.xml", "127.0.0.1", None),
        ("http://127.0.0.2:5000/desc.xml", "127.0.0.1", None),
        (f"http://{FOREIGN_NAME}:5000/desc.xml", "127.0.0.1", None),
        ("http://127.0.0.1:notaport/desc.xml", "127.0.0.1", None),
        ("http://user@127.0.0.1:99999/", "127.0.0.1", None),
        ("", "127.0.0.1", None),
    ],
)
def test_ac4_same_host_rule(location, responder, expected, socket_guard):
    assert upnp._same_host(location, responder) == expected
    assert socket_guard.connects == [] and socket_guard.resolutions == []


# --- P4 : séquence de mappings, règle d'abandon, retrait ------------------------------

# Fenêtre de découverte minimale acceptée (C8 : au moins `upnp.MX_MIN` seconde).
FAST = {"discovery_timeout_s": upnp.MX_MIN, "request_timeout_s": 0.5}


def _open(igd: FakeIGD, cancel: threading.Event | None = None, **overrides: object):
    return upnp.open_ports(_cfg(**{**FAST, **overrides}), PORTS, igd.ssdp_target, cancel)


def _gateway(igd: FakeIGD) -> upnp.Gateway:
    gateway, failure = upnp.find_gateway(_cfg(), igd.ssdp_target)
    assert failure is None and gateway is not None
    return gateway


def _dicts(couples) -> list[dict]:
    return [{"port": port, "protocol": protocol} for port, protocol in couples]


def _assert_manual(result: dict) -> None:
    text = result["manual"]
    for port, _ in PORTS:
        assert str(port) in text
    assert "UDP" in text and "TCP" in text


@pytest.mark.parametrize("service_type", ["WANIPConnection:1", "WANPPPConnection:1"])
def test_ac1_open_ports_is_mapped_for_both_services(service_type):
    with FakeIGD(service_type=service_type) as igd:
        result, gateway, cleared = _open(igd)
        assert result["status"] == "mapped"
        assert gateway == upnp.Gateway("127.0.0.1", igd.http_port, igd.control_path, service_type)
        assert cleared == []
        assert set(igd.mappings) == set(PORTS)


def test_ac5_silent_soap_is_refused_timeout_within_bound(socket_guard):
    with FakeIGD(silent=True) as igd:
        started = time.monotonic()
        result, gateway, cleared = _open(igd)
        elapsed = time.monotonic() - started
        assert elapsed <= 3 * 0.5 + 1
        assert result["status"] == "refused"
        assert result["failure"] == "timeout"
        assert "error_code" not in result and "error_text" not in result
        assert result["still_active"] == []
        _assert_manual(result)
        assert gateway is not None and cleared == []
        # la pré-suppression expirée arrête l'ouverture : rien d'autre n'est envoyé
        assert [c.action for c in igd.calls] == ["DeletePortMapping"]
    assert set(socket_guard.connects) == {"127.0.0.1"}


def test_ac10_three_adds_with_configured_values():
    cfg = _cfg()
    with FakeIGD() as igd:
        result, _, _ = _open(igd)
        assert result["status"] == "mapped"
        assert result["ports"] == _dicts(PORTS) and result["lease_s"] == cfg["lease_s"]
        adds = igd.actions("AddPortMapping")
        assert len(adds) == 3
        assert sorted(igd.couples("AddPortMapping")) == sorted(PORTS)
        for call in adds:
            args = call.args
            assert args["NewInternalPort"] == args["NewExternalPort"]
            assert args["NewPortMappingDescription"] == cfg["description"]
            assert args["NewLeaseDuration"] == str(cfg["lease_s"])
            assert args["NewEnabled"] == "1"
            assert args["NewInternalClient"] == "127.0.0.1"
            assert args["NewRemoteHost"] == ""
        assert [c.action for c in igd.calls][-1] == "GetExternalIPAddress"
        assert len(igd.calls) == 7


def test_ac11_delete_before_add_and_714_is_ignored():
    with FakeIGD(errors={"DeletePortMapping": 714}) as igd:
        result, _, cleared = _open(igd)
        assert result["status"] == "mapped"
        assert cleared == []
        order = [(c.action, int(c.args.get("NewExternalPort", 0) or 0), c.args.get("NewProtocol")) for c in igd.calls]
        for port, protocol in PORTS:
            delete = order.index(("DeletePortMapping", port, protocol))
            add = order.index(("AddPortMapping", port, protocol))
            assert delete < add
        assert set(igd.mappings) == set(PORTS)


def test_ac12_helper_flags_forbidden_calls():
    """L'assistant d'AC12 (passé sur chaque faux IGD du fichier) signale bien les fautes."""
    from doubles.fake_igd import Call

    rcon = RUNTIME["ports"]["B"]["rcon"]
    game_a = RUNTIME["ports"]["A"]["game"]
    bad = [
        Call(1, 1, "AddPortMapping", {"NewExternalPort": str(rcon), "NewRemoteHost": ""}),
        Call(2, 2, "AddPortMapping", {"NewExternalPort": "0", "NewRemoteHost": ""}),
        Call(3, 1, "DeletePortMapping", {"NewExternalPort": str(game_a), "NewRemoteHost": "1.2.3.4"}),
    ]
    assert len(ac12_violations(bad)) == 4
    good = [Call(1, 1, "AddPortMapping", {"NewExternalPort": str(PORTS[0][0]), "NewRemoteHost": ""})]
    assert ac12_violations(good) == []


@pytest.mark.parametrize("lease", [7201, 0])
def test_ac13_lease_out_of_range_is_invalid_config(lease, socket_guard):
    with FakeIGD() as igd:
        result, gateway, cleared = _open(igd, lease_s=lease)
        assert result["status"] == "invalid_config"
        assert result["key"] == "upnp.lease_s"
        assert "error_code" not in result
        _assert_manual(result)
        assert (gateway, cleared) == (None, [])
        assert igd.calls == [] and igd.msearches == [] and igd.fetches == []
    assert socket_guard.sends == [] and socket_guard.connects == []


def test_ac13_lease_7200_is_mapped():
    with FakeIGD() as igd:
        result, _, _ = _open(igd, lease_s=7200)
        assert result["status"] == "mapped"
        assert {c.args["NewLeaseDuration"] for c in igd.actions("AddPortMapping")} == {"7200"}


@pytest.mark.parametrize(
    ("cfg", "key"),
    [
        (None, "upnp"),
        ([], "upnp"),
        ({k: v for k, v in _cfg().items() if k != "description"}, "upnp.description"),
        ({**_cfg(), "lease_s": "7200"}, "upnp.lease_s"),
        ({**_cfg(), "lease_s": True}, "upnp.lease_s"),
        ({**_cfg(), "lease_s": 3600.0}, "upnp.lease_s"),
        ({**_cfg(), "request_timeout_s": 0}, "upnp.request_timeout_s"),
        ({**_cfg(), "discovery_timeout_s": "2"}, "upnp.discovery_timeout_s"),
        ({**_cfg(), "discovery_timeout_s": float("inf")}, "upnp.discovery_timeout_s"),
        ({**_cfg(), "request_timeout_s": float("nan")}, "upnp.request_timeout_s"),
        ({**_cfg(), "max_response_bytes": -1}, "upnp.max_response_bytes"),
        ({**_cfg(), "description": ""}, "upnp.description"),
    ],
)
def test_ac13_validate_names_faulty_key(cfg, key, socket_guard):
    assert upnp.validate(cfg) == {"status": "invalid_config", "key": key}
    result, gateway, cleared = upnp.open_ports(cfg, PORTS, ("127.0.0.1", 9))
    assert result["status"] == "invalid_config" and result["key"] == key
    _assert_manual(result)
    assert (gateway, cleared) == (None, [])
    assert socket_guard.sends == [] and socket_guard.connects == []


def test_validate_accepts_shipped_config():
    assert upnp.validate(_cfg()) is None
    assert upnp.validate(_cfg(), PORTS) is None


@pytest.mark.parametrize(
    "ports",
    [PORTS[:2], [*PORTS[:2], PORTS[0]], [*PORTS[:2], (0, "TCP")], [*PORTS[:2], (PORTS[2][0], "SCTP")]],
)
def test_bad_ports_are_invalid_config_without_traffic(ports, socket_guard):
    result, gateway, _ = upnp.open_ports(_cfg(), ports, ("127.0.0.1", 9))
    assert result["status"] == "invalid_config" and result["key"] == "ports"
    assert gateway is None
    assert socket_guard.sends == [] and socket_guard.connects == []


def test_ac14_718_on_third_add_rolls_back():
    with FakeIGD(errors={("AddPortMapping", 3): 718}) as igd:
        result, gateway, cleared = _open(igd)
        assert result["status"] == "refused"
        assert result["error_code"] == 718
        assert result["error_text"] == "ConflictInMappingEntry"
        assert "failure" not in result
        assert result["still_active"] == []
        _assert_manual(result)
        deletes = igd.couples("DeletePortMapping")
        assert deletes[3:] == PORTS[:2]
        assert len(deletes) == 5
        assert "GetExternalIPAddress" not in [c.action for c in igd.calls]
        assert igd.mappings == {}
        assert gateway is not None
        assert sorted(cleared) == sorted(PORTS)


def test_cleared_is_empty_after_mapped_and_on_no_gateway():
    with FakeIGD() as igd:
        result, gateway, cleared = _open(igd)
        assert result["status"] == "mapped" and gateway is not None and cleared == []
    with FakeIGD(ssdp_reply=False) as igd:
        result, gateway, cleared = _open(igd)
        assert result["status"] == "no_gateway"
        assert (gateway, cleared) == (None, [])
        assert result["ports"] == _dicts(PORTS)
        _assert_manual(result)
        assert igd.calls == []


ADDRESS_CASES = ["10.0.0.5", "100.64.1.1", "0.0.0.0", "203.0.113.77", "192.0.2.1", "", None]


@pytest.mark.parametrize("external_ip", ADDRESS_CASES, ids=[repr(c) for c in ADDRESS_CASES])
def test_ac21_non_public_address_is_mapped_without_address(external_ip):
    with FakeIGD(external_ip=external_ip) as igd:
        result, _, _ = _open(igd)
        assert result["status"] == "mapped"
        assert result["address"] is None
        note = result["address_note"]
        assert "public address is unknown" in note or "not reachable from the Internet" in note
        if external_ip:
            assert external_ip not in note


def test_ac21_public_address_is_proposed_and_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    with FakeIGD(external_ip="5.6.7.8") as igd:
        result, _, _ = _open(igd)
    assert result["status"] == "mapped"
    assert result["address"] == "5.6.7.8" and "address_note" not in result
    assert "5.6.7.8" not in caplog.text


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("5.6.7.8", True),
        ("8.8.8.8", True),
        ("198.51.100.1", False),
        ("100.127.255.255", False),
        ("100.128.0.1", True),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("192.0.0.9", False),
        ("192.0.0.10", False),
        ("192.31.196.1", False),
        ("192.52.193.1", False),
        ("192.175.48.1", False),
        (" 5.6.7.8", False),
        ("005.6.7.8", False),
        ("5.6.7", False),
        ("2001:db8::1", False),
        ("2a00:1450::1", False),
        ("", False),
        (None, False),
    ],
)
def test_ac21_public_rule(addr, expected):
    assert upnp._public(addr) is expected


def test_ac32_silent_rollback_deletes_leave_two_still_active():
    """718 au 3ᵉ ajout, puis retrait muet : le retrait R3 s'arrête à sa première expiration."""
    # ordres 1-6 : D, A, D, A, D, A(718) ; le premier retrait est l'appel 7
    with FakeIGD(errors={("AddPortMapping", 3): 718}, silent_from=7) as igd:
        started = time.monotonic()
        result, gateway, cleared = _open(igd)
        elapsed = time.monotonic() - started
        assert result["status"] == "refused" and result["error_code"] == 718
        assert result["still_active"] == _dicts(PORTS[:2])
        # une seule suppression de retrait, la seconde n'est pas tentée
        assert igd.couples("DeletePortMapping") == [*PORTS, PORTS[0]]
        assert len(igd.calls) == 7
        assert elapsed <= 3 * 0.5 + 1
        assert gateway is not None
        assert cleared == [PORTS[2]]
        assert set(igd.mappings) == set(PORTS[:2])
        # la box répond de nouveau : l'arrêt retire les deux couples encore actifs
        igd.set_responsive()
        failures = upnp.delete_mappings(
            gateway, PORTS[:2], 0.5, max_bytes=_cfg()["max_response_bytes"]
        )
        assert failures == []
        assert igd.couples("DeletePortMapping")[-2:] == PORTS[:2]
        assert igd.mappings == {}


def test_ac33_silent_after_first_add_is_refused_timeout():
    with FakeIGD(silent_from=2) as igd:
        started = time.monotonic()
        result, gateway, cleared = _open(igd)
        elapsed = time.monotonic() - started
        assert result["status"] == "refused"
        assert result["failure"] == "timeout" and "error_code" not in result
        assert result["still_active"] == _dicts(PORTS[:1])
        assert [(c.action, c.silent) for c in igd.calls] == [
            ("DeletePortMapping", False),
            ("AddPortMapping", True),
            ("DeletePortMapping", True),
        ]
        assert igd.couples("DeletePortMapping") == [PORTS[0], PORTS[0]]
        assert elapsed <= 3 * 0.5 + 1
        assert cleared == []
        # à l'arrêt, encore muette : exactement une suppression de plus pour ce couple
        failures = upnp.delete_mappings(gateway, PORTS[:1], 0.5, max_bytes=_cfg()["max_response_bytes"])
        assert failures == PORTS[:1]
        assert igd.couples("DeletePortMapping") == [PORTS[0]] * 3


def test_unreadable_add_is_rolled_back():
    with FakeIGD(malformed={("AddPortMapping", 2)}) as igd:
        result, _, cleared = _open(igd)
        assert result["status"] == "refused" and result["failure"] == "unreadable"
        assert result["still_active"] == []
        assert igd.couples("DeletePortMapping")[2:] == PORTS[:2]
        assert igd.mappings == {}
        assert sorted(cleared) == sorted(PORTS[:2])


def test_timeout_on_external_address_is_mapped_without_address():
    with FakeIGD(silent_actions={"GetExternalIPAddress"}) as igd:
        result, _, cleared = _open(igd)
        assert result["status"] == "mapped" and result["address"] is None
        assert "public address is unknown" in result["address_note"]
        assert len(igd.actions("DeletePortMapping")) == 3
        assert set(igd.mappings) == set(PORTS) and cleared == []


def test_cancel_before_opening_sends_nothing(socket_guard):
    cancel = threading.Event()
    cancel.set()
    with FakeIGD() as igd:
        result, gateway, cleared = _open(igd, cancel)
        assert result["status"] == "no_gateway" and (gateway, cleared) == (None, [])
        assert igd.msearches == [] and igd.calls == []
    assert socket_guard.sends == []


class _CancelWhen:
    """Annulation levée dès que `condition()` est vraie : simule l'arrêt pendant l'ouverture.

    Une condition sur le trafic reçu par le faux IGD, et non un nombre de consultations : la
    découverte consulte l'annulation à chaque tour de réception (P9F2), un nombre variable de fois.
    """

    def __init__(self, condition) -> None:
        self.condition = condition

    def is_set(self) -> bool:
        return bool(self.condition())


def test_cancel_during_opening_stops_at_next_request():
    # levée après D1 et A1 reçus : D2 n'est jamais envoyé
    with FakeIGD() as igd:
        result, gateway, _ = _open(igd, _CancelWhen(lambda: len(igd.calls) >= 2))
        assert result["status"] == "refused" and result["failure"] == "cancelled"
        assert result["still_active"] == _dicts(PORTS[:1])
        assert [c.action for c in igd.calls] == ["DeletePortMapping", "AddPortMapping"]
        assert gateway is not None


def test_cancel_during_discovery_skips_description():
    """Annulation levée pendant la découverte : la description n'est pas lue (frontière R6)."""
    with FakeIGD() as igd:
        result, gateway, cleared = _open(igd, _CancelWhen(lambda: len(igd.msearches) >= 1))
        assert result["status"] == "no_gateway" and result["reason"] == "cancelled"
        assert (gateway, cleared) == (None, [])
        assert len(igd.msearches) == 1
        assert igd.fetches == [] and igd.calls == []
        _assert_manual(result)


def test_infinite_discovery_timeout_is_invalid_config_not_overflow(socket_guard):
    # `1e999` dans runtime.json se lit `inf` : sans garde, `_msearch` lèverait OverflowError
    cfg = {**_cfg(), "discovery_timeout_s": json.loads("1e999")}
    assert cfg["discovery_timeout_s"] == float("inf")
    result, gateway, cleared = upnp.open_ports(cfg, PORTS, ("127.0.0.1", 9))
    assert result["status"] == "invalid_config" and result["key"] == "upnp.discovery_timeout_s"
    assert (gateway, cleared) == (None, [])
    assert socket_guard.sends == []


def test_delete_mappings_mute_gateway_tries_every_mapping():
    with FakeIGD() as igd:
        gateway = _gateway(igd)
        igd.configure(silent=True)
        started = time.monotonic()
        failures = upnp.delete_mappings(gateway, PORTS, 0.5, max_bytes=_cfg()["max_response_bytes"])
        elapsed = time.monotonic() - started
        assert failures == PORTS
        assert igd.couples("DeletePortMapping") == PORTS
        assert elapsed <= 3 * 0.5 + 1
        assert len(igd.msearches) == 1  # celle de `_gateway` seulement
        assert igd.fetches == ["/desc.xml"]


def test_delete_mappings_continues_after_first_timeout():
    """Finding P2 : une expiration sur le premier couple n'empêche pas les suivants."""
    with FakeIGD(silent_on={("DeletePortMapping", 1)}) as igd:
        gateway = _gateway(igd)
        with igd.lock:
            for port, protocol in PORTS:
                igd.mappings[(port, protocol)] = {}
        msearches, fetches = len(igd.msearches), list(igd.fetches)
        started = time.monotonic()
        failures = upnp.delete_mappings(gateway, PORTS, 0.5, max_bytes=_cfg()["max_response_bytes"])
        elapsed = time.monotonic() - started
        assert failures == PORTS[:1]
        assert igd.couples("DeletePortMapping") == PORTS
        assert set(igd.mappings) == {PORTS[0]}
        assert elapsed <= 3 * 0.5 + 1
        # ni découverte ni description pendant le retrait de l'arrêt
        assert len(igd.msearches) == msearches and igd.fetches == fetches


def test_delete_mappings_counts_errors_but_not_714():
    with FakeIGD(errors={("DeletePortMapping", 1): 714, ("DeletePortMapping", 2): 501}) as igd:
        gateway = _gateway(igd)
        failures = upnp.delete_mappings(gateway, PORTS, 0.5, max_bytes=_cfg()["max_response_bytes"])
        assert failures == [PORTS[1]]
        assert len(igd.actions("DeletePortMapping")) == 3


def test_delete_mappings_never_raises_on_closed_gateway():
    with FakeIGD() as igd:
        gateway = _gateway(igd)
    failures = upnp.delete_mappings(gateway, PORTS, 0.5, max_bytes=_cfg()["max_response_bytes"])
    assert failures == PORTS


def test_manual_instruction_names_ports_and_protocols():
    text = upnp.manual_instruction(PORTS)
    for port, protocol in PORTS:
        assert f"{port}/{protocol}" in text
    assert text.isascii()


def test_ac25_no_config_literal_in_upnp_module():
    tree = ast.parse(UPNP_PATH.read_text(encoding="utf-8"))
    cfg = _cfg()
    ports = {*RUNTIME["ports"]["A"].values(), *RUNTIME["ports"]["B"].values(), RUNTIME["share_port"]}
    constants = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    ints = [n.value for n in constants if isinstance(n.value, int) and not isinstance(n.value, bool)]
    assert not ports & set(ints)
    assert cfg["lease_s"] not in ints
    assert cfg["description"] not in [n.value for n in constants]
    ssdp = [n for n in constants if n.value in (upnp.SSDP_PORT, upnp.SSDP_GROUP) and not isinstance(n.value, bool)]
    assert len(ssdp) == 2
    assigned = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if node.value.value in (upnp.SSDP_PORT, upnp.SSDP_GROUP):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                assigned.setdefault(node.value.value, []).extend(names)
    assert assigned == {1900: ["SSDP_PORT"], "239.255.255.250": ["SSDP_GROUP"]}


# -- Moteur (P5) : action, état, arrêt ----------------------------------------------------
# Le banc de `tests/test_share.py` : vrai `Engine` réel sur les doubles de serveurs, `local_ip`
# remplacé (`detect`), et le faux IGD visé par `upnp_target`. Le garde de socket reste actif.

import test_snapshots  # noqa: E402
from test_contract import Lobby  # noqa: E402
from test_match import Duel  # noqa: E402
from test_modes import FastClock, settle, wait_phase  # noqa: E402
from test_servers import stopped_for_good  # noqa: E402
from test_share import LAN_ADDRESS, detect  # noqa: E402
from test_snapshots import PLAYER_PASSWORD, engine_rig, free_port  # noqa: E402,F401
from service.engine import Engine  # noqa: E402

PUBLIC = "5.6.7.8"
ENTERED = "198.51.100.9"
ENGINE_WAIT_S = 10
SHUTDOWN_BOUND_S = 3 * FAST["request_timeout_s"] + 1


@pytest.fixture
def igd():
    with FakeIGD() as fake:
        yield fake


@pytest.fixture
def upnp_rig(engine_rig, monkeypatch, igd):
    """Banc moteur dont chaque `Engine` vise le faux IGD ; délais UPnP courts."""
    rig = engine_rig
    rig.runtime["share_port"] = free_port(socket.SOCK_STREAM)
    rig.runtime["upnp"] = {**rig.runtime["upnp"], **FAST}
    detect(monkeypatch, LAN_ADDRESS)
    monkeypatch.setattr(test_snapshots, "Engine", _aimed(igd))
    yield rig
    if rig.engine is not None:
        rig.engine.shutdown()


def _aimed(igd: FakeIGD):
    def build(*args, **kwargs) -> Engine:
        return Engine(*args, upnp_target=igd.ssdp_target, **kwargs)

    return build


def _engine_ports(rig) -> list[tuple[int, str]]:
    guest = rig.runtime["ports"]["B"]
    return [(guest["game"], "UDP"), (guest["udp"], "UDP"), (rig.runtime["share_port"], "TCP")]


def _wait_opening(engine: Engine, timeout_s: float = ENGINE_WAIT_S) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        opening = engine.snapshot()["ports_opening"]
        if opening["status"] != "in_progress":
            return opening
        time.sleep(0.02)
    raise AssertionError(f"ouverture toujours en cours après {timeout_s} s")


def _click(engine: Engine) -> tuple[dict, float, float]:
    """Clic d'ouverture, attendu jusqu'au résultat : `(résultat, heure avant, heure après)`."""
    before = engine._clock()
    ok, message = engine.open_ports()
    after = engine._clock()
    assert ok, message
    return _wait_opening(engine), before, after


def _assert_engine_manual(result: dict, ports) -> None:
    text = result["manual"]
    for port, _ in ports:
        assert str(port) in text
    assert "UDP" in text and "TCP" in text


def _traffic(igd: FakeIGD, guard) -> tuple[int, int, int, int]:
    return len(igd.msearches), len(igd.calls), len(guard.sends), len(guard.connects)


def _ssdp_sends(guard) -> list[object]:
    return [
        t for t in guard.send_targets
        if isinstance(t, tuple) and (t[1] == upnp.SSDP_PORT or ipaddress.ip_address(t[0]).is_multicast)
    ]


@pytest.fixture
def mock_engine(runtime, tmp_path, monkeypatch, igd):
    detect(monkeypatch, LAN_ADDRESS)
    engine = Engine(
        "mock",
        runtime=runtime,
        clock=FastClock(),
        matches_dir=tmp_path / "matches",
        upnp_target=igd.ssdp_target,
    )
    yield engine
    engine.shutdown()


# -- AC6 / AC8 / AC13 : refus du moteur, sans trafic ----------------------------------------
def test_ac6_engine_refuses_outside_a_real_lobby(upnp_rig, igd, socket_guard):
    engine = upnp_rig.build()
    ok, message = engine.open_ports()
    assert not ok and "lobby" in message and "idle" in message
    assert engine.snapshot()["ports_opening"]["status"] == "idle"

    duel = Duel(upnp_rig)
    duel.start_match()
    for phase in ("running", "over"):
        if phase == "over":
            duel.at_match(duel.state()["config"]["duration_s"])
            duel.finish()
        before = _traffic(igd, socket_guard)
        ok, message = duel.engine.open_ports()
        after = _traffic(igd, socket_guard)
        assert not ok and message == f"only in the lobby, phase {phase}"
        # les connexions en boucle locale du rcon des doubles ne passent pas par l'IGD
        assert after[:3] == before[:3]
    assert igd.msearches == [] and igd.calls == [] and igd.fetches == []
    assert duel.state()["ports_opening"]["status"] == "idle"


def test_ac6_engine_refuses_in_simulated_mode(mock_engine, igd, socket_guard):
    assert mock_engine.action("start", {"preset": "demo"})[0]
    wait_phase(mock_engine, "lobby")
    ok, message = mock_engine.open_ports()
    assert not ok and message == "not available in simulated mode"
    assert igd.msearches == [] and igd.calls == []
    assert socket_guard.sends == [] and socket_guard.connects == []
    assert mock_engine.snapshot()["ports_opening"]["status"] == "idle"
    assert mock_engine.action("simulate_join")[0]
    wait_phase(mock_engine, "over")
    settle(mock_engine)


def test_ac6_engine_accepts_in_a_real_lobby(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    ok, message = lobby.engine.open_ports()
    assert ok, message
    assert _wait_opening(lobby.engine)["status"] == "mapped"
    assert igd.calls


def test_ac8_second_click_in_progress_is_refused(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    igd.configure(delay_s=0.2)
    assert lobby.engine.open_ports()[0]
    assert lobby.state()["ports_opening"]["status"] == "in_progress"
    ok, message = lobby.engine.open_ports()
    assert not ok and message == "an opening is already in progress"
    assert _wait_opening(lobby.engine)["status"] == "mapped"
    assert len(igd.msearches) == 1
    assert len(igd.actions("DeletePortMapping")) <= 3
    assert len(igd.actions("AddPortMapping")) <= 3
    assert len(igd.actions("GetExternalIPAddress")) <= 1
    assert len(igd.calls) <= 7


@pytest.mark.parametrize("lease", [7201, 0])
def test_ac13_engine_invalid_lease_is_synchronous_and_silent(upnp_rig, igd, socket_guard, lease):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    engine.runtime = {**engine.runtime, "upnp": {**engine.runtime["upnp"], "lease_s": lease}}
    before = _traffic(igd, socket_guard)
    ok, message = engine.open_ports()
    assert not ok and "upnp.lease_s" in message
    assert engine._ports_thread is None
    opening = lobby.state()["ports_opening"]
    assert opening["status"] == "invalid_config" and opening["key"] == "upnp.lease_s"
    assert "error_code" not in opening
    _assert_engine_manual(opening, _engine_ports(upnp_rig))
    assert _traffic(igd, socket_guard) == before
    assert igd.msearches == [] and igd.calls == []


# -- AC7 : aucun trafic sans clic ----------------------------------------------------------
def test_ac7_start_lobby_rematch_lobby_sends_nothing(upnp_rig, igd, socket_guard):
    duel = Duel(upnp_rig)
    engine = duel.engine
    duel.start_match()
    duel.at_match(duel.state()["config"]["duration_s"])
    duel.finish()
    for mod in duel.mods.values():
        mod.restart(1)
        mod.seq = 1
    assert engine.action("rematch")[0]
    state = upnp_rig.wait("lobby", "error")
    assert state["phase"] == "lobby", state["error"]
    assert state["ports_opening"]["status"] == "idle"
    assert igd.msearches == [] and igd.calls == [] and igd.fetches == []
    assert _ssdp_sends(socket_guard) == []


# -- AC15 / AC16 / AC17 : état rendu -------------------------------------------------------
def test_ac15_mapped_state_carries_ports_and_expiry(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    opening, before, after = _click(lobby.engine)
    lease = upnp_rig.runtime["upnp"]["lease_s"]
    ports = _engine_ports(upnp_rig)
    assert opening["status"] == "mapped"
    assert opening["ports"] == _dicts(ports)
    assert opening["lease_s"] == lease
    assert before + lease - 2 <= opening["expires_at"] <= after + lease + 2
    assert set(igd.mappings) == set(ports)


def test_ac15_no_gateway_state_carries_manual_text(upnp_rig, igd):
    igd.configure(ssdp_reply=False)
    lobby = Lobby(upnp_rig, "demo")
    opening, _, _ = _click(lobby.engine)
    assert opening["status"] == "no_gateway"
    _assert_engine_manual(opening, _engine_ports(upnp_rig))
    assert "expires_at" not in opening
    assert igd.calls == []


def test_ac15_refused_718_state_carries_code_and_manual_text(upnp_rig, igd):
    igd.configure(errors={("AddPortMapping", 3): 718})
    lobby = Lobby(upnp_rig, "demo")
    opening, _, _ = _click(lobby.engine)
    assert opening["status"] == "refused" and opening["error_code"] == 718
    assert opening["still_active"] == []
    _assert_engine_manual(opening, _engine_ports(upnp_rig))
    assert igd.mappings == {}


def test_ac16_slow_gateway_leaves_state_and_duel_responsive(upnp_rig, igd):
    rig = upnp_rig
    rig.runtime["upnp"] = {**rig.runtime["upnp"], "request_timeout_s": 3}
    duel = Duel(rig)
    igd.configure(delay_s=2)
    assert duel.engine.open_ports()[0]
    started = time.monotonic()
    state = duel.engine.snapshot()
    assert time.monotonic() - started < 0.5
    assert state["ports_opening"]["status"] == "in_progress"
    # le duel se lance pendant l'ouverture : le verrou n'est jamais tenu pendant une requête
    duel.start_match()
    started = time.monotonic()
    state = duel.engine.snapshot()
    assert time.monotonic() - started < 0.5
    assert state["phase"] == "running"
    assert state["ports_opening"]["status"] == "in_progress"
    igd.configure(delay_s=0)
    assert _wait_opening(duel.engine)["status"] == "mapped"


@pytest.mark.parametrize("failure", ["no_gateway", "refused"])
def test_ac17_failed_opening_leaves_invite_and_duel_untouched(upnp_rig, igd, failure):
    if failure == "no_gateway":
        igd.configure(ssdp_reply=False)
    else:
        igd.configure(errors={("AddPortMapping", 3): 718})
    duel = Duel(upnp_rig)
    invite = duel.state()["invite"]
    opening, _, _ = _click(duel.engine)
    assert opening["status"] == failure
    assert duel.state()["invite"] == invite
    duel.start_match()
    assert duel.state()["phase"] == "running"


# -- AC18 / AC19 / AC20 : état neutre et adresse proposée ----------------------------------
def test_ac18_state_without_click_only_adds_neutral_opening(upnp_rig, igd):
    engine = upnp_rig.build()
    neutral = {
        "status": "idle",
        "ports": _dicts(_engine_ports(upnp_rig)),
        "lease_s": upnp_rig.runtime["upnp"]["lease_s"],
    }
    state = engine.snapshot()
    assert state["ports_opening"] == neutral
    lobby = Lobby(upnp_rig, "demo")
    state = lobby.state()
    assert state["ports_opening"] == neutral
    # `invite` est celui de la phase 2 : celui du moteur, sans rien d'UPnP
    with lobby.engine.lock:
        assert state["invite"] == lobby.engine.state.invite
    assert not any("port" in key and "open" in key for key in state["invite"])
    assert igd.calls == [] and igd.msearches == []


def test_ac19_public_address_is_proposed_not_applied(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    invite = lobby.state()["invite"]
    opening, _, _ = _click(lobby.engine)
    assert opening["status"] == "mapped" and opening["address"] == PUBLIC
    state = lobby.state()
    assert state["invite"] == invite
    assert state["invite"]["address"] == LAN_ADDRESS
    assert PUBLIC not in json.dumps(state["invite"])
    assert lobby.engine.set_invite_address(PUBLIC)[0]
    assert PUBLIC in lobby.state()["invite"]["link"]


def test_ac20_public_address_never_logged_nor_journaled(upnp_rig, igd, socket_guard, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    rig = upnp_rig
    duel = Duel(rig)
    opening, _, _ = _click(duel.engine)
    assert opening["address"] == PUBLIC
    assert duel.engine.set_invite_address(PUBLIC)[0]
    duel.start_match()
    duel.at_match(duel.state()["config"]["duration_s"])
    duel.finish()
    logs = json.dumps(duel.state()["logs"])
    assert duel.engine.shutdown()
    logs += json.dumps(duel.engine.snapshot()["logs"])
    assert PUBLIC not in logs
    for path in rig.matches.rglob("*"):
        if path.is_file():
            assert PUBLIC.encode() not in path.read_bytes(), path
    assert PUBLIC not in caplog.text
    out, err = capsys.readouterr()
    assert PUBLIC not in out and PUBLIC not in err
    assert PUBLIC not in socket_guard.connects and PUBLIC not in socket_guard.sends


# -- AC22 / AC23 / AC24 : arrêt ------------------------------------------------------------
def _retained(engine: Engine) -> list[tuple[int, str]]:
    """Couples retenus pour l'arrêt, toutes box confondues, dans leur ordre d'insertion."""
    return [couple for _, couple in engine._upnp_retained]


def _deletes_after(igd: FakeIGD, count: int) -> list[tuple[int, str]]:
    return igd.couples("DeletePortMapping")[count:]


def test_ac22_shutdown_deletes_the_three_mappings(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    assert _click(lobby.engine)[0]["status"] == "mapped"
    seen = len(igd.actions("DeletePortMapping"))
    msearches = len(igd.msearches)
    assert lobby.engine.shutdown()
    assert sorted(_deletes_after(igd, seen)) == sorted(_engine_ports(upnp_rig))
    assert igd.mappings == {}
    assert len(igd.msearches) == msearches
    assert lobby.engine._upnp_retained == {}


def test_ac23_mute_gateway_at_shutdown_is_bounded(upnp_rig, igd, caplog):
    caplog.set_level(logging.DEBUG)
    lobby = Lobby(upnp_rig, "demo")
    assert _click(lobby.engine)[0]["status"] == "mapped"
    seen = len(igd.actions("DeletePortMapping"))
    igd.configure(silent=True)
    started = time.monotonic()
    lobby.engine.shutdown()
    elapsed = time.monotonic() - started
    assert elapsed <= SHUTDOWN_BOUND_S
    assert stopped_for_good(upnp_rig)
    assert len(_deletes_after(igd, seen)) == 3
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("could not remove port mappings" in w for w in warnings)
    assert PUBLIC not in caplog.text
    assert PUBLIC not in json.dumps(lobby.engine.snapshot()["logs"])


@pytest.mark.parametrize("case", ["no_opening", "no_gateway", "invalid_config", "refused_empty"])
def test_ac24_nothing_active_sends_nothing_at_shutdown(upnp_rig, igd, socket_guard, case):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    if case == "no_gateway":
        igd.configure(ssdp_reply=False)
        assert _click(engine)[0]["status"] == "no_gateway"
    elif case == "invalid_config":
        engine.runtime = {**engine.runtime, "upnp": {**engine.runtime["upnp"], "lease_s": 0}}
        assert not engine.open_ports()[0]
    elif case == "refused_empty":
        igd.configure(errors={("AddPortMapping", 3): 718})
        opening = _click(engine)[0]
        assert opening["status"] == "refused" and opening["still_active"] == []
    before = _traffic(igd, socket_guard)
    ssdp = _ssdp_sends(socket_guard)
    assert engine.shutdown()
    assert _traffic(igd, socket_guard)[:3] == before[:3]
    assert _ssdp_sends(socket_guard) == ssdp
    assert engine._upnp_retained == {}


# -- AC25 : valeurs de la copie de runtime ---------------------------------------------------
def test_ac25_runtime_copy_changes_the_arguments(upnp_rig, igd):
    rig = upnp_rig
    rig.runtime["share_port"] = free_port(socket.SOCK_STREAM)
    rig.runtime["upnp"] = {**rig.runtime["upnp"], "lease_s": 3600, "description": "Other Duel"}
    lobby = Lobby(rig, "demo")
    assert _click(lobby.engine)[0]["status"] == "mapped"
    ports = _engine_ports(rig)
    assert ports[2][0] == rig.runtime["share_port"]
    assert sorted(igd.couples("AddPortMapping")) == sorted(ports)
    for call in igd.actions("AddPortMapping"):
        assert call.args["NewLeaseDuration"] == "3600"
        assert call.args["NewPortMappingDescription"] == "Other Duel"


# -- AC32 / AC33 : bout en bout jusqu'à l'arrêt --------------------------------------------
def test_ac32_engine_retains_two_and_deletes_them_at_shutdown(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    ports = _engine_ports(upnp_rig)
    igd.configure(errors={("AddPortMapping", 3): 718}, silent_from=7)
    opening = _click(lobby.engine)[0]
    assert opening["status"] == "refused" and opening["error_code"] == 718
    assert opening["still_active"] == _dicts(ports[:2])
    assert set(_retained(lobby.engine)) == set(ports[:2])
    igd.set_responsive()
    seen = len(igd.actions("DeletePortMapping"))
    assert lobby.engine.shutdown()
    assert sorted(_deletes_after(igd, seen)) == sorted(ports[:2])
    assert igd.mappings == {}


def test_ac33_engine_retries_the_possibly_placed_mapping_at_shutdown(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    ports = _engine_ports(upnp_rig)
    igd.configure(silent_from=2)
    opening = _click(lobby.engine)[0]
    assert opening["status"] == "refused" and opening["failure"] == "timeout"
    assert opening["still_active"] == _dicts(ports[:1])
    assert igd.couples("DeletePortMapping") == [ports[0], ports[0]]
    started = time.monotonic()
    lobby.engine.shutdown()
    assert time.monotonic() - started <= SHUTDOWN_BOUND_S
    assert igd.couples("DeletePortMapping") == [ports[0]] * 3


# -- Finding P1 : rétention d'une ouverture à l'autre ---------------------------------------
def test_retention_survives_a_later_no_gateway(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    assert _click(lobby.engine)[0]["status"] == "mapped"
    igd.configure(ssdp_reply=False)
    assert _click(lobby.engine)[0]["status"] == "no_gateway"
    seen = len(igd.actions("DeletePortMapping"))
    assert lobby.engine.shutdown()
    deletes = _deletes_after(igd, seen)
    assert len(deletes) == 3 and sorted(deletes) == sorted(_engine_ports(upnp_rig))
    assert igd.mappings == {}


def test_retention_survives_a_later_invalid_config(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    assert _click(engine)[0]["status"] == "mapped"
    engine.runtime = {**engine.runtime, "upnp": {**engine.runtime["upnp"], "lease_s": 0}}
    assert not engine.open_ports()[0]
    assert lobby.state()["ports_opening"]["status"] == "invalid_config"
    seen = len(igd.actions("DeletePortMapping"))
    assert engine.shutdown()
    deletes = _deletes_after(igd, seen)
    assert len(deletes) == 3 and sorted(deletes) == sorted(_engine_ports(upnp_rig))
    assert igd.mappings == {}


def test_retention_is_cleared_by_a_confirmed_rollback(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    assert _click(lobby.engine)[0]["status"] == "mapped"
    igd.configure(errors={("AddPortMapping", 6): 718})
    opening = _click(lobby.engine)[0]
    assert opening["status"] == "refused" and opening["error_code"] == 718
    assert opening["still_active"] == []
    assert lobby.engine._upnp_retained == {}
    seen = len(igd.actions("DeletePortMapping"))
    assert lobby.engine.shutdown()
    assert _deletes_after(igd, seen) == []
    assert igd.mappings == {}


# -- Finding P2 : une suppression muette n'empêche pas les suivantes -------------------------
def test_shutdown_first_delete_silent_still_tries_the_others(upnp_rig, igd, caplog):
    caplog.set_level(logging.DEBUG)
    lobby = Lobby(upnp_rig, "demo")
    assert _click(lobby.engine)[0]["status"] == "mapped"
    retained = _retained(lobby.engine)
    seen = len(igd.actions("DeletePortMapping"))
    igd.configure(silent_on={("DeletePortMapping", seen + 1)})
    started = time.monotonic()
    lobby.engine.shutdown()
    assert time.monotonic() - started <= SHUTDOWN_BOUND_S
    deletes = _deletes_after(igd, seen)
    assert deletes == retained
    assert set(igd.mappings) == {retained[0]}
    first = f"{retained[0][0]}/{retained[0][1]}"
    warnings = [r.getMessage() for r in caplog.records if "could not remove port mappings" in r.getMessage()]
    assert len(warnings) == 1 and first in warnings[0]
    for port, protocol in retained[1:]:
        assert f"{port}/{protocol}" not in warnings[0]
    assert PUBLIC not in caplog.text


# -- P6 : la route `POST /api/ports/open` et l'arrêt du service ----------------------------
# AC6 par HTTP sur le vrai `Engine` servi par `make_server` ; AC22, seconde variante, sur
# `service.main` lancé par `tests/doubles/run_service.py` et visant le faux IGD du test.

import os  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from urllib.request import Request, urlopen  # noqa: E402

from service.main import STOP_SIGNALS  # noqa: E402
from test_servers import PROCESS_TIMEOUT_S, REPO_ROOT, RUN_SERVICE, SERVICE_URL_PREFIX  # noqa: E402
from test_share import api_port, request  # noqa: E402,F401

PORTS_ROUTE = "/api/ports/open"
if sys.platform == "win32":
    # `SIGBREAK` de `STOP_SIGNALS` : seul `CTRL_BREAK_EVENT` atteint un groupe de processus à part.
    STOP_SIGNAL = signal.CTRL_BREAK_EVENT
    SERVICE_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    STOP_SIGNAL = STOP_SIGNALS[-1]
    SERVICE_FLAGS = 0


def _open_by_http(port: int) -> tuple[int, dict]:
    status, _, body = request(port, "POST", PORTS_ROUTE, {})
    return status, json.loads(body)


def test_ac6_http_accepts_in_a_real_lobby(upnp_rig, igd, api_port):
    lobby = Lobby(upnp_rig, "demo")
    port = api_port(lobby.engine)
    status, answer = _open_by_http(port)
    assert status == 200 and answer["ok"] is True, answer
    assert answer["state"]["ports_opening"]["status"] in ("in_progress", "mapped")
    assert _wait_opening(lobby.engine)["status"] == "mapped"
    assert igd.msearches and igd.calls
    assert sorted(igd.couples("AddPortMapping")) == sorted(_engine_ports(upnp_rig))


def test_ac6_http_refuses_outside_a_real_lobby(upnp_rig, igd, api_port, socket_guard):
    engine = upnp_rig.build()
    status, answer = _open_by_http(api_port(engine))
    assert status == 409 and answer["ok"] is False
    assert answer["message"] == "only in the lobby, phase idle"
    assert answer["state"]["ports_opening"]["status"] == "idle"

    duel = Duel(upnp_rig)
    port = api_port(duel.engine)
    duel.start_match()
    for phase in ("running", "over"):
        if phase == "over":
            duel.at_match(duel.state()["config"]["duration_s"])
            duel.finish()
        status, answer = _open_by_http(port)
        assert status == 409 and answer["message"] == f"only in the lobby, phase {phase}"
    assert igd.msearches == [] and igd.calls == [] and igd.fetches == []
    assert _ssdp_sends(socket_guard) == []
    assert duel.state()["ports_opening"]["status"] == "idle"


def test_ac6_http_refuses_in_simulated_mode(mock_engine, igd, api_port, socket_guard):
    port = api_port(mock_engine)
    assert mock_engine.action("start", {"preset": "demo"})[0]
    wait_phase(mock_engine, "lobby")
    status, answer = _open_by_http(port)
    assert status == 409 and answer["message"] == "not available in simulated mode"
    assert igd.msearches == [] and igd.calls == []
    assert _ssdp_sends(socket_guard) == []
    assert mock_engine.action("simulate_join")[0]
    wait_phase(mock_engine, "over")
    settle(mock_engine)


class UpnpService:
    """`service.main` dans son processus, sur le double du banc, visant le faux IGD."""

    def __init__(self, rig, igd: FakeIGD) -> None:
        config = rig.pz_dir.parent / "service.json"
        config.write_text(
            json.dumps(
                {
                    "zomboid_dir": str(rig.zomboid),
                    "matches_dir": str(rig.matches),
                    "runtime": rig.runtime,
                    "upnp_target": list(igd.ssdp_target),
                    # Le banc remplace `local_ip` ; l'enfant n'en hérite pas, on la fixe ici.
                    "local_ip": LAN_ADDRESS,
                }
            ),
            encoding="utf-8",
        )
        self.process = subprocess.Popen(
            [
                sys.executable, str(RUN_SERVICE), str(config),
                "--mode", "real", "--no-browser", "--port", "0", "--pz-dir", str(rig.pz_dir),
            ],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            creationflags=SERVICE_FLAGS,
        )  # fmt: skip
        self.url = self._read_url()

    def _read_url(self) -> str:
        for line in self.process.stdout:
            if SERVICE_URL_PREFIX in line:
                return line.split(SERVICE_URL_PREFIX)[1].strip()
        raise AssertionError("le service est sorti sans publier son URL")

    def state(self) -> dict:
        with urlopen(f"{self.url}api/state", timeout=PROCESS_TIMEOUT_S) as response:
            return json.load(response)

    def post(self, path: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        req = Request(f"{self.url}{path.lstrip('/')}", json.dumps(payload).encode(), headers)
        with urlopen(req, timeout=PROCESS_TIMEOUT_S) as response:
            return json.load(response)

    def until(self, condition) -> dict:
        deadline = time.monotonic() + ENGINE_WAIT_S
        while time.monotonic() < deadline:
            state = self.state()
            if condition(state):
                return state
            time.sleep(0.05)
        raise AssertionError(f"état jamais atteint : {self.state()}")

    def kill(self) -> None:
        """Filet d'un test en échec : le service ne survit pas au test."""
        if self.process.poll() is None:
            self.process.kill()
        self.process.communicate(timeout=PROCESS_TIMEOUT_S)


def _quit_by_http(service: UpnpService) -> None:
    assert service.post("/api/quit", {})["ok"] is True


def _stop_signal(service: UpnpService) -> None:
    service.process.send_signal(STOP_SIGNAL)


@pytest.mark.parametrize("stop", [_quit_by_http, _stop_signal], ids=["api-quit", "stop-signal"])
def test_ac22_service_stop_deletes_the_three_mappings(upnp_rig, igd, stop):
    upnp_rig.build()
    service = UpnpService(upnp_rig, igd)
    try:
        service.post("/api/action", {"action": "start", "preset": "demo", "password": PLAYER_PASSWORD})
        state = service.until(lambda s: s["phase"] in ("lobby", "error"))
        assert state["phase"] == "lobby", state["error"]
        assert state["invite"]["ip"] == LAN_ADDRESS
        answer = service.post(PORTS_ROUTE, {})
        assert answer["ok"] is True, answer
        opened = service.until(lambda s: s["ports_opening"]["status"] != "in_progress")
        assert opened["ports_opening"]["status"] == "mapped", opened["ports_opening"]
        assert len(igd.mappings) == 3
        seen = len(igd.actions("DeletePortMapping"))

        stop(service)
        service.process.communicate(timeout=PROCESS_TIMEOUT_S)

        assert service.process.returncode == 0
        assert sorted(_deletes_after(igd, seen)) == sorted(_engine_ports(upnp_rig))
        assert igd.mappings == {}
    finally:
        service.kill()


# -- P9F1 : constats de la revue de branche ------------------------------------------------
# Chaque test tombe si la correction de son constat est retirée (mutation vérifiée).

NOT_IGD = f"{DEVICE}MediaServer:1"


def _wait_for(condition, timeout_s: float = ENGINE_WAIT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition toujours fausse après {timeout_s} s")
        time.sleep(0.02)


# C1 : l'arrêt et la fusion d'une ouverture tardive ne perdent aucun mapping.
def test_c1_opening_merged_after_the_shutdown_copy_removes_its_own_mappings(upnp_rig, igd, monkeypatch):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    ports = _engine_ports(upnp_rig)
    real_open = upnp.open_ports
    gate = threading.Event()

    def late_open(cfg, couples, target, cancel):
        # Le réseau est fait, mais la fusion n'arrive qu'après la copie de l'arrêt.
        outcome = real_open(cfg, couples, target, None)
        gate.wait(ENGINE_WAIT_S)
        return outcome

    monkeypatch.setattr(upnp, "open_ports", late_open)
    assert engine.open_ports()[0]
    _wait_for(lambda: len(igd.actions("GetExternalIPAddress")) == 1)
    assert set(igd.mappings) == set(ports)
    seen = len(igd.actions("DeletePortMapping"))
    stopper = threading.Thread(target=engine.shutdown, daemon=True)
    stopper.start()
    _wait_for(lambda: engine._ports_released)
    assert engine._upnp_retained == {}
    gate.set()
    stopper.join(ENGINE_WAIT_S)
    assert not stopper.is_alive()
    assert sorted(_deletes_after(igd, seen)) == sorted(ports)
    assert igd.mappings == {}
    assert engine._upnp_retained == {}


# C2 : une ouverture sur une autre box ne fait pas oublier les mappings de la première.
def test_c2_retention_is_indexed_by_gateway_and_couple(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    ports = _engine_ports(upnp_rig)
    assert _click(engine)[0]["status"] == "mapped"
    with FakeIGD() as other:
        engine._upnp_target = other.ssdp_target
        assert _click(engine)[0]["status"] == "mapped"
        assert set(igd.mappings) == set(ports) and set(other.mappings) == set(ports)
        assert len(engine._upnp_retained) == 2 * len(ports)
        assert engine.shutdown()
        assert igd.mappings == {}
        assert other.mappings == {}
    assert engine._upnp_retained == {}


# C3 : identité IGD vérifiée avant toute écriture SOAP.
@pytest.mark.parametrize("options", [{"device_type": NOT_IGD}, {"wan_in_root": True}])
def test_c3_wan_service_outside_an_igd_tree_is_no_gateway(options, socket_guard):
    with FakeIGD(**options) as igd:
        result, gateway, cleared = _open(igd)
        assert result["status"] == "no_gateway" and result["reason"] == "no_service"
        assert (gateway, cleared) == (None, [])
        assert igd.fetches == ["/desc.xml"]
        assert igd.calls == []
        _assert_manual(result)


def test_c3_find_service_requires_the_igd_tree():
    assert upnp._find_service(igd_description().encode()) == ("WANIPConnection:1", "/ctl")
    assert upnp._find_service(igd_description(root_type="InternetGatewayDevice:2").encode()) is not None
    assert upnp._find_service(igd_description(root_type="MediaServer:1").encode()) is None
    flat = (
        f'<root><device><deviceType>{upnp.SEARCH_TARGET}</deviceType><serviceList><service>'
        "<serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>"
        "<controlURL>/ctl</controlURL></service></serviceList></device></root>"
    )
    assert upnp._find_service(flat.encode()) is None


# C4 : filtrage sur `ST`, candidats suivants, budget global.
def test_c4_non_igd_responder_first_does_not_stop_the_search(socket_guard):
    with FakeIGD(device_type=NOT_IGD) as decoy, FakeIGD() as igd:
        igd.configure(ssdp_before=[decoy.ssdp_answer()])
        result, gateway, _ = _open(igd)
        assert result["status"] == "mapped"
        assert gateway is not None and gateway.port == igd.http_port
        assert decoy.fetches == ["/desc.xml"] and decoy.calls == []
        assert len(igd.actions("AddPortMapping")) == 3


def test_c4_reply_with_a_foreign_st_is_not_a_candidate():
    with FakeIGD() as decoy, FakeIGD() as igd:
        igd.configure(ssdp_before=[decoy.ssdp_answer(f"{DEVICE}MediaRenderer:1")])
        result, gateway, _ = _open(igd)
        assert result["status"] == "mapped"
        assert gateway is not None and gateway.port == igd.http_port
        assert decoy.fetches == [] and decoy.calls == []


def test_c4_location_without_igd_st_is_ignored():
    igd = FakeIGD(location="http://127.0.0.1:1/desc.xml")
    assert upnp._location(igd.ssdp_answer()) == "http://127.0.0.1:1/desc.xml"
    assert upnp._location(igd.ssdp_answer(f"{DEVICE}InternetGatewayDevice:2")) is not None
    assert upnp._location(igd.ssdp_answer("upnp:rootdevice")) is None
    assert upnp._location(igd.ssdp_answer(f"{DEVICE}MediaRenderer:1")) is None
    assert upnp._location(igd.ssdp_answer().replace(b"ST: ", b"X-ST: ")) is None


def test_c4_candidates_are_tried_within_a_global_budget():
    decoys = [FakeIGD(delay_s=5).start() for _ in range(6)]
    try:
        with FakeIGD(ssdp_reply=False, ssdp_before=[d.ssdp_answer() for d in decoys]) as igd:
            cfg = _cfg(**FAST)
            started = time.monotonic()
            gateway, failure = upnp.find_gateway(cfg, igd.ssdp_target)
            elapsed = time.monotonic() - started
    finally:
        for decoy in decoys:
            decoy.close()
    assert gateway is None and failure == {"status": "no_gateway", "reason": "timeout"}
    assert elapsed <= cfg["discovery_timeout_s"] + cfg["request_timeout_s"] + 0.4
    tried = sum(len(d.fetches) for d in decoys)
    # la réception tient toute la fenêtre, puis les descriptions se partagent le reste du budget
    assert 1 <= tried < len(decoys)


# C5 : un `mapped` échu ne s'affiche plus comme actif et ne propose plus l'adresse.
def test_c5_expired_mapping_is_neither_shown_nor_proposed(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    opening, _, _ = _click(engine)
    assert opening["status"] == "mapped" and opening["address"] == PUBLIC
    engine._clock = lambda: opening["expires_at"] - 1
    assert engine.snapshot()["ports_opening"]["status"] == "mapped"
    engine._clock = lambda: opening["expires_at"]
    view = engine.snapshot()["ports_opening"]
    assert view == {
        "status": "idle",
        "expired": True,
        "ports": _dicts(_engine_ports(upnp_rig)),
        "lease_s": upnp_rig.runtime["upnp"]["lease_s"],
    }
    assert PUBLIC not in json.dumps(view)


# C6 : l'échéance affichée ne dépasse jamais l'expiration réelle d'un mapping.
def test_c6_expiry_is_a_conservative_bound(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    # L'heure murale réelle, celle que note le faux IGD à chaque appel (le banc la fige sinon).
    lobby.engine._clock = time.time
    opening, _, _ = _click(lobby.engine)
    lease = upnp_rig.runtime["upnp"]["lease_s"]
    adds = igd.actions("AddPortMapping")
    assert len(adds) == 3
    assert opening["expires_at"] <= min(call.at for call in adds) + lease
    assert {expires for _, expires in lobby.engine._upnp_retained.values()} == {opening["expires_at"]}


# C7 : aucune exception ne sort, même sur des entrées malformées.
MALFORMED_PORTS = [
    None,
    5,
    "abc",
    [(1,)],
    [None, None, None],
    [("a", "UDP")] * 3,
    [[[], "UDP"], [[], "UDP"], [[], "TCP"]],
    [[{}, "UDP"], [1, "UDP"], [2, "TCP"]],
    [(True, "UDP"), (2, "UDP"), (3, "TCP")],
]


@pytest.mark.parametrize("ports", MALFORMED_PORTS)
def test_c7_malformed_ports_are_invalid_config_without_traffic(ports, socket_guard):
    result, gateway, cleared = upnp.open_ports(_cfg(), ports, ("127.0.0.1", 9))
    assert result["status"] == "invalid_config" and result["key"] == "ports"
    assert (gateway, cleared) == (None, [])
    assert isinstance(upnp.validate(_cfg(), ports), type(None))
    assert isinstance(upnp.validate({}, ports), dict)
    assert isinstance(upnp.manual_instruction(ports), str)
    assert socket_guard.sends == [] and socket_guard.connects == []


def test_c7_ports_ok_checks_types_before_building_a_set():
    assert upnp._ports_ok([([], "UDP"), ([], "UDP"), ([], "TCP")]) is False
    assert upnp._ports_ok([({}, "UDP"), (1, "UDP"), (2, ["TCP"])]) is False
    assert upnp._ports_ok(list(PORTS)) is True


def test_c7_huge_integer_setting_is_invalid_config(socket_guard):
    result, _, _ = upnp.open_ports(_cfg(request_timeout_s=10**400), PORTS, ("127.0.0.1", 9))
    assert result["status"] == "invalid_config" and result["key"] == "upnp.request_timeout_s"
    assert socket_guard.sends == []


def test_c7_delete_mappings_never_raises_on_malformed_mappings():
    with FakeIGD() as igd:
        gateway = _gateway(igd)
        max_bytes = _cfg()["max_response_bytes"]
        assert upnp.delete_mappings(gateway, None, 0.5, max_bytes=max_bytes) == []  # type: ignore[arg-type]
        bad = ["x", (1,), ([], "UDP"), (0, "UDP"), None]
        failures = upnp.delete_mappings(gateway, [PORTS[0], *bad], 0.5, max_bytes=max_bytes)
        assert failures == bad
        assert igd.couples("DeletePortMapping") == [PORTS[0]]


# C8 : `MX` borné à 1–5, fenêtre de découverte d'au moins une seconde.
@pytest.mark.parametrize(("window", "mx"), [(1, 1), (1.9, 1), (2, 2), (5, 5), (5.5, 5), (30, 5), (1e9, 5)])
def test_c8_mx_is_bounded(window, mx):
    request = upnp._msearch(("127.0.0.1", 9), window).decode("ascii")
    assert f"\r\nMX: {mx}\r\n" in request


@pytest.mark.parametrize("window", [0.5, 0.999])
def test_c8_discovery_window_under_one_second_is_invalid_config(window, socket_guard):
    result, gateway, _ = upnp.open_ports(_cfg(discovery_timeout_s=window), PORTS, ("127.0.0.1", 9))
    assert result["status"] == "invalid_config" and result["key"] == "upnp.discovery_timeout_s"
    assert gateway is None
    assert socket_guard.sends == [] and socket_guard.connects == []
    assert upnp.validate(_cfg(discovery_timeout_s=1)) is None


# M1 : un mapping retenu dont le bail est échu n'est plus supprimé à l'arrêt.
def test_m1_expired_retained_mapping_is_not_deleted_at_shutdown(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    opening, _, _ = _click(engine)
    assert opening["status"] == "mapped"
    seen = len(igd.actions("DeletePortMapping"))
    engine._clock = lambda: opening["expires_at"]
    assert engine.shutdown()
    assert _deletes_after(igd, seen) == []
    assert engine._upnp_retained == {}


# --- P9F2 : revue de P9F1 -------------------------------------------------------------------

from service import engine as engine_module  # noqa: E402

PRIVATE = "10.0.0.1"
# Plus longue attente de l'arrêt sur le fil d'ouverture : la découverte et une requête, puis une
# requête encore en vol (P9F2, C1), plus une seconde de marge.
ABANDON_BOUND_S = FAST["discovery_timeout_s"] + 2 * FAST["request_timeout_s"] + 1


def _gated_open(monkeypatch, gate: threading.Event):
    """Remplace `upnp.open_ports` : le réseau est fait, puis la fusion attend `gate`."""
    real_open = upnp.open_ports
    returned = threading.Event()

    def late_open(cfg, couples, target, cancel):
        outcome = real_open(cfg, couples, target, None)
        gate.wait(4 * ENGINE_WAIT_S)
        returned.set()
        return outcome

    monkeypatch.setattr(upnp, "open_ports", late_open)
    return returned


def _mapped_by_a_gated_opening(upnp_rig, igd, monkeypatch, gate):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    returned = _gated_open(monkeypatch, gate)
    assert engine.open_ports()[0]
    _wait_for(lambda: len(igd.actions("GetExternalIPAddress")) == 1)
    assert set(igd.mappings) == set(_engine_ports(upnp_rig))
    return engine, returned


# Item 1 (bloquant, C1) : une fusion retardée au-delà des deux attentes de l'arrêt.
def test_p9f2_c1_merge_delayed_beyond_both_waits_is_abandoned_with_its_couples_named(
    upnp_rig, igd, monkeypatch, caplog
):
    caplog.set_level(logging.WARNING)
    gate = threading.Event()
    engine, returned = _mapped_by_a_gated_opening(upnp_rig, igd, monkeypatch, gate)
    ports = _engine_ports(upnp_rig)
    seen = len(igd.actions("DeletePortMapping"))
    started = time.monotonic()
    assert engine.shutdown()
    assert time.monotonic() - started <= ABANDON_BOUND_S
    # L'arrêt a rendu la main sans la fusion : il nomme chacun des couples abandonnés.
    (message,) = [r.getMessage() for r in caplog.records if "abandoned at shutdown" in r.getMessage()]
    for port, protocol in ports:
        assert f"{port}/{protocol}" in message
    assert any("abandoned at shutdown" in entry["msg"] for entry in engine.snapshot()["logs"])
    assert PUBLIC not in message
    deletes_at_return = len(igd.actions("DeletePortMapping"))
    assert deletes_at_return == seen
    # La fusion arrive enfin : rien n'est plus fusionné, et rien ne part après l'arrêt.
    gate.set()
    assert returned.wait(ENGINE_WAIT_S)
    engine._ports_thread.join(ENGINE_WAIT_S)
    assert not engine._ports_thread.is_alive()
    assert engine._upnp_retained == {} and engine._ports_handoff == []
    assert len(igd.actions("DeletePortMapping")) == deletes_at_return


def test_p9f2_c1_merge_during_the_second_wait_is_removed_before_shutdown_returns(
    upnp_rig, igd, monkeypatch
):
    gate = threading.Event()
    engine, _ = _mapped_by_a_gated_opening(upnp_rig, igd, monkeypatch, gate)
    ports = _engine_ports(upnp_rig)
    seen = len(igd.actions("DeletePortMapping"))
    warning = engine_module.LOGGER.warning

    def opening_still_running(message, *args):
        # Ce message précède la seconde attente : la fusion arrive pendant celle-ci.
        if message == "port opening still running at shutdown":
            gate.set()
        return warning(message, *args)

    monkeypatch.setattr(engine_module.LOGGER, "warning", opening_still_running)
    assert engine.shutdown()
    # Au retour de l'arrêt, et non plus tard par le fil démon : les trois retraits ont eu lieu.
    assert gate.is_set()
    assert sorted(_deletes_after(igd, seen)) == sorted(ports)
    assert igd.mappings == {}
    assert engine._upnp_retained == {} and engine._ports_handoff == []
    assert not any("abandoned" in entry["msg"] for entry in engine.snapshot()["logs"])


# Item 3 (M1) : l'échéance relue immédiatement avant chaque suppression, retrait tardif compris.
def test_p9f2_m1_mapping_expiring_during_the_first_deletes_is_not_deleted(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    opening, _, _ = _click(engine)
    assert opening["status"] == "mapped"
    seen = len(igd.actions("DeletePortMapping"))
    expires_at = opening["expires_at"]
    # Le bail court jusqu'à la première suppression de l'arrêt, puis s'achève.
    engine._clock = lambda: expires_at - 1 if len(igd.actions("DeletePortMapping")) == seen else expires_at
    assert engine.shutdown()
    assert len(_deletes_after(igd, seen)) == 1
    assert engine._upnp_retained == {}


def test_p9f2_m1_late_merge_after_expiry_deletes_nothing(upnp_rig, igd, monkeypatch):
    gate = threading.Event()
    engine, _ = _mapped_by_a_gated_opening(upnp_rig, igd, monkeypatch, gate)
    seen = len(igd.actions("DeletePortMapping"))
    lease = upnp_rig.runtime["upnp"]["lease_s"]
    warning = engine_module.LOGGER.warning

    def expire_then_merge(message, *args):
        if message == "port opening still running at shutdown":
            # Le bail échoit avant la fusion tardive : le retrait ne doit rien supprimer.
            later = engine._clock() + 2 * lease
            engine._clock = lambda: later
            gate.set()
        return warning(message, *args)

    monkeypatch.setattr(engine_module.LOGGER, "warning", expire_then_merge)
    assert engine.shutdown()
    assert gate.is_set()
    assert _deletes_after(igd, seen) == []
    assert engine._upnp_retained == {} and engine._ports_handoff == []


# Item 6 (C5) : l'échéance de la proposition survit à un nouvel essai en échec.
def test_p9f2_c5_proposal_expiry_survives_a_failed_retry(upnp_rig, igd):
    lobby = Lobby(upnp_rig, "demo")
    engine = lobby.engine
    opening, _, _ = _click(engine)
    assert opening["status"] == "mapped" and opening["address"] == PUBLIC
    igd.configure(ssdp_reply=False)
    retry, _, _ = _click(engine)
    assert retry["status"] == "no_gateway" and "expired" not in retry
    engine._clock = lambda: opening["expires_at"] - 1
    assert "expired" not in engine.snapshot()["ports_opening"]
    engine._clock = lambda: opening["expires_at"]
    view = engine.snapshot()["ports_opening"]
    assert view["status"] == "no_gateway" and view["expired"] is True
    assert PUBLIC not in json.dumps(view)


# Item 2 (C3/C4) : sélection explicite entre deux IGD valides, dans les deux ordres.
def _two_igds(first_ip: str | None, second_ip: str | None):
    """Deux faux IGD valides ; la réponse SSDP de `second` arrive avant celle de `first`."""
    first, second = FakeIGD(external_ip=first_ip).start(), FakeIGD(external_ip=second_ip).start()
    first.configure(ssdp_before=[second.ssdp_answer()])
    return first, second


def _writes(igd: FakeIGD) -> list[str]:
    return [c.action for c in igd.calls if c.action != "GetExternalIPAddress"]


@pytest.mark.parametrize("public_first", [True, False])
def test_p9f2_c3_c4_the_igd_with_a_public_address_is_chosen_in_both_orders(public_first, socket_guard):
    first, second = _two_igds(PRIVATE, PUBLIC) if public_first else _two_igds(PUBLIC, PRIVATE)
    chosen, other = (second, first) if public_first else (first, second)
    try:
        result, gateway, _ = _open(first)
        assert result["status"] == "mapped" and result["address"] == PUBLIC
        assert gateway is not None and gateway.port == chosen.http_port
        assert len(chosen.actions("AddPortMapping")) == 3
        # L'autre IGD n'a reçu que la lecture de son adresse : aucune écriture.
        assert _writes(other) == [] and other.mappings == {}
        assert [c.action for c in other.calls] == ["GetExternalIPAddress"]
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("first_ip", "second_ip"),
    [
        (PUBLIC, "5.6.7.9"),
        (PRIVATE, "192.168.1.1"),
        (PUBLIC, None),
        (None, PUBLIC),
        (PUBLIC, ""),
        ("", PUBLIC),
        (PUBLIC, "not-an-ip"),
        ("010.0.0.1", PUBLIC),
    ],
    ids=[
        "both-public",
        "none-public",
        "first-unread",
        "second-unread",
        "first-empty",
        "second-empty",
        "first-malformed",
        "second-malformed",
    ],
)
def test_p9f2_c3_c4_unresolved_ambiguity_is_refused_without_writes(first_ip, second_ip, socket_guard):
    first, second = _two_igds(first_ip, second_ip)
    try:
        result, gateway, cleared = _open(first)
        assert result["status"] == "no_gateway" and result["reason"] == "ambiguous_gateway"
        assert (gateway, cleared) == (None, [])
        _assert_manual(result)
        assert _writes(first) == [] and _writes(second) == []
    finally:
        first.close()
        second.close()


# Item 4 (R1/C7) : une valeur numérique non représentable devient un résultat.
@pytest.mark.parametrize(
    ("key", "value"),
    [("max_response_bytes", 10**400), ("request_timeout_s", 1e300), ("discovery_timeout_s", 1e300)],
)
def test_p9f2_c7_unrepresentable_setting_is_invalid_config(key, value, socket_guard):
    result, gateway, cleared = upnp.open_ports(_cfg(**{key: value}), PORTS, ("127.0.0.1", 9))
    assert result["status"] == "invalid_config" and result["key"] == f"upnp.{key}"
    assert (gateway, cleared) == (None, [])
    assert socket_guard.sends == [] and socket_guard.connects == []


def test_p9f2_c7_largest_accepted_size_never_reaches_a_socket_unbounded():
    import io
    import sys

    cfg = _cfg(**FAST, max_response_bytes=sys.maxsize)
    assert upnp.validate(cfg) is None
    # Une description plus longue que le tampon de lecture : le corps se lit en plusieurs `read1`.
    with FakeIGD(description="oversized", oversize_bytes=4 * io.DEFAULT_BUFFER_SIZE) as igd:
        result, gateway, _ = upnp.open_ports(cfg, PORTS, igd.ssdp_target)
        assert result["status"] == "mapped" and gateway is not None
        assert igd.fetches == ["/desc.xml"]


def test_p9f2_c7_largest_accepted_size_reads_a_body_without_length():
    """Sans `Content-Length`, `read1` reçoit la taille demandée : jamais plus d'un tampon."""
    import io
    import sys

    body = b"x" * (4 * io.DEFAULT_BUFFER_SIZE)

    def serve(server: socket.socket) -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body[:10])
            time.sleep(0.1)
            conn.sendall(body[10:])

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        thread = threading.Thread(target=serve, args=(server,), daemon=True)
        thread.start()
        reply = upnp._fetch("127.0.0.1", server.getsockname()[1], "/", None, 2, sys.maxsize)
        thread.join(timeout=2)
    assert reply.failure is None and reply.body == body


def test_p9f2_c7_numeric_error_inside_the_module_is_a_result(monkeypatch):
    def overflow(*_args, **_kwargs):
        raise OverflowError("simulated")

    with FakeIGD() as igd:
        monkeypatch.setattr(upnp, "_fetch", overflow)
        result, gateway, cleared = _open(igd)
    assert result["status"] == "no_gateway" and (gateway, cleared) == (None, [])
    _assert_manual(result)


# Item 5 (C4) : échéance absolue et annulation à chaque tour de réception.
FLOOD_S = 4


@pytest.mark.parametrize("kind", ["incompatible", "duplicate"])
def test_p9f2_c4_flood_of_ignored_replies_does_not_outlive_the_budget(kind):
    with FakeIGD() as igd:
        flood = igd.ssdp_answer(f"{DEVICE}MediaRenderer:1") if kind == "incompatible" else igd.ssdp_answer()
        igd.configure(ssdp_flood=[flood], ssdp_flood_s=FLOOD_S)
        cfg = _cfg(**FAST)
        started = time.monotonic()
        gateway, failure = upnp.find_gateway(cfg, igd.ssdp_target)
        elapsed = time.monotonic() - started
        assert gateway is not None and failure is None
        assert elapsed <= cfg["discovery_timeout_s"] + cfg["request_timeout_s"] + 0.4 < FLOOD_S


def test_p9f2_c4_cancel_is_seen_under_a_flood_of_ignored_replies():
    with FakeIGD(ssdp_reply=False) as igd:
        igd.configure(ssdp_flood=[igd.ssdp_answer(f"{DEVICE}MediaRenderer:1")], ssdp_flood_s=FLOOD_S)
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        started = time.monotonic()
        timer.start()
        try:
            gateway, failure = upnp.find_gateway(_cfg(**FAST), igd.ssdp_target, cancel)
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started
        assert gateway is None and failure == {"status": "no_gateway", "reason": "cancelled"}
        assert elapsed < 0.2 + FAST["discovery_timeout_s"] / 2
        assert igd.fetches == []


class _EndlessSocket:
    """Socket UDP factice dont la réception rend toujours, sans attendre, un paquet à ignorer.

    Un flux plus rapide que tout traitement : chaque `recvfrom` est daté. Une sécurité lève
    `OSError` après `stop_s` secondes, pour qu'un client fautif finisse quand même.
    """

    calls: list[float] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.started = time.monotonic()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        pass

    def sendto(self, data, address) -> int:
        return len(data)

    def settimeout(self, value) -> None:
        pass

    def setblocking(self, flag) -> None:
        pass

    def recvfrom(self, size):
        now = time.monotonic()
        if now - self.started > FLOOD_S:
            raise OSError("flood safety stop")
        type(self).calls.append(now)
        packet = b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nLOCATION: http://127.0.0.1:1/d.xml\r\n\r\n"
        return packet, ("127.0.0.1", 1900)


def test_p9f2_c4_endless_ignored_replies_stop_at_the_absolute_deadline(monkeypatch):
    _EndlessSocket.calls = []
    monkeypatch.setattr(upnp.socket, "socket", _EndlessSocket)
    cfg = _cfg(**FAST)
    started = time.monotonic()
    gateway, failure = upnp.find_gateway(cfg, ("127.0.0.1", 9))
    elapsed = time.monotonic() - started
    assert gateway is None and failure == {"status": "no_gateway", "reason": "no_reply"}
    assert elapsed <= cfg["discovery_timeout_s"] + 0.4
    # Aucun tour de réception après l'échéance absolue de la fenêtre, fixée avant le premier tour.
    calls = _EndlessSocket.calls
    assert calls and max(calls) <= min(calls) + cfg["discovery_timeout_s"]
