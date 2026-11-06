"""Lobby et match réels sur les doubles : sondes, sceau, décompte, ticks, fins de match."""

from __future__ import annotations

import inspect
import json
import math
import threading
import shutil
from pathlib import Path

import pytest

from doubles.fake_mod import EMPTY_AGE_PER_BEAT, EmptyServer, FakeMod
from test_contract import (
    L_S,
    MS,
    PLAYERS,
    SIDES,
    Lobby,
    crash,
    crash_and_hold,
    state_entered_at,
)
from test_modes import call
from test_servers import stopped_for_good
from test_snapshots import (  # noqa: F401
    PLAYER_PASSWORD,
    WAIT_S,
    engine_rig,
    make_world,
    world_db,
    world_dir,
    worlds_state,
)
from service import engine as engine_module
from service.api import make_server
from service.journal import JOURNAL_NAME, JournalError
from service.proofs import PROOFS_NAME
from service.protocol import dump_line, parse_session
from service.replay import main as replay_main
from service.servers import read_ini
from service.state import PHASE_LABELS, MatchState
from service.transport import DRAIN_PASSES, POLL_MAX
from service.verdict import VerdictError, spawn_status, verdict

PRESET = "demo"
PROBE_TIMEOUT_S = 30
KILL = {"w": "Base.Axe", "x": 10500, "y": 9900, "origin": "world"}
LONG_MATCH_S = 300
RESTART_MATCH_S = 900
# Une seule ligne, jetée d'un bloc : elle retarde ce qui la suit sans remplir le journal.
OVER_POLL_LINE = b"x" * POLL_MAX + b"\n"
SETTLE_PASSES = 3


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


def answer_probes(lobby: Lobby, side: str, seq: int, status: str, reason: str) -> None:
    """Répond de même à chaque essai, jusqu'au terme du délai."""
    elapsed_s = 0
    while True:
        lobby.mods[side].cmd_result(seq, status, reason=reason)
        wait_retry_planned(lobby, side, seq)
        elapsed_s += lobby.rig.runtime["probe_retry_s"]
        lobby.at(elapsed_s)
        if elapsed_s >= PROBE_TIMEOUT_S:
            return
        seq = next_probe(lobby, side, seq)


def wait_retry_planned(lobby: Lobby, side: str, seq: int) -> None:
    """Attend que le moteur ait lu le refus de l'essai `seq` : l'horloge peut alors avancer."""

    def planned() -> bool:
        probe = lobby.engine._match._probes.get(side)
        return probe is not None and probe.seq == seq and probe.retry_ms is not None

    lobby.rig.until(lambda _: planned())


def still_refused(lobby: Lobby, side: str, seq: int) -> None:
    """Tous les essais du délai sont refusés : seul l'échec persistant conclut."""
    answer_probes(lobby, side, seq, "refused", "asleep")


def still_no_square(lobby: Lobby, side: str, seq: int) -> None:
    answer_probes(lobby, side, seq, "failed", "no_square")


def next_probe(lobby: Lobby, side: str, seq: int) -> int:
    """Attend l'essai suivant de la sonde, et rend son `seq`."""
    path = lobby.mods[side].in_path

    def latest() -> int:
        lines = path.read_bytes().splitlines()
        return json.loads(lines[-1])["seq"] if lines else seq

    lobby.rig.until(lambda _: latest() > seq)
    return latest()


def nothing_spawned(lobby: Lobby, side: str, seq: int) -> None:
    lobby.mods[side].cmd_result(seq, "applied", spawned=0)


def no_result(lobby: Lobby, side: str, seq: int) -> None:
    lobby.at(PROBE_TIMEOUT_S - 1)
    lobby.settle()
    assert lobby.state()["phase"] == "lobby"
    assert lobby.state()["capabilities"]["spawn"][side] == "unknown"
    lobby.at(PROBE_TIMEOUT_S)


@pytest.mark.parametrize(
    "scenario", [failed, still_refused, still_no_square, nothing_spawned, no_result]
)
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


@pytest.mark.parametrize(
    "status, reason", [("refused", "no_player"), ("refused", "asleep"), ("failed", "no_square")]
)
def test_transient_probe_refusal_is_retried(engine_rig, status, reason):
    """Deux refus passagers, puis un spawn : le lancement continue, sur les mêmes 30 s."""
    retry_s = engine_rig.runtime["probe_retry_s"]
    lobby = Lobby(engine_rig, PRESET)
    lobby.mods["B"].cmd_result(lobby.join("B"), "applied", spawned=1)

    seq = lobby.join("A")
    for attempt in (1, 2):
        lobby.mods["A"].cmd_result(seq, status, reason=reason)
        wait_retry_planned(lobby, "A", seq)
        lobby.settle()
        assert lobby.state()["phase"] == "lobby"
        assert lobby.state()["capabilities"]["spawn"]["A"] == "unknown"
        # L'intervalle borne les essais : rien ne repart avant son terme.
        assert json.loads(lobby.mods["A"].in_path.read_bytes().splitlines()[-1])["seq"] == seq
        lobby.at(attempt * retry_s)
        seq = next_probe(lobby, "A", seq)
    lobby.mods["A"].cmd_result(seq, "applied", spawned=1)

    state = engine_rig.wait("countdown", "error")
    assert state["phase"] == "countdown", state["error"]
    assert state["capabilities"]["spawn"]["A"] == "ok"
    assert state["capabilities"]["fallback"] is None
    assert spawn_status(engine_rig.journal(lobby.match_id), PROBE_TIMEOUT_S)["A"] == "ok"


def test_retried_probe_keeps_the_first_deadline(engine_rig):
    """Un essai sans réponse n'ouvre pas un nouveau délai : l'échéance reste celle du premier."""
    lobby = Lobby(engine_rig, PRESET)
    seq = lobby.join("A")
    lobby.mods["A"].cmd_result(seq, "refused", reason="asleep")
    wait_retry_planned(lobby, "A", seq)
    lobby.at(engine_rig.runtime["probe_retry_s"])
    next_probe(lobby, "A", seq)

    lobby.at(PROBE_TIMEOUT_S)
    state = engine_rig.wait("error")

    assert "refused asleep après 30 s" in state["error"]
    assert state["capabilities"]["spawn"]["A"] == "ko"


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
    engine_rig.until(lambda s: s["players"] == {"A": PLAYERS["A"], "B": None})
    assert lobby.state()["capabilities"]["spawn"]["A"] == "unknown"

    lobby.mods["A"].cmd_result(seq, "applied", spawned=1)
    lobby.mods["A"].beat(players=())
    engine_rig.until(lambda s: s["players"]["A"] is None)
    assert lobby.state()["phase"] == "lobby"


@pytest.mark.parametrize("phase", sorted(PHASE_LABELS), ids=lambda phase: phase)
def test_host_password_is_public_in_the_lobby_only(phase):
    config = {"host_password": "marqueur-hote", "password": "invite"}
    state = MatchState(phase=phase, config=config)

    assert ("host_password" in state.to_dict()["config"]) is (phase == "lobby")
    assert state.to_dict()["config"]["password"] == "invite"
    assert config["host_password"] == "marqueur-hote", "la revanche relit ce mot de passe"


