"""R6 : rejeu d'un journal et télémétrie (AC45, AC46, AC47, AC63, AC64, AC76).

Les fixtures de `tests/fixtures/` sont figées : chacune sort d'un flux scripté ci-dessous,
passé par `MatchCore.advance` et `Journal`, sans passer par le rejeu. Les tests assertent
des valeurs littérales, pour que le rejeu ne soit pas jugé contre lui-même. Pour en
régénérer une après un changement voulu : `PYTHONPATH=. python tests/test_replay.py`.
"""

from __future__ import annotations

import ast
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from service import replay, rules
from service.journal import Journal, read_journal
from service.protocol import PROTOCOL_VERSION, parse_event
from service.rules import MatchCore, load_effects, load_runtime, load_table, rules_hash

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PRESET_PATH = Path(rules.__file__).resolve().parent / "presets" / "duel_rapide.json"
ROOT = FIXTURES.parent.parent
T0_MS = 1_793_830_000_000
TICK_S = 0.25
SPAWN = {"kind": "spawn_pop", "n": 2, "distance": [30, 60], "warn_s": 6}
SPRINTER = {"kind": "spawn_sprinter", "n": 1, "distance": [40, 70], "warn_s": 10}
KILL_RULE = {"id": "kill_spawn", "when": "atom(zombie_kill)", "effects": [SPAWN]}
SERIES_RULE = {
    "id": "series",
    "when": "atom(build).then(atom(zombie_kill), within=30s)",
    "effects": [SPRINTER],
}
BEAT = {"age": 1, "players": [], "hp": None, "kills": None}
FORFEIT = {"outcome": "forfeit", "winner": "A", "reason": "absence de B"}


def make_table(*rule_list: dict, cooldown_s=20, threats=6, concurrent=4, duration_s=600) -> dict:
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
    runtime = load_runtime()
    return load_table(raw, load_effects(), runtime["limits"])


class Flow:
    """Match scripté : joue le rôle de `pump`, sans transport ni serveur.

    Entre `start` et la fin, `to` avance le cœur et journalise ce qu'il rend, ce que fera
    `RealMatch`. Le temps est celui du script : aucune horloge n'est lue.
    """

    def __init__(self, dir, table, runtime, catalog, duration_s=None):
        self.table = table
        self.runtime = runtime
        self.catalog = catalog
        self.duration_s = table["duration_s"] if duration_s is None else duration_s
        self.now_ms = T0_MS - 10_000
        self.seqs = {"A": 0, "B": 0}
        self.probes: list[int] = []
        self.core: MatchCore | None = None
        self.journal = Journal(dir, clock=lambda: self.now_ms)
        start = {
            "match_id": "m1",
            "preset": table,
            "rules_hash": rules_hash(table),
            "ports": runtime["ports"],
            "protocol": PROTOCOL_VERSION,
            "runtime": runtime,
            "effects": catalog,
            "duration_s": self.duration_s,
            "day_length": table["day_length"],
        }
        self.journal.write("match_start", start)

    def line(self, te_s, kind="zombie_kill", side="A", src=None, data=None) -> dict:
        src = src or side
        self.seqs[src] += 1
        line = {
            "v": 1,
            "boot": 1,
            "seq": self.seqs[src],
            "t_ms": T0_MS + round(te_s * 1000),
            "src": src,
            "side": side,
            "kind": kind,
            "data": {"origin": "world"} if data is None else data,
        }
        return dict(parse_event(line, self.catalog))

    def probe(self, side: str) -> None:
        seq = len(self.probes) + 1
        params = {k: v for k, v in self.runtime["probe_spawn"].items() if k != "origin"}
        origin = self.runtime["probe_spawn"]["origin"]
        command = {"v": PROTOCOL_VERSION, "seq": seq, "kind": "probe_spawn", **params}
        data = {"command": {**command, "origin": origin}, "latency_ms": None}
        self.journal.write("command", data, side)
        self.probes.append(seq)

    def start(self) -> None:
        self.now_ms = T0_MS
        self.journal.write("state", {"phase": "running", "t0_ms": T0_MS}, mt=0)
        self.core = MatchCore(self.table, self.runtime, self.catalog, T0_MS, self.duration_s)
        for seq in self.probes:
            self.core.reserve_seq(seq)

    def to(self, mt_s: float) -> None:
        self.now_ms = max(self.now_ms, T0_MS + round(mt_s * 1000))
        for record in self.core.advance(T0_MS + round(mt_s * 1000)):
            self.journal.write(**record)

    def submit(self, te_s, kind="zombie_kill", side="A", src=None, data=None) -> None:
        self.core.submit_event(self.line(te_s, kind, side, src, data))

    def send(self, mt_s, te_s=None, kind="zombie_kill", side="A", src=None, data=None) -> None:
        """Fait traiter un événement au tick `mt_s`, daté de `te_s` s'il est en retard."""
        self.to(mt_s - TICK_S)
        self.submit(mt_s if te_s is None else te_s, kind, side, src, data)
        self.to(mt_s)

    def answer(self, mt_s: float, cmd_seq: int, side="B") -> None:
        data = {"seq": cmd_seq, "status": "applied", "spawned": 2}
        self.send(mt_s, kind="cmd_result", side=side, data=data)

    def close(self, *lines: tuple[str, dict]) -> None:
        for type, data in lines:
            self.journal.write(type, data, mt=self.core.mt)
        self.journal.close()


