"""Lobby et match réels sur les doubles : sondes, sceau, décompte, ticks, fins de match."""

from __future__ import annotations

import json
import math
import threading
import shutil

import pytest

from test_contract import PLAYER, SIDES, Lobby, MS, L_S, state_entered_at
from test_servers import stopped_for_good
from test_snapshots import engine_rig  # noqa: F401
from service import engine as engine_module
from service.journal import JOURNAL_NAME
from service.replay import main as replay_main
from service.state import MatchState
from service.verdict import spawn_status, verdict

PRESET = "demo"
PROBE_TIMEOUT_S = 30
KILL = {"w": "Base.Axe", "x": 10500, "y": 9900, "origin": "world"}
LONG_MATCH_S = 300


@pytest.fixture
def presets(tmp_path, monkeypatch):
    """Copie des presets : modifier le fichier scellé ne touche pas à ceux du dépôt."""
    folder = tmp_path / "presets"
    shutil.copytree(engine_module.PRESETS_DIR, folder)
    monkeypatch.setattr(engine_module, "PRESETS_DIR", str(folder))
    return folder


def test_rules_hash_sealed(engine_rig, presets):
    lobby = Lobby(engine_rig, PRESET)
    sealed = lobby.state()["config"]["rules_hash"]
    path = presets / f"{PRESET}.json"
    content = path.read_bytes()
    assert content.count(b'"n": 2') == 1
    path.write_bytes(content.replace(b'"n": 2', b'"n": 3'))

    lobby.join_and_resolve()
    state = engine_rig.wait("error", "countdown")

    assert state["phase"] == "error"
    assert sealed in state["error"]
    assert "countdown" not in lobby.phases()
    assert lobby.step("lobby")["state"] == "error"
    assert stopped_for_good(engine_rig)


def test_blank_does_not_break_the_seal(engine_rig, presets):
    """Le hash porte sur le JSON canonique : un blanc ne change pas les règles."""
    lobby = Lobby(engine_rig, PRESET)
    path = presets / f"{PRESET}.json"
    path.write_bytes(path.read_bytes() + b"\n\n")

    lobby.join_and_resolve()

    state = engine_rig.wait("error", "countdown")
    assert state["phase"] == "countdown", state["error"]


def test_unreadable_preset_refuses_countdown(engine_rig, presets):
    lobby = Lobby(engine_rig, PRESET)
    sealed = lobby.state()["config"]["rules_hash"]
    (presets / f"{PRESET}.json").unlink()

    lobby.join_and_resolve()

    state = engine_rig.wait("error", "countdown")
    assert state["phase"] == "error"
    assert sealed in state["error"]
    assert stopped_for_good(engine_rig)


def failed(lobby: Lobby, side: str, seq: int) -> None:
    lobby.mods[side].cmd_result(seq, "failed", reason="exception")


def refused(lobby: Lobby, side: str, seq: int) -> None:
    lobby.mods[side].cmd_result(seq, "refused", reason="asleep")


def nothing_spawned(lobby: Lobby, side: str, seq: int) -> None:
    lobby.mods[side].cmd_result(seq, "applied", spawned=0)


def no_result(lobby: Lobby, side: str, seq: int) -> None:
    lobby.at(PROBE_TIMEOUT_S - 1)
    lobby.settle()
    assert lobby.state()["phase"] == "lobby"
    assert lobby.state()["capabilities"]["spawn"][side] == "unknown"
    lobby.at(PROBE_TIMEOUT_S)


@pytest.mark.parametrize("scenario", [failed, refused, nothing_spawned, no_result])
@pytest.mark.parametrize("side", SIDES)
def test_spawn_probe_failure_is_signalled(engine_rig, scenario, side):
    assert engine_rig.runtime["probe_timeout_s"] == PROBE_TIMEOUT_S
    lobby = Lobby(engine_rig, PRESET)

    scenario(lobby, side, lobby.join(side))
    state = engine_rig.wait("error")

    assert f"camp {side}" in state["error"]
    assert "addZombiesInOutfit" in state["error"]
    assert "solo_ipc" in state["error"]
    assert state["capabilities"]["fallback"] == "solo_ipc"
    assert state["capabilities"]["spawn"][side] == "ko"
    assert state["servers"] == {"A": "down", "B": "down"}
    assert stopped_for_good(engine_rig)
    assert lobby.step("lobby")["state"] == "error"
    assert spawn_status(engine_rig.journal(lobby.match_id), PROBE_TIMEOUT_S)[side] == "ko"