def test_api_state_drops_the_host_password_once_the_match_runs(engine_rig):
    """Au lobby, l'hôte le lit à l'écran ; ensuite, ni `/api/state` ni `/api/action` ne le rendent."""
    duel = Duel(engine_rig)
    server = make_server(duel.engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api"
    secret = duel.engine.state.config["host_password"]
    try:
        _, lobby = call(f"{base}/state")
        assert lobby["phase"] == "lobby"
        assert lobby["config"]["host_password"] == secret

        duel.start_match()
        _, running = call(f"{base}/state")
        _, reply = call(f"{base}/action", {"action": "open_game"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(WAIT_S)

    assert running["phase"] == "running"
    assert "host_password" not in running["config"]
    assert secret not in json.dumps(running)
    assert reply["ok"] is False
    assert secret not in json.dumps(reply)
    assert duel.engine.state.config["host_password"] == secret


def test_each_side_has_its_own_password(engine_rig):
    state = Lobby(engine_rig, PRESET).state()
    server_dir = engine_rig.zomboid / "Server"

    assert read_ini(server_dir / "duelB.ini")["Password"] == PLAYER_PASSWORD
    assert read_ini(server_dir / "duelA.ini")["Password"] == state["config"]["host_password"]
    assert state["config"]["host_password"] != PLAYER_PASSWORD
    assert state["config"]["host_password"] not in state["invite"].values()


def test_side_follows_the_connection_identity(engine_rig):
    lobby = Lobby(engine_rig, PRESET)
    host, guest = PLAYERS["A"], PLAYERS["B"]
    lobby.mods["A"].beat(players=(host,))
    engine_rig.until(lambda s: s["players"] == {"A": host, "B": None})

    # L'hôte sur B : un nom lié à A ne compte pas pour l'autre camp.
    lobby.mods["B"].beat(players=(host,))
    engine_rig.until(lambda _: lobby.records("player_refused"))
    lobby.sync_pump()
    assert lobby.state()["players"] == {"A": host, "B": None}

    lobby.mods["B"].beat(players=(guest,))
    engine_rig.until(lambda s: s["players"] == PLAYERS)

    # Un inconnu à la place de l'hôte : le camp est vide, jusqu'au retour de son joueur.
    lobby.mods["A"].beat(players=("intrus",))
    engine_rig.until(lambda s: s["players"] == {"A": None, "B": guest})
    assert lobby.records("player_refused")[-1]["data"] == {"player": "intrus"}
    lobby.mods["A"].beat(players=("intrus",))
    lobby.mods["A"].beat(players=(host,))
    engine_rig.until(lambda s: s["players"] == PLAYERS)
    assert len(lobby.records("player_refused")) == 2


def test_unauthenticated_camp_brings_no_event(engine_rig):
    """Personne n'a de nom lié sur B : rien de ce camp n'entre, seul le serveur y parle."""
    lobby = Lobby(engine_rig, PRESET)
    lobby.mods["B"].emit("player_death", {"player": "intrus"})
    lobby.mods["B"].emit("zombie_kill", {"player": "intrus", **KILL})
    lobby.mods["B"].emit("zombie_kill", KILL, side=None)
    lobby.mods["B"].hook_seen()

    engine_rig.until(lambda _: len(lobby.records("event_refused")) == 3)
    engine_rig.until(
        lambda _: any(r["data"]["kind"] == "hook_seen" for r in lobby.records("event"))
    )

    refused = lobby.records("event_refused")
    assert [(r["side"], r["data"]["kind"], r["data"]["player"]) for r in refused] == [
        ("B", "player_death", "intrus"),
        ("B", "zombie_kill", "intrus"),
        ("B", "zombie_kill", None),
    ]
    entered = {r["data"]["kind"] for r in lobby.records("event") if r["data"]["src"] == "B"}
    assert entered == {"caps", "hook_seen"}


def test_refused_player_cannot_decide_the_match(engine_rig):
    """Sa mort, et ses kills, s'arrêtent à la liaison d'identité : le match continue."""
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.mods["B"].emit("player_death", {"player": "intrus"})
    duel.mods["B"].emit("zombie_kill", {"player": "intrus", **KILL})
    duel.mods["B"].emit("damage_taken", {"player": PLAYERS["A"]})
    # Le double signe ses atomes : celui-ci s'écrit à la main, à la suite de sa séquence.
    mod = duel.mods["B"]
    mod.seq += 1
    envelope = {"v": 1, "boot": mod.boot, "seq": mod.seq, "t_ms": mod.t_ms, "src": "B", "side": "B"}
    mod.write_raw(dump_line({**envelope, "kind": "player_death", "data": {}}))
    duel.read("B")
    engine_rig.until(lambda _: len(duel.records("event_refused")) == 4)

    state = duel.reach(10)

    assert state["phase"] == "running"
    assert state["result"] is None
    assert state["score"] == {"A": 0, "B": 0}
    assert [r["data"]["kind"] for r in duel.records("event_refused")] == [
        "player_death",
        "zombie_kill",
        "damage_taken",
        "player_death",
    ]
    assert not any(r["data"]["kind"] == "player_death" for r in duel.records("event"))

    # Le camp reste décidable par sa propre identité.
    duel.mods["B"].emit("player_death", {})
    assert duel.finish()["result"]["winner"] == "A"
    assert duel.replays()


def withdrawn_duel(rig, preset: str) -> Duel:
    """Mène `preset` au match sur un échec constaté de `damage_taken` : l'atome y est retiré."""
    duel = Duel(rig, preset, failed_sides=("A", "B"))
    duel.start_match()
    return duel


def test_withdrawn_atom_disables_what_depends_on_it(engine_rig):
    """`damage_taken` retiré : `hurt_supply` se tait, les kills continuent, le rejeu suit."""
    duel = withdrawn_duel(engine_rig, "duel_rapide")
    assert duel.state()["capabilities"]["withdrawn"] == ["damage_taken"]
    assert any("hurt_supply" in line["msg"] for line in duel.state()["logs"])

    duel.at_match(1)
    duel.played(duel.mods["A"].emit("damage_taken", {"hp": 90}))
    duel.kills("A", 1)
    engine_rig.until(lambda _: duel.records("command")[-1]["data"]["command"]["kind"] == "spawn_pop")

    kinds = [r["data"]["command"]["kind"] for r in duel.records("command")]
    assert "supply_drop" not in kinds
    assert not [r for r in duel.records("rule_fired") if r["data"]["rule"] == "hurt_supply"]
    assert duel.replays()


def test_withdrawal_mutes_absence_and_combo(engine_rig):
    """`pression` : sans `damage_taken`, ni `not atom(...)` toujours vrai, ni combo sans fin."""
    duel = withdrawn_duel(engine_rig, "pression")
    played = duel.engine._match._played_table

    assert {"untouched_minute", "hurt_supply"}.isdisjoint(r["id"] for r in played["rules"])
    assert "combo" not in played["modifiers"]
    assert "score_gap" in played["modifiers"]
    # La table scellée ne bouge pas : le hash recontrôlé reste celui du fichier.
    assert "damage_taken" in duel.engine._match.table["atoms"]


def test_table_without_any_atom_left_is_refused(engine_rig, presets):
    """Seul cas de refus : plus aucun atome, l'erreur nomme l'atome, le hook et les camps."""
    path = presets / "duel_rapide.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["atoms"] = ["damage_taken"]
    raw["rules"] = [rule for rule in raw["rules"] if rule["id"] == "hurt_supply"]
    raw["score"]["points"] = {"damage_taken": 1}
    path.write_text(json.dumps(raw), encoding="utf-8")
    lobby = Lobby(engine_rig, "duel_rapide", failed_sides=("A", "B"))
    lobby.join_and_resolve()
    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "error"
    for name in ("damage_taken", "OnPlayerGetDamage", "A, B"):
        assert name in state["error"]
    assert stopped_for_good(engine_rig)
    assert state["capabilities"]["fallback"] is None


def test_atoms_left_without_any_rule_still_play(engine_rig, presets):
    """Un atome survivant suffit, même sans règle ni point : seule l'absence d'atome refuse."""
    path = presets / "duel_rapide.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["rules"] = [rule for rule in raw["rules"] if rule["id"] == "hurt_supply"]
    raw["score"]["points"] = {"damage_taken": 1}
    path.write_text(json.dumps(raw), encoding="utf-8")

    duel = withdrawn_duel(engine_rig, "duel_rapide")

    assert duel.engine._match._played_table["rules"] == []


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
    lobby.mods["A"].beat(players=(PLAYERS["A"],))

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

    def __init__(self, rig, preset: str = PRESET, failed_sides: tuple[str, ...] = ()) -> None:
        super().__init__(rig, preset, failed_sides)
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
            mod.t_ms = round((L_S + self.t0_s + te_s + self.wall.step_s) * MS)

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
        line = self.mods[side].beat(age=age, players=(PLAYERS[side],), kills=kills)
        self.read(side)
        return line

    def played(self, line: bytes) -> None:
        """Avance jusqu'à ce que le cœur ait journalisé `line`, à un tick suivant.

        Le journal précède la publication : l'effet sur l'état se lit par `wait_for`.
        """
        self.at_match(self.te_s + self.step_s)
        expected = json.loads(line)
        journal = self.rig.journal
        self.rig.until(lambda _: any(r["data"] == expected for r in journal(self.match_id)))

    def kills(self, side: str, count: int) -> None:
        for _ in range(count):
            line = self.mods[side].emit("zombie_kill", KILL)
        self.played(line)

    def settle_out(self) -> None:
        """Passe l'attente du tampon du mod : une passe la pose, une suivante la clôt."""
        wait_s = self.rig.runtime["conclusion_settle_ms"] / MS + self.step_s
        for _ in range(SETTLE_PASSES):
            if self.state()["phase"] == "running":
                self.at_match(self.te_s + wait_s)

    def finish(self) -> dict:
        self.settle_out()
        state = self.rig.wait("over", "error")
        assert state["phase"] == "over", state["error"]
        assert state["servers"] == {"A": "down", "B": "down"}
        assert stopped_for_good(self.rig)
        self.rig.closed()
        return state

    def match_end(self) -> dict:
        (record,) = self.records("match_end")
        return record

    def replays(self) -> bool:
        path = self.rig.matches / self.match_id / JOURNAL_NAME
        return replay_main([str(path)]) == 0


def test_wait_for_returns_as_soon_as_the_state_holds(engine_rig):
    """Rien à attendre : un seul relevé, sans délai fixe, et l'état rendu est celui qu'il a vu."""
    lobby = Lobby(engine_rig, PRESET)
    seen = []

    state = lobby.wait_for(lambda s: seen.append(s) or True)

    assert seen == [state]
    assert state["phase"] == "lobby"


def test_wait_for_names_the_expected_event_and_the_observed_state(engine_rig):
    lobby = Lobby(engine_rig, PRESET)

    with pytest.raises(AssertionError) as failure:
        lobby.wait_for(lambda s: s["score"]["A"] == 1, timeout_s=0.05, expected="kill de A")
    message = str(failure.value)
    assert "attendu en 0.05 s : kill de A" in message
    assert "'phase': 'lobby'" in message
    assert "'score': {'A': 0, 'B': 0}" in message

    with pytest.raises(AssertionError, match=r"s\["):
        lobby.wait_for(lambda s: s["score"]["B"] == 1, timeout_s=0.05)


def test_no_pass_dates_the_previous_instant_after_at(engine_rig):
    """Une passe partie avant `at` date encore à l'ancien instant : `at` l'attend."""
    lobby = Lobby(engine_rig, PRESET)
    lobby.sync_pump()
    match = lobby.engine._match
    passes, instants = match.pump, []

    def recorded(now_ms: int) -> str | None:
        instants.append(now_ms)
        return passes(now_ms)

    match.pump = recorded
    lobby.at(7)
    # Tout ce qui s'écrit à partir d'ici est lu par une passe datée de l'instant demandé.
    mark = len(instants)
    lobby.sync_pump()

    assert instants[mark:]
    assert set(instants[mark:]) == {(L_S + 7) * MS}


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
    duel.mods["A"].emit("player_death", {})


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
    duel.mods["A"].beat(age=first_age_a + advance_a, players=(PLAYERS["A"],))
    line = duel.mods["B"].beat(age=advance_b, players=(PLAYERS["B"],))

    state = engine_rig.until(lambda s: s["clock"]["drift_s"] == drift_s)
    if outcome is None:
        duel.played(line)
        duel.sync_pump()
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
        duel.mods["A"].beat(age=1.7, players=(PLAYERS["A"],))
        line = duel.mods["B"].beat(age=1.7, players=(PLAYERS["B"],))

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
    assert state["players"] == PLAYERS

    duel.at_match(1 + timeout_s)
    # `players` se publie en fin de passe, après les statuts.
    absent = {"A": None, "B": None}
    state = engine_rig.until(
        lambda s: s["servers"] == {"A": "suspect", "B": "suspect"} and s["players"] == absent
    )
    assert state["phase"] == "running"

    duel.at_match(duel.te_s + 1)
    line = duel.beat("A")
    state = engine_rig.until(
        lambda s: s["servers"]["A"] == "up" and s["players"] == {"A": PLAYERS["A"], "B": None}
    )
    assert state["servers"]["B"] == "suspect"
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


def verdict_refused(records) -> dict:
    raise VerdictError("champ `runtime` manquant dans `match_start`")


def journal_unreadable(path) -> None:
    raise JournalError("ligne 3 du journal illisible")


@pytest.mark.parametrize(
    "name, failing, cause",
    [
        ("verdict", verdict_refused, "runtime"),
        ("read_journal", journal_unreadable, "ligne 3"),
    ],
)
def test_decided_result_survives_a_failed_verdict(
    engine_rig, monkeypatch, name, failing, cause
):
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.kills("A", 1)
    monkeypatch.setattr(engine_module, name, failing)
    duel.at_match(duel.state()["config"]["duration_s"])
    state = duel.finish()

    assert state["error"] is None
    assert state["result"]["outcome"] == "win"
    assert state["result"]["winner"] == "A"
    assert state["score"] == {"A": 1, "B": 0}
    assert state["capabilities"]["attribution"] is None
    (failure,) = duel.records("verdict_failed")
    assert cause in failure["data"]["cause"]
    end = duel.match_end()
    assert end["n"] > failure["n"]
    assert end["data"]["result"] == state["result"]
    assert end["data"]["score"] == {"A": 1, "B": 0}
    assert end["data"]["attribution"] == "unknown"
    assert end["data"]["spawn"] == {"A": "ok", "B": "ok"}
    assert end["data"]["fallback"] is None
    warned = [log["msg"] for log in state["logs"] if log["level"] == "warn"]
    assert any("verdict non calculé" in msg and cause in msg for msg in warned)
    monkeypatch.undo()
    assert duel.replays()


def test_decided_result_survives_a_journal_failure(engine_rig):
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.kills("A", 1)
    journal = duel.engine._match._journal
    write = journal.write

    def disk_full(type_, *args):
        if type_ == "match_end":
            raise OSError("disque plein")
        return write(type_, *args)

    journal.write = disk_full
    duel.at_match(duel.state()["config"]["duration_s"])
    state = duel.finish()

    assert state["result"]["winner"] == "A"
    assert duel.records("match_end") == []
    warned = [log["msg"] for log in state["logs"] if log["level"] == "warn"]
    assert any("fin de match absente du journal" in msg for msg in warned)


def failing_journal(duel: Duel, failed: str, error: Exception) -> list[str]:
    """Fait échouer toute écriture du type `failed` ; rend les types qu'il a vu passer."""
    journal = duel.engine._match._journal
    write = journal.write
    attempts: list[str] = []

    def broken(type_, *args):
        attempts.append(type_)
        if type_ == failed:
            raise error
        return write(type_, *args)

    journal.write = broken
    return attempts


ENCODING_FAILURE = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")


@pytest.mark.parametrize(
    "failed",
    ["bad_line", "event", "rule_fired", "effect_dropped", "match_end"],
)
@pytest.mark.parametrize(
    "error", [OSError("disque plein"), ENCODING_FAILURE, TypeError("type")], ids=["disque", "encodage", "type"]
)
def test_write_failure_at_any_step_keeps_the_result(engine_rig, failed, error):
    """Mort et kill au même tick : le lot est jeté, et chaque écriture du chemin peut lâcher."""
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    attempts = failing_journal(duel, failed, error)
    duel.mods["B"].write_raw(b"pas du json\n")
    death_ms = duel.mods["B"].t_ms
    duel.mods["B"].emit("player_death", {})
    duel.at_match(1 + duel.step_s)
    # Lu pendant l'attente du tampon, le kill rejoint la mort au tick qui conclut.
    duel.mods["A"].emit("zombie_kill", KILL, t_ms=death_ms - 1)

    state = duel.finish()

    assert failed in attempts, "la panne n'a pas été exercée"
    assert state["result"]["outcome"] == "death"
    assert state["result"]["winner"] == "A"
    assert state["score"] == {"A": 1, "B": 0}
    logs = [log["msg"] for log in state["logs"]]
    assert any("match terminé — A 1 / B 0" in msg for msg in logs)
    warned = [log["msg"] for log in state["logs"] if log["level"] == "warn"]
    assert any("journal incomplet" in msg for msg in warned)
    if failed == "match_end":
        assert duel.records("match_end") == []
    else:
        ending = duel.match_end()["data"]
        assert ending["result"]["winner"] == "A"
        assert ending["score"] == {"A": 1, "B": 0}
        assert ending["attribution"]


def test_lost_command_does_not_stop_the_match(engine_rig):
    """Une commande qui ne peut pas partir ne prive pas le lot de son `match_end`."""
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)

    def disk_full(command):
        raise OSError("disque plein")

    for channel in duel.engine._match._channels.values():
        channel.write = disk_full
    duel.kills("A", 1)
    engine_rig.until(lambda _: duel.records("command"))

    duel.at_match(duel.te_s + duel.step_s)
    duel.mods["B"].emit("player_death", {})
    duel.at_match(duel.te_s + duel.step_s)

    assert duel.finish()["result"]["winner"] == "A"
    assert duel.match_end()["data"]["score"] == {"A": 1, "B": 0}


TRANSPORT_FAILURE = OSError("disque en panne")


def refuse(*_):
    raise TRANSPORT_FAILURE


def restart_preparation_fails(duel: Duel) -> None:
    duel.engine._match._channels["A"].prepare_restart = refuse
    crash(duel, "A")


def ack_poll_fails(duel: Duel) -> None:
    duel.engine._match._channels["B"].poll_ack = refuse


def out_read_fails(duel: Duel) -> None:
    duel.engine._match._readers["A"]._read_from_offset = refuse


@pytest.mark.parametrize(
    ("fail", "step"),
    [
        (restart_preparation_fails, "relance impossible"),
        (ack_poll_fails, "suivi de `ack` du camp B"),
        (out_read_fails, "lecture de `out` du camp A"),
    ],
    ids=["prepare_restart", "poll_ack", "out"],
)
def test_transport_failure_in_match_closes_the_journal(engine_rig, fail, step):
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.kills("A", 1)
    core = duel.engine._match._core

    fail(duel)
    engine_rig.until(lambda _: core._submitted_end is not None)
    duel.at_match(duel.te_s + duel.step_s)

    state = duel.finish()
    assert state["error"] is None
    assert state["result"]["outcome"] == "invalidated"
    assert state["result"]["winner"] is None
    assert state["score"] == {"A": 1, "B": 0}
    ending = duel.match_end()["data"]
    assert ending["result"] == state["result"]
    assert ending["score"] == {"A": 1, "B": 0}
    for text in (step, "disque en panne"):
        assert text in ending["result"]["reason"]
    assert duel.replays()


def test_transport_failure_before_the_match_stays_a_launch_failure(engine_rig):
    """Sans cœur, il n'y a ni score à sauver ni `match_end` à écrire."""
    lobby = Lobby(engine_rig, PRESET)
    out_read_fails(lobby)

    state = engine_rig.wait("error")

    assert "disque en panne" in state["error"]
    assert lobby.records("match_end") == []


CLOCK_MATCH_S = 60


@pytest.mark.parametrize("step_s", [200, -200], ids=["avance", "recul"])
def test_wall_clock_step_neither_ends_nor_freezes_the_match(engine_rig, presets, step_s):
    """200 s dépassent la grâce : sur l'heure murale, l'avance invaliderait, le recul figerait."""
    edit_preset(presets, duration_s=CLOCK_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(10)
    duel.kills("A", 1)

    duel.wall.step_s = step_s
    state = duel.reach(20)

    assert state["phase"] == "running"
    assert state["servers"] == {"A": "up", "B": "up"}
    assert duel.records("match_end") == []

    duel.at_match(CLOCK_MATCH_S + duel.step_s)
    state = duel.finish()
    assert state["result"]["winner"] == "A"
    assert duel.match_end()["mt"] == CLOCK_MATCH_S
    assert duel.replays()


@pytest.mark.parametrize("step_s", [200, -5], ids=["avance", "recul"])
def test_events_still_count_after_a_wall_clock_step(engine_rig, step_s):
    """Le `t_ms` du mod suit le saut : l'événement reste compté tant que `te` reste positif."""
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(10)
    duel.kills("A", 1)

    duel.wall.step_s = step_s
    duel.reach(20)
    duel.kills("A", 1)

    duel.wait_for(lambda s: s["score"] == {"A": 2, "B": 0}, expected="second kill au score")


def test_acks_are_timed_on_the_monotonic_clock(engine_rig):
    lobby = Lobby(engine_rig, PRESET)
    for channel in lobby.engine._match._channels.values():
        assert channel._clock is lobby.clock


def test_t0_is_read_on_the_wall_when_the_match_starts(engine_rig):
    """Un saut au lobby ne décale pas `te` : `t0_ms` et le mod voient la même heure murale."""
    duel = Duel(engine_rig)
    duel.wall.step_s = 200
    duel.start_match()

    (running,) = [r for r in duel.records("state") if "t0_ms" in r["data"]]
    assert running["data"]["t0_ms"] == round((L_S + duel.t0_s + 200) * MS)
    duel.at_match(5)
    # La passe de 5 s est close : daté de cet instant, le kill serait un retard légitime.
    duel.mods["A"].t_ms += engine_rig.runtime["tick_ms"]
    duel.kills("A", 1)
    duel.wait_for(lambda s: s["score"] == {"A": 1, "B": 0}, expected="kill de A au score")
    assert duel.records("late_event") == []


def test_journal_refuses_nothing_on_the_match_path_unguarded():
    """Seules `match_start` et `t0_ms` s'écrivent sans filet : avant elles, rien n'est fixé."""
    source = inspect.getsource(engine_module)
    assert source.count("self._journal.write(") == 3
    assert 'self._journal.write(\n            "match_start"' in source
    assert 'self._journal.write("state", {"phase": "running"' in source
    assert "self._journal.write(type_, data, side, mt)" in source


def test_refused_start_touches_no_world(engine_rig):
    """Une valeur inconnue s'arrête avant les serveurs, les mondes et le dossier du match."""
    engine = engine_rig.build()
    for side in SIDES:
        make_world(engine_rig.zomboid, side)
    before = worlds_state(engine_rig.zomboid)

    ok, message = engine.action("start", {"preset": PRESET, "day_length": 8})

    assert not ok and "DayLength" in message
    assert engine.snapshot()["phase"] == "idle"
    assert engine._thread is None
    assert worlds_state(engine_rig.zomboid) == before
    assert not (engine_rig.zomboid / "duel-snapshots").exists()
    assert not engine_rig.launches.exists()
    assert not engine_rig.matches.exists()


def test_effective_day_length_is_checked_against_the_runtime(engine_rig):
    """Le preset n'a été contrôlé que sur `runtime.json` : c'est la table du moteur qui sert."""
    engine = engine_rig.build()
    engine.runtime = {**engine.runtime, "day_length_minutes": {"7": 240}}

    ok, message = engine.action("start", {"preset": PRESET})

    assert not ok and "DayLength" in message
    assert engine._thread is None


def test_refused_start_leaves_a_running_match_alone(engine_rig):
    duel = Duel(engine_rig)
    duel.start_match()

    ok, _ = duel.engine.action("start", {"preset": PRESET, "day_length": 8})

    assert not ok
    state = duel.reach(1)
    assert state["phase"] == "running"
    assert state["error"] is None


def test_refused_start_keeps_the_previous_result(engine_rig):
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(duel.state()["config"]["duration_s"])
    finished = duel.finish()

    ok, _ = duel.engine.action("start", {"preset": PRESET, "duration_s": "abc"})

    assert not ok
    state = duel.state()
    assert state["phase"] == "over"
    assert state["result"] == finished["result"]


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
    engine_rig.closed()
    assert duel.records("match_end") == []
    assert all(r["mt"] is not None for r in duel.records("state")[-2:])
    assert duel.replays()


def launch_session(rig, side: str) -> dict:
    """Session que le dernier lancement du serveur `side` a trouvée sur le disque."""
    return dict(parse_session((rig.seen(side)[-1] / "Lua" / f"duel_{side}_session.txt").read_bytes()))


def sent(duel: Duel, side: str) -> list[dict]:
    """Commandes du match vers `side`, sondes exclues."""
    return [
        record
        for record in duel.records("command")
        if record["side"] == side and record["mt"] is not None
    ]


def wait_sent(duel: Duel, side: str, count: int) -> list[dict]:
    duel.rig.until(lambda _: len(sent(duel, side)) >= count)
    return sent(duel, side)


def server_states(duel: Duel, side: str) -> list[str]:
    return [
        record["data"]["server"]
        for record in duel.records("state")
        if record["side"] == side and record["mt"] is not None and "server" in record["data"]
    ]


def forfeit_after_a_fresh_grace(duel: Duel, back_s: float) -> None:
    grace_s = duel.engine.preset(PRESET)["disconnect_grace_s"]
    duel.at_match(back_s + 60)
    duel.beat("B")
    state = duel.reach(back_s + grace_s - 1)
    assert state["phase"] == "running"

    duel.at_match(back_s + grace_s)
    state = duel.finish()
    assert state["result"]["outcome"] == "forfeit"
    assert state["result"]["winner"] == "B"


def invalidated_by_second_exit(duel: Duel, back_s: float) -> None:
    launches = len(duel.rig.seen("A"))
    crash(duel, "A")
    duel.rig.until(lambda s: s["servers"]["A"] == "error")
    duel.at_match(back_s + 1)
    state = duel.finish()
    assert state["result"]["outcome"] == "invalidated"
    assert state["result"]["winner"] is None
    assert "serveur A perdu" in state["result"]["reason"]
    assert len(duel.rig.seen("A")) == launches


@pytest.mark.parametrize("ending", [forfeit_after_a_fresh_grace, invalidated_by_second_exit])
def test_restart_in_match(engine_rig, presets, ending):
    rig = engine_rig
    edit_preset(presets, duration_s=RESTART_MATCH_S)
    duel = Duel(rig)
    duel.start_match()
    assert duel.engine.preset(PRESET)["disconnect_grace_s"] == 120

    duel.at_match(10)
    duel.beat("A")
    duel.beat("B")
    duel.kills("A", 1)
    # `at_match` laisse une passe juger l'instant avant le `beat` : l'écart reste sous 30 s.
    for te_s in range(38, 291, 28):
        duel.at_match(te_s)
        duel.beat("A")
        duel.beat("B")
    state = duel.reach(300)
    assert state["score"] == {"A": 1, "B": 0}
    match = duel.engine._match
    core = match._core
    launches = len(rig.seen("A"))
    played = b"played before the exit"
    world = world_dir(rig.zomboid, "A") / "map" / "chunk.bin"
    world.write_bytes(played)

    release = crash_and_hold(duel, "A")
    state = duel.state()
    assert state["phase"] == "running"
    assert state["servers"] == {"A": "restarting", "B": "up"}
    assert duel.reach(301)["players"]["A"] is None

    # A est muet depuis 140 s, au-delà de la grâce : elle est suspendue, le minuteur non.
    duel.at_match(400)
    duel.beat("B")
    state = duel.reach(430)
    assert state["phase"] == "running"
    assert state["servers"]["A"] == "restarting"
    assert state["clock"]["remaining_s"] == RESTART_MATCH_S - 430
    assert server_states(duel, "A") == ["restarting"]
    assert len(rig.seen("A")) == launches

    duel.at_match(440)
    release.set()
    state = rig.until(lambda s: s["servers"]["A"] == "up")
    assert len(rig.seen("A")) == launches + 1
    assert launch_session(rig, "A")["boot"] == 2
    assert launch_session(rig, "B")["boot"] == 1
    assert (rig.seen("A")[-1] / "Lua" / "duel_A_in.txt").read_bytes() == b""
    assert world.read_bytes() == played
    assert state["score"] == {"A": 1, "B": 0}
    assert match._core is core

    # Le `boot` 2 repart de `seq` 1 et atteint les mêmes règles, sur le même score.
    assert duel.mods["A"].read_session()["boot"] == 2
    duel.kills("A", 1)
    state = rig.until(lambda s: s["score"]["A"] == 2)
    assert state["score"] == {"A": 2, "B": 0}
    assert server_states(duel, "A") == ["restarting", "up"]

    ending(duel, duel.te_s)
    assert stopped_for_good(rig)
    assert duel.replays()


def test_drift_decides_after_the_return(engine_rig, presets):
    """La relance ne compte pas, mais la dérive tranche sur ce qui suit le retour."""
    edit_preset(presets, day_length=1)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.beat("A")
    duel.beat("B")
    engine_rig.until(lambda s: s["clock"]["drift_s"] == 0)

    release = crash_and_hold(duel, "A")
    duel.at_match(2)
    duel.beat("B", age=1.7)
    state = duel.reach(3)
    assert state["phase"] == "running"
    assert state["clock"]["drift_s"] == 0

    release.set()
    engine_rig.until(lambda s: s["servers"]["A"] == "up")
    state = duel.reach(4)
    assert state["phase"] == "running"

    # B a pris 1,7 d'avance pendant la relance : c'est son nouveau repère, pas une dérive.
    duel.mods["A"].read_session()
    duel.beat("A", age=0.0)
    state = duel.reach(5)
    assert state["phase"] == "running"
    assert state["clock"]["drift_s"] == 0

    duel.at_match(6)
    duel.mods["B"].beat(age=3.4, players=(PLAYERS["B"],))
    duel.at_match(duel.te_s + duel.step_s)
    state = duel.finish()
    assert state["result"]["outcome"] == "invalidated"
    assert state["clock"]["drift_s"] == -63.75
    assert "dérive" in state["result"]["reason"]
    assert duel.replays()


def test_empty_server_double_ticks_and_beats_every_ten_seconds(tmp_path):
    """H3 contredite : avec `PauseEmpty=false`, un serveur vide tique et bat au rythme nominal."""
    mod = FakeMod(tmp_path, "A")

    empty = EmptyServer(mod, age=1.0, start_s=40)
    empty.run_until(49)
    assert not mod.out_path.exists()
    empty.run_until(60)

    lines = [json.loads(line) for line in mod.out_path.read_bytes().splitlines()]
    assert [line["kind"] for line in lines] == ["beat", "beat"]
    assert [line["data"]["players"] for line in lines] == [[], []]
    assert [line["data"]["age"] for line in lines] == pytest.approx([1.27, 1.54])


def test_restarted_empty_server_reopens_the_mark_before_its_player_returns(engine_rig, presets):
    """Le serveur relancé bat sans attendre son joueur : son premier `beat` ouvre le repère.

    Il avance ensuite au rythme de B pendant la reconnexion : aucune dérive ne s'y ajoute.
    Un monde qui s'arrêterait, lui, creuserait l'écart à chaque `beat` de B.
    """
    edit_preset(presets, day_length=1, duration_s=RESTART_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.beat("A", age=3.0)
    duel.beat("B", age=0.0)
    engine_rig.until(lambda s: s["clock"]["drift_s"] == 0)
    duel.at_match(11)
    duel.beat("A", age=3.5)
    duel.beat("B", age=0.0)
    engine_rig.until(lambda s: s["clock"]["drift_s"] == 18.75)

    release = crash_and_hold(duel, "A")
    duel.at_match(40)
    duel.beat("B", age=4.0)
    duel.reach(41)
    release.set()
    engine_rig.until(lambda s: s["servers"]["A"] == "up")
    duel.mods["A"].read_session()

    empty = EmptyServer(duel.mods["A"], age=1.0, start_s=41)
    duel.at_match(51)
    empty.run_until(51)
    duel.reach(52)
    age_b = 4.0
    for now_s in (61, 71):
        duel.at_match(now_s)
        empty.run_until(now_s)
        age_b += EMPTY_AGE_PER_BEAT
        duel.beat("B", age=age_b)
        state = duel.reach(now_s + 1)
        assert state["phase"] == "running"
        assert state["servers"]["A"] == "up"
        assert state["players"]["A"] is None
        assert state["clock"]["drift_s"] == 18.75

    duel.at_match(75)
    duel.beat("A", age=empty.age)
    state = engine_rig.until(lambda s: s["players"]["A"] == PLAYERS["A"])
    assert state["clock"]["drift_s"] == 18.75


def test_no_text_still_states_that_an_empty_server_stops_ticking():
    """H3 est contredite : le prochain lecteur ne doit pas raisonner sur cette prémisse."""
    stale = ("ne tique" + " pas", "arrête de" + " tiquer")
    root = Path(__file__).resolve().parent.parent
    texts = [
        *root.glob("service/*.py"),
        *root.glob("tests/*.py"),
        root / "docs/campaigns/phase1/spec.md",
    ]

    assert [
        path.name
        for path in texts
        if any(phrase in path.read_text(encoding="utf-8") for phrase in stale)
    ] == []


def test_restart_keeps_the_match_and_the_acquired_drift(engine_rig, presets):
    """Comportement d'une relance réelle : le monde repart de sa dernière sauvegarde.

    A revient avec un âge inférieur à celui de sa sortie, après un arrêt pendant lequel B a
    continué. Ni cet arrêt, ni ce recul ne sont de la dérive : le match continue. L'écart
    mesuré avant la sortie reste acquis, et s'ajoute à ce qui se mesure après le retour.
    """
    edit_preset(presets, day_length=1, duration_s=RESTART_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.beat("A", age=3.0)
    duel.beat("B", age=0.0)
    engine_rig.until(lambda s: s["clock"]["drift_s"] == 0)
    duel.at_match(11)
    duel.beat("A", age=3.5)
    duel.beat("B", age=0.0)
    engine_rig.until(lambda s: s["clock"]["drift_s"] == 18.75)

    release = crash_and_hold(duel, "A")
    duel.at_match(40)
    duel.beat("B", age=4.0)
    assert duel.reach(41)["clock"]["drift_s"] == 18.75

    release.set()
    engine_rig.until(lambda s: s["servers"]["A"] == "up")
    # Premier heartbeat en retard sur les 10 s nominales : le repère attend A, et B avance encore.
    duel.at_match(60)
    duel.beat("B", age=5.0)
    duel.at_match(71)
    duel.mods["A"].read_session()
    duel.beat("A", age=1.0)
    state = duel.reach(72)
    assert state["phase"] == "running"
    assert state["clock"]["drift_s"] == 18.75

    duel.at_match(73)
    duel.beat("A", age=1.25)
    state = engine_rig.until(lambda s: s["clock"]["drift_s"] == 28.125)
    assert state["phase"] == "running"

    duel.at_match(74)
    duel.mods["A"].beat(age=2.25, players=(PLAYERS["A"],))
    duel.at_match(duel.te_s + duel.step_s)
    state = duel.finish()
    assert state["result"]["outcome"] == "invalidated"
    assert state["clock"]["drift_s"] == 65.625
    assert duel.replays()


def test_drift_ignores_a_beat_of_the_old_boot(engine_rig, presets):
    """Le `java` survivant du `boot` révolu tique encore : ni âge, ni joueur, ni dérive."""
    edit_preset(presets, day_length=1)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.beat("A")
    duel.beat("B")
    engine_rig.until(lambda s: s["clock"]["drift_s"] == 0)

    release = crash_and_hold(duel, "A")
    duel.at_match(2)
    duel.beat("A")
    duel.beat("B", age=1.7)
    state = duel.reach(3)
    assert state["phase"] == "running"
    assert state["clock"]["drift_s"] == 0
    assert state["players"]["A"] is None

    release.set()
    engine_rig.until(lambda s: s["servers"]["A"] == "up")
    state = duel.reach(4)
    assert state["phase"] == "running"
    assert state["clock"]["drift_s"] == 0
    assert duel.replays()


def test_drift_waits_for_the_availability(engine_rig, presets):
    """Un heartbeat du nouveau `boot` reçu en relance ne pose aucun repère : le camp n'est pas `up`."""
    edit_preset(presets, day_length=1)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.beat("A")
    duel.beat("B")
    engine_rig.until(lambda s: s["clock"]["drift_s"] == 0)

    release = crash_and_hold(duel, "A")
    duel.mods["A"].read_session()
    duel.at_match(2)
    duel.beat("A")
    duel.beat("B", age=1.7)
    state = duel.reach(3)
    assert state["phase"] == "running"
    assert state["servers"]["A"] == "restarting"
    assert state["clock"]["drift_s"] == 0

    release.set()
    engine_rig.until(lambda s: s["servers"]["A"] == "up")
    # Pris dès la relance, le repère de A verrait ici 1,7 d'avance, au-delà du seuil.
    duel.at_match(4)
    duel.beat("A", age=1.7)
    state = duel.reach(5)
    assert state["phase"] == "running"
    assert state["clock"]["drift_s"] == 0
    assert duel.replays()


def test_no_command_while_restarting(engine_rig, presets):
    budget = {"threats_per_minute": 6, "concurrent_effects": 1, "per_effect_cooldown_s": 20}
    edit_preset(presets, budget=budget)
    duel = Duel(engine_rig)
    duel.start_match()
    assert engine_rig.runtime["cmd_result_timeout_s"] == 60

    # Sans `cmd_result`, cette commande tient l'unique place vers A jusqu'à `mt` = 61.
    duel.at_match(1)
    duel.kills("B", 1)
    (first,) = wait_sent(duel, "A", 1)
    first_seq = first["data"]["command"]["seq"]

    duel.reach(30)
    release = crash_and_hold(duel, "A")
    duel.at_match(duel.te_s + duel.step_s)
    engine_rig.until(lambda _: duel.records("lost_on_restart"))
    (lost,) = duel.records("lost_on_restart")
    assert (lost["data"], lost["side"]) == ({"seq": first_seq}, "A")

    duel.reach(31)
    duel.kills("B", 3)
    # Ce lot vers B est plus récent que celui qui attend A : il part quand même.
    duel.reach(33)
    duel.kills("A", 1)
    assert wait_sent(duel, "B", 1)[0]["mt"] >= 33
    duel.reach(35)
    assert len(sent(duel, "A")) == 1

    duel.at_match(40)
    release.set()
    engine_rig.until(lambda s: s["servers"]["A"] == "up")
    assert launch_session(engine_rig, "A")["cmd_floor"] == first_seq
    duel.at_match(40 + duel.step_s)

    second = wait_sent(duel, "A", 2)[1]
    assert second["data"]["command"]["n"] == 6
    assert second["data"]["command"]["seq"] > first_seq
    up = next(
        record
        for record in duel.records("state")
        if record["side"] == "A"
        and record["data"].get("server") == "up"
        and record["mt"] is not None
    )
    assert second["mt"] == up["mt"]
    assert duel.records("cmd_timeout") == []
    # La commande perdue compte encore : 2 vers A et 1 vers B sur la fenêtre d'une minute.
    state = engine_rig.until(lambda s: s["threats_per_min"] == 3.0)
    assert state["phase"] == "running"

    duel.at_match(engine_rig.engine.preset(PRESET)["duration_s"])
    duel.finish()
    assert duel.replays()


@pytest.mark.parametrize(
    ("lead", "backlog", "passes"),
    [
        (b"", b"", DRAIN_PASSES),
        (b"", OVER_POLL_LINE, DRAIN_PASSES),
        (b"", OVER_POLL_LINE, 1),
        (OVER_POLL_LINE, OVER_POLL_LINE, 1),
    ],
    ids=["a_jour", "retard_de_lecture", "budget_depasse", "repere_depasse"],
)
def test_result_written_before_exit_is_not_lost(
    engine_rig, presets, lead, backlog, passes, monkeypatch
):
    """`cmd_result` écrit juste avant la sortie : seule la commande muette est perdue.

    Derrière plus de `POLL_MAX` octets en attente, il compte encore : la relance attend tout,
    y compris quand ce retard dépasse le budget de lecture d'une passe, ou quand `lead` a posé
    un repère de lecture que le résultat dépasse.
    """
    monkeypatch.setattr("service.transport.DRAIN_PASSES", passes)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    duel.kills("B", 1)
    unanswered = wait_sent(duel, "A", 1)[0]["data"]["command"]["seq"]
    duel.at_match(30)
    duel.kills("B", 1)
    answered = wait_sent(duel, "A", 2)[1]["data"]["command"]["seq"]

    match = duel.engine._match
    server = match.pair.servers["A"]
    poll_out = match._poll_out
    te_s = duel.te_s + duel.step_s
    due_ms = int((L_S + (duel.t0_s + te_s)) * MS)

    def exit_right_after_the_read(now_ms: int) -> None:
        """Pire instant : `out` vient d'être lu, puis le résultat s'écrit et le serveur sort.

        Dans une passe qui exécute un tick : sinon le `cmd_result` lu ensuite passerait
        avant le `lost_on_restart` dans le tick, et masquerait une perte déclarée à tort.
        """
        if now_ms >= due_ms:
            duel.mods["A"].write_raw(lead)
        poll_out(now_ms)
        if now_ms >= due_ms:
            match._poll_out = poll_out
            duel.mods["A"].write_raw(backlog)
            duel.mods["A"].cmd_result(answered, "applied", spawned=2)
            crash(duel, "A")
            engine_rig.until(lambda _: server.exit_code is not None)

    channel = match._channels["A"]
    prepare_restart = channel.prepare_restart
    declared = []

    def spied_restart(unresolved):
        """Le cœur écarterait au tick suivant une perte contredite : le constat se lit ici."""
        declared.extend(unresolved)
        return prepare_restart(unresolved)

    channel.prepare_restart = spied_restart
    match._poll_out = exit_right_after_the_read
    duel.at_match(te_s)
    engine_rig.until(lambda s: s["servers"]["A"] == "restarting")
    assert declared == [unanswered]
    duel.at_match(te_s + duel.step_s)
    engine_rig.until(lambda _: duel.records("lost_on_restart"))
    assert [r["data"]["seq"] for r in duel.records("lost_on_restart")] == [unanswered]
    assert [r["data"]["data"]["seq"] for r in duel.records("cmd_result")][-1] == answered


@pytest.mark.parametrize("passes", [DRAIN_PASSES, 1], ids=["budget", "budget_depasse"])
def test_final_tick_counts_a_kill_behind_a_read_backlog(engine_rig, presets, passes, monkeypatch):
    """Le tick final conclut pour de bon : il ne laisse pas un kill au-delà de `POLL_MAX`.

    Un retard plus long que le budget d'une passe retient ce tick jusqu'à la fin de la lecture.
    """
    monkeypatch.setattr("service.transport.DRAIN_PASSES", passes)
    edit_preset(presets, duration_s=CLOCK_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.reach(CLOCK_MATCH_S - duel.step_s)

    match = duel.engine._match
    poll_out = match._poll_out
    due_ms = int((L_S + duel.t0_s + CLOCK_MATCH_S) * MS)

    def backlog_before_the_last_read(now_ms: int) -> None:
        if now_ms >= due_ms:
            match._poll_out = poll_out
            duel.mods["A"].write_raw(OVER_POLL_LINE)
            duel.mods["A"].emit("zombie_kill", KILL)
        poll_out(now_ms)

    match._poll_out = backlog_before_the_last_read
    # `at` et non `at_match` : le kill reste daté du dernier tick, avant la fin.
    duel.mods["A"].t_ms = round((L_S + duel.t0_s + CLOCK_MATCH_S - duel.step_s / 4) * MS)
    duel.at(duel.t0_s + CLOCK_MATCH_S)

    assert duel.finish()["result"]["winner"] == "A"
    assert duel.replays()


def after_a_stale_goal(duel: Duel, te_s: float, side: str, write) -> None:
    """À la passe de `te_s`, `write` écrit après le repère d'une lecture de `side` en retard.

    Cette passe atteint alors le repère, retenu d'une passe antérieure, et clôt son retard
    sans voir ce que `write` a écrit : la ligne de plus qui les sépare remplit son `poll`. Il
    faut un budget d'une seule passe.
    """
    match = duel.engine._match
    poll_out = match._poll_out
    due_ms = int((L_S + duel.t0_s + te_s) * MS)

    def behind_then_written(now_ms: int) -> None:
        if now_ms >= due_ms:
            match._poll_out = poll_out
            reader = match._readers[side]
            duel.mods[side].write_raw(OVER_POLL_LINE)
            list(reader.drain())
            assert reader.behind
            duel.mods[side].write_raw(OVER_POLL_LINE)
            write()
        poll_out(now_ms)

    match._poll_out = behind_then_written


def test_final_tick_reads_past_a_goal_older_than_the_deadline(engine_rig, presets, monkeypatch):
    """Un kill écrit après le repère d'une lecture en retard compte encore au tick final."""
    monkeypatch.setattr("service.transport.DRAIN_PASSES", 1)
    edit_preset(presets, duration_s=CLOCK_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.reach(CLOCK_MATCH_S - duel.step_s)

    after_a_stale_goal(
        duel, CLOCK_MATCH_S, "A", lambda: duel.mods["A"].emit("zombie_kill", KILL)
    )
    duel.mods["A"].t_ms = round((L_S + duel.t0_s + CLOCK_MATCH_S - duel.step_s / 4) * MS)
    duel.at(duel.t0_s + CLOCK_MATCH_S)

    assert duel.finish()["result"]["winner"] == "A"
    assert duel.replays()


def test_earlier_death_behind_a_backlog_beats_a_later_one(engine_rig, monkeypatch):
    """La mort la plus ancienne décide, même lue après une plus récente de l'autre camp."""
    monkeypatch.setattr("service.transport.DRAIN_PASSES", 1)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.reach(1)
    death_ms = duel.mods["A"].t_ms

    duel.mods["A"].write_raw(OVER_POLL_LINE * 3)
    duel.mods["A"].emit("player_death", {}, t_ms=death_ms)
    duel.mods["B"].emit("player_death", {}, t_ms=death_ms + 1)
    duel.at_match(1 + duel.step_s)
    duel.read("A")
    duel.at_match(1 + 2 * duel.step_s)

    state = duel.finish()
    assert (state["result"]["outcome"], state["result"]["winner"]) == ("death", "B")
    assert duel.replays()


def test_earlier_death_after_a_stale_goal_beats_a_later_one(engine_rig, monkeypatch):
    """Une mort reçue relit l'autre camp au-delà d'un repère plus ancien qu'elle."""
    monkeypatch.setattr("service.transport.DRAIN_PASSES", 1)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.reach(1)
    death_ms = duel.mods["A"].t_ms

    def deaths() -> None:
        duel.mods["A"].emit("player_death", {}, t_ms=death_ms)
        duel.mods["B"].emit("player_death", {}, t_ms=death_ms + 1)

    after_a_stale_goal(duel, 1 + duel.step_s, "A", deaths)
    duel.at_match(1 + duel.step_s)

    state = duel.finish()
    assert (state["result"]["outcome"], state["result"]["winner"]) == ("death", "B")
    assert duel.replays()


def to_countdown(rig) -> Duel:
    duel = Duel(rig)
    duel.t0_s = rig.runtime["countdown_s"]
    duel.join_and_resolve()
    state = rig.wait("countdown", "error")
    assert state["phase"] == "countdown", state["error"]
    for mod in duel.mods.values():
        mod.t_ms = round(L_S * MS)
    return duel


def test_death_during_the_countdown_is_judged_at_the_first_tick(engine_rig):
    """Le mod ne la redira pas : le camp vivant gagne à `t0`, le match ne court pas."""
    duel = to_countdown(engine_rig)
    line = duel.mods["A"].emit("player_death", {})
    duel.read("A")

    duel.at_match(0)
    state = duel.finish()

    assert (state["result"]["outcome"], state["result"]["winner"]) == ("death", "B")
    assert duel.match_end()["mt"] == 0
    (early,) = duel.records("early_death")
    assert (early["side"], early["mt"]) == ("A", None)
    # Un seul `event` : celui du tick qui juge, que le rejeu retrouve.
    assert [r["mt"] for r in duel.records("event") if r["data"] == json.loads(line)] == [0]
    assert duel.replays()


def test_death_just_before_t0_is_judged_once_the_buffer_has_settled(engine_rig):
    duel = to_countdown(engine_rig)
    duel.at(engine_rig.runtime["countdown_s"] - 0.25)
    duel.mods["B"].emit("player_death", {}, t_ms=round((L_S + 2.75) * MS))
    duel.read("B")

    duel.at_match(0)
    engine_rig.wait("running", "over")
    duel.at_match(engine_rig.runtime["conclusion_settle_ms"] / MS)
    state = engine_rig.wait("over", "error")

    assert (state["result"]["outcome"], state["result"]["winner"]) == ("death", "A")
    assert duel.match_end()["mt"] < 5
    assert duel.replays()


def test_death_dated_before_t0_but_read_after_it_decides(engine_rig):
    """Le tampon du mod peut passer `t0` : ce qui compte est la date du fait."""
    duel = to_countdown(engine_rig)
    duel.at_match(0)
    engine_rig.wait("running")
    duel.mods["A"].emit("player_death", {}, t_ms=round((L_S + 2.5) * MS))

    state = duel.finish()

    assert (state["result"]["outcome"], state["result"]["winner"]) == ("death", "B")
    assert duel.replays()


def test_oldest_of_two_deaths_before_t0_decides(engine_rig):
    duel = to_countdown(engine_rig)
    duel.mods["A"].emit("player_death", {}, t_ms=round((L_S + 1) * MS))
    duel.mods["B"].emit("player_death", {}, t_ms=round((L_S + 0.5) * MS))
    duel.read("A")
    duel.read("B")

    duel.at_match(0)
    state = duel.finish()

    assert (state["result"]["outcome"], state["result"]["winner"]) == ("death", "A")


def test_death_in_the_lobby_is_not_lost_by_a_cancel(engine_rig):
    """Sa trace part à la lecture : un duel annulé au décompte la garde au journal."""
    duel = to_countdown(engine_rig)
    duel.mods["A"].emit("player_death", {})
    duel.wait_for(lambda _: duel.records("early_death"), expected="`early_death` au journal")

    assert duel.engine.action("cancel")[0]
    engine_rig.wait("idle")
    assert len(duel.records("early_death")) == 1


def test_oldest_death_written_second_decides(engine_rig):
    """La mort de A, 400 ms plus récente, arrive d'abord : c'est celle de B qui compte."""
    duel = Duel(engine_rig)
    duel.start_match()
    duel.reach(1)
    death_ms = duel.mods["A"].t_ms

    duel.mods["A"].emit("player_death", {}, t_ms=death_ms + 400)
    duel.at_match(1 + duel.step_s)
    duel.read("A")
    assert duel.state()["result"] is None
    duel.mods["B"].emit("player_death", {}, t_ms=death_ms)

    state = duel.finish()
    assert (state["result"]["outcome"], state["result"]["winner"]) == ("death", "A")
    assert duel.replays()


def test_kill_dated_before_the_timer_but_flushed_after_it_counts(engine_rig, presets):
    edit_preset(presets, duration_s=CLOCK_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.reach(CLOCK_MATCH_S - duel.step_s)
    deadline_ms = round((L_S + duel.t0_s + CLOCK_MATCH_S) * MS)

    duel.at_match(CLOCK_MATCH_S)
    duel.sync_pump()
    assert duel.state()["phase"] == "running"
    duel.mods["A"].emit("zombie_kill", KILL, t_ms=deadline_ms - 200)

    state = duel.finish()
    assert (state["result"]["outcome"], state["result"]["winner"]) == ("win", "A")
    assert duel.match_end()["mt"] == CLOCK_MATCH_S
    assert duel.replays()


def test_kill_dated_after_the_timer_does_not_count(engine_rig, presets):
    """L'attente laisse lire plus loin que le terme : elle n'allonge pas le match."""
    edit_preset(presets, duration_s=CLOCK_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.reach(CLOCK_MATCH_S - duel.step_s)
    deadline_ms = round((L_S + duel.t0_s + CLOCK_MATCH_S) * MS)

    duel.at_match(CLOCK_MATCH_S)
    duel.sync_pump()
    duel.mods["A"].emit("zombie_kill", KILL, t_ms=deadline_ms + 200)

    state = duel.finish()
    assert state["result"]["outcome"] == "draw"
    assert duel.replays()


def test_forfeit_waits_for_the_buffer_too(engine_rig, presets):
    """Une mort encore dans le tampon passe avant la fin que le service allait prononcer."""
    edit_preset(presets, duration_s=LONG_MATCH_S, disconnect_grace_s=CLOCK_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(50)
    for side in SIDES:
        duel.beat(side)
    duel.at_match(95)
    duel.beat("B")
    duel.at_match(112)
    duel.sync_pump()
    assert duel.state()["result"] is None
    duel.mods["B"].emit("player_death", {})

    state = duel.finish()
    assert state["result"]["outcome"] == "death"


def test_return_during_the_wait_leaves_the_next_conclusion_its_own(engine_rig, presets):
    """L'absent revient pendant l'attente : la mort d'après attend encore le tampon du mod."""
    edit_preset(presets, duration_s=LONG_MATCH_S, disconnect_grace_s=CLOCK_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(50)
    for side in SIDES:
        duel.beat(side)
    duel.at_match(95)
    duel.beat("B")
    duel.at_match(112)
    duel.sync_pump()
    duel.beat("A")
    duel.beat("B")

    duel.at_match(112 + engine_rig.runtime["conclusion_settle_ms"] / MS + duel.step_s)
    duel.sync_pump()
    duel.sync_pump()
    assert duel.state()["result"] is None

    death_ms = duel.mods["A"].t_ms
    duel.mods["A"].emit("player_death", {}, t_ms=death_ms)
    duel.read("A")
    duel.at_match(duel.te_s + duel.step_s)
    duel.sync_pump()
    assert duel.state()["result"] is None
    duel.mods["B"].emit("player_death", {}, t_ms=death_ms - 400)

    state = duel.finish()
    assert (state["result"]["outcome"], state["result"]["winner"]) == ("death", "A")
    assert duel.replays()


def test_hurt_supply_plays_without_any_wound_before_the_match(engine_rig):
    """Le preset par défaut, sans blessure ni `hook_seen` : la table annoncée est celle jouée."""
    duel = Duel(engine_rig, "duel_rapide")
    duel.start_match()
    match = duel.engine._match
    assert match._played_table == match.table
    assert duel.state()["capabilities"]["withdrawn"] == []

    duel.at_match(1)
    duel.played(duel.mods["A"].emit("damage_taken", {"hp": 88, "source": "health"}))
    engine_rig.until(lambda _: duel.records("command"))

    assert [r["data"]["rule"] for r in duel.records("rule_fired")] == ["hurt_supply"]
    assert duel.records("command")[-1]["data"]["command"]["kind"] == "supply_drop"
    assert [r for r in duel.records("event") if r["data"]["kind"] == "hook_seen"] == []


def proofs_file(rig) -> dict:
    return json.loads((rig.matches / PROOFS_NAME).read_text(encoding="utf-8"))


def rematch(duel: Duel) -> None:
    """Ramène `duel` au lobby d'une revanche, à l'instant courant du banc."""
    for mod in duel.mods.values():
        mod.restart(1)
        mod.seq = 1
    ok, message = duel.engine.action("rematch")
    assert ok, message
    state = duel.rig.wait("lobby", "error")
    assert state["phase"] == "lobby", state["error"]
    duel.match_id = state["match_id"]


def test_proof_survives_to_the_next_match(engine_rig):
    """Une blessure vue une fois vaut pour le couple : la revanche n'attend ni ne retire rien."""
    duel = Duel(engine_rig, "duel_rapide")
    duel.start_match()
    duel.at_match(1)
    duel.mods["A"].emit("damage_taken", {"hp": 88})
    duel.mods["A"].emit("player_death", {})
    duel.finish()
    (hooks,) = proofs_file(engine_rig).values()
    assert hooks == {"OnPlayerGetDamage": {"status": "proven"}}

    rematch(duel)
    now_s = duel.t0_s + duel.te_s
    duel.at(now_s)
    duel.join_and_resolve()
    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state_entered_at(duel, "countdown") == (L_S + now_s) * MS
    assert state["capabilities"]["withdrawn"] == []


def test_death_no_signal_saw_withdraws_from_the_next_match_only(engine_rig):
    """Seule preuve d'échec : un mort sans blessure vue. Le match suivant s'en souvient."""
    duel = Duel(engine_rig, "duel_rapide")
    duel.start_match()
    assert duel.state()["capabilities"]["withdrawn"] == []
    duel.at_match(1)
    duel.mods["B"].emit("player_death", {})
    duel.finish()
    (hooks,) = proofs_file(engine_rig).values()
    assert hooks == {"OnPlayerGetDamage": {"status": "failed", "sides": ["B"]}}

    rematch(duel)
    duel.at(duel.t0_s + duel.te_s)
    duel.join_and_resolve()
    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state["capabilities"]["withdrawn"] == ["damage_taken"]
    assert state["capabilities"]["disabled_rules"] == ["hurt_supply"]


def test_other_endings_prove_no_failure(engine_rig, presets):
    edit_preset(presets, duration_s=CLOCK_MATCH_S)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(CLOCK_MATCH_S)

    assert duel.finish()["result"]["outcome"] == "draw"
    assert not (engine_rig.matches / PROOFS_NAME).exists()


def test_no_command_to_an_exited_server_during_a_read_backlog(engine_rig, monkeypatch):
    """Tant que la lecture du serveur sorti finit, rien ne part vers lui : son budget reste."""
    monkeypatch.setattr("service.transport.DRAIN_PASSES", 1)
    duel = Duel(engine_rig)
    duel.start_match()
    duel.reach(1)
    match = duel.engine._match
    server = match.pair.servers["A"]
    stop, release = server.stop, threading.Event()

    def held_stop() -> bool:
        release.wait(WAIT_S)
        return stop()

    server.stop = held_stop
    pump = match.pump
    te_s = 1 + duel.step_s
    due_ms = int((L_S + duel.t0_s + te_s) * MS)
    crashed = []

    def exit_behind_a_backlog(now_ms: int) -> str | None:
        """Dans une passe qui exécute un tick : le kill de B y vise un serveur déjà sorti."""
        if now_ms >= due_ms and not crashed:
            crashed.append(now_ms)
            duel.mods["A"].write_raw(OVER_POLL_LINE * 2)
            crash(duel, "A")
            engine_rig.until(lambda _: server.exit_code is not None)
            duel.mods["B"].emit("zombie_kill", KILL)
        return pump(now_ms)

    match.pump = exit_behind_a_backlog
    duel.at_match(te_s)
    engine_rig.until(lambda s: s["servers"]["A"] == "restarting")
    duel.at_match(te_s + duel.step_s)
    engine_rig.until(lambda s: s["score"]["B"] == 1)
    duel.at_match(te_s + 2 * duel.step_s)
    duel.reach(te_s + 3 * duel.step_s)

    assert sent(duel, "A") == []
    assert server_states(duel, "A") == ["restarting"]
    release.set()
    engine_rig.until(lambda s: s["servers"]["A"] == "up")
    duel.at_match(te_s + 4 * duel.step_s)
    assert len(wait_sent(duel, "A", 1)) == 1
    assert duel.records("lost_on_restart") == []


def refuse_launch(duel: Duel, server) -> None:
    def start() -> None:
        raise OSError("plus de mémoire")

    server.start = start


def exit_during_launch(duel: Duel, server) -> None:
    launch = server.start

    def start() -> None:
        launch()
        crash(duel, "A")

    server.start = start


@pytest.mark.parametrize(
    "failure, cause",
    [
        (refuse_launch, "relance impossible"),
        (exit_during_launch, "sortie inattendue pendant la relance"),
    ],
)
def test_failed_relaunch_invalidates(engine_rig, failure, cause):
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    failure(duel, duel.engine._match.pair.servers["A"])

    crash(duel, "A")
    engine_rig.until(lambda s: s["servers"]["A"] == "error")
    duel.at_match(2)

    state = duel.finish()
    assert state["result"]["outcome"] == "invalidated"
    assert cause in state["result"]["reason"]
    assert duel.replays()


def cancelled(duel: Duel) -> dict:
    assert duel.engine.action("cancel")[0]
    state = duel.rig.wait("idle", "error")
    assert state["phase"] == "idle", state["error"]
    return state


def timer_over(duel: Duel) -> dict:
    duel.at_match(duel.state()["config"]["duration_s"])
    return duel.finish()


@pytest.mark.parametrize("ending", [cancelled, timer_over])
def test_no_relaunch_after_the_end(engine_rig, ending):
    duel = Duel(engine_rig)
    duel.start_match()
    duel.at_match(1)
    launches = len(engine_rig.seen("A"))
    release = crash_and_hold(duel, "A")

    ending(duel)
    release.set()
    for thread in threading.enumerate():
        if thread.name == "duel-relaunch-A":
            thread.join(WAIT_S)

    assert len(engine_rig.seen("A")) == launches
    assert stopped_for_good(engine_rig)


def in_lobby(lobby: Lobby) -> None:
    pass


def in_countdown(lobby: Lobby) -> None:
    lobby.join_and_resolve()
    state = lobby.rig.wait("countdown", "error")
    assert state["phase"] == "countdown", state["error"]


@pytest.mark.parametrize("reach", [in_lobby, in_countdown])
def test_exit_outside_running_stays_an_error(engine_rig, reach):
    lobby = Lobby(engine_rig, PRESET)
    reach(lobby)
    launches = len(engine_rig.seen("A"))

    crash(lobby, "A")

    state = engine_rig.wait("error")
    assert "serveur A : sortie inattendue" in state["error"]
    assert len(engine_rig.seen("A")) == launches
    assert stopped_for_good(engine_rig)


def test_rematch(engine_rig, presets):
    rig = engine_rig
    duel = Duel(rig)
    origin = worlds_state(rig.zomboid)
    duel.start_match()
    first = duel.state()
    host_password = duel.engine.state.config["host_password"]
    first_window = duel.engine._match.window
    assert first["capabilities"]["active_atoms"] is not None

    # Le premier match finit à `boot` 2, sur des mondes joués.
    duel.at_match(1)
    crash_and_hold(duel, "A").set()
    rig.until(lambda s: s["servers"]["A"] == "up")
    assert launch_session(rig, "A")["boot"] == 2
    for side in SIDES:
        (world_dir(rig.zomboid, side) / "map" / "chunk.bin").write_bytes(b"played")
        world_db(rig.zomboid, side).write_bytes(b"played")
    assert worlds_state(rig.zomboid) != origin
    duel.at_match(first["config"]["duration_s"])
    duel.finish()
    launches = len(rig.seen("A"))

    for mod in duel.mods.values():
        mod.restart(1)
        # Le `caps` posé par le double de lancement a pris le `seq` 1.
        mod.seq = 1
    ok, message = duel.engine.action("rematch")
    assert ok, message
    state = rig.wait("lobby", "error")

    assert state["phase"] == "lobby", state["error"]
    assert state["config"]["code"] == first["config"]["code"]
    assert state["config"]["password"] == first["config"]["password"] == PLAYER_PASSWORD
    assert state["config"]["host_password"] == host_password
    assert state["invite"]["code"] == first["invite"]["code"]
    assert state["invite"]["password"] == PLAYER_PASSWORD
    assert state["match_id"] != first["match_id"]
    assert state["score"] == {"A": 0, "B": 0}
    assert state["result"] is None
    assert worlds_state(rig.zomboid) == origin
    for side in SIDES:
        session = launch_session(rig, side)
        assert (session["boot"], session["cmd_floor"]) == (1, 0)
        assert session["match"] == state["match_id"]
        ini = read_ini(rig.zomboid / "Server" / f"duel{side}.ini")
        passwords = {"A": host_password, "B": PLAYER_PASSWORD}
        assert ini["Password"] == passwords[side]
    assert len(rig.seen("A")) == launches + 1

    # Nouveau match : la fenêtre de contrat se rouvre et se referme sur ses propres preuves.
    assert duel.engine._match.window is not first_window
    assert state["capabilities"].get("active_atoms") is None
    duel.match_id = state["match_id"]
    duel.start_match(lobby_s=duel.t0_s + duel.te_s)
    assert duel.state()["capabilities"]["active_atoms"] is not None
    duel.at_match(1)
    duel.kills("A", 1)
    # Le journal précède la publication du score : `kills` ne garantit que le premier.
    rig.until(lambda s: s["score"] == {"A": 1, "B": 0})
    duel.at_match(first["config"]["duration_s"])
    state = duel.finish()
    assert state["config"]["code"] == first["config"]["code"]
    assert duel.replays()
