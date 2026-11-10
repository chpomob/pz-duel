"""Faux IGD local : répondeur SSDP et passerelle HTTP/SOAP, sur la boucle locale seulement.

Double de la phase 3 (R9). Il n'adhère à aucun groupe multicast et n'émet que vers le
demandeur, sur `127.0.0.1` : le client UPnP le vise en lui passant `ssdp_target` à la place
de `(SSDP_GROUP, SSDP_PORT)`.

- SSDP : socket UDP liée à `127.0.0.1:0` ; une M-SEARCH reçue est notée dans `msearches`,
  et la réponse unicast porte `LOCATION: http://<location_host>:<http_port>/desc.xml`
  (`location_host` vaut `127.0.0.1` ; `127.0.0.2` ou un nom pour AC4 ; `location` remplace
  l'URL entière). `ssdp_reply=False` ne répond pas (aucune passerelle). `ssdp_st` remplace le
  `ST` de la réponse (par défaut, celui de la demande) ; `ssdp_before` liste des réponses brutes
  envoyées, dans l'ordre, avant la sienne, depuis la même socket : d'autres répondeurs (P9F1, C4).
- `device_type` : type du périphérique racine de la description (par défaut l'IGD) ;
  `wan_in_root=True` range le service WAN dans la liste du périphérique racine au lieu de
  l'arborescence `WANDevice` / `WANConnectionDevice` (P9F1, C3).
- HTTP (`ThreadingHTTPServer` sur `127.0.0.1:0`) : `GET /desc.xml` rend la description,
  notée dans `fetches`. `service_type` vaut `"WANIPConnection:1"`, `"WANPPPConnection:1"`, ou
  `None` (aucun service compatible) ; `description="malformed"` rend un XML cassé,
  `description="oversized"` un XML valide de `oversize_bytes` octets (par défaut
  `upnp.max_response_bytes + 1` de `service/runtime.json`). `POST` sur `control_path` traite
  `DeletePortMapping`, `AddPortMapping`, `GetExternalIPAddress`.
- Chaque appel SOAP est noté dans `calls` (`order` global à partir de 1, `index` dans son
  action à partir de 1, `action`, `args`, `at` l'heure murale `time.time()` de réception)
  **avant** toute décision : un appel muet est donc
  enregistré (AC33). Le tableau `mappings` `{(port, protocole): args}` suit les ajouts et
  suppressions qui ont reçu une réponse ; `apply_silent=True` applique aussi les appels muets.
- Règles, toutes modifiables en cours de route (`configure`, `set_responsive`) :
  - `errors` : `{action: code}` ou `{(action, index): code}` ; le code peut être un couple
    `(code, texte)`. Ex. `{"DeletePortMapping": 714, ("AddPortMapping", 3): 718}` ;
  - `malformed` : `True`, ou ensemble de clés au même format, pour une réponse SOAP cassée ;
  - silence : `silent=True` (tout), `silent_from=N` (à partir de l'appel d'ordre global N),
    `silent_actions={"DeletePortMapping"}`, `silent_on={4, ("DeletePortMapping", 1)}` ;
    `set_responsive()` les lève tous (AC32) ;
  - `delay_s` : délai avant chaque réponse HTTP (AC16) ;
  - `external_ip` : adresse rendue par `GetExternalIPAddress` ; `None` rend l'erreur 501 ;
  - `strict_deletes=True` : une suppression d'un mapping absent rend 714.
- Un gestionnaire muet attend sur un `threading.Event` libéré par `close()` (ou par
  `set_responsive()`), puis ferme la connexion sans répondre : le démontage ne bloque jamais,
  et `daemon_threads = True` ne laisse aucun fil retenir l'interpréteur.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LOOPBACK = "127.0.0.1"
IGD_TYPE = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
RUNTIME_PATH = Path(__file__).resolve().parents[2] / "service" / "runtime.json"
SERVICE_PREFIX = "urn:schemas-upnp-org:service:"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
DESC_PATH = "/desc.xml"
CONTROL_PATH = "/ctl/wan"
ACTIONS = ("DeletePortMapping", "AddPortMapping", "GetExternalIPAddress")
ERROR_TEXTS = {
    401: "Invalid Action",
    402: "Invalid Args",
    501: "Action Failed",
    714: "NoSuchEntryInArray",
    718: "ConflictInMappingEntry",
}
POLL_S = 0.05
MAX_SOAP_BODY = 1 << 16


def _runtime_max_bytes() -> int:
    return int(json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))["upnp"]["max_response_bytes"])


@dataclass
class Call:
    """Un appel SOAP reçu, dans son ordre d'arrivée."""

    order: int
    index: int
    action: str
    args: dict[str, str]
    client: str = ""
    silent: bool = False
    at: float = 0.0


