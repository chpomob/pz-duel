"""Client IGD (UPnP) en stdlib : découverte, mappings, abandon et retrait (phase 3, R1–R6).

Rien ne part d'ici sans une demande explicite de l'hôte : le module n'a aucun effet à
l'import, et seul l'appelant décide d'une découverte. Toutes les valeurs (délais, taille
maximale d'une réponse) viennent de la section `upnp` de `service/runtime.json`, passée par
l'appelant ; les deux seules constantes du protocole SSDP sont nommées ci-dessous, une fois.

Garde-fous :
- seule une réponse SSDP `200` dont le `ST` désigne une passerelle IGD est candidate. La
  réception s'arrête à une échéance absolue (`discovery_timeout_s`), vérifiée avec l'annulation à
  chaque tour, même pour un paquet ignoré (P9F2). Les candidats reçus sont ensuite essayés dans
  leur ordre d'arrivée, le tout dans un budget global de `discovery_timeout_s +
  request_timeout_s` (C4) ;
- sélection explicite de la passerelle (P9F2, C3/C4) : une seule passerelle validée est retenue ;
  avec plusieurs, l'adresse externe de chacune est lue (`GetExternalIPAddress`, lecture seule) et
  seule est retenue l'unique passerelle dont l'adresse est publique, à condition que celle de
  chacune des autres ait été lue et ne le soit pas. Tout autre cas (aucune publique, plusieurs,
  une lecture manquée, un candidat laissé sans essai faute de budget) est une ambiguïté non
  résolue : `no_gateway` (`ambiguous_gateway`), sans aucune écriture ;
- une passerelle n'est retenue, avant toute écriture SOAP, que si sa description est celle d'un
  IGD : périphérique racine `InternetGatewayDevice`, et service WAN compatible rangé sous
  `WANDevice` puis `WANConnectionDevice` (C3). Rien de plus ne l'authentifie : un hôte du réseau
  local qui imite tout cela reste indiscernable, d'où une adresse publique présentée par l'UI
  comme non vérifiée ;
- l'URL de description (`LOCATION`) doit être en `http`, et son hôte un littéral IP égal à
  l'adresse qui a répondu à la découverte ; un nom ou une autre adresse rend `no_gateway`
  sans résolution DNS ni connexion (AC4). L'URL de contrôle suit la même règle ;
- une réponse HTTP est lue contre une échéance monotone (vérifiée à chaque lecture de la
  socket : ligne d'état, en-têtes, entêtes de morceaux et corps) et la taille maximale ;
- une description portant `<!DOCTYPE` ou `<!ENTITY` (en ASCII) est refusée avant toute analyse
  XML, et l'analyseur lui-même refuse toute déclaration de DTD ou d'entité, quel que soit
  l'encodage du document (UTF-16…) ; un encodage inconnu rend `no_gateway` ;
- aucune exception ne sort du module, même sur des entrées malformées ou des valeurs numériques
  qu'une socket ne sait pas représenter : `validate` les refuse, les tailles passées aux sockets
  sont bornées par le protocole, et une erreur numérique restante devient un résultat (P9F2).

L'adresse locale vue par la socket de la requête (`getsockname`) sert de `NewInternalClient`,
jamais une adresse saisie par l'utilisateur (AC10).

Ouverture (`open_ports`, R3) : pour chacun des trois couples passés par l'appelant,
`DeletePortMapping` (toute erreur UPnP ignorée) puis `AddPortMapping`, puis
`GetExternalIPAddress`. La première requête dont le délai expire arrête l'ouverture ; le
retrait (`_rollback`) qui suit fait au plus une suppression par mapping possiblement posé et
s'arrête lui aussi à la première expiration. Le retrait de l'arrêt (`delete_mappings`, R6),
lui, tente chaque mapping retenu une fois, quoi qu'il soit arrivé aux précédents : les deux
chemins ne partagent aucun drapeau.

L'adresse publique lue n'est placée que dans le résultat rendu ; ce module ne la consigne
jamais (R5).
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import logging
import math
import socket
import sys
import time
import urllib.parse
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from xml.parsers import expat
from xml.sax.saxutils import escape

SSDP_PORT = 1900
SSDP_GROUP = "239.255.255.250"

# Plus grand datagramme IPv4 (champ « longueur totale » sur 16 bits) : la réception SSDP n'a
# jamais besoin d'un tampon plus grand, quelle que soit `max_response_bytes` (P9F2).
UDP_MAX_DATAGRAM = 65535
# Plus grand délai accepté pour chacune des deux attentes : leur somme, la plus longue attente
# du moteur sur le fil d'ouverture, reste représentable par `Thread.join` comme par
# `socket.settimeout` (P9F2, R1/C7). Une limite de la plateforme, pas une valeur opérationnelle.
MAX_WAIT_S = threading.TIMEOUT_MAX / 2

# Bornes de `MX` fixées par le protocole SSDP (UDA 1.1 : de 1 à 5 secondes). La fenêtre de
# découverte ne peut pas être plus courte que l'attente minimale annoncée (C8).
MX_MIN = 1
MX_MAX = 5

DEVICE_PREFIX = "urn:schemas-upnp-org:device:"
SEARCH_TARGET = f"{DEVICE_PREFIX}InternetGatewayDevice:1"
# `ST` acceptés dans une réponse, et types attendus à chaque niveau de l'arborescence IGD (C3, C4).
IGD_TYPES = (SEARCH_TARGET, f"{DEVICE_PREFIX}InternetGatewayDevice:2")
WAN_DEVICE_TYPES = (f"{DEVICE_PREFIX}WANDevice:1", f"{DEVICE_PREFIX}WANDevice:2")
WAN_CONNECTION_TYPES = (f"{DEVICE_PREFIX}WANConnectionDevice:1", f"{DEVICE_PREFIX}WANConnectionDevice:2")
SERVICE_PREFIX = "urn:schemas-upnp-org:service:"
SERVICE_TYPES = ("WANIPConnection:1", "WANIPConnection:2", "WANPPPConnection:1")
FORBIDDEN_MARKUP = (b"<!doctype", b"<!entity")

TIMEOUT = "timeout"
UNREADABLE = "unreadable"
CANCELLED = "cancelled"
UPNP_ERROR = "upnp_error"
NO_GATEWAY = "no_gateway"
MAPPED = "mapped"
REFUSED = "refused"
INVALID_CONFIG = "invalid_config"
AMBIGUOUS = "ambiguous_gateway"

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENCODING = "http://schemas.xmlsoap.org/soap/encoding/"
NO_SUCH_ENTRY = 714  # code UPnP `NoSuchEntryInArray` : le mapping est absent
PROTOCOLS = ("UDP", "TCP")
MAPPING_COUNT = 3  # R3 : exactement trois mappings (deux UDP du camp B, le TCP de partage)
PORT_RANGE = range(1, 65536)
# Borne du bail fixée par la spécification (R3 : au plus deux heures), pas une valeur de
# configuration : la durée elle-même vient de `upnp.lease_s`.
MAX_LEASE_S = 2 * 60 * 60
CONFIG_KEYS = ("discovery_timeout_s", "request_timeout_s", "lease_s", "description", "max_response_bytes")

NOTE_UNKNOWN = "The router did not report a public address: the public address is unknown."
NOTE_NOT_PUBLIC = (
    "The router reports an address that is not reachable from the Internet "
    "(double NAT or carrier-grade NAT): a guest outside your network may not get in."
)
# Blocs exclus en plus de `is_global`, pour que la décision ne dérive pas d'une version de
# Python à l'autre (RFC 5737 et espace partagé RFC 6598), et blocs à usage spécial IANA que
# `is_global` laisse passer parce que le registre les dit « globally reachable » :
# `192.0.0.0/24` (dont `192.0.0.9` et `192.0.0.10`), AS112 `192.31.196.0/24` et
# `192.175.48.0/24`, AMT `192.52.193.0/24` (R5 : hors de tout bloc à usage spécial).
NOT_PUBLIC_NETS = tuple(
    ipaddress.IPv4Network(net)
    for net in (
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "192.31.196.0/24",
        "192.52.193.0/24",
        "192.175.48.0/24",
    )
)

# `ArithmeticError` (dont `OverflowError`) : une erreur numérique devient un résultat (P9F2).
ERRORS = (
    OSError,
    socket.timeout,
    http.client.HTTPException,
    ET.ParseError,
    ValueError,
    UnicodeError,
    ArithmeticError,
)
# Un encodage XML inconnu lève `LookupError` dans l'analyseur ; `KeyError` en hérite.
PARSE_ERRORS = (*ERRORS, LookupError, expat.ExpatError)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gateway:
    """Passerelle identifiée : hôte, port et chemin de contrôle, service retenu.

    Rien d'autre : le moteur la garde pour l'arrêt (R6) et la compare d'une ouverture à
    l'autre. L'adresse locale (`NewInternalClient`) est relue sur la socket de chaque ajout.
    """

    host: str
    port: int
    control_path: str
    service_type: str


@dataclass
class Reply:
    """Résultat d'une requête HTTP ; `failure` vaut `None`, `timeout` ou `unreadable`."""

    failure: str | None
    status: int | None = None
    body: bytes = b""
    local_address: str | None = None


