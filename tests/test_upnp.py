"""UPnP de la phase 3 : faux IGD local (P2) et client `service/upnp.py` (P3–P6).

Un garde de socket, actif sur chaque test du fichier, refuse et note toute connexion, tout
envoi et toute résolution hors `127.0.0.0/8`, et tout envoi vers le groupe SSDP (AC30) : le
faux IGD local est le seul interlocuteur.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
import time

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
        assert gateway == upnp.Gateway("127.0.0.1", igd.http_port, igd.control_path, service_type, "127.0.0.1")
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