@dataclass
class _Rules:
    errors: dict[Any, Any] = field(default_factory=dict)
    malformed: Any = False
    silent: bool = False
    silent_from: int | None = None
    silent_actions: set[str] = field(default_factory=set)
    silent_on: set[Any] = field(default_factory=set)
    delay_s: float = 0.0
    external_ip: str | None = "5.6.7.8"
    strict_deletes: bool = False
    apply_silent: bool = False
    ssdp_reply: bool = True
    ssdp_st: str | None = None
    ssdp_before: list[bytes] = field(default_factory=list)


def _match(keys: Any, call: Call) -> bool:
    if keys is True:
        return True
    if not keys:
        return False
    return call.action in keys or (call.action, call.index) in keys or call.order in keys


class FakeIGD:
    """Passerelle factice ; `with FakeIGD(...) as igd:` démarre puis ferme."""

    def __init__(
        self,
        *,
        service_type: str | None = "WANIPConnection:1",
        description: str = "ok",
        oversize_bytes: int | None = None,
        location_host: str = LOOPBACK,
        location: str | None = None,
        control_path: str = CONTROL_PATH,
        control_url: str | None = None,
        device_type: str = IGD_TYPE,
        wan_in_root: bool = False,
        **rules: Any,
    ) -> None:
        if description not in ("ok", "malformed", "oversized"):
            raise ValueError(f"description inconnue : {description!r}")
        self.service_type = service_type
        self.description = description
        self.oversize_bytes = oversize_bytes
        self.location_host = location_host
        self._location = location
        self.control_path = control_path
        self.control_url = control_url if control_url is not None else control_path
        self.device_type = device_type
        self.wan_in_root = wan_in_root
        self.rules = _Rules()
        self.configure(**rules)

        self.calls: list[Call] = []
        self.fetches: list[str] = []
        self.msearches: list[tuple[str, int]] = []
        self.mappings: dict[tuple[int, str], dict[str, str]] = {}
        self.lock = threading.Lock()
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._http: ThreadingHTTPServer | None = None
        self._udp: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._order = 0
        self._per_action: dict[str, int] = {}

    # --- cycle de vie -----------------------------------------------------------

    def start(self) -> FakeIGD:
        igd = self

        class Handler(_Handler):
            owner = igd

        self._http = ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self._http.daemon_threads = True
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind((LOOPBACK, 0))
        self._udp.settimeout(POLL_S)
        for name, target in (("fake-igd-http", self._serve_http), ("fake-igd-ssdp", self._serve_ssdp)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def close(self) -> None:
        """Libère les gestionnaires muets, arrête les deux serveurs ; idempotent."""
        self._closed.set()
        with self.lock:
            self._wake.set()
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
        if self._udp is not None:
            self._udp.close()
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads.clear()

    def __enter__(self) -> FakeIGD:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- réglages ---------------------------------------------------------------

    def configure(self, **rules: Any) -> None:
        """Change les règles en cours de route ; une clé inconnue est une erreur."""
        for key, value in rules.items():
            if not hasattr(self.rules, key):
                raise TypeError(f"règle inconnue : {key!r}")
            if key in ("silent_actions", "silent_on") and value is not None:
                value = set(value)
            setattr(self.rules, key, value)

    def set_responsive(self) -> None:
        """Lève tout silence ; les gestionnaires déjà muets ferment sans répondre."""
        with self.lock:
            self.rules.silent = False
            self.rules.silent_from = None
            self.rules.silent_actions = set()
            self.rules.silent_on = set()
            self._wake.set()
            self._wake = threading.Event()

    # --- adresses ---------------------------------------------------------------

    @property
    def http_port(self) -> int:
        assert self._http is not None, "FakeIGD non démarré"
        return self._http.server_address[1]

    @property
    def ssdp_target(self) -> tuple[str, int]:
        assert self._udp is not None, "FakeIGD non démarré"
        return (LOOPBACK, self._udp.getsockname()[1])

    @property
    def location(self) -> str:
        if self._location is not None:
            return self._location
        return f"http://{self.location_host}:{self.http_port}{DESC_PATH}"

    # --- lecture des enregistrements --------------------------------------------

    def actions(self, name: str | None = None) -> list[Call]:
        with self.lock:
            return [c for c in self.calls if name is None or c.action == name]

    def couples(self, name: str) -> list[tuple[int, str]]:
        """Couples (port externe, protocole) des appels `name`, dans l'ordre d'arrivée."""
        return [
            (int(c.args.get("NewExternalPort", "0") or 0), c.args.get("NewProtocol", ""))
            for c in self.actions(name)
        ]

    # --- SSDP -------------------------------------------------------------------

    def ssdp_answer(self, st: str = IGD_TYPE) -> bytes:
        """Réponse SSDP de ce faux IGD ; à glisser dans `ssdp_before` d'un autre (C4)."""
        return (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=120\r\n"
            "EXT:\r\n"
            f"LOCATION: {self.location}\r\n"
            "SERVER: FakeIGD/1.0 UPnP/1.1\r\n"
            f"ST: {st}\r\n"
            f"USN: uuid:fake-igd::{st}\r\n"
            "\r\n"
        ).encode("latin-1")

    def _serve_ssdp(self) -> None:
        sock = self._udp
        assert sock is not None
        while not self._closed.is_set():
            try:
                data, peer = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not ipaddress.ip_address(peer[0]).is_loopback:
                continue
            if not data.startswith(b"M-SEARCH"):
                continue
            st = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
            for line in data.decode("latin-1").split("\r\n"):
                name, _, value = line.partition(":")
                if name.strip().upper() == "ST" and value.strip():
                    st = value.strip()
            with self.lock:
                self.msearches.append(peer)
                reply = self.rules.ssdp_reply
                st = self.rules.ssdp_st or st
                before = list(self.rules.ssdp_before)
            try:
                for other in before:
                    sock.sendto(other, peer)
            except OSError:
                return
            if not reply:
                continue
            answer = self.ssdp_answer(st)
            try:
                sock.sendto(answer, peer)
            except OSError:
                return

    # --- HTTP -------------------------------------------------------------------

    def _serve_http(self) -> None:
        assert self._http is not None
        self._http.serve_forever(poll_interval=POLL_S)

    def description_body(self) -> bytes:
        if self.description == "malformed":
            return b'<?xml version="1.0"?><root><device><serviceList><service>'
        services = (
            "<service><serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType>"
            "<serviceId>urn:upnp-org:serviceId:L3Forwarding1</serviceId>"
            "<controlURL>/ctl/l3f</controlURL><eventSubURL>/evt/l3f</eventSubURL>"
            "<SCPDURL>/l3f.xml</SCPDURL></service>"
        )
        wan = ""
        if self.service_type is not None and self.wan_in_root:
            services += (
                "<service>"
                f"<serviceType>{SERVICE_PREFIX}{self.service_type}</serviceType>"
                "<serviceId>urn:upnp-org:serviceId:WANConn1</serviceId>"
                f"<controlURL>{self.control_url}</controlURL>"
                "<eventSubURL>/evt/wan</eventSubURL><SCPDURL>/wan.xml</SCPDURL></service>"
            )
        elif self.service_type is not None:
            wan = (
                "<device><deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>"
                "<serviceList><service>"
                f"<serviceType>{SERVICE_PREFIX}{self.service_type}</serviceType>"
                "<serviceId>urn:upnp-org:serviceId:WANConn1</serviceId>"
                f"<controlURL>{self.control_url}</controlURL>"
                "<eventSubURL>/evt/wan</eventSubURL><SCPDURL>/wan.xml</SCPDURL>"
                "</service></serviceList></device>"
            )
        body = (
            '<?xml version="1.0"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            f"<device><deviceType>{self.device_type}</deviceType>"
            "<friendlyName>Fake IGD</friendlyName>"
            f"<serviceList>{services}</serviceList>"
            "<deviceList><device>"
            "<deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>"
            f"<deviceList>{wan}</deviceList>"
            "</device></deviceList></device></root>"
        ).encode("utf-8")
        if self.description == "oversized":
            size = self.oversize_bytes if self.oversize_bytes is not None else _runtime_max_bytes() + 1
            if size < len(body):
                raise ValueError(f"oversize_bytes trop petit : {size} < {len(body)}")
            body += b" " * (size - len(body))
        return body

    def _record(self, action: str, args: dict[str, str], client: str) -> tuple[Call, _Rules, threading.Event]:
        with self.lock:
            self._order += 1
            self._per_action[action] = self._per_action.get(action, 0) + 1
            call = Call(self._order, self._per_action[action], action, args, client, at=time.time())
            r = self.rules
            call.silent = (
                r.silent
                or (r.silent_from is not None and call.order >= r.silent_from)
                or action in r.silent_actions
                or _match(r.silent_on, call)
            )
            self.calls.append(call)
            snapshot = _Rules(**{k: getattr(r, k) for k in r.__dataclass_fields__})
            return call, snapshot, self._wake

    def _apply(self, call: Call) -> int | None:
        """Applique l'appel au tableau ; rend un code d'erreur UPnP ou `None`."""
        args = call.args
        try:
            key = (int(args.get("NewExternalPort", "")), args.get("NewProtocol", ""))
        except ValueError:
            key = None
        with self.lock:
            if call.action == "AddPortMapping":
                if key is None:
                    return 402
                self.mappings[key] = dict(args)
            elif call.action == "DeletePortMapping":
                if key is None:
                    return 402
                if key not in self.mappings and self.rules.strict_deletes:
                    return 714
                self.mappings.pop(key, None)
        return None

    def _answer(self, call: Call, rules: _Rules) -> tuple[int, bytes]:
        error = rules.errors.get((call.action, call.index), rules.errors.get(call.action))
        if call.action not in ACTIONS:
            error = 401
        if error is None:
            error = self._apply(call)
        if error is None and call.action == "GetExternalIPAddress" and rules.external_ip is None:
            error = 501
        if _match(rules.malformed, call):
            return 200, b'<?xml version="1.0"?><s:Envelope xmlns:s="' + SOAP_ENV.encode() + b'"><s:Body><u:'
        if error is not None:
            code, text = error if isinstance(error, tuple) else (error, ERROR_TEXTS.get(error, "Error"))
            return 500, _fault(code, text)
        out = ""
        if call.action == "GetExternalIPAddress":
            out = f"<NewExternalIPAddress>{rules.external_ip}</NewExternalIPAddress>"
        stype = SERVICE_PREFIX + (self.service_type or "WANIPConnection:1")
        return 200, _envelope(f'<u:{call.action}Response xmlns:u="{stype}">{out}</u:{call.action}Response>')


def _envelope(inner: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENV}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    ).encode("utf-8")


