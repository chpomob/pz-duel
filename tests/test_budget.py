"""R4 : budgets, files de lots et fin de match de `MatchCore`.

AC26 à AC29, AC71, AC72 et AC77 ; minuteur et mort subite pour la part de R5 du cœur.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from service import rules
from service.protocol import parse_command, parse_event
from service.rules import MatchCore, load_effects, load_runtime, load_table

T0_MS = 1_793_830_000_000
CATALOG = load_effects()
SPAWN = {"kind": "spawn_pop", "n": 2, "distance": [30, 60], "warn_s": 6}
SPRINTER = {"kind": "spawn_sprinter", "n": 1, "distance": [40, 70], "warn_s": 10}
RAIN = {"kind": "weather_rain", "duration_s": 5, "warn_s": 0}
CARE = {
    "kind": "supply_drop",
    "distance": [25, 40],
    "warn_s": 3,
    "items": [{"item": "Base.Bandage", "count": 2}],
}


def rule(id: str, atom: str, effect: dict) -> dict:
    return {"id": id, "when": f"atom({atom})", "effects": [effect]}


KILL_SPAWN = rule("kill_spawn", "zombie_kill", SPAWN)
BUILD_SPRINTER = rule("build_sprinter", "build", SPRINTER)


def make_core(
    *rule_list: dict, cooldown_s=20, threats=6, concurrent=4, duration_s=600, **runtime_values
) -> MatchCore:
    raw = {
        "id": "essai",
        "label": "Essai",
        "tagline": "table de test",
        "duration_s": duration_s,
        "day_length": 1,
        "mods": ["duelcore"],
        "atoms": ["zombie_kill", "build"],
        "rules": list(rule_list),
        "modifiers": {},
        "budget": {
            "threats_per_minute": threats,
            "concurrent_effects": concurrent,
            "per_effect_cooldown_s": cooldown_s,
        },
        "score": {"points": {"zombie_kill": 1}},
        "disconnect_grace_s": 120,
        "summary": ["essai"],
    }
    runtime = {**load_runtime(), **runtime_values}
    table = load_table(raw, CATALOG, runtime["limits"])
    return MatchCore(table, runtime, CATALOG, T0_MS, raw["duration_s"])


class Script:
    """Pousse des événements datés dans un cœur et relève ses commandes."""

    def __init__(self, core: MatchCore):
        self.core = core
        self.seq = 0
        self.records: list[dict] = []

    @property
    def commands(self) -> list[dict]:
        return [r for r in self.records if r["type"] == "command"]

    def to(self, mt_s: float) -> None:
        self.records += self.core.advance(T0_MS + round(mt_s * 1000))

    def send(self, te_s: float, kind="zombie_kill", side="A", data=None) -> None:
        """Fait traiter l'événement au tick de sa date."""
        self.to(te_s - 0.25)
        self.seq += 1
        line = {
            "v": 1,
            "boot": 1,
            "seq": self.seq,
            "t_ms": T0_MS + round(te_s * 1000),
            "src": side,
            "side": side,
            "kind": kind,
            "data": {"origin": "world"} if data is None else data,
        }
        self.core.submit_event(parse_event(line, CATALOG))
        self.to(te_s)

    def result(self, te_s: float, cmd_seq: int, side="B") -> None:
        self.send(te_s, "cmd_result", side, {"seq": cmd_seq, "status": "applied"})

    def of_type(self, type: str) -> list:
        return [r for r in self.records if r["type"] == type]

    def sent(self) -> list:
        return [
            (c["mt"], c["side"], c["data"]["command"]["kind"], c["data"]["command"].get("n"))
            for c in self.commands
        ]


def test_cooldown_merges_not_drops():
    script = Script(make_core(KILL_SPAWN))
    for te in (0, 1, 2):
        script.send(te)
    script.to(19.75)
    assert script.sent() == [(0, "B", "spawn_pop", 2)]
    script.to(20)
    assert script.sent() == [(0, "B", "spawn_pop", 2), (20, "B", "spawn_pop", 4)]
    assert sum(c["data"]["command"]["n"] for c in script.commands) == 6
    # Le lot date de son événement le plus ancien, à t = 1 (D8).
    assert script.commands[1]["data"]["latency_ms"] == 19_000
    assert script.commands[1]["data"]["command"]["origin"] == {
        "rule": "kill_spawn",
        "boot": 1,
        "event_seq": 2,
    }
    for command in script.commands:
        parse_command(command["data"]["command"], CATALOG)


