"""Moteur de règles : chargement strict des tables (presets).

`load_table` refuse tout ce qui sort du schéma de la spec, avec un message français
nommant la règle, ou la section hors règle, et le champ. Les bornes viennent de
`limits`, le catalogue a la forme de `service/effects.json`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

RUNTIME_PATH = Path(__file__).resolve().parent / "runtime.json"
EFFECTS_PATH = Path(__file__).resolve().parent / "effects.json"

ALLOWED_MODS = ["duelcore"]
ALLOWED_SOURCE = "server"
SIDES = ("A", "B")
TARGETS = ("self", "opponent")

TABLE_KEYS = (
    "id",
    "label",
    "tagline",
    "duration_s",
    "day_length",
    "mods",
    "atoms",
    "rules",
    "modifiers",
    "budget",
    "score",
    "disconnect_grace_s",
    "summary",
)
BOUNDED_PARAMS = {
    "n": (1, "max_n"),
    "warn_s": (0, "max_warn_s"),
    "radius": (1, "max_radius"),
    "duration_s": (1, "max_effect_duration_s"),
    "cap_per_min": (1, "max_cap_per_min"),
}

_ID = r"[a-z0-9_]+"
IDENTIFIER_RE = re.compile(_ID)
_POSITIVE = r"[1-9][0-9]*"
WHEN_RE = re.compile(
    rf"atom\((?P<atom>{_ID})\)"
    rf"(?:\.count\((?P<count>{_POSITIVE})\)\.within\((?P<within>{_POSITIVE})s\))?"
    rf"(?:\.and\(not atom\((?P<absent>{_ID})\)\.within\((?P<absent_within>{_POSITIVE})s\)\))?"
    rf"(?:\.then\(atom\((?P<then>{_ID})\), within=(?P<then_within>{_POSITIVE})s\))?"
)


class TableError(ValueError):
    """Table refusée : le message, en français, nomme la règle ou la section et le champ."""


@dataclass(frozen=True)
class When:
    """AST d'un `when` : une base, puis une absence et une série facultatives."""

    atom: str
    count: int | None = None
    within_s: int | None = None
    absent_atom: str | None = None
    absent_within_s: int | None = None
    then_atom: str | None = None
    then_within_s: int | None = None

    @property
    def is_single_atom(self) -> bool:
        return self.count is None and self.absent_atom is None and self.then_atom is None

    @property
    def atoms(self) -> tuple[str, ...]:
        cited = (self.atom, self.absent_atom, self.then_atom)
        return tuple(atom for atom in cited if atom is not None)


