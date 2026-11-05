"""Contrat de transport v1 : validation pure des lignes et sérialisation.

Aucune fonction de ce module ne touche au disque ni à l'horloge. Chaque `parse_*`
prend une ligne (`bytes` ou `str`) ou un objet déjà décodé, rend l'objet tel quel,
champs inconnus compris, et lève `ProtocolError` s'il sort du contrat.

Dans `data`, seuls les champs lus par le service sont contrôlés : un contrôle plus
large rejetterait des lignes réelles. Les bornes ci-dessous sont celles du contrat
(`seq` part de 1, un plancher vaut 0 avant toute commande), pas des réglages.

Les champs inconnus sont préservés jusqu'au journal : ils y doivent au moins rester
sérialisables et relisibles. `_check_bounds` borne donc toute la ligne, sans regarder
ce que ses champs signifient.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

PROTOCOL_VERSION = 1
SIDES = ("A", "B")
CONTROL_KINDS = ("player_death", "caps", "hook_seen", "cmd_result", "beat")
ZOMBIE_ORIGINS = ("duel", "world")
CMD_STATUSES = ("applied", "refused", "failed")

BAD_JSON = "bad_json"
NOT_OBJECT = "not_object"
BAD_VERSION = "bad_version"
MISSING_FIELD = "missing_field"
BAD_FIELD = "bad_field"
UNKNOWN_KIND = "unknown_kind"
SIDE_MISMATCH = "side_mismatch"

# Plus grand entier exact d'un nombre Lua : au-delà, le mod n'a pas pu écrire une mesure.
MAX_NUMBER = 2**53
# Une ligne réelle tient sur quatre niveaux. La copie et le journal descendent récursivement.
MAX_DEPTH = 16


class ProtocolError(ValueError):
    """Ligne hors contrat : `code` est stable, `detail` est lisible en français."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _reject_constant(name: str) -> None:
    raise ValueError(f"constante non JSON : {name}")


def _check_bounds(value: Any, depth: int = 0) -> None:
    """Refuse, à toute profondeur, ce que le journal ne saurait écrire puis relire.

    `1e999` se décode en infini sans passer par `parse_constant`, et `dump_line` le
    refuserait une fois la ligne acceptée. Une comparaison ne déborde pas, même sur un
    entier géant, et elle est fausse pour NaN.
    """
    if depth > MAX_DEPTH:
        raise _bad("la ligne", f"imbriquée sur au plus {MAX_DEPTH} niveaux")
    if isinstance(value, Mapping):
        children: Iterable[Any] = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        if type(value) in (int, float) and not abs(value) <= MAX_NUMBER:
            raise _bad("tout nombre", f"fini et borné par {MAX_NUMBER} en valeur absolue")
        return
    for child in children:
        _check_bounds(child, depth + 1)


def _load(raw: bytes | str | Mapping[str, Any]) -> Mapping[str, Any]:
    """Décode une ligne et vérifie la version, avant tout autre contrôle."""
    obj: Any = raw
    if isinstance(raw, (bytes, str)):
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            obj = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            raise ProtocolError(BAD_JSON, f"ligne illisible : {exc}") from exc
    if not isinstance(obj, Mapping):
        raise ProtocolError(NOT_OBJECT, "la ligne n'est pas un objet JSON")
    version = obj.get("v")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ProtocolError(BAD_VERSION, f"`v` doit valoir {PROTOCOL_VERSION}")
    _check_bounds(obj)
    return obj


def _get(obj: Mapping[str, Any], key: str) -> Any:
    if key not in obj:
        raise ProtocolError(MISSING_FIELD, f"champ `{key}` absent")
    return obj[key]


def _bad(key: str, expected: str) -> ProtocolError:
    return ProtocolError(BAD_FIELD, f"`{key}` doit être {expected}")


def _check_int(value: Any, key: str, minimum: int) -> None:
    # `type` et non `isinstance` : un booléen n'est pas un entier du contrat.
    if type(value) is not int or value < minimum:
        raise _bad(key, f"un entier au moins égal à {minimum}")