def script_replay_1000(flow: Flow) -> None:
    """1000 `event` sur 2 camps : kills, blessures, retards, `side` nul et `cmd_result`."""
    rng = random.Random(1000)
    flow.start()
    answered = 0
    for index in range(1000):
        mt = TICK_S * (4 + index * 5)
        side = "A" if rng.random() < 0.5 else "B"
        draw = rng.random()
        late = 1.5 if rng.random() < 0.1 else 0
        if draw < 0.05:
            flow.send(mt, mt - late, side=None, src=side)
        elif draw < 0.2:
            flow.send(mt, mt - late, "damage_taken", side, data={})
        else:
            flow.send(mt, mt - late, side=side)
        if index % 25 == 24:
            answered += 1
            flow.answer(mt + TICK_S, answered)
    flow.to(flow.duration_s)
    flow.close(("state", {"phase": "over"}))


def script_replay_clock(flow: Flow) -> None:
    """Un match complet, du lobby à la fin : voir `test_replay_same_clock_rule`."""
    write = flow.journal.write
    for phase in ("preflight", "starting", "contract", "lobby"):
        write("state", {"phase": phase})
    write("bad_line", {"offset": 12, "reason": "bad_json"}, "A")
    for side in ("A", "B"):
        write("event", flow.line(-9, "beat", side, data=BEAT), side)
        flow.probe(side)
        data = {"seq": len(flow.probes), "status": "applied", "spawned": 1}
        write("cmd_result", flow.line(-8, "cmd_result", side, data=data), side)
    withdrawn = {"atom": "damage_taken", "hook": "OnWeaponHitCharacter", "sides": ["B"]}
    write("atom_withdrawn", withdrawn)
    write("state", {"phase": "countdown"})
    flow.start()

    flow.to(0)
    # De `te`=0, mais reçu tick 0 clos : déjà en retard au tick 0,25.
    flow.send(0.25, 0)
    flow.to(4.75)
    # Même tick, même `t_ms` : le tri de `submit` l'emporte sur l'ordre de réception.
    flow.submit(5, side="B")
    flow.submit(5, side="A")
    flow.to(5)
    # Silence de 60 s : le lot vers B part à 20,25 et un `cmd_timeout` tombe sans intrant.
    flow.send(65)
    flow.send(66, kind="build", data={})
    # Retards : le kill de `te`=66,5 arrive à 68 et déclenche la série à sa date source.
    flow.send(68, 66.5)
    flow.send(70.25, 69)
    flow.to(79.75)
    flow.core.submit_state("B", "restarting")
    flow.core.submit_lost(6)
    flow.to(80)
    flow.send(85, side="B")
    flow.to(89.75)
    flow.core.submit_state("B", "up")
    flow.to(90)
    write("dup", {"src": "A", "boot": 1, "seq": 3}, "A", mt=flow.core.mt)
    # À 110, le cooldown est écoulé : seul `match_end` retient le lot de ce kill.
    flow.to(109.75)
    flow.submit(110)
    flow.core.submit_end(FORFEIT)
    flow.to(110)
    flow.close(("state", {"phase": "stopping"}), ("state", {"phase": "over"}))