def load_runtime() -> dict:
    with open(RUNTIME_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def load_effects() -> dict:
    with open(EFFECTS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def rules_hash(table: Mapping[str, Any]) -> str:
    """SHA-256 du JSON canonique : clés triées, sans espace, UTF-8."""
    canonical = json.dumps(table, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_when(text: Any) -> When:
    """Analyse un `when` selon la grammaire exacte ; compte et durées valent au moins 1."""
    match = WHEN_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise TableError(f"expression hors grammaire : {text!r}")
    groups = match.groupdict()
    try:
        numbers = {
            name: None if groups[name] is None else int(groups[name])
            for name in ("count", "within", "absent_within", "then_within")
        }
    except ValueError as exc:  # entier au-delà de ce que `int` accepte de convertir
        raise TableError(f"expression hors grammaire : {text!r}") from exc
    return When(
        atom=groups["atom"],
        count=numbers["count"],
        within_s=numbers["within"],
        absent_atom=groups["absent"],
        absent_within_s=numbers["absent_within"],
        then_atom=groups["then"],
        then_within_s=numbers["then_within"],
    )


def load_table(
    raw: Any,
    catalog: Mapping[str, Any],
    limits: Mapping[str, Any],
    file_stem: str | None = None,
    active_atoms: Collection[str] | None = None,
) -> dict:
    """Valide `raw` et en rend une copie, de même contenu donc de même `rules_hash`.

    `file_stem`, obligatoire pour un fichier livré, impose `id`. `active_atoms` restreint
    le catalogue aux atomes retenus à la clôture de la fenêtre de contrat.
    """
    where = "section table"
    _check_keys(raw, where, "", TABLE_KEYS)
    _check_header(raw, where, file_stem)
    atoms = _check_atoms(raw["atoms"], catalog, active_atoms)
    _check_budget(raw["budget"], limits)
    _check_score(raw["score"], atoms)
    _check_modifiers(raw["modifiers"], atoms, catalog, limits)
    _check_rules(raw["rules"], atoms, catalog, limits)
    return copy.deepcopy(raw)


def _fail(where: str, field: str, reason: str) -> NoReturn:
    raise TableError(f"{where}, champ « {field} » : {reason}")


def _join(prefix: str, key: Any) -> str:
    return f"{prefix}.{key}" if prefix else str(key)


def _check_keys(
    obj: Any,
    where: str,
    field: str,
    required: Collection[str],
    optional: Collection[str] = (),
) -> None:
    if not isinstance(obj, dict):
        _fail(where, field or "table", "objet attendu")
    for key in obj:
        if key not in required and key not in optional:
            _fail(where, _join(field, key), "clé inconnue")
    for key in required:
        if key not in obj:
            _fail(where, _join(field, key), "champ obligatoire manquant")


def _check_int(
    value: Any, where: str, field: str, minimum: int, maximum: int | None = None
) -> None:
    if type(value) is not int:
        _fail(where, field, f"entier attendu, reçu {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"au moins {minimum}" if maximum is None else f"de {minimum} à {maximum}"
        _fail(where, field, f"entier {bounds} attendu, reçu {value}")


def _check_number(
    value: Any, where: str, field: str, minimum: float, *, exclusive: bool = False
) -> None:
    if type(value) is not int and (type(value) is not float or not math.isfinite(value)):
        _fail(where, field, f"nombre attendu, reçu {value!r}")
    if value < minimum or (exclusive and value == minimum):
        floor = f"supérieur à {minimum}" if exclusive else f"d'au moins {minimum}"
        _fail(where, field, f"nombre {floor} attendu, reçu {value}")


def _check_text(value: Any, where: str, field: str) -> None:
    if not isinstance(value, str) or not value:
        _fail(where, field, f"chaîne non vide attendue, reçu {value!r}")


def _check_identifier(value: Any, where: str, field: str) -> None:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        _fail(where, field, f"identifiant [a-z0-9_]+ attendu, reçu {value!r}")


def _check_list(value: Any, where: str, field: str) -> None:
    if not isinstance(value, list) or not value:
        _fail(where, field, f"liste non vide attendue, reçu {value!r}")


def _check_header(raw: dict, where: str, file_stem: str | None) -> None:
    _check_identifier(raw["id"], where, "id")
    if file_stem is not None and raw["id"] != file_stem:
        _fail(where, "id", f"« {raw['id']} » diffère du nom de fichier « {file_stem} »")
    _check_text(raw["label"], where, "label")
    _check_text(raw["tagline"], where, "tagline")
    _check_int(raw["duration_s"], where, "duration_s", 1)
    _check_int(raw["disconnect_grace_s"], where, "disconnect_grace_s", 1)
    day_length = raw["day_length"]
    if type(day_length) is not int or str(day_length) not in load_runtime()["day_length_minutes"]:
        _fail(where, "day_length", f"clé de la table DayLength attendue, reçu {day_length!r}")
    if raw["mods"] != ALLOWED_MODS:
        _fail(where, "mods", f"exactement {ALLOWED_MODS} attendu, reçu {raw['mods']!r}")
    _check_list(raw["summary"], where, "summary")
    for index, line in enumerate(raw["summary"]):
        if not isinstance(line, str):
            _fail(where, f"summary[{index}]", f"chaîne attendue, reçu {line!r}")


def _check_atoms(
    atoms: Any, catalog: Mapping[str, Any], active_atoms: Collection[str] | None
) -> set[str]:
    where = "section atoms"
    _check_list(atoms, where, "atoms")
    seen: set[str] = set()
    for index, atom in enumerate(atoms):
        field = f"atoms[{index}]"
        if not isinstance(atom, str):
            _fail(where, field, f"atome attendu, reçu {atom!r}")
        if atom in seen:
            _fail(where, field, f"atome « {atom} » cité deux fois")
        declared = catalog["atoms"].get(atom)
        if declared is None:
            _fail(where, field, f"atome « {atom} » absent du catalogue")
        if declared.get("source") != ALLOWED_SOURCE:
            source = declared.get("source")
            _fail(where, field, f"atome « {atom} » de source « {source} », non autoritaire")
        if active_atoms is not None and atom not in active_atoms:
            _fail(where, field, f"atome « {atom} » retiré, absent des atomes actifs du match")
        seen.add(atom)
    return seen


def _check_budget(budget: Any, limits: Mapping[str, Any]) -> None:
    where = "section budget"
    keys = ("threats_per_minute", "concurrent_effects", "per_effect_cooldown_s")
    _check_keys(budget, where, "", keys)
    _check_int(
        budget["threats_per_minute"],
        where,
        "threats_per_minute",
        1,
        limits["max_threats_per_minute"],
    )
    _check_int(
        budget["concurrent_effects"],
        where,
        "concurrent_effects",
        1,
        limits["max_concurrent_effects"],
    )
    _check_int(budget["per_effect_cooldown_s"], where, "per_effect_cooldown_s", 0)


def _check_score(score: Any, atoms: set[str]) -> None:
    where = "section score"
    _check_keys(score, where, "", ("points",))
    points = score["points"]
    if not isinstance(points, dict) or not points:
        _fail(where, "points", f"objet non vide attendu, reçu {points!r}")
    for atom, value in points.items():
        field = f"points.{atom}"
        if atom not in atoms:
            _fail(where, field, f"atome « {atom} » absent de atoms")
        _check_int(value, where, field, 0)


def _check_modifiers(
    modifiers: Any, atoms: set[str], catalog: Mapping[str, Any], limits: Mapping[str, Any]
) -> None:
    _check_keys(modifiers, "section modifiers", "", (), ("combo", "score_gap"))
    if "combo" in modifiers:
        where = "section modifiers.combo"
        combo = modifiers["combo"]
        _check_keys(combo, where, "", ("per", "max", "on", "reset_on"))
        _check_number(combo["per"], where, "per", 0, exclusive=True)
        _check_number(combo["max"], where, "max", 1)
        for field in ("on", "reset_on"):
            if not isinstance(combo[field], str) or combo[field] not in atoms:
                _fail(where, field, f"atome de atoms attendu, reçu {combo[field]!r}")
    if "score_gap" in modifiers:
        where = "section modifiers.score_gap"
        gap = modifiers["score_gap"]
        _check_keys(gap, where, "", ("when_gt", "attacker", "defender_bonus"))
        _check_int(gap["when_gt"], where, "when_gt", 0)
        _check_number(gap["attacker"], where, "attacker", 0, exclusive=True)
        _check_effect(
            gap["defender_bonus"], where, "defender_bonus", catalog, limits, allow_target=False
        )


def _check_rules(
    rules: Any, atoms: set[str], catalog: Mapping[str, Any], limits: Mapping[str, Any]
) -> None:
    _check_list(rules, "section rules", "rules")
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        named = isinstance(rule, dict) and isinstance(rule.get("id"), str)
        where = f"règle « {rule['id']} »" if named else f"règle n°{index + 1}"
        _check_keys(rule, where, "", ("id", "when", "effects"), ("every", "sides"))
        _check_identifier(rule["id"], where, "id")
        if rule["id"] in seen:
            _fail(where, "id", "identifiant déjà utilisé dans la table")
        seen.add(rule["id"])
        _check_when(rule, where, atoms)
        if "sides" in rule:
            _check_sides(rule["sides"], where)
        _check_list(rule["effects"], where, "effects")
        for position, effect in enumerate(rule["effects"]):
            _check_effect(effect, where, f"effects[{position}]", catalog, limits)


def _check_when(rule: dict, where: str, atoms: set[str]) -> None:
    try:
        when = parse_when(rule["when"])
    except TableError as exc:
        _fail(where, "when", str(exc))
    for atom in when.atoms:
        if atom not in atoms:
            _fail(where, "when", f"atome « {atom} » absent de atoms")
    if "every" in rule:
        if not when.is_single_atom:
            _fail(where, "every", "permis seulement sur un when réduit à un atome seul")
        _check_int(rule["every"], where, "every", 1)


def _check_sides(sides: Any, where: str) -> None:
    _check_list(sides, where, "sides")
    for index, side in enumerate(sides):
        if side not in SIDES or sides.index(side) != index:
            _fail(where, f"sides[{index}]", f"« A » ou « B » sans doublon attendu, reçu {side!r}")


def _check_effect(
    effect: Any,
    where: str,
    field: str,
    catalog: Mapping[str, Any],
    limits: Mapping[str, Any],
    *,
    allow_target: bool = True,
) -> None:
    if not isinstance(effect, dict):
        _fail(where, field, f"objet effet attendu, reçu {effect!r}")
    kind_field = f"{field}.kind"
    if "kind" not in effect:
        _fail(where, kind_field, "champ obligatoire manquant")
    kind = effect["kind"]
    if not isinstance(kind, str):
        _fail(where, kind_field, f"nom d'effet attendu, reçu {kind!r}")
    if kind == "combo_reset":
        _fail(where, kind_field, "combo_reset n'est pas un effet de table : voir modifiers.combo")
    declared = catalog["effects"].get(kind)
    if declared is None:
        _fail(where, kind_field, f"effet « {kind} » absent du catalogue")
    if not declared["table"]:
        _fail(where, kind_field, f"« {kind} » est une commande de service, interdite en table")
    optional = [key for key in declared["optional"] if allow_target or key != "target"]
    _check_keys(effect, where, field, ("kind", *declared["required"]), optional)
    for key, value in effect.items():
        if key != "kind":
            _check_param(key, value, where, f"{field}.{key}", catalog, limits)


def _check_param(
    key: str,
    value: Any,
    where: str,
    field: str,
    catalog: Mapping[str, Any],
    limits: Mapping[str, Any],
) -> None:
    if key in BOUNDED_PARAMS:
        minimum, limit = BOUNDED_PARAMS[key]
        _check_int(value, where, field, minimum, limits[limit])
    elif key == "distance":
        _check_distance(value, where, field, limits)
    elif key == "items":
        _check_items(value, where, field, catalog, limits)
    elif key == "target":
        if value not in TARGETS:
            _fail(where, field, f"« self » ou « opponent » attendu, reçu {value!r}")
    else:
        _fail(where, field, "paramètre sans validation dans le chargeur")


def _check_distance(value: Any, where: str, field: str, limits: Mapping[str, Any]) -> None:
    if not isinstance(value, list) or len(value) != 2:
        _fail(where, field, f"[min, max] attendu, reçu {value!r}")
    _check_int(value[0], where, f"{field}[0]", limits["min_distance"], limits["max_distance"])
    _check_int(value[1], where, f"{field}[1]", value[0], limits["max_distance"])


def _check_items(
    items: Any,
    where: str,
    field: str,
    catalog: Mapping[str, Any],
    limits: Mapping[str, Any],
) -> None:
    _check_list(items, where, field)
    if len(items) > limits["max_supply_entries"]:
        maximum = limits["max_supply_entries"]
        _fail(where, field, f"au plus {maximum} entrées attendues, reçu {len(items)}")
    seen: set[str] = set()
    for index, entry in enumerate(items):
        path = f"{field}[{index}]"
        _check_keys(entry, where, path, ("item", "count"))
        item = entry["item"]
        if not isinstance(item, str) or item not in catalog["supply_items"]:
            _fail(where, f"{path}.item", f"objet {item!r} absent de supply_items")
        if item in seen:
            _fail(where, f"{path}.item", f"objet « {item} » cité deux fois")
        seen.add(item)
        _check_int(entry["count"], where, f"{path}.count", 1, limits["max_item_count"])