def test_probe_sent_once_at_first_connection(engine_rig):
    lobby = Lobby(engine_rig, PRESET)
    probe = engine_rig.runtime["probe_spawn"]

    first = lobby.join("B")
    second = lobby.join("A")
    assert (first, second) == (1, 2)
    lobby.mods["B"].ack(first)
    engine_rig.until(lambda _: not lobby.mods["B"].in_path.read_bytes())
    lobby.settle()

    commands = lobby.records("command")
    assert [record["side"] for record in commands] == ["B", "A"]
    assert all(record["mt"] is None for record in commands)
    assert commands[0]["data"] == {
        "command": {"v": 1, "seq": 1, "kind": "probe_spawn", **probe},
        "latency_ms": None,
    }
    written = json.loads(lobby.mods["A"].in_path.read_bytes())
    assert written == commands[1]["data"]["command"]

    lobby.mods["A"].cmd_result(second, "applied", spawned=1)
    lobby.mods["B"].cmd_result(first, "applied", spawned=2)
    state = engine_rig.wait("countdown", "error")
    assert state["capabilities"]["spawn"] == {"A": "ok", "B": "ok"}
    assert state["capabilities"]["fallback"] is None
    assert len(lobby.records("cmd_result")) == len(SIDES)
    records = engine_rig.journal(lobby.match_id)
    assert spawn_status(records, PROBE_TIMEOUT_S) == {"A": "ok", "B": "ok"}


def test_players_come_from_beats_and_silence_is_no_failure(engine_rig):
    lobby = Lobby(engine_rig, PRESET)
    assert lobby.state()["players"] == {"A": None, "B": None}

    lobby.at(300)
    lobby.settle()
    state = lobby.state()
    assert state["phase"] == "lobby"
    assert state["error"] is None
    assert state["servers"] == {"A": "up", "B": "up"}

    seq = lobby.join("A")
    engine_rig.until(lambda s: s["players"] == {"A": PLAYER, "B": None})
    assert lobby.state()["capabilities"]["spawn"]["A"] == "unknown"

    lobby.mods["A"].cmd_result(seq, "applied", spawned=1)
    lobby.mods["A"].beat(players=())
    engine_rig.until(lambda s: s["players"]["A"] is None)
    assert lobby.state()["phase"] == "lobby"


def test_no_countdown_without_both_players(engine_rig):
    """Sondes résolues, joueur parti : `L` et le décompte attendent son retour."""
    lobby = Lobby(engine_rig, PRESET)
    seq_a = lobby.join("A")
    lobby.mods["A"].cmd_result(seq_a, "applied", spawned=1)
    seq_b = lobby.join("B")
    lobby.mods["A"].beat(players=())
    engine_rig.until(lambda s: s["players"]["A"] is None)
    lobby.mods["B"].cmd_result(seq_b, "applied", spawned=1)
    engine_rig.until(lambda s: s["capabilities"]["spawn"]["B"] == "ok")
    lobby.settle()
    assert lobby.state()["phase"] == "lobby"

    lobby.at(10)
    lobby.mods["A"].beat(players=(PLAYER,))

    state = engine_rig.wait("countdown", "error")
    assert state["phase"] == "countdown", state["error"]
    assert state_entered_at(lobby, "countdown") == (L_S + 10) * MS


def test_countdown_follows_current_instant(engine_rig):
    lobby = Lobby(engine_rig, PRESET)
    lobby.join_and_resolve()
    state = engine_rig.wait("countdown", "error")

    assert state["clock"]["countdown"] == engine_rig.runtime["countdown_s"] == 3
    assert state["clock"]["remaining_s"] == state["config"]["duration_s"]
    assert lobby.step("lobby")["state"] == "ok"

    lobby.at(0.5)
    lobby.settle()
    assert lobby.state()["clock"]["countdown"] == 3
    lobby.at(1)
    engine_rig.until(lambda s: s["clock"].get("countdown") == 2)
    lobby.at(2)
    engine_rig.until(lambda s: s["clock"].get("countdown") == 1)
    lobby.at(3)
    engine_rig.until(lambda s: "countdown" not in s["clock"])