# --- découverte ------------------------------------------------------------------


def _mx(timeout_s: float) -> int:
    """`MX` annoncé : la fenêtre en secondes entières, bornée à [MX_MIN, MX_MAX] (C8)."""
    return min(MX_MAX, max(MX_MIN, int(timeout_s)))


def _msearch(target: tuple[str, int], timeout_s: float) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {target[0]}:{target[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {_mx(timeout_s)}\r\n"
        f"ST: {SEARCH_TARGET}\r\n\r\n"
    ).encode("ascii")


def _location(data: bytes) -> str | None:
    """`LOCATION` d'une réponse SSDP `200` dont le `ST` désigne un IGD, sinon `None` (C4)."""
    text = data.decode("latin-1")
    lines = text.split("\r\n")
    status = lines[0].split()
    if len(status) < 2 or not status[0].upper().startswith("HTTP/") or status[1] != "200":
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers.setdefault(name.strip().upper(), value.strip())
    if headers.get("ST") not in IGD_TYPES or not headers.get("LOCATION"):
        return None
    return headers["LOCATION"]


def _replies(
    target: tuple[str, int],
    timeout_s: float,
    deadline: float,
    max_bytes: int,
    cancel: threading.Event | None = None,
) -> Iterator[tuple[str, str]]:
    """Envoie une M-SEARCH, puis rend chaque `(répondeur, LOCATION)` compatible, une fois.

    `deadline` (monotone) est absolue : chaque tour de réception, y compris celui d'un paquet
    ignoré (incompatible, en double, illisible), vérifie d'abord l'annulation puis l'échéance, et
    rien n'est plus reçu au-delà (P9F2). Un flux continu de réponses ignorées ne retient donc
    jamais la découverte plus longtemps que sa fenêtre. Le tampon de réception est borné par
    `UDP_MAX_DATAGRAM`. Les erreurs de socket remontent à l'appelant, qui les convertit.
    """
    seen: set[tuple[str, str]] = set()
    size = min(max_bytes, UDP_MAX_DATAGRAM)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(_msearch(target, timeout_s), target)
        while True:
            if _stopped(cancel):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            sock.settimeout(remaining)
            try:
                data, peer = sock.recvfrom(size)
            except (socket.timeout, TimeoutError):
                return
            except OSError:
                # datagramme trop long, ICMP renvoyé en erreur par Windows : tour suivant
                continue
            location = _location(data)
            if location is None or (peer[0], location) in seen:
                continue
            seen.add((peer[0], location))
            yield peer[0], location


def discover(
    target: tuple[str, int] = (SSDP_GROUP, SSDP_PORT),
    *,
    timeout_s: float,
    max_bytes: int,
) -> tuple[str, str] | None:
    """Envoie une M-SEARCH à `target` et rend le premier `(répondeur, LOCATION)` compatible.

    L'attente totale est bornée par une échéance monotone de `timeout_s` ; `None` sans réponse
    compatible. `find_gateway` ne s'arrête pas là : il essaie les candidats suivants (C4).
    Aucune exception ne sort.
    """
    try:
        replies = _replies(target, timeout_s, time.monotonic() + timeout_s, max_bytes, None)
        try:
            return next(replies, None)
        finally:
            replies.close()
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
    body: bytes | Callable[[str], bytes] | None,
    timeout_s: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> Reply:
    """Une requête HTTP (`GET` sans corps, `POST` sinon), lue sous échéance et plafond.

    Plus de `max_bytes` octets de corps rend `unreadable` ; l'échéance dépassée rend
    `timeout`. L'adresse locale de la socket est rendue avec la réponse. Un `body` appelable
    reçoit cette adresse locale, une fois la connexion établie, et rend le corps à envoyer.
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
        if callable(body):
            body = body(local)
        conn.request("POST" if body is not None else "GET", path, body, headers or {})
        response = conn.getresponse()
        status = response.status
        length = response.getheader("Content-Length")
        if length is not None and length.strip().isdigit() and int(length) > max_bytes:
            return Reply(UNREADABLE, response.status, local_address=local)
        data = b""
        while not response.isclosed():
            # Jamais plus d'un tampon par lecture : `read1` alloue la taille demandée (P9F2).
            chunk = response.read1(min(io.DEFAULT_BUFFER_SIZE, max_bytes + 1 - len(data)))
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


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _text(element: ET.Element, name: str) -> str:
    found = _children(element, name)
    return (found[0].text or "").strip() if found else ""


def _subdevices(device: ET.Element, types: tuple[str, ...]) -> Iterator[ET.Element]:
    for listing in _children(device, "deviceList"):
        for child in _children(listing, "device"):
            if _text(child, "deviceType") in types:
                yield child


def _find_service(xml: bytes) -> tuple[str, str] | None:
    """`(type de service, controlURL)` du premier service WAN compatible d'un IGD, sinon `None`.

    Identité vérifiée (C3) : la racine porte un périphérique `InternetGatewayDevice`, et le
    service n'est cherché que sous `WANDevice` puis `WANConnectionDevice` de ce périphérique ;
    un service WAN rangé ailleurs, ou porté par un autre type de périphérique, ne compte pas.
    """
    lowered = xml.lower()
    if any(marker in lowered for marker in FORBIDDEN_MARKUP):
        return None
    try:
        root = _parse(xml)
    except PARSE_ERRORS:
        return None
    if _local_name(root.tag) != "root":
        return None
    for igd in _children(root, "device"):
        if _text(igd, "deviceType") not in IGD_TYPES:
            continue
        for wan in _subdevices(igd, WAN_DEVICE_TYPES):
            for connection in _subdevices(wan, WAN_CONNECTION_TYPES):
                for listing in _children(connection, "serviceList"):
                    for element in _children(listing, "service"):
                        service_type = _text(element, "serviceType")
                        control_url = _text(element, "controlURL")
                        if not service_type.startswith(SERVICE_PREFIX) or not control_url:
                            continue
                        short = service_type[len(SERVICE_PREFIX) :]
                        if short in SERVICE_TYPES:
                            return short, control_url
    return None


def _no_gateway(reason: str) -> tuple[None, dict]:
    log.info("upnp: no gateway (%s)", reason)
    return None, {"status": NO_GATEWAY, "reason": reason}


def _candidate(
    responder_ip: str, location: str, timeout_s: float, max_bytes: int
) -> tuple[Gateway | None, str]:
    """Valide un candidat : `(Gateway, "")` ou `(None, raison)`. Aucune écriture SOAP ici."""
    place = _same_host(location, responder_ip)
    if place is None:
        return None, "foreign_location"
    host, port, path = place
    reply = _fetch(host, port, path, None, timeout_s, max_bytes)
    if reply.failure is not None:
        return None, reply.failure
    if reply.status != http.client.OK:
        return None, UNREADABLE
    service = _find_service(reply.body)
    if service is None:
        return None, "no_service"
    service_type, control_url = service
    control_path = _control_path(location, control_url, host, port)
    if control_path is None:
        return None, "foreign_control_url"
    return Gateway(host, port, control_path, service_type), ""


def _collect(
    target: tuple[str, int], cfg: dict, window_end: float, cancel: threading.Event | None
) -> list[tuple[str, str]]:
    """Réponses compatibles reçues jusqu'à `window_end`, dans leur ordre d'arrivée.

    Une erreur de socket arrête la réception ; ce qui a été reçu avant est gardé.
    """
    found: list[tuple[str, str]] = []
    replies = _replies(target, cfg["discovery_timeout_s"], window_end, cfg["max_response_bytes"], cancel)
    try:
        for reply in replies:
            found.append(reply)
    except ERRORS as exc:
        log.info("upnp: discovery failed (%s)", type(exc).__name__)
    finally:
        replies.close()
    return found


def _select(
    gateways: list[Gateway], cfg: dict, budget_end: float, cancel: threading.Event | None
) -> tuple[Gateway | None, dict | None]:
    """Choisit entre plusieurs passerelles validées, avant toute écriture (P9F2, C3/C4).

    Règle : l'adresse externe de chacune est lue (lecture seule) ; est retenue l'unique
    passerelle dont l'adresse est publique, si celle de chacune des autres a été lue — IPv4
    pointée valide — et n'est pas publique. Une adresse absente, vide ou mal formée n'est pas lue. Sinon l'ambiguïté n'est pas résolue : `no_gateway` (`ambiguous_gateway`).
    Les adresses lues ne sont ni rendues ni consignées (R5).
    """
    public: list[Gateway] = []
    for gateway in gateways:
        if _stopped(cancel):
            return _no_gateway(CANCELLED)
        remaining = budget_end - time.monotonic()
        if remaining <= 0:
            return _no_gateway(AMBIGUOUS)
        answer = _soap(
            gateway,
            "GetExternalIPAddress",
            lambda _local: [],
            min(cfg["request_timeout_s"], remaining),
            cfg["max_response_bytes"],
        )
        if answer.failure is not None:
            return _no_gateway(AMBIGUOUS)
        # Une adresse absente, vide ou mal formée n'est pas lue : elle ne classe pas la passerelle
        # comme non publique, l'ambiguïté reste non résolue (P9F2, revue A1).
        value = answer.values.get("NewExternalIPAddress", "")
        if _ipv4(value) is None:
            return _no_gateway(AMBIGUOUS)
        if _public(value):
            public.append(gateway)
    if len(public) != 1:
        return _no_gateway(AMBIGUOUS)
    return public[0], None


def find_gateway(
    cfg: dict,
    target: tuple[str, int] = (SSDP_GROUP, SSDP_PORT),
    cancel: threading.Event | None = None,
) -> tuple[Gateway | None, dict | None]:
    """Découvre la box et lit sa description : `(Gateway, None)` ou `(None, résultat no_gateway)`.

    `cfg` est la section `upnp` de `runtime.json`. Les réponses SSDP compatibles sont reçues
    jusqu'à l'échéance absolue de la fenêtre de découverte, puis essayées dans leur ordre
    d'arrivée (C4) ; le tout tient dans `discovery_timeout_s + request_timeout_s`, chaque requête
    étant aussi bornée par `request_timeout_s`. Une passerelle validée seule est retenue ;
    plusieurs passent par `_select`, et un candidat laissé sans essai faute de budget rend
    l'ambiguïté non résolue (P9F2). Sans passerelle, la raison est celle du dernier candidat
    essayé, ou `no_reply`. `cancel`, levé, arrête la réception et empêche toute requête suivante
    (raison `cancelled`). Aucune exception ne sort.
    """
    try:
        window_end = time.monotonic() + cfg["discovery_timeout_s"]
        budget_end = window_end + cfg["request_timeout_s"]
        found = _collect(target, cfg, window_end, cancel)
        reason = "no_reply"
        gateways: list[Gateway] = []
        for responder_ip, location in found:
            if _stopped(cancel):
                return _no_gateway(CANCELLED)
            remaining = budget_end - time.monotonic()
            if remaining <= 0:
                if gateways:
                    return _no_gateway(AMBIGUOUS)
                break
            gateway, reason = _candidate(
                responder_ip, location, min(cfg["request_timeout_s"], remaining), cfg["max_response_bytes"]
            )
            if gateway is not None and gateway not in gateways:
                gateways.append(gateway)
        if _stopped(cancel):
            return _no_gateway(CANCELLED)
        if not gateways:
            return _no_gateway(reason)
        if len(gateways) == 1:
            return gateways[0], None
        return _select(gateways, cfg, budget_end, cancel)
    except (*PARSE_ERRORS, TypeError) as exc:
        return _no_gateway(type(exc).__name__)


# --- configuration et consigne manuelle --------------------------------------------

Couple = tuple[int, str]


def _couples(ports: object) -> list[Couple] | None:
    """Couples `(port, protocole)` de `ports`, ou `None` si `ports` ne se lit pas ainsi (C7).

    Seule la forme est vérifiée ici (un entier qui n'est pas un booléen, une chaîne) ; les
    valeurs le sont par `_ports_ok`. Aucune exception ne sort.
    """
    try:
        couples = [tuple(item) for item in ports]  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - un itérable quelconque peut lever n'importe quoi
        return None
    for couple in couples:
        if len(couple) != 2:
            return None
        port, protocol = couple
        if not isinstance(port, int) or isinstance(port, bool) or not isinstance(protocol, str):
            return None
    return couples  # type: ignore[return-value]


def _as_dicts(couples: Iterable[Couple]) -> list[dict]:
    return [{"port": port, "protocol": protocol} for port, protocol in couples]


def manual_instruction(ports: Iterable[Sequence[object]]) -> str:
    """Consigne manuelle (anglais) qui nomme chaque port et son protocole (R4, AC15)."""
    named = ", ".join(f"{port}/{protocol}" for port, protocol in _couples(ports) or [])
    return (
        f"Open these ports by hand in your router's settings and forward them to this computer: "
        f"{named}. Then enter your public address in the field below."
    )


def _invalid(key: str, ports: Iterable[Sequence[object]] | None) -> dict:
    result: dict = {"status": INVALID_CONFIG, "key": key}
    couples = None if ports is None else _couples(ports)
    if couples:
        result["ports"] = _as_dicts(couples)
        result["manual"] = manual_instruction(couples)
    return result


def _positive_number(value: object) -> bool:
    # `1e999` se lit `inf` dans runtime.json : un délai infini ou NaN est refusé ici (R3). Un
    # entier trop grand pour un flottant fait lever `isfinite` : refusé lui aussi (C7).
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _wait(value: object) -> bool:
    # Représentable par les sockets et par `Thread.join`, même additionné à l'autre (P9F2).
    return _positive_number(value) and value <= MAX_WAIT_S  # type: ignore[operator]


def _discovery_window(value: object) -> bool:
    # Une fenêtre plus courte que `MX_MIN` se fermerait avant les réponses différées (C8).
    return _wait(value) and value >= MX_MIN  # type: ignore[operator]


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(upnp_cfg: object, ports: Iterable[Sequence[object]] | None = None) -> dict | None:
    """`None` si la section `upnp` est utilisable, sinon le résultat `invalid_config` (R3, AC13).

    Aucun trafic : section absente ou d'un autre type → clé `upnp` ; clé absente ou mal typée,
    bail hors de ]0, MAX_LEASE_S], fenêtre de découverte de moins de `MX_MIN` seconde, délai
    au-delà de `MAX_WAIT_S` ou taille au-delà de `sys.maxsize` (non représentables) → clé
    `upnp.<nom>`. Avec `ports`, le résultat porte la
    consigne manuelle (R4).
    """
    if not isinstance(upnp_cfg, dict):
        return _invalid("upnp", ports)
    checks: dict[str, Callable[[object], bool]] = {
        "discovery_timeout_s": _discovery_window,
        "request_timeout_s": _wait,
        "lease_s": lambda v: _positive_int(v) and v <= MAX_LEASE_S,  # type: ignore[operator]
        "description": lambda v: isinstance(v, str) and bool(v.strip()),
        # Représentable comme taille C (`Py_ssize_t`) : au-delà, toute API de lecture lève (P9F2).
        "max_response_bytes": lambda v: _positive_int(v) and v <= sys.maxsize,  # type: ignore[operator]
    }
    for key in CONFIG_KEYS:
        if key not in upnp_cfg or not checks[key](upnp_cfg[key]):
            return _invalid(f"upnp.{key}", ports)
    return None


def _couple_ok(couple: Couple) -> bool:
    port, protocol = couple
    return isinstance(port, int) and not isinstance(port, bool) and port in PORT_RANGE and protocol in PROTOCOLS


def _ports_ok(couples: list | None) -> bool:
    """Trois couples distincts, chacun valide ; les types sont vérifiés avant le `set` (C7)."""
    if couples is None or len(couples) != MAPPING_COUNT or not all(map(_couple_ok, couples)):
        return False
    return len(set(couples)) == len(couples)


# --- adresse publique ---------------------------------------------------------------


def _ipv4(addr: object) -> ipaddress.IPv4Address | None:
    """L'IPv4 pointée canonique que `addr` écrit exactement, ou `None` (vide, absente, mal formée)."""
    if not isinstance(addr, str) or not addr or addr.strip() != addr:
        return None
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return None
    return ip if str(ip) == addr else None