def script_summary_known(flow: Flow) -> None:
    """12 commandes sur A en 2 minutes, 20 unités plafonnées, 3 `side` nuls de `src` A."""
    flow.start()
    for index in range(12):
        mt = index * 10
        # La latence vaut 0, 100, ou 200 pour la dernière : c'est le p95 de ces 12 valeurs.
        flow.send(mt, mt - (0 if index == 0 else 0.2 if index == 11 else 0.1), side="B")
        if index == 5:
            for extra in range(4):
                flow.send(55 + extra, side="B")
        if index % 4 == 0:
            flow.send(mt + 1, side=None, src="A")
    flow.send(130, kind="beat", side="A", data=BEAT)
    flow.to(flow.duration_s)
    flow.close()


def script_restart_records(flow: Flow) -> None:
    """AC64 : une commande sans résultat à 290, un lot en attente, une perte à 300."""
    flow.start()
    flow.send(290)
    flow.send(295)
    flow.to(299.75)
    flow.core.submit_lost(1)
    flow.to(flow.duration_s)
    flow.close()


def kill_rule_capped() -> dict:
    effect = {"kind": "spawn_pop", "n": 5, "distance": [30, 60], "warn_s": 0, "cap_per_min": 30}
    return {"id": "kill_spawn", "when": "atom(zombie_kill)", "effects": [effect]}


SCRIPTS = {
    "replay_1000": (script_replay_1000, lambda: json.loads(PRESET_PATH.read_text("utf-8")), None),
    "replay_clock": (
        script_replay_clock,
        lambda: make_table(KILL_RULE, SERIES_RULE),
        None,
    ),
    "summary_known": (
        script_summary_known,
        lambda: make_table(kill_rule_capped(), cooldown_s=10, threats=12, concurrent=8),
        180,
    ),
    # `duration_s` de la table plus court que le match : le rejeu doit lire celui de D15.
    "restart_records": (
        script_restart_records,
        lambda: make_table(KILL_RULE, cooldown_s=0, concurrent=1, duration_s=100),
        310,
    ),
}


def run_script(name: str, dir, table=None, runtime=None, catalog=None, duration_s=None) -> Path:
    script, make, default_duration = SCRIPTS[name]
    flow = Flow(
        dir,
        table or make(),
        runtime or load_runtime(),
        catalog or load_effects(),
        duration_s or default_duration,
    )
    script(flow)
    return flow.journal.path


def fixture(name: str) -> Path:
    return FIXTURES / f"{name}.jsonl"


def records_of(path: Path) -> list[dict]:
    return read_journal(path).records


def write_records(path: Path, records: list[dict]) -> Path:
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def tampered(tmp_path, name: str, change) -> Path:
    """Copie la fixture `name` après `change`, qui reçoit la liste de ses enregistrements."""
    records = records_of(fixture(name))
    change(records)
    return write_records(tmp_path / "tampered.jsonl", records)


def without(records: list[dict], n: int) -> None:
    records[:] = [record for record in records if record["n"] != n]


def run(capsys, *argv) -> tuple[int, str]:
    code = replay.main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def of_type(records: list[dict], type: str) -> list[dict]:
    return [record for record in records if record["type"] == type]


def sent(records: list[dict]) -> list[tuple]:
    return [
        (r["n"], r["mt"], r["side"], r["data"]["command"]["seq"], r["data"]["command"].get("n"))
        for r in of_type(records, "command")
    ]


@pytest.mark.parametrize("name", SCRIPTS)
def test_fixture_is_the_script_output(tmp_path, name):
    """Le flux vivant, sans le rejeu, redonne la fixture octet pour octet.

    Le script repart du `match_start` figé : ce test ne dépend pas de `runtime.json`.
    """
    start = records_of(fixture(name))[0]["data"]
    path = run_script(
        name, tmp_path, start["preset"], start["runtime"], start["effects"], start["duration_s"]
    )
    assert path.read_bytes() == fixture(name).read_bytes()


def test_replay_matches_and_detects_tamper(tmp_path, capsys):
    records = records_of(fixture("replay_1000"))
    events = of_type(records, "event")
    assert len(events) == 1000
    assert {event["data"]["src"] for event in events} == {"A", "B"}

    produced = replay.replay_records(records)
    journaled, replayed = of_type(records, "command"), of_type(produced, "command")
    assert len(journaled) == len(replayed) == 173
    for record, other in zip(journaled, replayed):
        assert record["data"] == other["data"]
        assert abs(record["mt"] - other["mt"]) <= TICK_S
    assert len(of_type(records, "late_event")) == len(of_type(produced, "late_event")) == 113

    code, output = run(capsys, fixture("replay_1000"))
    assert code == 0 and "rejeu identique" in output

    target = journaled[100]

    def alter(lines):
        lines[target["n"] - 1]["data"]["command"]["distance"] = [31, 60]

    code, output = run(capsys, tampered(tmp_path, "replay_1000", alter))
    assert code == 1 and f"n={target['n']} " in output

    # Une commande retirée en fin de journal : le rejeu en produit une de trop.
    code, output = run(capsys, tampered(tmp_path, "replay_clock", lambda r: without(r, 47)))
    assert code == 1 and "après n=46 " in output