def _check_number(value: Any, key: str, minimum: float | None = None) -> None:
    # `1e999` se décode en infini sans passer par `parse_constant`.
    if type(value) is not int and (type(value) is not float or not math.isfinite(value)):
        raise _bad(key, "un nombre fini")
    if minimum is not None and value < minimum:
        raise _bad(key, f"un nombre au moins égal à {minimum}")


def _check_text(value: Any, key: str) -> None:
    if not isinstance(value, str) or not value:
        raise _bad(key, "une chaîne non vide")


def _check_text_list(value: Any, key: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _bad(key, "une liste de chaînes")


def _check_flags(value: Any, key: str) -> None:
    if not isinstance(value, Mapping) or not all(type(flag) is bool for flag in value.values()):
        raise _bad(key, "un objet de booléens")


def _check_atom_data(data: Mapping[str, Any]) -> None:
    if "origin" in data and data["origin"] not in ZOMBIE_ORIGINS:
        raise _bad("data.origin", " ou ".join(ZOMBIE_ORIGINS))


def _check_beat_data(data: Mapping[str, Any]) -> None:
    _check_number(_get(data, "age"), "data.age")
    _check_text_list(_get(data, "players"), "data.players")
    hp = _get(data, "hp")
    if hp is not None:
        _check_number(hp, "data.hp")
    kills = _get(data, "kills")
    if kills is not None:
        _check_int(kills, "data.kills", 0)


def _check_caps_data(data: Mapping[str, Any]) -> None:
    _check_text(_get(data, "build"), "data.build")
    _check_text(_get(data, "mod"), "data.mod")
    _check_int(_get(data, "protocol"), "data.protocol", 0)
    _check_flags(_get(data, "hooks"), "data.hooks")
    _check_flags(_get(data, "functions"), "data.functions")
    _check_text_list(_get(data, "atoms"), "data.atoms")
    _check_text_list(_get(data, "effects"), "data.effects")


def _check_hook_seen_data(data: Mapping[str, Any]) -> None:
    _check_text(_get(data, "hook"), "data.hook")


def _check_cmd_result_data(data: Mapping[str, Any]) -> None:
    _check_int(_get(data, "seq"), "data.seq", 1)
    if _get(data, "status") not in CMD_STATUSES:
        raise _bad("data.status", " ou ".join(CMD_STATUSES))
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise _bad("data.reason", "une chaîne")
    if "spawned" in data:
        _check_int(data["spawned"], "data.spawned", 0)


_CONTROL_DATA_CHECKS = {
    "beat": _check_beat_data,
    "caps": _check_caps_data,
    "hook_seen": _check_hook_seen_data,
    "cmd_result": _check_cmd_result_data,
}


def _check_envelope(obj: Mapping[str, Any]) -> None:
    """Contrôle les champs communs à toute ligne de `out`, hors `kind`."""
    _check_int(_get(obj, "boot"), "boot", 1)
    _check_int(_get(obj, "seq"), "seq", 1)
    _check_int(_get(obj, "t_ms"), "t_ms", 0)
    src = _get(obj, "src")
    if not isinstance(src, str):
        raise _bad("src", "une chaîne")
    if src not in SIDES:
        raise ProtocolError(SIDE_MISMATCH, f"`src` inconnu : {src!r}")
    side = _get(obj, "side")
    if side is not None and not isinstance(side, str):
        raise _bad("side", "une chaîne ou null")
    if side is not None and side != src:
        raise ProtocolError(SIDE_MISMATCH, f"`side` {side!r} pour `src` {src!r}")


def _check_data(obj: Mapping[str, Any], kind: str) -> None:
    data = _get(obj, "data")
    if not isinstance(data, Mapping):
        raise _bad("data", "un objet")
    if kind == "player_death":
        return
    _CONTROL_DATA_CHECKS.get(kind, _check_atom_data)(data)


def parse_event(raw: bytes | str | Mapping[str, Any], catalog: Mapping[str, Any]) -> Mapping[str, Any]:
    """Valide une ligne de `out` ; `catalog` a la forme de `service/effects.json`."""
    obj = _load(raw)
    _check_envelope(obj)
    kind = _get(obj, "kind")
    if not isinstance(kind, str):
        raise _bad("kind", "une chaîne")
    if kind not in CONTROL_KINDS and kind not in catalog["atoms"]:
        raise ProtocolError(UNKNOWN_KIND, f"`kind` hors catalogue : {kind!r}")
    _check_data(obj, kind)
    return obj


def parse_beat(raw: bytes | str | Mapping[str, Any]) -> Mapping[str, Any]:
    """Valide un heartbeat : toute autre ligne de `out` est refusée."""
    obj = _load(raw)
    _check_envelope(obj)
    if _get(obj, "kind") != "beat":
        raise ProtocolError(UNKNOWN_KIND, "`kind` doit valoir beat")
    _check_data(obj, "beat")
    return obj


def _check_items(items: Any) -> None:
    if not isinstance(items, list) or not items:
        raise _bad("items", "une liste non vide")
    for entry in items:
        if not isinstance(entry, Mapping):
            raise _bad("items", "une liste d'objets")
        _check_text(_get(entry, "item"), "items.item")
        _check_int(_get(entry, "count"), "items.count", 1)


def _check_distance(distance: Any) -> None:
    if not isinstance(distance, list) or len(distance) != 2:
        raise _bad("distance", "une paire [min, max]")
    low, high = distance
    _check_number(low, "distance", 0)
    _check_number(high, "distance", 0)
    if low > high:
        raise _bad("distance", "une paire [min, max] ordonnée")


def _check_origin(origin: Any) -> None:
    """`boot` et `event_seq` manquent sur `probe_spawn`, sans événement source."""
    if not isinstance(origin, Mapping):
        raise _bad("origin", "un objet")
    _check_text(_get(origin, "rule"), "origin.rule")
    if "boot" in origin:
        _check_int(origin["boot"], "origin.boot", 1)
    if "event_seq" in origin:
        _check_int(origin["event_seq"], "origin.event_seq", 1)


_COMMAND_PARAM_CHECKS = {
    "n": lambda value: _check_int(value, "n", 1),
    "radius": lambda value: _check_number(value, "radius", 0),
    "distance": _check_distance,
    "warn_s": lambda value: _check_number(value, "warn_s", 0),
    "duration_s": lambda value: _check_number(value, "duration_s", 0),
    "items": _check_items,
}


def parse_command(raw: bytes | str | Mapping[str, Any], catalog: Mapping[str, Any]) -> Mapping[str, Any]:
    """Valide une ligne de `in` ; `catalog` a la forme de `service/effects.json`.

    `kind` n'est pas borné au catalogue : le contrat fait répondre `unknown_kind`
    au mod, par `cmd_result`, pour un effet qu'il ne connaît pas. Un tel `kind` n'a
    donc aucun paramètre obligatoire, mais ceux qu'il porte restent contrôlés.
    """
    obj = _load(raw)
    _check_int(_get(obj, "seq"), "seq", 1)
    kind = _get(obj, "kind")
    _check_text(kind, "kind")
    _check_origin(_get(obj, "origin"))
    for key in catalog["effects"].get(kind, {}).get("required", ()):
        _get(obj, key)
    for key, check in _COMMAND_PARAM_CHECKS.items():
        if key in obj:
            check(obj[key])
    return obj


def parse_ack(raw: bytes | str | Mapping[str, Any]) -> Mapping[str, Any]:
    """Valide le contenu de `ack` ; `seq` vaut 0 tant qu'aucune commande n'est prise."""
    obj = _load(raw)
    _check_int(_get(obj, "boot"), "boot", 1)
    _check_int(_get(obj, "seq"), "seq", 0)
    return obj


def parse_session(raw: bytes | str | Mapping[str, Any]) -> Mapping[str, Any]:
    """Valide le contenu de `session` ; `cmd_floor` vaut 0 au premier `boot`."""
    obj = _load(raw)
    _check_text(_get(obj, "match"), "match")
    if _get(obj, "side") not in SIDES:
        raise _bad("side", " ou ".join(SIDES))
    _check_int(_get(obj, "boot"), "boot", 1)
    _check_int(_get(obj, "cmd_floor"), "cmd_floor", 0)
    return obj


def dump_line(obj: Mapping[str, Any]) -> bytes:
    """Sérialise en JSON UTF-8 compact, terminé par `\\n`, prêt pour un seul `write`."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return (text + "\n").encode("utf-8")