def test_latency_counts_from_oldest_event_of_lot():
    script = Script(make_core(KILL_SPAWN))
    script.send(0)
    script.send(1)
    script.to(2)
    script.send(0.5)
    script.to(20)
    assert script.commands[1]["data"]["latency_ms"] == 19_500


def test_queues_are_per_target():
    script = Script(make_core(KILL_SPAWN, BUILD_SPRINTER, cooldown_s=0))
    script.to(0)
    script.core.submit_state("A", "restarting")
    script.send(10, side="B")
    script.send(10.5, "build", side="B")
    script.send(11, side="A")
    script.to(12)
    assert script.sent() == [(11, "B", "spawn_pop", 2)]
    script.core.submit_state("A", "up")
    script.to(12.25)
    assert script.sent()[1:] == [(12.25, "A", "spawn_pop", 2), (12.25, "A", "spawn_sprinter", 1)]
    seqs = [c["data"]["command"]["seq"] for c in script.commands]
    assert seqs == [1, 2, 3]

    script = Script(make_core(KILL_SPAWN, BUILD_SPRINTER))
    script.send(0, side="B")
    script.send(1, side="B")
    script.send(2, "build", side="B")
    script.send(3, side="A")
    script.to(19.75)
    assert script.sent() == [(0, "A", "spawn_pop", 2), (3, "B", "spawn_pop", 2)]
    script.to(20)
    assert script.sent()[2:] == [(20, "A", "spawn_pop", 2), (20, "A", "spawn_sprinter", 1)]


def test_threats_window_is_open_on_the_left():
    script = Script(make_core(KILL_SPAWN, cooldown_s=0, threats=1))
    script.send(0)
    script.send(1)
    script.to(59.75)
    assert script.sent() == [(0, "B", "spawn_pop", 2)]
    script.to(60)
    assert script.sent()[1:] == [(60, "B", "spawn_pop", 2)]


def test_cmd_result_frees_place_once_after_duration():
    script = Script(make_core(rule("rain", "build", RAIN), KILL_SPAWN, cooldown_s=0, concurrent=1))
    script.send(0, "build")
    script.send(1)
    script.result(2, cmd_seq=1)
    script.to(6.75)
    assert script.sent() == [(0, "B", "weather_rain", None)]
    # Un doublon ne repousse pas la fin, et la place libérée sert dans le même tick.
    script.result(4, cmd_seq=1)
    script.to(7)
    assert script.sent()[1:] == [(7, "B", "spawn_pop", 2)]


def test_cmd_result_of_another_target_keeps_place():
    script = Script(make_core(KILL_SPAWN, cooldown_s=0, concurrent=1))
    script.send(0, side="A")
    script.send(0.25, side="B")
    assert script.sent() == [(0, "B", "spawn_pop", 2), (0.25, "A", "spawn_pop", 2)]
    script.send(1, side="A")
    script.result(2, cmd_seq=2, side="A")
    script.to(3)
    assert len(script.commands) == 2


def test_reserve_seq_keeps_commands_increasing():
    core = make_core(KILL_SPAWN, cooldown_s=0, concurrent=2)
    core.reserve_seq(3)
    core.reserve_seq(2)
    script = Script(core)
    script.send(0)
    script.send(1)
    # Le `cmd_result` d'une sonde ne libère aucune place de commande.
    script.result(1.5, cmd_seq=3)
    assert [c["data"]["command"]["seq"] for c in script.commands] == [4, 5]
    script.send(2)
    assert len(script.commands) == 2


def test_effect_without_n_coalesces():
    script = Script(make_core(rule("care", "zombie_kill", CARE)))
    script.send(0)
    script.send(1)
    script.send(1.5)
    assert [r["mt"] for r in script.records if r["type"] == "effect_coalesced"] == [1.5]
    script.to(20)
    assert [c["mt"] for c in script.commands] == [0, 20]
    assert script.commands[1]["data"]["command"]["items"] == CARE["items"]


