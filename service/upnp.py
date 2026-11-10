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
s'arrête lui aussi à la première expiration.

Suivi de vie (P9F3) : `Ledger` est l'unique propriétaire des mappings retenus — une entrée par
box (`Gateway.box`, l'hôte qui a répondu : deux descriptions d'une même box sont une seule box)
et par couple, avec son échéance sur une horloge monotone et son état « à nous » (bail en cours)
ou « peut-être à un tiers » (bail échu). La vue (proposition encore valable) et le retrait de
l'arrêt (R6) se décident par lui. Ce retrait tente chaque mapping retenu une fois, quoi qu'il soit
arrivé aux précédents, sous une échéance unique : supprimé directement s'il est à nous ; s'il est
peut-être à un tiers, supprimé seulement si la box (`GetSpecificPortMappingEntry`) le rend encore
pointé vers nous avec notre description — une box qui ignore le bail ne garde donc pas le mapping
pour toujours, et un couple repris par un tiers n'est pas touché.

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

    @property
    def box(self) -> str:
        """Identité stable de la box : l'hôte qui a répondu (P9F3).

        Le port et le chemin de contrôle ne disent que par où la joindre : une même box qui
        publie deux descriptions (IGD:1 et IGD:2, deux appareils racine) reste une seule box.
        """
        return self.host


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
        # Un port explicite à 0 n'est pas « absent » : il rend l'URL inutilisable (revue I6).
        port = http.client.HTTP_PORT if parts.port is None else parts.port
        if port not in PORT_RANGE:
            return None
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


def _box_of(responder_ip: str) -> str:
    """La box d'un répondeur SSDP : son adresse IP canonique, celle que portera `Gateway.box`."""
    ip = _ip_literal(responder_ip)
    return responder_ip if ip is None else str(ip)


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
    """Choisit entre plusieurs box validées, avant toute écriture (P9F2, C3/C4).

    `gateways` porte une passerelle par box (`Gateway.box`, P9F3) : seules des box réellement
    différentes, jamais deux descriptions d'une même box, sont départagées ici. Règle : l'adresse externe de chacune est lue (lecture seule) ; est retenue l'unique
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
        # Une passerelle par box (P9F3) : une fois une description d'un hôte validée, les autres
        # descriptions du même hôte ne sont ni lues ni comptées comme une seconde passerelle.
        gateways: dict[str, Gateway] = {}
        for responder_ip, location in found:
            if _stopped(cancel):
                return _no_gateway(CANCELLED)
            if _box_of(responder_ip) in gateways:
                continue
            remaining = budget_end - time.monotonic()
            if remaining <= 0:
                if gateways:
                    return _no_gateway(AMBIGUOUS)
                break
            gateway, reason = _candidate(
                responder_ip, location, min(cfg["request_timeout_s"], remaining), cfg["max_response_bytes"]
            )
            if gateway is not None:
                gateways.setdefault(gateway.box, gateway)
        if _stopped(cancel):
            return _no_gateway(CANCELLED)
        if not gateways:
            return _no_gateway(reason)
        if len(gateways) == 1:
            return next(iter(gateways.values())), None
        return _select(list(gateways.values()), cfg, budget_end, cancel)
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
    # Adresse locale de la socket de la requête : celle que la box voit comme client (P9F3).
    local: str | None = None

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
        answer = _read_answer(action, reply)
        answer.local = reply.local_address
        return answer
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


# --- suivi de vie des mappings (P9F3) ----------------------------------------------------
#
# `Ledger` est le seul propriétaire du suivi de vie d'un mapping posé par le service : ce qui est
# retenu, sur quelle box, jusqu'à quand il est sûrement à nous, ce que la vue peut encore
# proposer, et comment il est retiré à l'arrêt. Le moteur ne recopie aucune de ces conditions :
# il lui passe le résultat d'une ouverture, puis lui demande la vue et le retrait.

OURS = "ours"
MAYBE_THIRD_PARTY = "maybe_third_party"
PROPOSAL_NONE = "none"
PROPOSAL_LIVE = "live"
PROPOSAL_EXPIRED = "expired"
# Issues de la vérification d'un mapping peut-être repris par un tiers.
STILL_OURS = "still_ours"
GONE = "gone"
OTHER_OWNER = "other_owner"


@dataclass
class Lease:
    """Un mapping retenu : sa box, son couple, par où la joindre, et son échéance.

    `deadline` (horloge monotone du registre) borne l'intervalle où le mapping est sûrement à
    nous : heure du clic + `lease_s`, jamais postérieure à l'expiration réelle (C6), et insensible
    aux sauts de l'heure murale (revue A2/I2). `expires_at` (heure murale) ne sert qu'à l'affichage.
    `description` est celle posée par l'ajout : elle sert à reconnaître le mapping comme le nôtre.
    """

    box: str
    couple: Couple
    gateway: Gateway
    deadline: float
    expires_at: float
    description: str


@dataclass
class Released:
    """Issue d'un retrait : couples tentés, en échec, et laissés à un autre client de la box."""

    tried: list[Couple] = field(default_factory=list)
    failed: list[Couple] = field(default_factory=list)
    foreign: list[Couple] = field(default_factory=list)

    def extend(self, other: Released) -> None:
        self.tried.extend(other.tried)
        self.failed.extend(other.failed)
        self.foreign.extend(other.foreign)


