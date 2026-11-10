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
        gateway, failure = upnp.find_gateway(_cfg(discovery_timeout_s=0.5), igd.ssdp_target)
        elapsed = time.monotonic() - started
        assert gateway is None and failure == {"status": "no_gateway", "reason": "no_reply"}
        assert elapsed <= 0.5 + 1
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


GOOD_DESCRIPTION = (
    '<root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList><service>'
    "<serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>"
    "<controlURL>{control}</controlURL></service></serviceList></device></root>"
)


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
    good = (
        b'<root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList><service>'
        b"<serviceType>urn:schemas-upnp-org:service:WANIPConnection:2</serviceType>"
        b"<controlURL>/ctl</controlURL></service></serviceList></device></root>"
    )
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

FAST = {"discovery_timeout_s": 0.5, "request_timeout_s": 0.5}


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


class _CancelAfter:
    """Annulation levée après `n` consultations : simule l'arrêt pendant l'ouverture."""

    def __init__(self, n: int) -> None:
        self.n = n

    def is_set(self) -> bool:
        self.n -= 1
        return self.n < 0


def test_cancel_during_opening_stops_at_next_request():
    # consultations : avant la découverte, avant la description, avant D1, avant A1, avant D2 → levée
    with FakeIGD() as igd:
        result, gateway, _ = _open(igd, _CancelAfter(4))
        assert result["status"] == "refused" and result["failure"] == "cancelled"
        assert result["still_active"] == _dicts(PORTS[:1])
        assert [c.action for c in igd.calls] == ["DeletePortMapping", "AddPortMapping"]
        assert gateway is not None


def test_cancel_during_discovery_skips_description():
    """Annulation levée pendant la découverte : la description n'est pas lue (frontière R6)."""
    with FakeIGD() as igd:
        result, gateway, cleared = _open(igd, _CancelAfter(1))
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
