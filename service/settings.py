"""Réglages du monde et des règles, choisis avant le départ (P9F15).

Le catalogue est déclaratif : chaque champ dit sa clé, son libellé, son groupe (`monde` ou
`regles`), son type (`int`, `float`, `bool`, `range`), ses bornes, son défaut, son unité et son
aide. La page le reçoit par `GET /api/settings` et ne connaît aucun champ.

Les clés `monde` vont dans `SandboxVars`, sauf `DayLength`, que `write_sandbox` porte déjà, et
`monde_neuf`, qui commande l'effacement des mondes. Les clés `regles` vont dans le preset
effectif, construit en mémoire : `service/presets/*.json` n'est jamais réécrit.

Deux clés de `SandboxVars` restent hors du catalogue et sont fixées à chaque départ réglé :
`ZombieLore.Speed = 4` et `ZombieLore.SprinterPercentage = 0`. Le mod compte seul ses
sprinters (`floor(k × pct / 100)`) : un second mécanisme fausserait `sprinter_percent`.

Les options énumérées de PZ (`Zombies`, `ZombieLore.*`, `StartTime`, `DayLength`) sont des
indices, pas des grandeurs : leurs bornes et leurs libellés viennent du `SandboxVars` qu'écrit
PZ 42, et `choices` les donne à la page.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

GROUP_WORLD = "monde"
GROUP_RULES = "regles"
GROUPS = ((GROUP_WORLD, "Monde"), (GROUP_RULES, "Règles"))
# Le texte du bouton de remise : la page ne porte aucun texte de réglage.
RESET_LABEL = "Valeurs par défaut"

# Règles du preset que les réglages visent, par leur `id`.
KILL_RULE = "kill_baseline"
CARE_RULE = "hurt_supply"
POP_KIND = "spawn_pop"
CARE_ATOM = "damage_taken"
# La caisse de soin de `duel_rapide`, ajoutée telle quelle à un preset qui ne l'a pas.
CARE_RULE_BODY = {
    "id": CARE_RULE,
    "when": f"atom({CARE_ATOM})",
    "effects": [
        {
            "kind": "supply_drop",
            "distance": [25, 40],
            "warn_s": 3,
            "items": [
                {"item": "Base.Bandage", "count": 2},
                {"item": "Base.Disinfectant", "count": 1},
            ],
            "target": "self",
        }
    ],
}

# Hors du catalogue, jamais lues dans les réglages, écrites à chaque départ réglé.
LOCKED_SANDBOX = {"ZombieLore.Speed": 4, "ZombieLore.SprinterPercentage": 0}

# `Zombies` ne change les multiplicateurs de population que dans l'écran de PZ : un serveur
# dédié lit `ZombieConfig`. Valeurs relevées dans les commentaires du `SandboxVars` de PZ 42.
POPULATION = {
    1: (2.5, 3.0),
    2: (1.6, 2.0),
    3: (1.2, 1.5),
    4: (0.65, 1.0),
    5: (0.15, 0.5),
    6: (0.0, 0.0),
}
POPULATION_KEYS = ("ZombieConfig.PopulationMultiplier", "ZombieConfig.PopulationStartMultiplier")

MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)
# Indice d'option `StartTime` → heure, tel que l'écrit PZ 42.
START_HOURS = {1: 7, 2: 9, 3: 12, 4: 14, 5: 17, 6: 21, 7: 0, 8: 2, 9: 5}


def _day_label(index: int) -> str:
    minutes = {1: 15, 2: 30, 3: 60, 4: 90}.get(index, (index - 3) * 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d}" if rest else f"{hours} h"


def _choices(labels: Mapping[int, str]) -> list[dict[str, Any]]:
    return [{"value": value, "label": label} for value, label in labels.items()]


def _field(key: str, label: str, group: str, type_: str, low: Any, high: Any, default: Any,
           unit: str = "", help_: str = "", labels: Mapping[int, str] | None = None) -> dict:
    field = {
        "key": key,
        "label": label,
        "group": group,
        "type": type_,
        "min": low,
        "max": high,
        "defaut": default,
        "unit": unit,
        "help": help_,
    }
    if labels is not None:
        field["choices"] = _choices(labels)
    return field


WORLD_ONLY_NEW = "Ne compte qu'avec un monde neuf : un monde gardé a déjà sa date et sa population."

FIELDS: tuple[dict[str, Any], ...] = (
    _field("Zombies", "Population de base", GROUP_WORLD, "int", 1, 6, 3,
           help_="Écrit aussi les multiplicateurs de ZombieConfig, que lit le serveur dédié.",
           labels={1: "Hardcore", 2: "Très élevé", 3: "Élevé", 4: "Normal", 5: "Faible",
                   6: "Aucun"}),
    _field("ZombieLore.Sight", "Vision des zombies", GROUP_WORLD, "int", 1, 3, 2,
           labels={1: "Aiguisée", 2: "Normale", 3: "Pauvre"}),
    _field("ZombieLore.Hearing", "Ouïe des zombies", GROUP_WORLD, "int", 1, 3, 2,
           labels={1: "Précise", 2: "Normale", 3: "Pauvre"}),
    _field("ZombieLore.Strength", "Force des zombies", GROUP_WORLD, "int", 1, 4, 2,
           labels={1: "Hors norme", 2: "Normale", 3: "Faible", 4: "Aléatoire"}),
    _field("ZombieLore.Toughness", "Résistance des zombies", GROUP_WORLD, "int", 1, 4, 2,
           labels={1: "Résistants", 2: "Normaux", 3: "Fragiles", 4: "Aléatoire"}),
    _field("ZombieLore.Cognition", "Intelligence des zombies", GROUP_WORLD, "int", 1, 3, 3,
           labels={1: "Ouvrent les portes", 2: "Se déplacent", 3: "Déplacements basiques"}),
    _field("ZombieLore.Memory", "Mémoire des zombies", GROUP_WORLD, "int", 1, 4, 2,
           labels={1: "Longue", 2: "Normale", 3: "Courte", 4: "Aucune"}),
    _field("ZombieLore.Transmission", "Infection", GROUP_WORLD, "int", 1, 3, 1,
           labels={1: "Sang et salive", 2: "Salive uniquement", 3: "Tout le monde est infecté"}),
    _field("StartTime", "Heure de départ", GROUP_WORLD, "int", 1, 9, 2, help_=WORLD_ONLY_NEW,
           labels={index: f"{hour} h" for index, hour in START_HOURS.items()}),
    _field("StartMonth", "Mois de départ", GROUP_WORLD, "int", 1, 12, 7, help_=WORLD_ONLY_NEW,
           labels={index: name for index, name in enumerate(MONTHS, start=1)}),
    _field("DayLength", "Durée d'un jour de jeu", GROUP_WORLD, "int", 1, 7, 7,
           unit="réelles", labels={index: _day_label(index) for index in range(1, 8)}),
    _field("monde_neuf", "Monde neuf", GROUP_WORLD, "bool", None, None, True,
           help_="Efface les deux mondes et leurs snapshots avant le lancement : PZ les recrée."),
    _field("zombies_par_kill", "Zombies par kill", GROUP_RULES, "int", 1, 30, 10,
           unit="zombies", help_="Envoyés chez l'adversaire à chaque zombie tué."),
    _field("sprinter_percent", "Sprinters", GROUP_RULES, "int", 0, 100, 1, unit="%",
           help_="Le mod compte les zombies envoyés : le 100e, le 200e… à 1 %."),
    _field("pop_distance", "Distance des pops", GROUP_RULES, "range", 10, 100, [12, 20],
           unit="cases", help_="Minimum et maximum, le minimum strictement plus petit."),
    _field("aggro", "Agressivité des pops", GROUP_RULES, "float", 0, 1, 0.6,
           help_="Part des zombies envoyés qui foncent sur le joueur."),
    _field("spacing", "Espacement des pops", GROUP_RULES, "int", 0, 20, 4, unit="cases"),
    _field("warn_s", "Préavis", GROUP_RULES, "int", 0, 15, 6, unit="s"),
    _field("soin_sur_blessure", "Caisse de soin sur blessure", GROUP_RULES, "bool", None, None,
           True),
    _field("duration_s", "Durée du match", GROUP_RULES, "int", 300, 3600, 1500, unit="s"),
)
FIELD_BY_KEY = {field["key"]: field for field in FIELDS}
# Les réglages qui vont dans `SandboxVars` tels quels ; `DayLength` passe par `write_sandbox`.
SANDBOX_KEYS = tuple(
    field["key"]
    for field in FIELDS
    if field["group"] == GROUP_WORLD and field["key"] not in ("DayLength", "monde_neuf")
)
REPR_MAX = 40


class SettingsRefused(ValueError):
    """Un réglage refusé : le message nomme la clé, la borne et la valeur reçue."""


def _shown(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= REPR_MAX else text[: REPR_MAX - 1] + "…"


def _refuse(key: str, reason: str, value: Any) -> SettingsRefused:
    return SettingsRefused(f"réglage « {key} » refusé : {reason}, reçu {_shown(value)}")


def _kill_effect(table: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for rule in table.get("rules", []):
        if rule.get("id") == KILL_RULE:
            for effect in rule["effects"]:
                if effect.get("kind") == POP_KIND:
                    return effect
    return None


def _clamped(field: Mapping[str, Any], value: Any) -> Any:
    """Ramène dans les bornes un défaut lu dans un preset : jamais une valeur reçue."""
    if field["type"] == "range":
        low, high = (max(field["min"], min(field["max"], part)) for part in value)
        return [low, high] if low < high else list(field["defaut"])
    return max(field["min"], min(field["max"], value))


def defaults(preset: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Les défauts du catalogue ; ceux des règles se lisent dans `preset` quand il les porte.

    Pour `duel_rapide`, les deux coïncident : c'est le preset dont le catalogue est tiré.
    """
    values = {field["key"]: copy.deepcopy(field["defaut"]) for field in FIELDS}
    if preset is None:
        return values
    found: dict[str, Any] = {"duration_s": preset.get("duration_s"),
                             "DayLength": preset.get("day_length")}
    for key in ("sprinter_percent", "aggro", "spacing"):
        found[key] = preset.get(key)
    # Sans la clé, le cœur ne compte aucun sprinter : le défaut honnête est zéro.
    found["sprinter_percent"] = preset.get("sprinter_percent", 0)
    effect = _kill_effect(preset)
    if effect is not None:
        found["zombies_par_kill"] = effect.get("n")
        found["pop_distance"] = effect.get("distance")
        found["warn_s"] = effect.get("warn_s")
    found["soin_sur_blessure"] = any(rule.get("id") == CARE_RULE for rule in preset["rules"])
    for key, value in found.items():
        if value is None:
            continue
        field = FIELD_BY_KEY[key]
        if field["type"] in ("int", "float", "range") and "choices" not in field:
            value = _clamped(field, value)
        elif "choices" in field and value not in {c["value"] for c in field["choices"]}:
            continue
        values[key] = copy.deepcopy(value)
    return values


