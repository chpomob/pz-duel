"""Verdict d'un journal de match : `python -m service.verdict <journal>`.

Fonctions pures sur les enregistrements : attribution des kills, spawn serveur, branche
de repli et capacités publiées. Ce module ne lit ni l'horloge système ni
`service/runtime.json` : les seuils viennent de `match_start`, et l'instant courant d'un
journal clos est le `ts_ms` de sa dernière ligne.

Code de sortie 0 quel que soit le verdict, JSON sur la sortie standard ; 1 sur un journal
illisible, message sur la sortie d'erreur. Une branche de repli n'est que signalée.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .journal import JournalError, read_journal
from .rules import MS_PER_S, SIDES

EXIT_OK = 0
EXIT_UNREADABLE = 1
MATCH_START = "match_start"
MATCH_END = "match_end"
EVENT = "event"
COMMAND = "command"
CMD_RESULT = "cmd_result"
BEAT = "beat"
ZOMBIE_KILL = "zombie_kill"
CAPS = "caps"
HOOK_SEEN = "hook_seen"
PROBE_KIND = "probe_spawn"
REFUSED = "refused"
NO_SQUARE = "no_square"
APPLIED = "applied"
MIN_SPAWNED = 1
RUNTIME_FIELDS = ("attribution_min_kills", "probe_timeout_s")

OK = "ok"
KO = "ko"
INSUFFICIENT = "insufficient"
UNKNOWN = "unknown"
SPAWN_STATUSES = (OK, KO, UNKNOWN)
ATTRIBUTION_STATUSES = (OK, KO, INSUFFICIENT)
FALLBACK_OBSERVATION = "java_agent"
FALLBACK_GENERATION = "solo_ipc"

Record = Mapping[str, Any]


class VerdictError(ValueError):
    """Journal sans verdict possible : le message nomme le champ en cause."""


def _position(event: Mapping[str, Any]) -> tuple[int, int]:
    return event["boot"], event["seq"]


def _match_events(records: Iterable[Record]) -> Iterable[tuple[Record, Mapping[str, Any]]]:
    """Rend chaque `event` avec sa ligne, jusqu'à `match_end` exclu."""
    for record in records:
        if record["type"] == MATCH_END:
            return
        if record["type"] == EVENT:
            yield record, record["data"]


def _retained_beats(records: Iterable[Record]) -> dict[str, list[Mapping[str, Any]]]:
    """Heartbeats retenus par camp, dans l'ordre de `(boot, seq)`.

    Un tick traite ses événements par `t_ms` : l'ordre du journal ne suffit pas à
    désigner H0 et Hk. `mt` 0 et `kills` 0 sont des valeurs, seul `None` écarte.
    """
    beats: dict[str, list[Mapping[str, Any]]] = {side: [] for side in SIDES}
    for record, event in _match_events(records):
        if event["kind"] == BEAT and record["mt"] is not None:
            if event["data"]["kills"] is not None:
                beats[event["src"]].append(event)
    return {side: sorted(found, key=_position) for side, found in beats.items()}


def _rise(beats: Sequence[Mapping[str, Any]]) -> int:
    kills = [beat["data"]["kills"] for beat in beats]
    return sum(max(after - before, 0) for before, after in zip(kills, kills[1:]))


def attribution(records: Sequence[Record], min_kills: int) -> dict[str, Any]:
    """Compare les kills du jeu aux `zombie_kill` attribués, sur l'intervalle de chaque camp.

    `(boot, seq)` ne se compare qu'au sein d'un même serveur : chaque camp a ses bornes.
    Un `zombie_kill` à `side` nul n'est crédité à personne, mais la hausse de `kills` qui
    lui répond compte dans `reference`.
    """
    by_side = {}
    beats = _retained_beats(records)
    for side in SIDES:
        by_side[side] = {"reference": 0, "attributed": 0}
        if len(beats[side]) < 2:
            continue
        first, last = _position(beats[side][0]), _position(beats[side][-1])
        by_side[side]["reference"] = _rise(beats[side])
        by_side[side]["attributed"] = sum(
            event["kind"] == ZOMBIE_KILL
            and event["side"] == side
            and first < _position(event) < last
            for _, event in _match_events(records)
        )
    reference = sum(counts["reference"] for counts in by_side.values())
    attributed = sum(counts["attributed"] for counts in by_side.values())
    if reference < min_kills:
        status = INSUFFICIENT
    else:
        status = OK if attributed == reference else KO
    return {
        "status": status,
        "reference": reference,
        "attributed": attributed,
        "by_side": by_side,
    }


def probe_may_retry(result: Mapping[str, Any]) -> bool:
    """Dit si un `cmd_result` de sonde décrit un état passager, et non un spawn impossible.

    Un joueur endormi, ou des cases pas encore chargées, ne disent rien de la fonction.
    """
    return result["status"] == REFUSED or result.get("reason") == NO_SQUARE


def _probes(records: Iterable[Record]) -> dict[str, list[Record]]:
    """Essais de `probe_spawn` de chaque camp, le premier ouvrant le délai."""
    probes: dict[str, list[Record]] = {}
    for record in records:
        if record["type"] == COMMAND and record["data"]["command"]["kind"] == PROBE_KIND:
            probes.setdefault(record["side"], []).append(record)
    return probes


def _result_of(records: Iterable[Record], side: str, seq: int) -> Record | None:
    """Le `seq` d'une commande ne vaut que sur son canal : le camp fait partie de la clé."""
    for record in records:
        if record["type"] == CMD_RESULT:
            event = record["data"]
            if event["src"] == side and event["data"]["seq"] == seq:
                return record
    return None


