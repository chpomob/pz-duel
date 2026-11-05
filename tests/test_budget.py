"""R4 : budgets et files de lots de `MatchCore` (AC26, AC72).

`cap_per_min`, tranches, `cmd_timeout` et fin de match viennent avec les budgets complets.
"""

from __future__ import annotations

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


def make_core(*rule_list: dict, cooldown_s=20, threats=6, concurrent=4) -> MatchCore:
    raw = {
        "id": "essai",
        "label": "Essai",
        "tagline": "table de test",
        "duration_s": 600,
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
    runtime = load_runtime()
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