def test_truncated_tail(tmp_path, capsys):
    whole = fixture("replay_clock").read_bytes()
    cut = tmp_path / "cut.jsonl"

    cut.write_bytes(whole[:-5])
    code, output = run(capsys, cut)
    assert code == 0 and "1 ligne ignorée" in output

    # Coupé au milieu du tick 90, puis sur `match_end` : le rejeu va plus loin que le
    # journal dans ce dernier tick, sans que ce soit un écart.
    lines = whole.splitlines(keepends=True)
    for n in (46, 47, 52):
        cut.write_bytes(b"".join(lines[: n - 1]) + lines[n - 1][:40])
        code, output = run(capsys, cut)
        assert code == 0 and "1 ligne ignorée" in output
        assert len(read_journal(cut).records) == n - 1

    # Une troncature ne couvre que le dernier tick : un écart antérieur reste un écart.
    records = read_journal(cut).records
    without(records, 30)
    code, output = run(capsys, write_records(cut, records))
    assert code == 1 and "n=33 " in output

    # Une ligne écrite tick clos prouve que rien n'en manque : `dup` suit la commande
    # retirée, qui n'a donc pas pu être perdue par la coupure.
    records = [record for record in records_of(fixture("replay_clock")) if record["n"] <= 48]
    without(records, 47)
    code, output = run(capsys, write_records(cut, records))
    assert code == 1 and "après n=46 " in output and '"seq": 9' in output

    # Même preuve par une ligne du cœur : `rule_fired` s'écrit après le `late_event` retiré.
    records = [record for record in records_of(fixture("replay_clock")) if record["n"] <= 18]
    without(records, 17)
    code, output = run(capsys, write_records(cut, records))
    assert code == 1 and "late_event" in output and "absent du journal" in output

    cut.write_bytes(whole)
    code, output = run(capsys, cut)
    assert code == 0 and "ignorée" not in output


@pytest.mark.parametrize("name", ["replay_clock", "restart_records", "summary_known"])
def test_every_cut_replays(tmp_path, capsys, name):
    whole = fixture(name).read_bytes()
    cut = tmp_path / "cut.jsonl"
    lines = whole.splitlines(keepends=True)
    for n in range(2, len(lines) + 1):
        cut.write_bytes(b"".join(lines[: n - 1]) + lines[n - 1][:40])
        code, output = run(capsys, cut)
        assert code == 0 and "1 ligne ignorée" in output, f"coupe à n={n} : {output}"


def test_summary_values(capsys):
    code, output = run(capsys, "--summary", fixture("summary_known"))
    assert code == 0
    assert json.loads(output) == {
        "A": {
            "commands": 12,
            "commands_per_minute": [6, 6, 0],
            "units": 60,
            "units_per_minute": [30, 30, 0],
            "capped_units": 20,
            "null_side_events": 3,
            # Le `beat` de la minute 2 n'est pas une activité : A n'a joué qu'en minutes 0 et 1.
            "silent_minutes": 1,
            "latency_ms_p95": 200,
        },
        "B": {
            "commands": 0,
            "commands_per_minute": [0, 0, 0],
            "units": 0,
            "units_per_minute": [0, 0, 0],
            "capped_units": 0,
            "null_side_events": 0,
            "silent_minutes": 1,
            "latency_ms_p95": None,
        },
    }