def _probe_status(
    records: Sequence[Record], attempts: Sequence[Record], timeout_ms: int, now_ms: int
) -> str:
    side, deadline_ms = attempts[0]["side"], attempts[0]["ts_ms"] + timeout_ms
    for attempt in attempts:
        result = _result_of(records, side, attempt["data"]["command"]["seq"])
        if result is None:
            continue
        if result["ts_ms"] > deadline_ms:
            return KO
        data = result["data"]["data"]
        if data["status"] == APPLIED and data.get("spawned", 0) >= MIN_SPAWNED:
            return OK
        if not probe_may_retry(data):
            return KO
    return KO if now_ms >= deadline_ms else UNKNOWN


def spawn_status(
    records: Sequence[Record], probe_timeout_s: float, now_ms: int | None = None
) -> dict[str, str]:
    """Statut de la sonde de spawn par camp.

    La sonde part à la connexion du joueur : le délai court depuis sa première ligne
    `command`, et un état passager n'est `ko` qu'à son terme. `now_ms` vaut par défaut le
    `ts_ms` de la dernière ligne. Un résultat arrivé après le délai ne rattrape pas la
    sonde, que le lancement a alors déjà refusée.
    """
    if now_ms is None:
        now_ms = records[-1]["ts_ms"] if records else 0
    timeout_ms = probe_timeout_s * MS_PER_S
    probes = _probes(records)
    return {
        side: _probe_status(records, probes[side], timeout_ms, now_ms)
        if side in probes
        else UNKNOWN
        for side in SIDES
    }


def combined_spawn(by_side: Mapping[str, str]) -> str:
    """Résume les camps pour la table de repli : un seul `ko` suffit à la conclure."""
    statuses = set(by_side.values())
    if KO in statuses:
        return KO
    return OK if statuses == {OK} else UNKNOWN


def fallback(spawn: str, attribution: str) -> str | None:
    """Branche de repli selon la table des critères d'échec, à partir des deux statuts."""
    if spawn not in SPAWN_STATUSES:
        raise ValueError(f"statut de spawn inconnu : {spawn!r}")
    if attribution not in ATTRIBUTION_STATUSES:
        raise ValueError(f"statut d'attribution inconnu : {attribution!r}")
    if spawn == KO:
        return FALLBACK_GENERATION
    return FALLBACK_OBSERVATION if attribution == KO else None


def publish_capabilities(
    caps: Mapping[str, Any] | None, hooks_seen: Iterable[str]
) -> dict[str, Any]:
    """Capacités publiées d'un camp, à partir du `data` de son `caps`.

    Un enregistrement réussi ne prouve pas la réception : `registered` vient de `caps`,
    `seen` des seuls `hook_seen`. Sans `caps`, rien n'est constaté.
    """
    hooks = caps["hooks"] if caps else {}
    seen = set(hooks_seen)
    return {
        "hooks": {
            name: {"registered": hooks.get(name, False), "seen": name in seen}
            for name in sorted(hooks.keys() | seen)
        },
        "functions": dict(caps["functions"]) if caps else {},
    }


def _capabilities(records: Iterable[Record]) -> dict[str, dict[str, Any]]:
    """Le dernier `caps` d'un camp fait foi ; un hook vu le reste à travers les `boot`."""
    caps: dict[str, Mapping[str, Any]] = {}
    seen: dict[str, set[str]] = {side: set() for side in SIDES}
    for _, event in _match_events(records):
        if event["kind"] == CAPS:
            caps[event["src"]] = event["data"]
        elif event["kind"] == HOOK_SEEN:
            seen[event["src"]].add(event["data"]["hook"])
    return {side: publish_capabilities(caps.get(side), seen[side]) for side in SIDES}


def _runtime_of(records: Sequence[Record]) -> Mapping[str, Any]:
    start = next((record for record in records if record["type"] == MATCH_START), None)
    if start is None:
        raise VerdictError(f"champ `{MATCH_START}` manquant")
    runtime = start["data"].get("runtime")
    if not isinstance(runtime, Mapping):
        raise VerdictError(f"champ `runtime` manquant dans `{MATCH_START}`")
    for name in RUNTIME_FIELDS:
        if runtime.get(name) is None:
            raise VerdictError(f"champ `runtime.{name}` manquant dans `{MATCH_START}`")
    return runtime


def verdict(records: Sequence[Record]) -> dict[str, Any]:
    """Verdict complet d'un journal, avec les seuils copiés dans son `match_start`."""
    try:
        runtime = _runtime_of(records)
        kills = attribution(records, runtime["attribution_min_kills"])
        spawn = spawn_status(records, runtime["probe_timeout_s"])
        capabilities = _capabilities(records)
    except (AttributeError, KeyError, TypeError) as exc:
        raise VerdictError(f"enregistrement illisible, champ manquant : {exc}") from exc
    return {
        **capabilities,
        "attribution": kills,
        "spawn": spawn,
        "fallback": fallback(combined_spawn(spawn), kills["status"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m service.verdict", description="Verdict d'un journal de match."
    )
    parser.add_argument("journal", help="chemin de journal.jsonl")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        content = read_journal(args.journal)
        result = verdict(content.records)
    except (VerdictError, JournalError, OSError) as exc:
        print(f"journal illisible : {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    if content.incomplete is not None:
        print("1 ligne ignorée : dernière ligne incomplète", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