def test_cancel_during_countdown_stops_servers(engine_rig):
    lobby = Lobby(engine_rig, PRESET)
    lobby.join_and_resolve()
    engine_rig.wait("countdown")

    assert lobby.engine.action("cancel")[0]

    state = engine_rig.wait("idle", "error")
    assert state["phase"] == "idle", state["error"]
    assert stopped_for_good(engine_rig)


def edit_preset(folder, **changes) -> None:
    """Change la copie du preset : le sceau se calcule ensuite sur ce contenu."""
    path = folder / f"{PRESET}.json"
    table = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**table, **changes}), encoding="utf-8")


class Duel(Lobby):
    """Match réel à un instant fourni par le test, `te` étant compté depuis `t0_ms`."""

    def __init__(self, rig, preset: str = PRESET) -> None:
        super().__init__(rig, preset)
        self.t0_s = 0.0
        self.te_s = 0.0
        # Deux ticks : l'arrondi de l'horloge flottante peut en manger un.
        self.step_s = 2 * rig.runtime["tick_ms"] / MS

    def start_match(self, lobby_s: float = 0) -> None:
        self.t0_s = lobby_s + self.rig.runtime["countdown_s"]
        self.join_and_resolve()
        state = self.rig.wait("countdown", "error")
        assert state["phase"] == "countdown", state["error"]
        self.at_match(0)
        state = self.rig.wait("running", "error")
        assert state["phase"] == "running", state["error"]

    def at_match(self, te_s: float) -> None:
        self.te_s = te_s
        self.at(self.t0_s + te_s)
        for mod in self.mods.values():
            mod.t_ms = round((L_S + self.t0_s + te_s) * MS)

    def reach(self, te_s: float) -> dict:
        """Avance à `te_s` et attend la passe qui y a jugé le match."""
        self.at_match(te_s)
        duration_s = self.state()["config"]["duration_s"]
        remaining_s = math.ceil(duration_s - te_s)
        return self.rig.until(
            lambda s: s["phase"] != "running" or s["clock"]["remaining_s"] == remaining_s
        )

    def read(self, side: str) -> None:
        """Attend que `pump` ait lu tout `out` : ce qu'il a daté porte l'instant courant."""
        reader = self.engine._match._readers[side]
        size = self.mods[side].out_path.stat().st_size
        self.rig.until(lambda _: reader.offset == size)

    def beat(self, side: str, age: float = 0.0, kills: int | None = None) -> bytes:
        line = self.mods[side].beat(age=age, players=(PLAYER,), kills=kills)
        self.read(side)
        return line

    def played(self, line: bytes) -> None:
        """Avance jusqu'à ce que le cœur ait journalisé `line`, à un tick suivant."""
        self.at_match(self.te_s + self.step_s)
        expected = json.loads(line)
        journal = self.rig.journal
        self.rig.until(lambda _: any(r["data"] == expected for r in journal(self.match_id)))

    def kills(self, side: str, count: int) -> None:
        for _ in range(count):
            line = self.mods[side].emit("zombie_kill", KILL)
        self.played(line)

    def finish(self) -> dict:
        state = self.rig.wait("over", "error")
        assert state["phase"] == "over", state["error"]
        assert state["servers"] == {"A": "down", "B": "down"}
        assert stopped_for_good(self.rig)
        return state

    def match_end(self) -> dict:
        (record,) = self.records("match_end")
        return record

    def replays(self) -> bool:
        path = self.rig.matches / self.match_id / JOURNAL_NAME
        return replay_main([str(path)]) == 0