def catalog(preset: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Le catalogue servi à la page, dans l'ordre d'affichage, défauts de `preset` compris."""
    values = defaults(preset)
    return [{**copy.deepcopy(field), "defaut": values[field["key"]]} for field in FIELDS]


def _number(key: str, value: Any, integer: bool) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        kind = "entier" if integer else "nombre"
        raise _refuse(key, f"{kind} attendu", value)
    if integer and not isinstance(value, int):
        raise _refuse(key, "entier attendu", value)
    # Un entier Python n'a pas de borne : il ne passe jamais par `float`, qui déborderait
    # (OverflowError) sur quelques centaines de chiffres. Les bornes du champ le refuseront.
    if isinstance(value, float) and not math.isfinite(value):
        raise _refuse(key, "nombre fini attendu", value)
    return value


def _bounded(field: Mapping[str, Any], value: int | float, raw: Any) -> None:
    key = field["key"]
    if value < field["min"]:
        raise _refuse(key, f"borne min {field['min']}", raw)
    if value > field["max"]:
        raise _refuse(key, f"borne max {field['max']}", raw)


def _check(field: Mapping[str, Any], value: Any) -> Any:
    key, kind = field["key"], field["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise _refuse(key, "vrai ou faux attendu", value)
        return value
    if kind == "range":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise _refuse(key, "deux entiers [min, max] attendus", value)
        low, high = (_number(key, part, True) for part in value)
        for part in (low, high):
            _bounded(field, part, value)
        if low >= high:
            raise _refuse(key, "minimum strictement inférieur au maximum attendu", value)
        return [low, high]
    number = _number(key, value, kind == "int")
    _bounded(field, number, value)
    if "choices" in field and number not in {choice["value"] for choice in field["choices"]}:
        raise _refuse(key, "valeur hors des choix proposés", value)
    return number


def validate(raw: Any, preset: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Rend les réglages complets : ceux de `raw`, contrôlés, sur les défauts de `preset`.

    Tout se refuse, rien ne s'écrête : une clé inconnue, un type faux, une valeur hors bornes.
    """
    if not isinstance(raw, Mapping):
        raise SettingsRefused(f"réglages refusés : objet attendu, reçu {_shown(raw)}")
    values = defaults(preset)
    for key, value in raw.items():
        field = FIELD_BY_KEY.get(key) if isinstance(key, str) else None
        if field is None:
            raise SettingsRefused(f"réglage inconnu « {_shown(key)[1:-1]} » refusé")
        values[key] = _check(field, value)
    return values


def sandbox_values(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Ce que les réglages écrivent dans `SandboxVars`, hors `DayLength`, verrous compris."""
    values = {key: settings[key] for key in SANDBOX_KEYS}
    values.update(zip(POPULATION_KEYS, POPULATION[settings["Zombies"]]))
    values.update(LOCKED_SANDBOX)
    return values


def _summary_lines(table: Mapping[str, Any], settings: Mapping[str, Any]) -> list[str]:
    low, high = settings["pop_distance"]
    pct = settings["sprinter_percent"]
    lines = [
        f"1 zombie tué → {settings['zombies_par_kill']} zombies chez l'adversaire, à "
        f"{low}–{high} cases, annoncés {settings['warn_s']} s avant",
    ]
    if pct:
        lines.append(
            f"{pct} % des zombies envoyés sont des rôdeurs rapides, à la place d'un zombie, "
            "jamais en plus"
        )
    if settings["soin_sur_blessure"]:
        lines.append("1 blessure reçue → caisse de soin à proximité")
    others = [rule["id"] for rule in table["rules"] if rule["id"] not in (KILL_RULE, CARE_RULE)]
    if others:
        lines.append(f"Autres règles du preset : {', '.join(others)}")
    return lines


def effective_table(preset: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    """Le preset joué, construit en mémoire sur une copie : le fichier ne change jamais.

    Un preset sans règle `kill_baseline` ne peut pas recevoir les réglages de pop : refus.
    """
    table = copy.deepcopy(dict(preset))
    effect = _kill_effect(table)
    if effect is None:
        raise SettingsRefused(
            f"réglages refusés : le preset {table.get('id')} n'a pas de règle {KILL_RULE} à régler"
        )
    effect["n"] = settings["zombies_par_kill"]
    effect["distance"] = list(settings["pop_distance"])
    effect["warn_s"] = settings["warn_s"]
    for key in ("sprinter_percent", "aggro", "spacing", "duration_s"):
        table[key] = settings[key]
    table["day_length"] = settings["DayLength"]
    has_care = any(rule["id"] == CARE_RULE for rule in table["rules"])
    if settings["soin_sur_blessure"] and not has_care:
        table["rules"].append(copy.deepcopy(CARE_RULE_BODY))
        if CARE_ATOM not in table["atoms"]:
            table["atoms"].append(CARE_ATOM)
    elif not settings["soin_sur_blessure"] and has_care:
        table["rules"] = [rule for rule in table["rules"] if rule["id"] != CARE_RULE]
    table["summary"] = _summary_lines(table, settings)
    return table


def _choice_label(key: str, value: Any) -> str:
    for choice in FIELD_BY_KEY[key].get("choices", []):
        if choice["value"] == value:
            return choice["label"]
    return str(value)


def _minutes(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min {rest:02d}" if rest else f"{minutes} min"


def summary(settings: Mapping[str, Any]) -> str:
    """Une ligne lisible, en français, de ce qui se jouera."""
    low, high = settings["pop_distance"]
    parts = [
        f"{_choice_label('Zombies', settings['Zombies'])} ({settings['Zombies']})",
        f"vision {settings['ZombieLore.Sight']}",
        f"ouïe {settings['ZombieLore.Hearing']}",
        f"zombies {_choice_label('ZombieLore.Toughness', settings['ZombieLore.Toughness']).lower()}",
        f"{settings['zombies_par_kill']} z par kill",
        f"{settings['sprinter_percent']} % sprinter",
        f"pops à {low}–{high} cases",
        "monde neuf" if settings["monde_neuf"] else "monde gardé",
        _minutes(settings["duration_s"]),
    ]
    return " · ".join(parts)