def capped_units(script: Script) -> int:
    return sum(r["data"]["units"] for r in script.of_type("effect_capped"))


def sent_units(script: Script) -> int:
    return sum(c["data"]["command"]["n"] for c in script.commands)


def test_cap_per_min():
    capped = rule("kill_spawn", "zombie_kill", {**SPAWN, "cap_per_min": 60})
    script = Script(make_core(capped, cooldown_s=0, threats=12, concurrent=8))
    for te in range(40):
        script.send(te)
    script.to(200)
    assert sent_units(script) == 60
    assert capped_units(script) == 20
    assert [r["mt"] for r in script.of_type("effect_capped")] == list(range(30, 40))
    assert script.of_type("effect_capped")[0]["data"] == {
        "rule": "kill_spawn",
        "kind": "spawn_pop",
        "units": 2,
    }
    assert script.of_type("effect_capped")[0]["side"] == "B"


def test_cap_admits_partially_and_window_is_open_on_the_left():
    capped = rule("kill_spawn", "zombie_kill", {**SPAWN, "cap_per_min": 3})
    script = Script(make_core(capped, cooldown_s=0, threats=12, concurrent=8))
    script.send(0)
    script.send(1)
    assert [c["data"]["command"]["n"] for c in script.commands] == [2, 1]
    assert capped_units(script) == 1
    script.send(2)
    assert len(script.commands) == 2 and capped_units(script) == 3
    # À t = 60, les 2 unités de t = 0 sortent de ]0, 60], celle de t = 1 y reste.
    script.send(59.75)
    assert len(script.commands) == 2 and capped_units(script) == 5
    script.send(60)
    assert script.sent()[2:] == [(60, "B", "spawn_pop", 2)]
    assert capped_units(script) == 5


def test_cap_is_per_rule_effect_and_side():
    effect = {**SPAWN, "cap_per_min": 2}
    other = {**effect, "distance": [40, 80]}
    twin = {"id": "twin", "when": "atom(zombie_kill)", "effects": [effect, other]}
    build = rule("build_spawn", "build", effect)
    script = Script(make_core(twin, build, cooldown_s=0, concurrent=8))
    script.send(0)
    script.send(0.25, "build")
    script.send(0.5, side="B")
    assert [c["data"]["command"]["n"] for c in script.commands] == [2, 2, 2, 2, 2]
    assert capped_units(script) == 0
    script.send(1)
    assert capped_units(script) == 4


def test_capped_event_does_not_age_pending_lot():
    capped = rule("kill_spawn", "zombie_kill", {**SPAWN, "cap_per_min": 2})
    script = Script(make_core(capped, rule("build_spawn", "build", SPAWN)))
    script.send(0)
    script.send(1, "build")
    script.to(2)
    # En retard, de `te` antérieur au lot : retiré en entier, il ne le date pas.
    script.send(0.5)
    script.to(20)
    assert capped_units(script) == 2
    assert script.commands[1]["data"]["latency_ms"] == 19_000
    assert script.commands[1]["data"]["command"]["n"] == 2


def eight_effects() -> list:
    noise = {"kind": "noise", "radius": 10, "distance": [30, 60], "warn_s": 0}
    horde = {"kind": "horde_area", "n": 3, "radius": 5, "distance": [40, 70], "warn_s": 5}
    return [
        SPAWN,
        SPRINTER,
        horde,
        noise,
        RAIN,
        CARE,
        {**SPAWN, "distance": [35, 65]},
        {**noise, "radius": 20},
    ]


def five_lots(**values) -> Script:
    effects = [e for e in eight_effects() if "duration_s" not in e][:5]
    lots = {"id": "lots", "when": "atom(zombie_kill)", "effects": effects}
    return Script(make_core(lots, cooldown_s=0, concurrent=4, cmd_result_timeout_s=1, **values))


