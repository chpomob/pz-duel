"""Client IGD (UPnP) en stdlib : découverte SSDP et lecture de la description (phase 3, R1).

Rien ne part d'ici sans une demande explicite de l'hôte : le module n'a aucun effet à
l'import, et seul l'appelant décide d'une découverte. Toutes les valeurs (délais, taille
maximale d'une réponse) viennent de la section `upnp` de `service/runtime.json`, passée par
l'appelant ; les deux seules constantes du protocole SSDP sont nommées ci-dessous, une fois.

Garde-fous :
- l'URL de description (`LOCATION`) doit être en `http`, et son hôte un littéral IP égal à
  l'adresse qui a répondu à la découverte ; un nom ou une autre adresse rend `no_gateway`
  sans résolution DNS ni connexion (AC4). L'URL de contrôle suit la même règle ;
- une réponse HTTP est lue contre une échéance monotone (vérifiée à chaque lecture de la
  socket : ligne d'état, en-têtes, entêtes de morceaux et corps) et la taille maximale ;
- une description portant `<!DOCTYPE` ou `<!ENTITY` (en ASCII) est refusée avant toute analyse
  XML, et l'analyseur lui-même refuse toute déclaration de DTD ou d'entité, quel que soit
  l'encodage du document (UTF-16…) ; un encodage inconnu rend `no_gateway` ;
- aucune exception ne sort du module : chaque échec devient un résultat.

L'adresse locale vue par la socket de la requête (`getsockname`) sert de `NewInternalClient`,
jamais une adresse saisie par l'utilisateur (AC10).
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import logging
import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.parsers import expat

SSDP_PORT = 1900
SSDP_GROUP = "239.255.255.250"

SEARCH_TARGET = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
SERVICE_PREFIX = "urn:schemas-upnp-org:service:"
SERVICE_TYPES = ("WANIPConnection:1", "WANIPConnection:2", "WANPPPConnection:1")
FORBIDDEN_MARKUP = (b"<!doctype", b"<!entity")

TIMEOUT = "timeout"
UNREADABLE = "unreadable"
NO_GATEWAY = "no_gateway"

ERRORS = (OSError, socket.timeout, http.client.HTTPException, ET.ParseError, ValueError, UnicodeError)
# Un encodage XML inconnu lève `LookupError` dans l'analyseur ; `KeyError` en hérite.
PARSE_ERRORS = (*ERRORS, LookupError, expat.ExpatError)

log = logging.getLogger(__name__)


@dataclass
class Gateway:
    """Passerelle identifiée : adresse de contrôle, service retenu, adresse locale vue."""

    host: str
    port: int
    control_path: str
    service_type: str
    local_address: str


@dataclass
class Reply:
    """Résultat d'une requête HTTP ; `failure` vaut `None`, `timeout` ou `unreadable`."""

    failure: str | None
    status: int | None = None
    body: bytes = b""
    local_address: str | None = None


# --- découverte ------------------------------------------------------------------


