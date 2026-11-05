"""Rejeu d'un journal et télémétrie : `python -m service.replay [--summary] <journal>`.

Le rejeu relit `match_start`, construit un `MatchCore` neuf à partir de ce seul
enregistrement et lui repasse les intrants du journal, chacun à son `mt` : l'horloge
reste celle du cœur, et l'instant courant est le tick journalisé. Ce module ne lit ni
l'horloge système, ni `service/runtime.json`, ni `service/effects.json`.

Code de sortie 0 si le rejeu redonne les mêmes `command` et `late_event` au même tick,
1 sinon, avec le premier écart. Les messages vont sur la sortie d'erreur : `--summary`
garde la sortie standard pour son JSON.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any

from .journal import JournalError, read_journal
from .protocol import ProtocolError, parse_event
from .rules import MS_PER_S, SIDES, MatchCore

EXIT_SAME = 0
EXIT_DIFFERENT = 1
MATCH_START = "match_start"
MATCH_END = "match_end"
COMMAND = "command"
LATE_EVENT = "late_event"
EFFECT_DROPPED = "effect_dropped"
PROBE_KIND = "probe_spawn"
EVENT_TYPES = ("event", "cmd_result")
START_FIELDS = ("runtime", "effects", "preset", "duration_s")
COMPARED_FIELDS = ("type", "side", "mt", "data")
# Lignes de service : un heartbeat par camp ferait de toute minute une minute active.
TECHNICAL_KINDS = ("caps", "hook_seen", "beat")
SECONDS_PER_MINUTE = 60
LATENCY_PERCENTILE = 95
PERCENT = 100

Record = dict[str, Any]


class ReplayError(ValueError):
    """Journal impossible à rejouer : le message nomme le champ ou cite le `n`."""


def _check_lines(records: Sequence[Record]) -> None:
    for number, record in enumerate(records, start=1):
        n = record.get("n")
        complete = "mt" in record and isinstance(record.get("data"), dict)
        if type(n) is not int or not isinstance(record.get("type"), str) or not complete:
            raise ReplayError(f"enregistrement hors format à la ligne {number} du journal")


def _is_probe(record: Record) -> bool:
    command = record["data"].get(COMMAND)
    return (
        record["type"] == COMMAND
        and isinstance(command, dict)
        and command.get("kind") == PROBE_KIND
    )


def _start_of(records: Sequence[Record]) -> Mapping[str, Any]:
    if not records or records[0]["type"] != MATCH_START:
        raise ReplayError(f"champ `{MATCH_START}` manquant : premier enregistrement attendu")
    start = records[0]["data"]
    for name in START_FIELDS:
        if start.get(name) is None:
            raise ReplayError(f"champ `{name}` manquant dans `{MATCH_START}`")
    return start


def _t0_ms(records: Sequence[Record]) -> int:
    for record in records:
        if record["type"] == "state" and type(record["data"].get("t0_ms")) is int:
            return record["data"]["t0_ms"]
    raise ReplayError("champ `t0_ms` manquant : aucun `state` ne le porte")


def _tick_of(record: Record, tick_ms: int) -> int:
    """Rend l'index de tick de `mt`, compté en valeur exacte et non en flottant."""
    mt = record["mt"]
    try:
        if type(mt) not in (int, float):
            raise ValueError
        tick, rest = divmod(Fraction(str(mt)) * MS_PER_S, tick_ms)
    except ValueError:
        tick, rest = -1, 0
    if rest or tick < 0:
        raise ReplayError(f"`mt` hors grille à n={record['n']} : {mt!r}")
    return int(tick)