def test_threats_and_concurrency():
    # AC28 : 8 lots distincts, `threats_per_minute` tranche.
    lots = {"id": "lots", "when": "atom(zombie_kill)", "effects": eight_effects()}
    script = Script(make_core(lots, cooldown_s=0, concurrent=8))
    script.send(0)
    script.to(59.75)
    assert [c["mt"] for c in script.commands] == [0] * 6
    script.to(60)
    assert [c["mt"] for c in script.commands] == [0] * 6 + [60] * 2
    assert {c["data"]["command"]["kind"] for c in script.commands[:6]} == {
        "spawn_pop",
        "spawn_sprinter",
        "horde_area",
        "noise",
        "weather_rain",
        "supply_drop",
    }

    # AC28 : `concurrent_effects` tranche, sans expiration sur la fenêtre bornée.
    script = five_lots()
    script.send(0)
    script.to(0.75)
    assert [c["mt"] for c in script.commands] == [0] * 4
    assert script.of_type("cmd_timeout") == []

    # AC28 : un `cmd_result` libère sa place dans la passe de son tick.
    script = five_lots()
    script.send(0)
    script.result(0.5, cmd_seq=1)
    assert [c["mt"] for c in script.commands] == [0] * 4 + [0.5]
    script.to(0.75)
    assert len(script.commands) == 5

    # AC77 : les 4 expirent au même tick et la 5e part juste après, dans la même passe.
    script = five_lots()
    script.send(0)
    script.to(0.75)
    script.records = []
    script.to(1)
    assert [(r["type"], r["mt"]) for r in script.records] == [("cmd_timeout", 1)] * 4 + [
        ("command", 1)
    ]
    assert [r["data"] for r in script.records[:4]] == [{"seq": seq} for seq in (1, 2, 3, 4)]
    assert [r["side"] for r in script.records[:4]] == ["B"] * 4
    assert script.records[4]["data"]["command"]["seq"] == 5

    script = five_lots()
    script.send(0)
    script.to(10)
    assert [c["mt"] for c in script.commands] == [0] * 4 + [1]
    assert [r["mt"] for r in script.of_type("cmd_timeout")] == [1] * 4 + [2]


def test_late_cmd_result_frees_nothing():
    core = make_core(KILL_SPAWN, cooldown_s=0, concurrent=1, cmd_result_timeout_s=1)
    script = Script(core)
    script.send(0)
    script.send(0.5)
    script.to(1)
    assert [c["mt"] for c in script.commands] == [0, 1]
    script.send(1.25)
    # La place de la commande 1 est déjà rendue : la commande 2 garde la sienne.
    script.result(1.5, cmd_seq=1)
    script.to(1.75)
    assert len(script.commands) == 2
    script.to(2)
    assert [c["mt"] for c in script.commands] == [0, 1, 2]
    assert [r["data"]["seq"] for r in script.of_type("cmd_timeout")] == [1, 2]


def test_cmd_result_at_timeout_tick_is_not_a_timeout():
    script = Script(make_core(KILL_SPAWN, cooldown_s=0, concurrent=1, cmd_result_timeout_s=1))
    script.send(0)
    script.result(1, cmd_seq=1)
    script.to(5)
    assert script.of_type("cmd_timeout") == []


def test_lost_on_restart_frees_place_once():
    script = Script(make_core(KILL_SPAWN, cooldown_s=0, concurrent=1, threats=2))
    script.send(0)
    script.send(1)
    script.to(2)
    assert len(script.commands) == 1
    script.core.submit_lost(1)
    script.core.submit_lost(1)
    script.to(2.25)
    # Le lot part dans la passe de `lost_on_restart`, la perte n'étant journalisée qu'une fois.
    assert [r["type"] for r in script.records[-2:]] == ["lost_on_restart", "command"]
    assert script.of_type("lost_on_restart") == [
        {"type": "lost_on_restart", "data": {"seq": 1}, "side": "B", "mt": 2.25}
    ]
    assert script.sent()[1:] == [(2.25, "B", "spawn_pop", 2)]
    # La commande perdue reste comptée dans `threats_per_minute` : 2 sur 2.
    script.core.submit_lost(2)
    script.send(3)
    script.to(59.75)
    assert len(script.commands) == 2
    script.to(60)
    assert script.sent()[2:] == [(60, "B", "spawn_pop", 2)]
    # Sa place étant rendue, une commande perdue n'expire plus : 1 et 2 auraient expiré.
    assert script.of_type("cmd_timeout") == []