def _public(addr: object) -> bool:
    """Vrai seulement pour une IPv4 pointée, globale, hors des blocs de `NOT_PUBLIC_NETS` (R5)."""
    ip = _ipv4(addr)
    if ip is None or not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    return not any(ip in net for net in NOT_PUBLIC_NETS)


# --- SOAP -----------------------------------------------------------------------------


@dataclass
class Answer:
    """Réponse SOAP : `failure` vaut `None` (succès), `upnp_error`, `timeout` ou `unreadable`."""

    failure: str | None
    code: int | None = None
    text: str = ""
    values: dict[str, str] = field(default_factory=dict)

    @property
    def gone(self) -> bool:
        """Pour une suppression : la box confirme le mapping absent (succès ou 714)."""
        return self.failure is None or (self.failure == UPNP_ERROR and self.code == NO_SUCH_ENTRY)


Args = Callable[[str], list[tuple[str, object]]]


def _envelope(service_type: str, action: str, args: list[tuple[str, object]]) -> bytes:
    inner = "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in args)
    return (
        '<?xml version="1.0"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENV}" s:encodingStyle="{SOAP_ENCODING}">'
        f'<s:Body><u:{action} xmlns:u="{SERVICE_PREFIX}{service_type}">{inner}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")


def _read_answer(action: str, reply: Reply) -> Answer:
    if reply.failure is not None:
        return Answer(reply.failure)
    if any(marker in reply.body.lower() for marker in FORBIDDEN_MARKUP):
        return Answer(UNREADABLE)
    try:
        root = _parse(reply.body)
    except PARSE_ERRORS:
        return Answer(UNREADABLE)
    if reply.status == http.client.OK:
        for element in root.iter():
            if _local_name(element.tag) == f"{action}Response":
                return Answer(None, values={_local_name(c.tag): (c.text or "").strip() for c in element})
        return Answer(UNREADABLE)
    for element in root.iter():
        if _local_name(element.tag) != "UPnPError":
            continue
        fields = {_local_name(c.tag): (c.text or "").strip() for c in element}
        try:
            code = int(fields.get("errorCode", ""))
        except ValueError:
            return Answer(UNREADABLE)
        return Answer(UPNP_ERROR, code, fields.get("errorDescription", ""))
    return Answer(UNREADABLE)


def _soap(gateway: Gateway, action: str, args: Args, timeout_s: float, max_bytes: int) -> Answer:
    """Un appel SOAP ; `args` reçoit l'adresse locale de la socket. Aucune exception ne sort."""
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{SERVICE_PREFIX}{gateway.service_type}#{action}"',
    }
    try:
        reply = _fetch(
            gateway.host,
            gateway.port,
            gateway.control_path,
            lambda local: _envelope(gateway.service_type, action, args(local)),
            timeout_s,
            max_bytes,
            headers,
        )
        return _read_answer(action, reply)
    except (*PARSE_ERRORS, TypeError) as exc:
        log.info("upnp: %s failed (%s)", action, type(exc).__name__)
        return Answer(UNREADABLE)