def deadline_in(seconds: float) -> float:
    """Échéance réseau (monotone, celle des requêtes de ce module) dans `seconds` secondes."""
    return time.monotonic() + seconds


def _is_address(value: str | None) -> bool:
    try:
        ipaddress.ip_address(value or "")
    except ValueError:
        return False
    return True


def _is_port(value: str | None) -> bool:
    return bool(value) and value.isascii() and value.isdigit() and 0 < int(value) < 65536


def _owner(gateway: Gateway, lease: Lease, timeout_s: float, max_bytes: int) -> str:
    """Demande à la box à qui est le couple de `lease` : `STILL_OURS`, `GONE`, `OTHER_OWNER`,
    ou l'échec de la requête (`timeout`, `unreadable`, `upnp_error`).

    Lecture seule (`GetSpecificPortMappingEntry`). Le mapping est encore le nôtre si la box le
    rend pointé sur l'adresse locale de cette requête (celle qu'elle voit pour nous, comme
    `NewInternalClient` de l'ajout), au même port interne, avec notre description.
    """
    port, protocol = lease.couple
    args = [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", protocol)]
    answer = _soap(gateway, "GetSpecificPortMappingEntry", lambda _local: args, timeout_s, max_bytes)
    if answer.failure == UPNP_ERROR and answer.code == NO_SUCH_ENTRY:
        return GONE
    if answer.failure is not None:
        return answer.failure
    values = answer.values
    client = values.get("NewInternalClient")
    internal = values.get("NewInternalPort")
    description = values.get("NewPortMappingDescription")
    # Une réponse qui ne nomme pas le propriétaire, ou le nomme mal (client qui n'est pas une
    # adresse, port interne qui n'est pas un port), ne prouve pas un autre client : elle compte
    # comme un échec, sans suppression (revue P9F3 A3).
    if answer.local is None or not _is_address(client) or not _is_port(internal) or description is None:
        return UNREADABLE
    # `_read_answer` retire les blancs autour de chaque champ : la description retenue est
    # comparée de même (revue P9F3 A2), sinon un mapping à nous passerait pour celui d'un tiers.
    ours = (
        client == answer.local
        and internal == str(port)
        and description == lease.description.strip()
    )
    return STILL_OURS if ours else OTHER_OWNER


class Ledger:
    """Registre des mappings posés par le service : l'unique primitive de leur suivi de vie.

    Une entrée par box (`Gateway.box`, l'hôte de la passerelle) et par couple (port, protocole) :
    une ouverture sur une autre box n'efface pas les mappings de la première (C2), et deux
    descriptions d'une même box ne font qu'une box (P9F3). Chaque entrée est dans l'un de deux
    états, lu sur l'horloge monotone `clock` :

    - `OURS` : avant son échéance, le bail que nous avons demandé court encore ; la box ne peut
      pas avoir rendu le couple à un tiers. L'arrêt le supprime directement ;
    - `MAYBE_THIRD_PARTY` : passé l'échéance, deux cas sont indiscernables sans la box — elle a
      honoré le bail et un tiers a pu reprendre le couple, ou elle l'a ignoré et le mapping est
      toujours le nôtre, ouvert pour toujours. L'arrêt ne renonce pas : il demande à la box
      (`GetSpecificPortMappingEntry`) et ne supprime que si elle le rend encore pointé vers nous
      avec notre description ; absent, il n'y a rien à faire ; à un autre client, il est laissé ;
      une réponse qui ne permet pas de trancher compte comme un échec, nommé, sans suppression.

    `record` fusionne une ouverture, `proposal` dit si sa proposition `mapped` vaut encore, et
    `release` retire tout ce qui est retenu sous une échéance réseau unique (R6). Sûr entre fils.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, Couple], Lease] = {}
        self._proposal_deadline: float | None = None

    # -- lecture -------------------------------------------------------------------------
    def state(self, lease: Lease) -> str:
        return OURS if self.clock() < lease.deadline else MAYBE_THIRD_PARTY

    def entries(self) -> list[Lease]:
        with self._lock:
            return list(self._entries.values())

    def proposal(self) -> str:
        """`PROPOSAL_NONE` sans ouverture `mapped`, sinon si la dernière vaut encore."""
        with self._lock:
            deadline = self._proposal_deadline
        if deadline is None:
            return PROPOSAL_NONE
        return PROPOSAL_LIVE if self.clock() < deadline else PROPOSAL_EXPIRED

    # -- écriture ------------------------------------------------------------------------
    def record(
        self,
        result: dict,
        gateway: Gateway | None,
        cleared: Iterable[Sequence[object]],
        *,
        started: float,
        expires_at: float,
        lease_s: float,
        description: str,
    ) -> None:
        """Fusionne l'issue d'une ouverture ; ne remplace jamais une autre box (C2).

        `started` est l'heure du clic sur `clock`, `expires_at` l'heure murale affichée. Les
        couples dont l'ouverture a confirmé l'absence (`cleared`) quittent la box ; ceux encore
        réputés actifs (`mapped` : les trois ; `refused` : `still_active`) y entrent. Un
        résultat `mapped` devient la proposition courante, quel que soit le résultat suivant.
        """
        status = result.get("status")
        deadline = started + lease_s
        if status == MAPPED:
            active = result.get("ports", [])
        elif status == REFUSED:
            active = result.get("still_active", [])
        else:
            active = []
        couples: list[Couple] = []
        for item in active if isinstance(active, list) else []:
            if isinstance(item, dict):
                couples.extend(_couples([(item.get("port"), item.get("protocol"))]) or [])
        with self._lock:
            if status == MAPPED:
                self._proposal_deadline = deadline
            if gateway is None:
                return
            for item in cleared:
                for couple in _couples([item]) or []:
                    self._entries.pop((gateway.box, couple), None)
            for couple in couples:
                if _couple_ok(couple):
                    self._entries[(gateway.box, couple)] = Lease(
                        gateway.box, couple, gateway, deadline, expires_at, description
                    )

    def retain(
        self,
        gateway: Gateway,
        couples: Iterable[Sequence[object]],
        *,
        started: float,
        expires_at: float,
        lease_s: float,
        description: str,
    ) -> None:
        """Retient `couples` comme possiblement posés sur `gateway` (filet d'une fusion en échec)."""
        dicts = [{"port": c[0], "protocol": c[1]} for c in _couples(couples) or []]
        self.record(
            {"status": REFUSED, "still_active": dicts},
            gateway,
            [],
            started=started,
            expires_at=expires_at,
            lease_s=lease_s,
            description=description,
        )

    def suspect(
        self,
        gateway: Gateway,
        couples: Iterable[Sequence[object]],
        *,
        expires_at: float,
        description: str,
    ) -> None:
        """Retient `couples` comme peut-être posés sur `gateway`, sans jamais les présumer à nous.

        Filet d'une fusion en échec (revue P9F3 A1) : l'issue réelle de l'ouverture est inconnue,
        et un couple jamais touché peut appartenir à un autre appareil. Chaque couple entre donc
        déjà échu (`MAYBE_THIRD_PARTY`) : l'arrêt demande à la box à qui il est avant de le
        supprimer. Un couple déjà retenu (par exemple par un `record` passé avant l'échec) garde
        son entrée.
        """
        with self._lock:
            for couple in _couples(couples) or []:
                key = (gateway.box, couple)
                if _couple_ok(couple) and key not in self._entries:
                    self._entries[key] = Lease(
                        gateway.box, couple, gateway, -math.inf, expires_at, description
                    )

    def release(self, request_timeout_s: float, max_bytes: int, deadline: float) -> Released:
        """Retrait de l'arrêt (R6) : prend tout ce qui est retenu, une tentative par mapping.

        Ni découverte ni description : chaque box est jointe par la passerelle gardée de son
        ouverture. Une expiration n'arrête pas les mappings suivants (finding P2), mais toutes les
        requêtes, toutes box confondues, tiennent sous `deadline` (monotone, `deadline_in`) :
        chacune est bornée par `min(request_timeout_s, reste)`, et un mapping atteint après
        l'échéance n'est pas tenté et compte parmi les échecs. Aucune exception ne sort.
        """
        with self._lock:
            taken = list(self._entries.values())
            self._entries.clear()
        outcome = Released()
        for lease in taken:
            outcome.tried.append(lease.couple)
            try:
                verdict = self._release_one(lease, request_timeout_s, max_bytes, deadline)
            except Exception as exc:  # noqa: BLE001 - l'arrêt ne doit jamais lever
                log.info("upnp: shutdown delete failed (%s)", type(exc).__name__)
                verdict = UNREADABLE
            if verdict == OTHER_OWNER:
                outcome.foreign.append(lease.couple)
            elif verdict != GONE:
                outcome.failed.append(lease.couple)
        return outcome

    def _release_one(self, lease: Lease, request_timeout_s: float, max_bytes: int, deadline: float) -> str:
        def budget() -> float | None:
            remaining = deadline - time.monotonic()
            return None if remaining <= 0 else min(request_timeout_s, remaining)

        timeout_s = budget()
        if timeout_s is None:
            return TIMEOUT
        if self.state(lease) == MAYBE_THIRD_PARTY:
            owner = _owner(lease.gateway, lease, timeout_s, max_bytes)
            if owner != STILL_OURS:
                return owner
            timeout_s = budget()
            if timeout_s is None:
                return TIMEOUT
        answer = _delete(lease.gateway, lease.couple, timeout_s, max_bytes)
        return GONE if answer.gone else (answer.failure or UNREADABLE)


# --- arrêt -----------------------------------------------------------------------------


def delete_mappings(
    gateway: Gateway,
    mappings: Iterable[Sequence[object]],
    request_timeout_s: float,
    *,
    max_bytes: int,
) -> list[Couple]:
    """Retrait R6 de `mappings`, tous réputés à nous, par le `Ledger` : rend les couples en échec.

    Une tentative par couple, une expiration n'arrêtant pas les suivants, sous une échéance
    unique d'une expiration de requête par couple. Un élément qui n'est pas un couple valide
    n'est jamais envoyé et ne figure pas parmi les échecs, qui ne portent que des couples
    `(port, protocole)` (revue A6) : il est seulement consigné. Aucune exception ne sort.
    """
    try:
        items = list(mappings)
    except Exception:  # noqa: BLE001 - l'arrêt ne doit jamais lever
        log.info("upnp: shutdown delete given no mappings")
        return []
    couples: list[Couple] = []
    for item in items:
        found = _couples([item])
        if found and _couple_ok(found[0]):
            couples.append(found[0])
        else:
            log.info("upnp: malformed mapping ignored at shutdown")
    ledger = Ledger(clock=lambda: 0.0)
    ledger.retain(
        gateway, couples, started=0.0, expires_at=0.0, lease_s=math.inf, description=""
    )
    released = ledger.release(
        request_timeout_s, max_bytes, deadline_in(request_timeout_s * max(len(couples), 1))
    )
    return released.failed