def test_full_real_path(engine_rig, presets, runtime, monkeypatch):
    phases = []

    class RecordedState(MatchState):
        def __setattr__(self, name, value):
            if name == "phase" and phases[-1:] != [value]:
                phases.append(value)
            super().__setattr__(name, value)

    monkeypatch.setattr(engine_module, "MatchState", RecordedState)
    engine_rig.runtime["ports"] = runtime["ports"]
    duel = Duel(engine_rig)
    assert duel.state()["invite"]["port"] == 17271

    duel.start_match()
    t0_ms = round((L_S + duel.t0_s) * MS)
    mod = duel.mods["B"]
    duel.at_match(1)
    mod.write_raw(b"not json\n")
    duel.kills("A", 1)
    # La sonde de B, jamais acquittée par le test, reste en tête de `in`.
    engine_rig.until(lambda _: len(mod.in_path.read_bytes().splitlines()) == 2)
    command = json.loads(mod.in_path.read_bytes().splitlines()[-1])
    # Les sondes ont pris les `seq` 1 et 2, et un kill de A vise B.
    assert command["seq"] == 3
    assert (command["kind"], command["n"]) == ("spawn_pop", 2)
    assert command["origin"]["rule"] == "kill_baseline"

    mod.ack(command["seq"])
    engine_rig.until(lambda _: not mod.in_path.read_bytes())
    line = mod.cmd_result(command["seq"], "applied", spawned=2)
    duel.played(line)
    state = engine_rig.until(lambda s: s["score"]["A"] == 1)
    assert state["threats_per_min"] == 1.0
    assert state["clock"]["remaining_s"] < state["config"]["duration_s"]
    assert any("spawn_pop" in event["text"] for event in state["events"])

    duel.at_match(state["config"]["duration_s"])
    state = duel.finish()

    assert phases == [
        "idle", "preflight", "starting", "contract", "lobby",
        "countdown", "running", "stopping", "over",
    ]  # fmt: skip
    assert state["result"]["outcome"] == "win"
    assert state["result"]["winner"] == "A"
    assert state["clock"]["remaining_s"] == 0
    assert state["capabilities"]["attribution"] == "insufficient"
    assert "simulé" not in json.dumps(state, ensure_ascii=False)

    records = engine_rig.journal(duel.match_id)
    start = next(
        index for index, r in enumerate(records) if r["data"].get("phase") == "running"
    )
    assert records[start]["data"]["t0_ms"] == t0_ms
    assert records[start]["mt"] == 0
    assert all(record["mt"] is None for record in records[:start])
    tick_s = engine_rig.runtime["tick_ms"] / MS
    for record in records[start:]:
        assert record["mt"] is not None
        assert abs(record["mt"] / tick_s - round(record["mt"] / tick_s)) < 1e-6
    (bad_line,) = duel.records("bad_line")
    assert bad_line["side"] == "B"
    end = next(index for index, r in enumerate(records) if r["type"] == "match_end")
    assert [r["data"]["phase"] for r in records[end:] if "phase" in r["data"]] == [
        "stopping",
        "over",
    ]
    assert records[end]["mt"] == state["config"]["duration_s"]
    assert {r["mt"] for r in records[end:]} == {records[end]["mt"]}
    assert duel.replays()


def timer_win(duel: Duel) -> None:
    duel.at_match(10)
    duel.kills("A", 4)
    duel.kills("B", 3)
    duel.at_match(LONG_MATCH_S)


def timer_draw(duel: Duel) -> None:
    duel.at_match(10)
    duel.kills("A", 3)
    duel.kills("B", 3)
    duel.at_match(LONG_MATCH_S)


def sudden_death(duel: Duel) -> None:
    duel.at_match(100)
    duel.played(duel.mods["A"].emit("player_death", {}))


@pytest.mark.parametrize(
    "scenario, outcome, winner, score, end_s",
    [
        (timer_win, "win", "A", {"A": 40, "B": 30}, LONG_MATCH_S),
        (timer_draw, "draw", None, {"A": 30, "B": 30}, LONG_MATCH_S),
        (sudden_death, "death", "B", {"A": 0, "B": 0}, None),
    ],
)
def test_outcomes(engine_rig, presets, scenario, outcome, winner, score, end_s):
    edit_preset(
        presets,
        duration_s=LONG_MATCH_S,
        disconnect_grace_s=2 * LONG_MATCH_S,
        score={"points": {"zombie_kill": 10}},
    )
    duel = Duel(engine_rig)
    duel.start_match()

    scenario(duel)
    state = duel.finish()

    assert state["result"]["outcome"] == outcome
    assert state["result"]["winner"] == winner
    assert state["score"] == score
    record = duel.match_end()
    assert record["data"]["result"] == state["result"]
    assert record["data"]["score"] == score
    if end_s is None:
        assert record["mt"] < LONG_MATCH_S / 2
    else:
        assert record["mt"] == end_s
    assert duel.replays()