def test_lost_after_timeout_or_result():
    script = Script(make_core(KILL_SPAWN, cooldown_s=0, concurrent=2, cmd_result_timeout_s=1))
    script.send(0)
    script.send(0.25)
    script.result(0.5, cmd_seq=2)
    script.to(1)
    assert [r["data"]["seq"] for r in script.of_type("cmd_timeout")] == [1]
    # Sans `cmd_result`, la 1 est perdue malgré son `cmd_timeout` ; la 2 ne l'est pas.
    script.core.submit_lost(1)
    script.core.submit_lost(2)
    script.core.submit_lost(99)
    script.to(1.25)
    assert [r["data"]["seq"] for r in script.of_type("lost_on_restart")] == [1]


def test_lost_keeps_duration_of_answered_effect():
    script = Script(make_core(rule("rain", "build", RAIN), KILL_SPAWN, cooldown_s=0, concurrent=1))
    script.send(0, "build")
    script.result(1, cmd_seq=1)
    script.send(2)
    script.core.submit_lost(1)
    script.to(5.75)
    assert len(script.commands) == 1
    script.to(6)
    assert script.sent()[1:] == [(6, "B", "spawn_pop", 2)]


def test_inputs_keep_creation_order():
    script = Script(make_core(KILL_SPAWN))
    script.send(0)
    script.core.submit_lost(1)
    script.core.submit_state("A", "restarting")
    script.to(0.25)
    assert [r["type"] for r in script.records[-2:]] == ["lost_on_restart", "state"]


def test_slices_are_commands_and_rest_keeps_head():
    # Le reste repasse les conditions : ici, `concurrent_effects` le retient.
    script = Script(make_core(KILL_SPAWN, cooldown_s=0, concurrent=1, max_units_per_command=3))
    script.send(0)
    for te in (1, 2, 3):
        script.send(te)
    script.result(4, cmd_seq=1)
    assert script.sent()[1:] == [(4, "B", "spawn_pop", 3)]
    script.result(5, cmd_seq=2)
    assert script.sent()[2:] == [(5, "B", "spawn_pop", 3)]
    assert script.commands[2]["data"]["latency_ms"] == 4_000

    # Cooldown nul et places libres : le reste repart dans la même passe.
    script = Script(make_core(KILL_SPAWN, cooldown_s=0, max_units_per_command=1))
    script.send(0)
    assert script.sent() == [(0, "B", "spawn_pop", 1), (0, "B", "spawn_pop", 1)]
    assert [c["data"]["command"]["seq"] for c in script.commands] == [1, 2]
    assert script.commands[0]["data"]["command"]["origin"] == {
        "rule": "kill_spawn",
        "boot": 1,
        "event_seq": 1,
    }

    # Une tranche pose le cooldown : le reste attend, et le lot suivant ne le dépasse pas.
    core = make_core(KILL_SPAWN, BUILD_SPRINTER, max_units_per_command=5)
    script = Script(core)
    script.send(0)
    for te in (1, 2, 3, 4):
        script.send(te)
    script.send(5, "build")
    script.send(6)
    script.to(45)
    assert script.sent()[1:] == [
        (20, "B", "spawn_pop", 5),
        (40, "B", "spawn_pop", 5),
        (40, "B", "spawn_sprinter", 1),
    ]


def test_rest_of_lot_counts_against_threats():
    script = Script(make_core(KILL_SPAWN, cooldown_s=0, threats=1, max_units_per_command=1))
    script.send(0)
    assert script.sent() == [(0, "B", "spawn_pop", 1)]
    script.to(59.75)
    assert len(script.commands) == 1
    script.to(60)
    assert script.sent()[1:] == [(60, "B", "spawn_pop", 1)]


