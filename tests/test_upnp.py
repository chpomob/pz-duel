"""UPnP de la phase 3 : auto-test du faux IGD local (P2).

Le client `service/upnp.py` et ses critères d'acceptation arrivent en P3–P6. Ici, seul le
double est vérifié, et uniquement sur la boucle locale.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time

from doubles.fake_igd import FakeIGD

SOAP_NS = "urn:schemas-upnp-org:service:WANIPConnection:1"


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