@pytest.mark.parametrize("absent", SIDES)
def test_disconnect_grace(engine_rig, presets, absent):
    edit_preset(presets, duration_s=LONG_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    present = "B" if absent == "A" else "A"
    grace_s = duel.engine.preset(PRESET)["disconnect_grace_s"]
    assert grace_s == 120

    duel.at_match(10)
    duel.beat("A")
    duel.beat("B")
    duel.at_match(100)
    duel.beat(present)
    # Un heartbeat sans joueur ne compte pas : le serveur tique, le joueur reste absent.
    duel.mods[absent].beat(players=())
    duel.read(absent)

    state = duel.reach(10 + grace_s - 1)
    assert state["phase"] == "running"
    assert state["result"] is None

    duel.at_match(10 + grace_s)
    state = duel.finish()
    assert state["result"]["outcome"] == "forfeit"
    assert state["result"]["winner"] == present
    assert absent in state["result"]["reason"]
    assert duel.replays()


def test_disconnect_grace_both_absent(engine_rig, presets):
    edit_preset(presets, duration_s=LONG_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(10)
    duel.beat("A")
    duel.beat("B")

    state = duel.reach(10 + 119)
    assert state["phase"] == "running"

    duel.at_match(10 + 120)
    state = duel.finish()
    assert state["result"]["outcome"] == "invalidated"
    assert state["result"]["winner"] is None
    assert duel.match_end()["data"]["result"] == state["result"]
    assert duel.replays()


def test_returning_player_restarts_the_grace(engine_rig, presets):
    edit_preset(presets, duration_s=LONG_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()

    duel.at_match(100)
    duel.beat("A")
    duel.beat("B")
    duel.at_match(200)
    duel.beat("A")
    duel.beat("B")

    state = duel.reach(250)
    assert state["phase"] == "running"


@pytest.mark.parametrize(
    "day_length, first_age_a, advance_a, advance_b, drift_s, outcome",
    [
        (1, 3.0, 1.6, 0.0, 60.0, None),
        (1, 3.0, 1.7, 0.0, 63.75, "invalidated"),
        (1, 0.0, 0.0, 1.7, -63.75, "invalidated"),
        (2, 0.0, 1.0, 0.0, 75.0, "invalidated"),
        (1, 3.0, 1.25, 1.25, 0.0, None),
    ],
)
def test_drift_real_seconds(
    engine_rig, presets, day_length, first_age_a, advance_a, advance_b, drift_s, outcome
):
    edit_preset(presets, day_length=day_length)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.beat("A", age=first_age_a)
    duel.beat("B", age=0.0)
    state = engine_rig.until(lambda s: s["clock"]["drift_s"] == 0)
    assert state["phase"] == "running"

    duel.at_match(11)
    # Sans attendre la lecture : la fin de match arrête celle de `out` (D11).
    duel.mods["A"].beat(age=first_age_a + advance_a, players=(PLAYER,))
    line = duel.mods["B"].beat(age=advance_b, players=(PLAYER,))

    state = engine_rig.until(lambda s: s["clock"]["drift_s"] == drift_s)
    if outcome is None:
        duel.played(line)
        assert duel.state()["phase"] == "running"
        assert duel.state()["result"] is None
    else:
        duel.at_match(duel.te_s + duel.step_s)
        state = duel.finish()
        assert state["clock"]["drift_s"] == drift_s
        assert state["result"]["outcome"] == outcome
        assert state["result"]["winner"] is None
        assert str(abs(drift_s)) in state["result"]["reason"]
        assert duel.replays()


def test_drift_judged_after_both_queues(engine_rig, presets):
    """Deux camps à égalité, lus A puis B dans une même passe : aucune dérive à juger."""
    edit_preset(presets, day_length=1)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.beat("A", age=0.0)
    duel.beat("B", age=0.0)
    engine_rig.until(lambda s: s["clock"]["drift_s"] == 0)

    match = duel.engine._match
    pump, gate = match.pump, threading.Lock()

    def gated_pump(now_ms: int) -> str | None:
        with gate:
            return pump(now_ms)

    match.pump = gated_pump
    duel.at_match(11)
    # La passe attend : les deux heartbeats sont en file quand elle lit `out`.
    with gate:
        duel.mods["A"].beat(age=1.7, players=(PLAYER,))
        line = duel.mods["B"].beat(age=1.7, players=(PLAYER,))

    duel.played(line)
    state = duel.reach(duel.te_s + duel.step_s)
    assert state["phase"] == "running"
    assert state["result"] is None
    assert state["clock"]["drift_s"] == 0


def test_beat_silence_by_phase(engine_rig):
    duel = Duel(engine_rig)
    timeout_s = engine_rig.runtime["beat_timeout_s"]
    assert timeout_s == 30

    duel.at(300)
    duel.settle()
    state = duel.state()
    assert state["phase"] == "lobby"
    assert state["error"] is None
    assert state["servers"] == {"A": "up", "B": "up"}

    duel.start_match(300)
    duel.at_match(1)
    duel.beat("A")
    duel.beat("B")

    state = duel.reach(1 + timeout_s - 1)
    assert state["phase"] == "running"
    assert state["servers"] == {"A": "up", "B": "up"}
    assert state["players"] == {"A": PLAYER, "B": PLAYER}

    duel.at_match(1 + timeout_s)
    state = engine_rig.until(lambda s: s["servers"] == {"A": "suspect", "B": "suspect"})
    assert state["phase"] == "running"
    assert state["players"] == {"A": None, "B": None}

    duel.at_match(duel.te_s + 1)
    line = duel.beat("A")
    state = engine_rig.until(lambda s: s["servers"]["A"] == "up")
    assert state["servers"]["B"] == "suspect"
    assert state["players"] == {"A": PLAYER, "B": None}
    duel.played(line)

    states = [r for r in duel.records("state") if "server" in r["data"]]
    in_match = [r for r in states if r["mt"] is not None]
    assert [(r["side"], r["data"]["server"]) for r in in_match] == [
        ("A", "suspect"),
        ("B", "suspect"),
        ("A", "up"),
    ]


def attributed_flow(duel: Duel, attributed: int, reference: int) -> dict:
    """`reference` kills du jeu entre H0 et Hk, dont `attributed` attribués à A."""
    duel.start_match()
    duel.at_match(1)
    duel.beat("A", kills=0)
    mod = duel.mods["A"]
    for _ in range(attributed):
        mod.emit("zombie_kill", KILL)
    for _ in range(reference - attributed):
        mod.emit("zombie_kill", KILL, side=None)
    duel.at_match(2)
    duel.beat("A", kills=reference)
    duel.at_match(duel.state()["config"]["duration_s"])
    return duel.finish()


def test_attribution_ko_reported_at_end(engine_rig):
    reference = engine_rig.runtime["attribution_min_kills"]

    measured = Duel(engine_rig)
    fine = attributed_flow(measured, reference, reference)
    assert fine["capabilities"]["attribution"] == "ok"
    assert fine["capabilities"]["fallback"] is None
    assert "attribution" not in fine["result"]["reason"]
    assert measured.match_end()["data"]["attribution"] == "ok"
    assert measured.match_end()["data"]["fallback"] is None
    assert measured.replays()

    faulty = Duel(engine_rig)
    state = attributed_flow(faulty, reference - 1, reference)

    assert state["result"]["outcome"] == fine["result"]["outcome"] == "win"
    assert state["result"]["winner"] == fine["result"]["winner"] == "A"
    assert state["result"]["reason"].startswith(fine["result"]["reason"])
    assert "attribution" in state["result"]["reason"]
    assert "java_agent" in state["result"]["reason"]
    assert state["capabilities"]["attribution"] == "ko"
    assert state["capabilities"]["fallback"] == "java_agent"
    end = faulty.match_end()["data"]
    assert end["attribution"] == "ko"
    assert end["fallback"] == "java_agent"
    assert end["spawn"] == {"A": "ok", "B": "ok"}
    assert end["result"] == state["result"]
    warned = [log["msg"] for log in state["logs"] if log["level"] == "warn"]
    assert any("attribution" in msg and "java_agent" in msg for msg in warned)
    # Le verdict en ligne de commande relit le même journal et conclut pareil.
    report = verdict(engine_rig.journal(faulty.match_id))
    assert report["attribution"]["status"] == "ko"
    assert (report["attribution"]["reference"], report["attribution"]["attributed"]) == (
        reference,
        reference - 1,
    )
    assert faulty.replays()


def test_cancel_during_match_stops_servers(engine_rig):
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.kills("A", 1)

    assert duel.engine.action("cancel")[0]

    state = engine_rig.wait("idle", "error")
    assert state["phase"] == "idle", state["error"]
    assert state["result"] is None
    assert stopped_for_good(engine_rig)
    assert duel.records("match_end") == []
    assert all(r["mt"] is not None for r in duel.records("state")[-2:])
    assert duel.replays()