def _delete(gateway: Gateway, couple: Couple, timeout_s: float, max_bytes: int) -> Answer:
    port, protocol = couple
    args = [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", protocol)]
    return _soap(gateway, "DeletePortMapping", lambda _local: args, timeout_s, max_bytes)


def _add(gateway: Gateway, couple: Couple, cfg: dict) -> Answer:
    port, protocol = couple

    def args(local: str) -> list[tuple[str, object]]:
        return [
            ("NewRemoteHost", ""),
            ("NewExternalPort", port),
            ("NewProtocol", protocol),
            ("NewInternalPort", port),
            ("NewInternalClient", local),
            ("NewEnabled", 1),
            ("NewPortMappingDescription", cfg["description"]),
            ("NewLeaseDuration", cfg["lease_s"]),
        ]

    return _soap(gateway, "AddPortMapping", args, cfg["request_timeout_s"], cfg["max_response_bytes"])


# --- ouverture -------------------------------------------------------------------------


def _stopped(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _rollback(
    gateway: Gateway, possibly: list[Couple], cfg: dict, cancel: threading.Event | None
) -> tuple[list[Couple], list[Couple]]:
    """Retrait R3 : `(encore réputés actifs, supprimés confirmés)`.

    Au plus une suppression par mapping possiblement posé ; la première suppression dont le
    délai expire arrête le retrait, et ce mapping comme les suivants restent réputés actifs.
    Une suppression refusée ou illisible laisse son mapping réputé actif et le retrait continue.
    """
    still: list[Couple] = []
    removed: list[Couple] = []
    for i, couple in enumerate(possibly):
        if _stopped(cancel):
            still.extend(possibly[i:])
            break
        answer = _delete(gateway, couple, cfg["request_timeout_s"], cfg["max_response_bytes"])
        if answer.gone:
            removed.append(couple)
        elif answer.failure == TIMEOUT:
            still.extend(possibly[i:])
            break
        else:
            still.append(couple)
    return still, removed


def _refused(couples: list[Couple], reason: dict, still: list[Couple]) -> dict:
    result = {"status": REFUSED, "ports": _as_dicts(couples), **reason}
    result["still_active"] = _as_dicts(still)
    result["manual"] = manual_instruction(couples)
    return result


def _mapped(couples: list[Couple], cfg: dict, address: str | None, note: str | None) -> dict:
    result = {"status": MAPPED, "ports": _as_dicts(couples), "lease_s": cfg["lease_s"], "address": address}
    if address is None:
        result["address_note"] = note
    return result


def _external_address(gateway: Gateway, cfg: dict) -> tuple[str | None, str]:
    """`(adresse publique ou None, note)` ; l'adresse lue n'est jamais consignée (R5)."""
    answer = _soap(
        gateway,
        "GetExternalIPAddress",
        lambda _local: [],
        cfg["request_timeout_s"],
        cfg["max_response_bytes"],
    )
    if answer.failure is not None:
        return None, NOTE_UNKNOWN
    value = answer.values.get("NewExternalIPAddress", "")
    if not value:
        return None, NOTE_UNKNOWN
    if _public(value):
        return value, ""
    return None, NOTE_NOT_PUBLIC


def _open(
    gateway: Gateway, cfg: dict, couples: list[Couple], cancel: threading.Event | None
) -> tuple[dict, list[Couple]]:
    timeout_s, max_bytes = cfg["request_timeout_s"], cfg["max_response_bytes"]
    confirmed: list[Couple] = []
    cleared: list[Couple] = []

    def abandon(possibly: list[Couple], reason: dict) -> tuple[dict, list[Couple]]:
        still, removed = _rollback(gateway, possibly, cfg, cancel)
        cleared.extend(c for c in removed if c not in cleared)
        return _refused(couples, reason, still), cleared

    for couple in couples:
        if _stopped(cancel):
            return _refused(couples, {"failure": CANCELLED}, confirmed), cleared
        answer = _delete(gateway, couple, timeout_s, max_bytes)
        if answer.failure == TIMEOUT:
            return abandon(confirmed, {"failure": TIMEOUT})
        if answer.gone:
            cleared.append(couple)
        if _stopped(cancel):
            return _refused(couples, {"failure": CANCELLED}, confirmed), cleared
        answer = _add(gateway, couple, cfg)
        if answer.failure is None:
            confirmed.append(couple)
            if couple in cleared:
                cleared.remove(couple)
            continue
        if answer.failure == UPNP_ERROR:
            return abandon(confirmed, {"error_code": answer.code, "error_text": answer.text})
        # délai dépassé ou réponse illisible : l'ajout a pu être posé
        if couple in cleared:
            cleared.remove(couple)
        return abandon([*confirmed, couple], {"failure": answer.failure})

    if _stopped(cancel):
        return _mapped(couples, cfg, None, NOTE_UNKNOWN), cleared
    address, note = _external_address(gateway, cfg)
    return _mapped(couples, cfg, address, note), cleared


def open_ports(
    cfg: object,
    ports: Iterable[Sequence[object]],
    target: tuple[str, int] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[dict, Gateway | None, list[Couple]]:
    """Ouvre les trois mappings de `ports` : `(résultat, passerelle ou None, couples effacés)`.

    `cfg` est la section `upnp` de `runtime.json`, `ports` les trois couples
    `(port, protocole)` fournis par l'appelant, `target` la cible de la découverte (`None` :
    le groupe SSDP). `cancel`, levé, arrête l'ouverture à la frontière de requête suivante.
    `cleared` liste les couples dont cette ouverture a confirmé l'absence sur cette passerelle.
    Aucune exception ne sort.
    """
    couples = _couples(ports)
    invalid = validate(cfg, couples)
    if invalid is not None:
        return invalid, None, []
    if not _ports_ok(couples):
        return _invalid("ports", couples), None, []
    assert isinstance(cfg, dict) and couples is not None
    try:
        if _stopped(cancel):
            _, failure = _no_gateway(CANCELLED)
            gateway = None
        else:
            gateway, failure = find_gateway(cfg, target or (SSDP_GROUP, SSDP_PORT), cancel)
        if gateway is None:
            result = {**(failure or {"status": NO_GATEWAY})}
            result["ports"] = _as_dicts(couples)
            result["manual"] = manual_instruction(couples)
            return result, None, []
        result, cleared = _open(gateway, cfg, couples, cancel)
    except (*PARSE_ERRORS, TypeError, KeyError) as exc:
        # `PARSE_ERRORS` couvre `ArithmeticError` : une erreur numérique devient un résultat.
        log.info("upnp: opening failed (%s)", type(exc).__name__)
        result = {"status": NO_GATEWAY, "reason": UNREADABLE, "ports": _as_dicts(couples)}
        result["manual"] = manual_instruction(couples)
        return result, None, []
    log.info("upnp: opening ended (%s)", result["status"])
    return result, gateway, cleared


# --- arrêt -----------------------------------------------------------------------------


def _still_ours(alive: Callable[[Couple], bool] | None, couple: Couple) -> bool:
    if alive is None:
        return True
    try:
        return bool(alive(couple))
    except Exception:  # noqa: BLE001 - sans preuve que le bail court, on ne supprime pas
        return False


def delete_mappings(
    gateway: Gateway,
    mappings: Iterable[Sequence[object]],
    request_timeout_s: float,
    *,
    max_bytes: int,
    alive: Callable[[Couple], bool] | None = None,
) -> list[Couple]:
    """Retrait de l'arrêt (R6) : une tentative par mapping retenu ; rend les couples en échec.

    Ni découverte ni description : la passerelle est celle gardée de l'ouverture. Contrairement
    au retrait R3, une expiration (ou une erreur UPnP, ou une réponse illisible) sur un mapping
    est notée et le mapping suivant est tenté quand même. `alive`, consulté **immédiatement
    avant** chaque tentative, dit si le bail de ce couple court encore ; faux (ou en erreur), le
    couple n'est pas supprimé, puisqu'il a pu être repris par un tiers sur la box (M1), et il ne
    compte pas parmi les échecs. Aucune exception ne sort.
    """
    failures: list[Couple] = []
    try:
        items = list(mappings)
    except Exception:  # noqa: BLE001 - l'arrêt ne doit jamais lever
        log.info("upnp: shutdown delete given no mappings")
        return failures
    for item in items:
        couples = _couples([item])
        if not couples or not _couple_ok(couples[0]):
            # Un couple malformé ne part pas : il compte parmi les échecs (C7).
            failures.append(item)  # type: ignore[arg-type]
            continue
        couple = couples[0]
        if not _still_ours(alive, couple):
            continue
        try:
            answer = _delete(gateway, couple, request_timeout_s, max_bytes)
        except Exception as exc:  # noqa: BLE001 - l'arrêt ne doit jamais lever
            log.info("upnp: shutdown delete failed (%s)", type(exc).__name__)
            answer = Answer(UNREADABLE)
        if not answer.gone:
            failures.append(couple)
    return failures
