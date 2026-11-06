"""R4 : horloge à ticks de `MatchCore` (AC73, AC74, AC75).

Les événements passent par `parse_event` et les tables par `load_table` : le cœur ne
voit que ce que le contrat et le chargeur admettent.
"""

from __future__ import annotations

from service.journal import Journal, read_journal
from service.protocol import parse_event
from service.rules import MatchCore, load_effects, load_runtime, load_table

T0_MS = 1_793_830_000_000
CATALOG = load_effects()
SPAWN = {"kind": "spawn_pop", "n": 2, "distance": [30, 60], "warn_s": 6}
KILL_RULE = {"id": "kill_spawn", "when": "atom(zombie_kill)", "effects": [SPAWN]}
SERIES_RULE = {
    "id": "series",
    "when": "atom(build).then(atom(zombie_kill), within=30s)",
    "effects": [SPAWN],
}


def make_core(*rule_list: dict, cooldown_s=20, concurrent=4) -> MatchCore:
    raw = {
        "id": "essai",
        "label": "Essai",
        "tagline": "table de test",
        "duration_s": 600,
        "day_length": 1,
        "mods": ["duelcore"],
        "atoms": ["zombie_kill", "build"],
        "rules": list(rule_list) or [KILL_RULE],
        "modifiers": {},
        "budget": {
            "threats_per_minute": 6,
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


def event(te_s: float, seq=1, kind="zombie_kill", side="A", data=None):
    line = {
        "v": 1,
        "boot": 1,
        "seq": seq,
        "t_ms": T0_MS + round(te_s * 1000),
        "src": side,
        "side": side,
        "kind": kind,
        "data": {"origin": "world"} if data is None else data,
    }
    return parse_event(line, CATALOG)


def result(te_s: float, cmd_seq: int, seq=2, side="B"):
    data = {"seq": cmd_seq, "status": "applied", "spawned": 2}
    return event(te_s, seq, "cmd_result", side, data)


def at(mt_s: float) -> int:
    return T0_MS + round(mt_s * 1000)


def of_type(records: list, type: str) -> list:
    return [record for record in records if record["type"] == type]


def spy_ticks(core: MatchCore, monkeypatch) -> list:
    """Relève le `mt` de chaque tick réellement exécuté par `advance`."""
    ticks: list = []
    run_tick = core.run_tick

    def counted():
        records = run_tick()
        ticks.append(core.mt)
        return records

    monkeypatch.setattr(core, "run_tick", counted)
    return ticks


def test_ticks_advance_in_silence(monkeypatch):
    core = make_core()
    ticks = spy_ticks(core, monkeypatch)
    assert core.advance(T0_MS - 1) == [] and core.mt is None
    core.submit_event(event(0, seq=1))
    records = core.advance(at(0))
    # De `te`=0 lui aussi, mais reçu après la clôture du tick 0 : le lot attend le cooldown.
    core.submit_event(event(0, seq=2))
    records += core.advance(at(20))

    assert ticks == [k * 0.25 for k in range(81)]
    assert [(r["mt"], r["data"]["command"]["n"]) for r in of_type(records, "command")] == [
        (0, 2),
        (20, 2),
    ]

    core = make_core()
    core.advance(at(5.1))
    assert core.mt == 5
    ticks = spy_ticks(core, monkeypatch)
    core.advance(at(6.6))
    assert ticks == [5.25, 5.5, 5.75, 6, 6.25, 6.5]

    core = make_core()
    core.advance(at(7))
    core.submit_event(event(500))
    assert core.advance(at(7)) == [] and core.mt == 7
    records = core.advance(at(7.25))
    assert core.mt == 7.25
    assert [r["type"] for r in records[:2]] == ["event", "rule_fired"]
    assert records[0]["mt"] == 7.25 and of_type(records, "late_event") == []


def test_late_event_next_tick():
    core = make_core()
    core.advance(at(10))
    core.submit_event(event(10.1))
    records = core.advance(at(10.25))
    assert records[0]["type"] == "event" and records[0]["mt"] == 10.25
    assert of_type(records, "late_event") == []

    core = make_core()
    core.advance(at(10.5))
    core.submit_event(event(10.1))
    records = core.advance(at(10.75))
    assert of_type(records, "event")[0]["mt"] == 10.75
    assert of_type(records, "late_event") == [
        {
            "type": "late_event",
            "data": {"boot": 1, "seq": 1, "te": 10.1, "mt": 10.75},
            "side": "A",
            "mt": 10.75,
        }
    ]

    core = make_core()
    core.advance(at(10.5))
    core.submit_event(event(10.5))
    assert len(of_type(core.advance(at(10.75)), "late_event")) == 1

    core = make_core(SERIES_RULE)
    core.submit_event(event(0, seq=1, kind="build"))
    core.advance(at(30.75))
    core.submit_event(event(30, seq=2))
    records = core.advance(at(31))
    assert [r["mt"] for r in of_type(records, "late_event")] == [31]
    fired = of_type(records, "rule_fired")
    assert [(r["data"]["rule"], r["mt"]) for r in fired] == [("series", 31)]

    core = make_core(SERIES_RULE)
    core.submit_event(event(0, seq=1, kind="build"))
    core.advance(at(30.75))
    core.submit_event(event(30.25, seq=2))
    assert of_type(core.advance(at(31)), "rule_fired") == []


def test_tick_zero_has_no_closed_tick():
    core = make_core()
    core.submit_event(event(-1, seq=1))
    core.submit_event(event(0, seq=2))
    records = core.advance(at(0))
    assert of_type(records, "late_event") == []
    assert len(of_type(records, "event")) == 2
    assert core.scores == {"A": 1, "B": 0}

    core.submit_event(event(-1, seq=3))
    assert len(of_type(core.advance(at(0.25)), "late_event")) == 1


def test_tick_order():
    def journal(first: str, second: str) -> list:
        core = make_core()
        core.advance(at(4.75))
        core.submit_event(event(5, seq=7, side=first))
        core.submit_event(event(5, seq=7, side=second))
        return core.advance(at(5))

    assert journal("A", "B") == journal("B", "A")
    records = journal("B", "A")
    assert [r["side"] for r in of_type(records, "event")] == ["A", "B"]
    assert [r["type"] for r in records] == [
        "event",
        "rule_fired",
        "event",
        "rule_fired",
        "command",
        "command",
    ]
    assert [r["side"] for r in of_type(records, "command")] == ["A", "B"]

    def with_full_place(result_te=None, kill_te=4.9) -> list:
        core = make_core(cooldown_s=0, concurrent=1)
        core.submit_event(event(0, seq=1))
        core.advance(at(4.75))
        core.submit_event(event(kill_te, seq=2))
        if result_te is not None:
            core.submit_event(result(result_te, cmd_seq=1))
        return core.advance(at(5))

    assert of_type(with_full_place(), "command") == []
    for result_te, kill_te in ((4.9, 4.95), (4.95, 4.9)):
        records = with_full_place(result_te, kill_te)
        sent = [(r["data"]["command"]["seq"], r["mt"]) for r in of_type(records, "command")]
        assert sent == [(2, 5)]
        assert records[-1]["type"] == "command"
        assert len(of_type(records, "cmd_result")) == 1

    # Un tick qui traite `match_end` n'émet rien, pas même le lot de son propre kill.
    core = make_core(cooldown_s=0)
    core.advance(at(4.75))
    core.submit_event(event(5, seq=1))
    core.submit_end({"outcome": "forfeit", "winner": "A", "reason": "absence de B"})
    records = core.advance(at(5))
    assert [r["type"] for r in records] == ["event", "rule_fired", "effect_dropped", "match_end"]
    assert {r["mt"] for r in records} == {5}
    assert core.advance(at(6)) == [] and core.mt == 5


def test_service_inputs_after_events_before_emission():
    core = make_core()
    core.advance(at(0))
    core.submit_state("B", "restarting")
    core.submit_event(event(0.25))
    records = core.advance(at(0.25))
    assert [r["type"] for r in records] == ["event", "rule_fired", "state"]
    assert records[-1]["side"] == "B" and records[-1]["data"] == {"server": "restarting"}


def test_records_fit_journal(tmp_path):
    core = make_core()
    core.advance(at(0))
    core.submit_event(event(0.1))
    records = core.advance(at(0.25))
    core.submit_event(event(0.3, seq=2))
    core.submit_end({"outcome": "draw", "winner": None, "reason": "fin soumise"})
    records += core.advance(at(0.5))
    journal = Journal(tmp_path, clock=lambda: T0_MS)
    for record in records:
        journal.write(**record)
    journal.close()

    written = read_journal(journal.path).records
    assert [(r["type"], r["mt"], r["side"]) for r in written] == [
        ("event", 0.25, "A"),
        ("rule_fired", 0.25, "A"),
        ("command", 0.25, "B"),
        ("event", 0.5, "A"),
        ("rule_fired", 0.5, "A"),
        ("effect_dropped", 0.5, "B"),
        ("match_end", 0.5, None),
    ]
    assert written[1]["data"]["event"] == {"src": "A", "boot": 1, "seq": 1}


def test_short_of_end_holds_a_death_for_the_tick_that_may_conclude():
    """Lue la première, la mort de B attend : celle de A, plus ancienne, décide en arrivant."""
    core = make_core()
    core.advance(at(1))
    core.submit_event(event(2, seq=2, kind="player_death", side="B", data={}))
    assert core.conclusion_due(at(2))

    core.advance_short_of_end(at(3))
    assert core.result is None

    core.submit_event(event(1.5, seq=3, kind="player_death", side="A", data={}))
    records = core.advance(at(3.25))
    assert core.result["winner"] == "B"
    assert of_type(records, "match_end")


def test_short_of_end_keeps_what_does_not_conclude_moving():
    core = make_core()
    core.submit_event(event(0, seq=1, kind="player_death", side="B", data={}))
    core.submit_event(event(-1, seq=2, kind="player_death", side="A", data={}))
    core.submit_event(event(0, seq=3))
    records = core.advance_short_of_end(at(0))

    assert [r["data"]["seq"] for r in of_type(records, "event")] == [2, 3]
    assert core.scores["A"] == 1


def test_short_of_end_lets_a_submitted_end_close():
    """Une panne de `out` soumet sa fin et interdit de finir la lecture : elle doit clore."""
    core = make_core()
    core.submit_event(event(0, seq=1, kind="player_death", side="B", data={}))
    core.submit_end({"outcome": "invalidated", "winner": None, "reason": "panne"})
    core.advance_short_of_end(at(0))

    assert core.result["winner"] == "A"


def test_conclusion_is_due_at_the_tick_of_the_timer_only():
    core = make_core()
    end_s = core.duration_s
    assert not core.conclusion_due(at(end_s) - 1)
    assert core.conclusion_due(at(end_s))
    core.advance(at(end_s))
    assert not core.conclusion_due(at(end_s))