def test_pending_dropped_at_end():
    script = Script(make_core(KILL_SPAWN, rule("care", "build", CARE), duration_s=30))
    script.send(0)
    script.send(1)
    script.send(2)
    script.send(3, "build")
    script.send(4, side="B")
    script.send(5, side="B")
    script.to(29.75)
    # Le cooldown de 20 s a servi les lots de `spawn_pop` à t = 20.
    assert len(script.commands) == 5
    script.send(29.75)
    script.send(29.8)
    script.to(30)
    assert len(script.commands) == 5
    assert script.of_type("effect_dropped") == [
        {
            "type": "effect_dropped",
            "data": {"rule": "kill_spawn", "kind": "spawn_pop", "units": 4},
            "side": "B",
            "mt": 30,
        }
    ]
    assert script.records[-1]["type"] == "match_end"

    # Sans `n`, le lot jeté n'a pas d'unités ; les deux files sont vidées, A puis B.
    script = Script(make_core(rule("care", "build", CARE), KILL_SPAWN, cooldown_s=60))
    script.send(0, "build")
    script.send(1, "build")
    script.send(2, side="B")
    script.send(3, side="B")
    script.send(4)
    script.send(5)
    script.core.submit_end({"outcome": "forfeit", "winner": "A", "reason": "absence"})
    script.to(6)
    dropped = script.of_type("effect_dropped")
    assert [(r["side"], r["data"]["kind"], r["data"]["units"]) for r in dropped] == [
        ("A", "spawn_pop", 2),
        ("B", "supply_drop", None),
        ("B", "spawn_pop", 4),
    ]
    assert script.records[-1]["type"] == "match_end"
    written = len(script.records)
    script.to(100)
    assert len(script.records) == written


def end_of(script: Script) -> dict:
    (record,) = script.of_type("match_end")
    return record


def test_timer_gives_win_or_draw():
    script = Script(make_core(KILL_SPAWN, duration_s=10))
    script.send(1, side="B")
    script.to(9.75)
    assert script.core.result is None
    script.to(10)
    record = end_of(script)
    assert record["mt"] == 10 and record["side"] is None
    assert record["data"]["score"] == {"A": 0, "B": 1}
    assert record["data"]["result"]["outcome"] == "win"
    assert record["data"]["result"]["winner"] == "B"
    assert script.core.result == record["data"]["result"]

    script = Script(make_core(KILL_SPAWN, duration_s=10))
    script.send(1)
    script.send(2, side="B")
    script.to(10)
    assert end_of(script)["data"]["result"]["outcome"] == "draw"
    assert end_of(script)["data"]["result"]["winner"] is None

    # Un kill de l'ultime tick compte avant le minuteur.
    script = Script(make_core(KILL_SPAWN, duration_s=10))
    script.send(10)
    assert end_of(script)["data"]["result"]["winner"] == "A"


def test_sudden_death():
    script = Script(make_core(KILL_SPAWN))
    script.send(100, "player_death")
    record = end_of(script)
    assert record["mt"] == 100
    assert record["data"]["result"]["outcome"] == "death"
    assert record["data"]["result"]["winner"] == "B"

    # Deux morts au même tick : la première traitée décide, sans second `match_end`.
    script = Script(make_core(KILL_SPAWN))
    script.to(4.75)
    script.send(4.9, "player_death", side="B")
    assert script.core.result is None
    script.send(5, "player_death", side="A")
    assert end_of(script)["mt"] == 5
    assert end_of(script)["data"]["result"]["outcome"] == "death"
    assert end_of(script)["data"]["result"]["winner"] == "A"


def test_death_outside_match_is_ignored():
    script = Script(make_core(KILL_SPAWN))
    script.core.submit_event(
        parse_event(
            {
                "v": 1,
                "boot": 1,
                "seq": 1,
                "t_ms": T0_MS - 1000,
                "src": "A",
                "side": "A",
                "kind": "player_death",
                "data": {},
            },
            CATALOG,
        )
    )
    script.to(1)
    # D14 : journalisée, sans effet sur le résultat.
    assert [r["data"]["kind"] for r in script.of_type("event")] == ["player_death"]
    assert script.core.result is None

    script = Script(make_core(KILL_SPAWN))
    script.core.submit_event(
        parse_event(
            {
                "v": 1,
                "boot": 1,
                "seq": 1,
                "t_ms": T0_MS + 1000,
                "src": "A",
                "side": None,
                "kind": "player_death",
                "data": {},
            },
            CATALOG,
        )
    )
    script.to(1)
    assert script.core.result is None