def _fault(code: int, text: str) -> bytes:
    return _envelope(
        "<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
        '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode><errorDescription>{text}</errorDescription>"
        "</UPnPError></detail></s:Fault>"
    )


def _parse_soap(header: str, body: bytes) -> tuple[str, dict[str, str]]:
    action = header.strip().strip('"').rpartition("#")[2]
    args: dict[str, str] = {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return action, args
    for element in root.iter():
        name = element.tag.rpartition("}")[2]
        if name == action or (not action and name in ACTIONS):
            action = name
            for child in element:
                args[child.tag.rpartition("}")[2]] = (child.text or "").strip()
            break
    return action, args


class _Handler(BaseHTTPRequestHandler):
    owner: FakeIGD
    server_version = "FakeIGD/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature imposée
        return

    def _send(self, status: int, body: bytes, content_type: str = 'text/xml; charset="utf-8"') -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _delay(self, delay_s: float) -> bool:
        """Attend `delay_s` ; faux si `close()` est passé entre-temps."""
        return not (delay_s > 0 and self.owner._closed.wait(delay_s))

    def do_GET(self) -> None:  # noqa: N802 - nom imposé par http.server
        igd = self.owner
        with igd.lock:
            igd.fetches.append(self.path)
            delay_s = igd.rules.delay_s
        if self.path != DESC_PATH:
            self._send(404, b"")
            return
        if self._delay(delay_s):
            self._send(200, igd.description_body())

    def do_POST(self) -> None:  # noqa: N802 - nom imposé par http.server
        igd = self.owner
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(max(length, 0), MAX_SOAP_BODY))
        if self.path != igd.control_path:
            self._send(404, b"")
            return
        action, args = _parse_soap(self.headers.get("SOAPAction", ""), body)
        call, rules, wake = igd._record(action, args, self.client_address[0])
        if call.silent:
            if rules.apply_silent:
                igd._apply(call)
            while not igd._closed.is_set() and not wake.is_set():
                wake.wait(POLL_S)
            self.close_connection = True
            return
        if self._delay(rules.delay_s):
            status, payload = igd._answer(call, rules)
            self._send(status, payload)