def _feed(core: MatchCore, record: Record, catalog: Mapping[str, Any]) -> None:
    """Rattache un enregistrement à l'ordonnancement du cœur, s'il y change quelque chose.

    Seul un `state` de serveur est un intrant : un changement de phase n'en est pas un.
    """
    type_, data = record["type"], record["data"]
    try:
        if type_ in EVENT_TYPES:
            core.submit_event(parse_event(data, catalog))
        elif type_ == "state" and "server" in data:
            core.submit_state(record["side"], data["server"])
        elif type_ == "lost_on_restart":
            core.submit_lost(data["seq"])
        elif type_ == MATCH_END:
            core.submit_end(data["result"])
        elif _is_probe(record):
            core.reserve_seq(data[COMMAND]["seq"])
    except (KeyError, TypeError, AttributeError, ProtocolError) as exc:
        raise ReplayError(f"`{type_}` illisible à n={record['n']} : {exc}") from exc


def replay_records(records: Sequence[Record]) -> list[Record]:
    """Rejoue `records` et rend tout ce que le cœur a produit, dans l'ordre d'écriture.

    `ReplayError` signale un journal qu'aucun moteur ne peut rejouer. Le parcours s'arrête
    au plus grand `mt` lu : celui de `match_end` dans un journal complet, car une ligne
    postérieure prend le dernier tick clos.
    """
    _check_lines(records)
    records = sorted(records, key=lambda record: record["n"])
    start = _start_of(records)
    catalog = start["effects"]
    timed = [record for record in records if record["mt"] is not None]
    if not timed:
        return []
    t0_ms = _t0_ms(records)
    try:
        tick_ms = start["runtime"]["tick_ms"]
        core = MatchCore(start["preset"], start["runtime"], catalog, t0_ms, start["duration_s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError(f"`{MATCH_START}` inutilisable, champ invalide : {exc}") from exc

    by_tick: dict[int, list[Record]] = {}
    for record in timed:
        by_tick.setdefault(_tick_of(record, tick_ms), []).append(record)
    for record in records:
        if record["mt"] is None:
            _feed(core, record, catalog)

    produced: list[Record] = []
    for tick in range(max(by_tick) + 1):
        for record in by_tick.get(tick, ()):
            _feed(core, record, catalog)
        produced += core.advance(t0_ms + tick * tick_ms)
    return produced


def _compared(records: Sequence[Record]) -> list[Record]:
    return [
        record
        for record in records
        if record["type"] in (COMMAND, LATE_EVENT) and not _is_probe(record)
    ]


def _describe(record: Record) -> str:
    fields = {name: record.get(name) for name in COMPARED_FIELDS}
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def _differs(record: Record, other: Record) -> bool:
    return any(record.get(name) != other[name] for name in COMPARED_FIELDS)


def _tick_may_be_cut(records: Sequence[Record], produced: Sequence[Record], mt: Any) -> bool:
    """Dit si les lignes retenues à `mt` peuvent être le début d'un tick non terminé.

    Un tick s'écrit d'un bloc : coupé, il en reste le début de ce que le rejeu produit.
    Une ligne de plus, comme `dup`, s'écrit tick clos et prouve que rien n'y manque.
    Le `state` qui porte `t0_ms` précède le tick 0 et ne dit rien de lui. Des
    `effect_dropped` finaux annoncent un `match_end` coupé : sans lui, le rejeu sert
    les lots que le cœur a jetés, et le bloc s'arrête donc juste avant eux.
    """
    written = [r for r in records if r["mt"] == mt and "t0_ms" not in r["data"]]
    while written and written[-1]["type"] == EFFECT_DROPPED:
        written.pop()
    expected = [r for r in produced if r["mt"] == mt]
    if len(written) > len(expected):
        return False
    return not any(_differs(record, other) for record, other in zip(written, expected))


def first_gap(records: Sequence[Record], produced: Sequence[Record]) -> str | None:
    """Compare journal et rejeu, et décrit le premier écart, ou rend `None`.

    Sans `match_end`, le journal s'arrête peut-être au milieu de son dernier tick : ce
    que le rejeu produit en plus à ce tick n'a simplement pas été écrit, sauf si les
    lignes retenues à ce tick montrent qu'il était terminé.
    """
    records = sorted(records, key=lambda record: record["n"])
    journaled, replayed = _compared(records), _compared(produced)
    for record, other in zip(journaled, replayed):
        if _differs(record, other):
            return (
                f"premier écart à n={record['n']} : journal {_describe(record)}, "
                f"rejeu {_describe(other)}"
            )
    if len(journaled) > len(replayed):
        record = journaled[len(replayed)]
        return f"premier écart à n={record['n']} : {_describe(record)} absent du rejeu"
    ended = any(record["type"] == MATCH_END for record in records)
    last_mt = max((r["mt"] for r in records if r["mt"] is not None), default=None)
    cut = not ended and _tick_may_be_cut(records, produced, last_mt)
    for other in replayed[len(journaled) :]:
        if not cut or other["mt"] != last_mt:
            after = journaled[-1]["n"] if journaled else records[0]["n"]
            return f"premier écart après n={after} : {_describe(other)} absent du journal"
    return None


def _percentile(values: Sequence[int]) -> int | None:
    """Rang le plus proche, en entiers : le p95 est une valeur réellement mesurée."""
    if not values:
        return None
    rank = -(-LATENCY_PERCENTILE * len(values) // PERCENT)
    return sorted(values)[rank - 1]


def _side_summary(side: str, timed: Sequence[Record], minutes: int) -> dict[str, Any]:
    commands = [0] * minutes
    units = [0] * minutes
    latencies: list[int] = []
    capped = null_side = 0
    active: set[int] = set()

    for record in timed:
        minute = min(int(record["mt"] // SECONDS_PER_MINUTE), minutes - 1)
        type_, data = record["type"], record["data"]
        if type_ == COMMAND and record["side"] == side and not _is_probe(record):
            commands[minute] += 1
            units[minute] += data[COMMAND].get("n", 0)
            latencies.append(data["latency_ms"])
        elif type_ == "effect_capped" and record["side"] == side:
            capped += data["units"]
        elif type_ == "event" and data["src"] == side and data["kind"] not in TECHNICAL_KINDS:
            active.add(minute)
            null_side += data["side"] is None
    return {
        "commands": sum(commands),
        "commands_per_minute": commands,
        "units": sum(units),
        "units_per_minute": units,
        "capped_units": capped,
        "null_side_events": null_side,
        "silent_minutes": minutes - len(active),
        "latency_ms_p95": _percentile(latencies),
    }


def summarize(records: Sequence[Record]) -> dict[str, dict[str, Any]]:
    """Télémétrie par camp, sur les lignes du match, celles dont `mt` n'est pas nul.

    Une commande et un plafond comptent au camp visé, seul `side` qu'ils portent ; un
    événement compte à son `src`, y compris à `side` nul, car il n'a pas d'autre camp.
    Une minute entamée compte : un tick posé sur la dernière borne reste dans celle
    qu'il termine.
    """
    _check_lines(records)
    timed = [record for record in records if record["mt"] is not None]
    try:
        last_mt = max((record["mt"] for record in timed), default=0)
        minutes = max(1, math.ceil(last_mt / SECONDS_PER_MINUTE)) if timed else 0
        return {side: _side_summary(side, timed, minutes) for side in SIDES}
    except (KeyError, TypeError) as exc:
        raise ReplayError(f"enregistrement illisible, champ manquant : {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m service.replay", description="Rejoue un journal de match."
    )
    parser.add_argument("journal", help="chemin de journal.jsonl")
    parser.add_argument("--summary", action="store_true", help="télémétrie par camp, en JSON")
    return parser


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return EXIT_DIFFERENT


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        content = read_journal(args.journal)
    except (JournalError, OSError) as exc:
        return _fail(f"journal illisible : {exc}")
    if content.incomplete is not None:
        print("1 ligne ignorée : dernière ligne incomplète", file=sys.stderr)
    try:
        if args.summary:
            summary = summarize(content.records)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return EXIT_SAME
        produced = replay_records(content.records)
    except ReplayError as exc:
        return _fail(str(exc))
    gap = first_gap(content.records, produced)
    if gap is not None:
        return _fail(gap)
    count = len(_compared(content.records))
    print(f"rejeu identique : {count} enregistrements comparés")
    return EXIT_SAME


if __name__ == "__main__":
    sys.exit(main())