def test_summary_counts_no_probe_and_keeps_stdout_json(tmp_path, capsys):
    summary = replay.summarize(records_of(fixture("replay_clock")))
    assert summary["A"]["commands"] + summary["B"]["commands"] == 7

    cut = tmp_path / "cut.jsonl"
    cut.write_bytes(fixture("summary_known").read_bytes()[:-5])
    assert replay.main(["--summary", str(cut)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["A"]["commands"] == 12
    assert "1 ligne ignorée" in captured.err


def test_replay_uses_journaled_runtime(tmp_path, capsys, monkeypatch):
    runtime = {**load_runtime(), "max_units_per_command": 30, "cmd_result_timeout_s": 20}
    effect = {"kind": "spawn_pop", "n": 20, "distance": [30, 60], "warn_s": 6}
    rule = {"id": "kill_spawn", "when": "atom(zombie_kill)", "effects": [effect]}
    flow = Flow(tmp_path, make_table(rule, cooldown_s=0, concurrent=1), runtime, load_effects())
    flow.start()
    flow.to(4.75)
    flow.submit(5)
    flow.submit(5)
    flow.to(40)
    flow.core.submit_end(FORFEIT)
    flow.to(40.25)
    flow.close()

    records = records_of(flow.journal.path)
    assert [item[1:] for item in sent(records)] == [(5, "B", 1, 30), (25, "B", 2, 10)]
    start = records[0]["data"]["runtime"]
    assert (start["max_units_per_command"], start["cmd_result_timeout_s"]) == (30, 20)

    disk = tmp_path / "disk_runtime.json"
    disk.write_text(
        json.dumps({**load_runtime(), "max_units_per_command": 5, "cmd_result_timeout_s": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr(rules, "RUNTIME_PATH", disk)
    monkeypatch.setattr(rules, "EFFECTS_PATH", tmp_path / "absent.json")
    assert rules.load_runtime()["max_units_per_command"] == 5

    code, output = run(capsys, flow.journal.path)
    assert code == 0, output

    for name in ("runtime", "effects"):
        stripped = [dict(record) for record in records]
        stripped[0]["data"] = {k: v for k, v in records[0]["data"].items() if k != name}
        code, output = run(capsys, write_records(tmp_path / "stripped.jsonl", stripped))
        assert code == 1 and f"`{name}`" in output


def test_replay_applies_restart_records(tmp_path, capsys):
    records = records_of(fixture("restart_records"))
    start = records[0]["data"]
    assert start["preset"]["budget"]["concurrent_effects"] == 1
    assert start["runtime"]["cmd_result_timeout_s"] == 60
    assert [item[1:] for item in sent(records)] == [(290, "B", 1, 2), (300, "B", 2, 2)]
    lost = of_type(records, "lost_on_restart")
    assert [(r["n"], r["mt"], r["data"]) for r in lost] == [(8, 300, {"seq": 1})]
    assert of_type(records, "cmd_result") == []
    # Le lot part dans la passe qui traite la perte, juste après elle.
    assert records[8]["type"] == "command" and records[8]["n"] == 9

    produced = replay.replay_records(records)
    assert [(r["mt"], r["data"]["command"]["seq"]) for r in of_type(produced, "command")] == [
        (290, 1),
        (300, 2),
    ]
    code, output = run(capsys, fixture("restart_records"))
    assert code == 0, output

    code, output = run(capsys, tampered(tmp_path, "restart_records", lambda r: without(r, 8)))
    assert code == 1 and "n=9 " in output


def test_replay_same_clock_rule(tmp_path, capsys, monkeypatch):
    records = records_of(fixture("replay_clock"))

    running = next(r for r in records if r["data"].get("phase") == "running")
    assert (running["n"], running["mt"]) == (15, 0)
    assert all(r["mt"] is None for r in records[: running["n"] - 1])
    assert {"match_start", "state", "atom_withdrawn", "bad_line"} <= {
        r["type"] for r in records[: running["n"] - 1]
    }
    assert all((r["mt"] * 4) % 1 == 0 for r in records[running["n"] - 1 :])
    assert records[-3]["type"] == "match_end" and records[-3]["mt"] == 110
    assert records[-4]["type"] == "effect_dropped"

    # Sondes 1 et 2 ; 20,25 en plein silence ; 90 au retour de B, qui attendait depuis 85.
    assert [item[1:] for item in sent(records)] == [
        (None, "A", 1, 1),
        (None, "B", 2, 1),
        (0.25, "B", 3, 2),
        (5, "A", 4, 2),
        (20.25, "B", 5, 2),
        (65, "B", 6, 2),
        (85, "A", 7, 2),
        (90, "B", 8, 4),
        (90, "B", 9, 1),
    ]
    assert [(r["n"], r["mt"], r["data"]["te"]) for r in of_type(records, "late_event")] == [
        (17, 0.25, 0),
        (33, 68, 66.5),
        (37, 70.25, 69),
    ]
    same_tick = [r["data"]["src"] for r in of_type(records, "event") if r["mt"] == 5]
    assert same_tick == ["A", "B"]

    def no_clock(*args):
        raise AssertionError("horloge système lue pendant le rejeu")

    with monkeypatch.context() as patch:
        for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter"):
            patch.setattr(time, name, no_clock)
        code = replay.main([str(fixture("replay_clock"))])
        produced = replay.replay_records(records)
    assert code == 0, capsys.readouterr().err

    def compared(lines):
        kept = [r for r in lines if r["type"] in ("command", "late_event") and r["mt"] is not None]
        return [(r["type"], r["mt"], r["data"]) for r in kept]

    assert compared(produced) == compared(records)

    def move(lines):
        assert lines[26]["n"] == 27 and lines[26]["type"] == "event"
        lines[26]["mt"] += TICK_S

    code, output = run(capsys, tampered(tmp_path, "replay_clock", move))
    assert code == 1 and "n=30 " in output

    def drop_t0(lines):
        del lines[14]["data"]["t0_ms"]

    code, output = run(capsys, tampered(tmp_path, "replay_clock", drop_t0))
    assert code == 1 and "`t0_ms`" in output


def test_module_reads_no_clock():
    tree = ast.parse(Path(replay.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module)
    assert "json" in imported and "time" not in imported


def test_state_and_probes_are_inputs(tmp_path, capsys):
    # Sans `restarting`, le lot vers B n'attend plus le retour du serveur.
    code, output = run(capsys, tampered(tmp_path, "replay_clock", lambda r: without(r, 39)))
    assert code == 1 and "n=46 " in output

    # Sans les sondes, la première commande du moteur reprendrait le `seq` 1.
    def drop_probes(lines):
        without(lines, 8)
        without(lines, 11)

    code, output = run(capsys, tampered(tmp_path, "replay_clock", drop_probes))
    assert code == 1 and "n=19 " in output


def test_mt_off_grid_cites_n(tmp_path, capsys):
    for mt in (5.1, -0.25, "5"):

        def shift(lines, mt=mt):
            lines[20]["mt"] = mt

        code, output = run(capsys, tampered(tmp_path, "replay_clock", shift))
        assert code == 1 and "hors grille" in output and "n=21 " in output


def test_unusable_journals_give_code_1(tmp_path, capsys):
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    code, output = run(capsys, empty)
    assert code == 1 and "`match_start`" in output

    code, output = run(capsys, tmp_path / "absent.jsonl")
    assert code == 1 and "journal illisible" in output

    def break_event(lines):
        del lines[15]["data"]["t_ms"]

    code, output = run(capsys, tampered(tmp_path, "replay_clock", break_event))
    assert code == 1 and "n=16 " in output

    def drop_duration(lines):
        del lines[0]["data"]["duration_s"]

    code, output = run(capsys, tampered(tmp_path, "replay_clock", drop_duration))
    assert code == 1 and "`duration_s`" in output


def test_lobby_only_journal_has_nothing_to_replay(tmp_path, capsys):
    records = [r for r in records_of(fixture("replay_clock")) if r["mt"] is None]
    code, output = run(capsys, write_records(tmp_path / "lobby.jsonl", records))
    assert code == 0, output


@pytest.mark.parametrize("honest", [True, False])
def test_seal_is_checked_even_without_a_single_tick(tmp_path, capsys, honest):
    """Journal annulé au décompte : rien à rejouer, mais la table scellée se contrôle."""
    records = [r for r in records_of(fixture("replay_clock")) if r["mt"] is None]
    sealed = rules_hash(records[0]["data"]["preset"]) if honest else "0" * 64
    n = records[-1]["n"] + 1
    records.append({"n": n, "type": "table_sealed", "mt": None, "data": {"rules_hash": sealed}})

    code, output = run(capsys, write_records(tmp_path / "countdown.jsonl", records))

    assert code == (0 if honest else 1), output
    assert honest or f"`table_sealed` à n={n}" in output


def test_command_line(tmp_path):
    def cli(*argv):
        command = [sys.executable, "-m", "service.replay", *map(str, argv)]
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

    assert cli(fixture("restart_records")).returncode == 0
    done = cli("--summary", fixture("summary_known"))
    assert done.returncode == 0 and json.loads(done.stdout)["A"]["capped_units"] == 20

    broken = tampered(tmp_path, "restart_records", lambda r: without(r, 8))
    done = cli(broken)
    assert done.returncode == 1 and "n=9 " in done.stderr


if __name__ == "__main__":
    import tempfile

    for script_name in SCRIPTS:
        with tempfile.TemporaryDirectory() as scratch:
            journal = run_script(script_name, scratch)
            fixture(script_name).write_bytes(journal.read_bytes())