def test_end_is_idempotent():
    forfeit = {"outcome": "forfeit", "winner": "B", "reason": "absence de A"}
    script = Script(make_core(KILL_SPAWN, duration_s=10))
    script.send(0)
    script.to(9.75)
    script.core.submit_end(forfeit)
    script.core.submit_end({"outcome": "invalidated", "winner": None, "reason": "dérive"})
    script.to(10)
    assert end_of(script)["data"]["result"] == forfeit
    assert end_of(script)["mt"] == 10

    # Après la fin : aucun tick, aucun enregistrement, même sur un nouvel intrant.
    mt = script.core.mt
    script.core.submit_end({"outcome": "invalidated", "winner": None, "reason": "dérive"})
    script.core.submit_state("A", "restarting")
    script.core.submit_lost(1)
    assert script.core.advance(T0_MS + 60_000) == []
    assert script.core.run_tick() == []
    assert script.core.mt == mt and script.core.result == forfeit


def test_death_wins_over_submitted_end_of_same_tick():
    script = Script(make_core(KILL_SPAWN))
    script.to(4.75)
    script.core.submit_end({"outcome": "forfeit", "winner": "A", "reason": "absence de B"})
    script.send(5, "player_death", side="A")
    assert end_of(script)["data"]["result"]["outcome"] == "death"
    assert end_of(script)["data"]["result"]["winner"] == "B"


def test_cmd_timeout_of_end_tick_precedes_match_end():
    script = Script(make_core(KILL_SPAWN, duration_s=1, cmd_result_timeout_s=1))
    script.send(0)
    script.to(1)
    assert [r["type"] for r in script.records[-2:]] == ["cmd_timeout", "match_end"]


def test_supply_items_reach_command():
    presets = Path(rules.__file__).resolve().parent / "presets"
    preset = json.loads((presets / "duel_rapide.json").read_text(encoding="utf-8"))

    def hurt(raw: dict) -> list:
        runtime = load_runtime()
        table = load_table(raw, CATALOG, runtime["limits"])
        script = Script(MatchCore(table, runtime, CATALOG, T0_MS, raw["duration_s"]))
        script.send(0, "damage_taken")
        (command,) = script.commands
        parse_command(command["data"]["command"], CATALOG)
        assert command["data"]["command"]["kind"] == "supply_drop"
        return command["data"]["command"]["items"]

    assert hurt(preset) == [
        {"item": "Base.Bandage", "count": 2},
        {"item": "Base.Disinfectant", "count": 1},
    ]
    edited = copy.deepcopy(preset)
    pills = [{"item": "Base.Pills", "count": 3}]
    (care,) = [r for r in edited["rules"] if r["id"] == "hurt_supply"][0]["effects"]
    care["items"] = pills
    assert hurt(edited) == pills

    # Mêmes `items` en attente : 1 lot, 1 `effect_coalesced`.
    script = Script(make_core(rule("care", "zombie_kill", CARE), rule("twin", "build", CARE)))
    script.send(0)
    script.send(1)
    script.send(2, "build")
    assert len(script.of_type("effect_coalesced")) == 1
    script.to(100)
    assert [c["mt"] for c in script.commands] == [0, 20]

    # `items` différents : 2 lots, sans fusion.
    other = {**CARE, "items": pills}
    script = Script(make_core(rule("care", "zombie_kill", CARE), rule("other", "build", other)))
    script.send(0)
    script.send(1)
    script.send(2, "build")
    assert script.of_type("effect_coalesced") == []
    script.to(100)
    assert [c["mt"] for c in script.commands] == [0, 20, 40]
    assert script.commands[1]["data"]["command"]["items"] == CARE["items"]
    assert script.commands[2]["data"]["command"]["items"] == pills