def _msearch(target: tuple[str, int], timeout_s: float) -> bytes:
    wait = max(1, int(timeout_s))
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {target[0]}:{target[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {wait}\r\n"
        f"ST: {SEARCH_TARGET}\r\n\r\n"
    ).encode("ascii")


def _location(data: bytes) -> str | None:
    """`LOCATION` d'une réponse SSDP `200`, ou `None` si la réponse ne se lit pas."""
    text = data.decode("latin-1")
    lines = text.split("\r\n")
    status = lines[0].split()
    if len(status) < 2 or not status[0].upper().startswith("HTTP/") or status[1] != "200":
        return None
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().upper() == "LOCATION" and value.strip():
            return value.strip()
    return None


def discover(
    target: tuple[str, int] = (SSDP_GROUP, SSDP_PORT),
    *,
    timeout_s: float,
    max_bytes: int,
) -> tuple[str, str] | None:
    """Envoie une M-SEARCH à `target` et rend `(adresse du répondeur, LOCATION)` ou `None`.

    La première réponse qui se lit l'emporte ; l'attente totale est bornée par une échéance
    monotone de `timeout_s`. Un datagramme illisible (trop long, ICMP renvoyé en erreur par
    Windows) est ignoré jusqu'à l'échéance. Aucune exception ne sort.
    """
    deadline = time.monotonic() + timeout_s
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(_msearch(target, timeout_s), target)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                try:
                    data, peer = sock.recvfrom(max_bytes)
                except (socket.timeout, TimeoutError):
                    return None
                except OSError:
                    continue
                location = _location(data)
                if location is not None:
                    return peer[0], location
    except ERRORS as exc:
        log.info("upnp: discovery failed (%s)", type(exc).__name__)
        return None


# --- contrôle des URL ---------------------------------------------------------------


def _ip_literal(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _same_host(location: str, responder_ip: str) -> tuple[str, int, str] | None:
    """`(hôte, port, chemin)` de `location` s'il désigne l'hôte qui a répondu, sinon `None`.

    L'hôte doit être un littéral IP : un nom n'est jamais résolu. Aucune connexion ici.
    """
    try:
        parts = urllib.parse.urlsplit(location)
        if parts.scheme.lower() != "http":
            return None
        host = _ip_literal(parts.hostname)
        responder = _ip_literal(responder_ip)
        if host is None or responder is None or host != responder:
            return None
        port = parts.port or http.client.HTTP_PORT
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return str(host), port, path
    except ERRORS:
        return None


def _control_path(location: str, control_url: str, host: str, port: int) -> str | None:
    """Chemin de contrôle si `control_url` (relative, ou absolue) vise le même hôte et port."""
    try:
        absolute = urllib.parse.urljoin(location, control_url.strip())
    except ERRORS:
        return None
    checked = _same_host(absolute, host)
    if checked is None or checked[1] != port:
        return None
    return checked[2]


# --- HTTP -----------------------------------------------------------------------


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("upnp: request deadline reached")
    return remaining


class _DeadlineReader(io.RawIOBase):
    """Lecture brute de la socket, l'échéance absolue étant vérifiée avant chaque `recv`."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        super().__init__()
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:  # type: ignore[override]
        self._sock.settimeout(_remaining(self._deadline))
        return self._sock.recv_into(buffer)


class _DeadlineSocket:
    """Socket vue par `http.client` : chaque envoi et chaque lecture respecte l'échéance.

    `HTTPResponse` lit la ligne d'état, les en-têtes, les entêtes de morceaux et le corps par
    `makefile("rb")` ; ici ce fichier repose sur `_DeadlineReader`, si bien qu'un répondeur
    qui égrène ses octets ne retient jamais la lecture au-delà de l'échéance. La vraie socket
    est fermée par `_fetch`, pas par `http.client` (qui la ferme dès que la réponse annonce
    `Connection: close`, avant la lecture du corps).
    """

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def sendall(self, data: bytes) -> None:
        self._sock.settimeout(_remaining(self._deadline))
        self._sock.sendall(data)

    def makefile(self, mode: str = "rb", *args: object, **kwargs: object) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError(f"upnp: unsupported socket file mode {mode!r}")
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline))

    def close(self) -> None:
        pass

    def getsockname(self) -> object:
        return self._sock.getsockname()

    def settimeout(self, value: float | None) -> None:
        pass

    def setsockopt(self, *args: object) -> None:
        self._sock.setsockopt(*args)


def _fetch(
    host: str,
    port: int,
    path: str,
    body: bytes | None,
    timeout_s: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> Reply:
    """Une requête HTTP (`GET` sans corps, `POST` sinon), lue sous échéance et plafond.

    Plus de `max_bytes` octets de corps rend `unreadable` ; l'échéance dépassée rend
    `timeout`. L'adresse locale de la socket est rendue avec la réponse.
    """
    deadline = time.monotonic() + timeout_s
    conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    local: str | None = None
    sock: socket.socket | None = None
    status: int | None = None
    try:
        conn.connect()
        sock = conn.sock
        local = sock.getsockname()[0]
        conn.sock = _DeadlineSocket(sock, deadline)
        conn.request("POST" if body is not None else "GET", path, body, headers or {})
        response = conn.getresponse()
        status = response.status
        length = response.getheader("Content-Length")
        if length is not None and length.strip().isdigit() and int(length) > max_bytes:
            return Reply(UNREADABLE, response.status, local_address=local)
        data = b""
        while not response.isclosed():
            chunk = response.read1(max_bytes + 1 - len(data))
            if not chunk:
                break
            data += chunk
            if len(data) > max_bytes:
                return Reply(UNREADABLE, response.status, local_address=local)
        return Reply(None, response.status, data, local)
    except (socket.timeout, TimeoutError):
        return Reply(TIMEOUT, status, local_address=local)
    except ERRORS as exc:
        log.info("upnp: request failed (%s)", type(exc).__name__)
        return Reply(UNREADABLE, status, local_address=local)
    finally:
        try:
            conn.close()
        except ERRORS:
            pass
        if sock is not None:
            try:
                sock.close()
            except ERRORS:
                pass


# --- description -------------------------------------------------------------------


def _local_name(tag: object) -> str:
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


class _ForbiddenMarkup(ValueError):
    """Déclaration de DTD ou d'entité rencontrée par l'analyseur."""


def _refuse(*_args: object) -> None:
    raise _ForbiddenMarkup("upnp: DTD or entity declaration in description")


def _parse(xml: bytes) -> ET.Element:
    """Analyse `xml` en refusant DTD et entités, quel que soit l'encodage déclaré.

    Une première passe `expat` (le même analyseur que `ElementTree`) voit le document décodé
    et lève dès une déclaration de DTD ou d'entité : un `<!DOCTYPE` en UTF-16 (octets
    entrecoupés de NUL) n'échappe donc pas au contrôle. L'arbre n'est construit
    qu'ensuite, sur un document qui n'en porte aucune.
    """
    check = expat.ParserCreate()
    check.StartDoctypeDeclHandler = _refuse
    check.EntityDeclHandler = _refuse
    check.UnparsedEntityDeclHandler = _refuse
    check.ExternalEntityRefHandler = _refuse
    check.Parse(xml, True)
    return ET.fromstring(xml)


def _find_service(xml: bytes) -> tuple[str, str] | None:
    """`(type de service, controlURL)` du premier service WAN compatible, sinon `None`."""
    lowered = xml.lower()
    if any(marker in lowered for marker in FORBIDDEN_MARKUP):
        return None
    try:
        root = _parse(xml)
    except PARSE_ERRORS:
        return None
    for element in root.iter():
        if _local_name(element.tag) != "service":
            continue
        fields = {_local_name(child.tag): (child.text or "").strip() for child in element}
        service_type = fields.get("serviceType", "")
        control_url = fields.get("controlURL", "")
        if not service_type.startswith(SERVICE_PREFIX) or not control_url:
            continue
        short = service_type[len(SERVICE_PREFIX) :]
        if short in SERVICE_TYPES:
            return short, control_url
    return None


def _no_gateway(reason: str) -> tuple[None, dict]:
    log.info("upnp: no gateway (%s)", reason)
    return None, {"status": NO_GATEWAY, "reason": reason}


def find_gateway(
    cfg: dict, target: tuple[str, int] = (SSDP_GROUP, SSDP_PORT)
) -> tuple[Gateway | None, dict | None]:
    """Découvre la box et lit sa description : `(Gateway, None)` ou `(None, résultat no_gateway)`.

    `cfg` est la section `upnp` de `runtime.json`. Aucune exception ne sort.
    """
    try:
        timeout_s = cfg["discovery_timeout_s"]
        request_timeout_s = cfg["request_timeout_s"]
        max_bytes = cfg["max_response_bytes"]
        found = discover(target, timeout_s=timeout_s, max_bytes=max_bytes)
        if found is None:
            return _no_gateway("no_reply")
        responder_ip, location = found
        place = _same_host(location, responder_ip)
        if place is None:
            return _no_gateway("foreign_location")
        host, port, path = place
        reply = _fetch(host, port, path, None, request_timeout_s, max_bytes)
        if reply.failure is not None:
            return _no_gateway(reply.failure)
        if reply.status != http.client.OK or reply.local_address is None:
            return _no_gateway(UNREADABLE)
        service = _find_service(reply.body)
        if service is None:
            return _no_gateway("no_service")
        service_type, control_url = service
        control_path = _control_path(location, control_url, host, port)
        if control_path is None:
            return _no_gateway("foreign_control_url")
        return Gateway(host, port, control_path, service_type, reply.local_address), None
    except (*PARSE_ERRORS, TypeError) as exc:
        return _no_gateway(type(exc).__name__)
