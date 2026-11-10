"""Contrat : écarts statiques refusés et nommés, puis fenêtre de preuve des hooks."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from doubles.fake_mod import FakeMod
from doubles.fake_server import kill_tree
from test_preflight import zomboid_home  # noqa: F401
from test_servers import stopped_for_good
from test_snapshots import PLAYER_PASSWORD, WAIT_S, engine_rig  # noqa: F401
from service.engine import ContractWindow
from service.rules import load_effects, load_table
from service.servers import verify_contract, write_ini, write_sandbox

PRESET = Path(__file__).resolve().parent.parent / "service" / "presets" / "demo.json"
BUILD = "42.21.0"
RCON_SECRET = "rcon-marker"
SANDBOX = "SandboxVars = {\n    DayLength = 4,\n    StartYear = 3,\n    StartMonth = 1,\n" + (
    "    StartDay = 2,\n    StartTime = 5,\n}\n"
)
SIDES = ("A", "B")
ATOM = "damage_taken"
HOOK = "OnPlayerGetDamage"
WITH_ATOM = "duel_rapide"
WITHOUT_ATOM = "demo"
PLAYERS = {"A": "durand", "B": "dupont"}
# `L` : le test fige l'horloge du moteur, tout se passe à cet instant sauf s'il l'avance.
L_S = 1000.0
MS = 1000
# Les phases où le fil du moteur enchaîne les passes de `pump`.
PUMPED_PHASES = ("lobby", "countdown", "running")


def caps_data(table: dict) -> dict:
    return {
        "build": BUILD,
        "mod": "0.1.0",
        "protocol": 1,
        "hooks": {},
        "functions": {},
        "atoms": list(table["atoms"]),
        "effects": [effect["kind"] for rule in table["rules"] for effect in rule["effects"]],
    }


@pytest.fixture
def table(runtime):
    raw = json.loads(PRESET.read_text(encoding="utf-8"))
    return load_table(raw, load_effects(), runtime["limits"], file_stem="demo")


@pytest.fixture
def conforming(zomboid_home, runtime, table):
    for side, ports in runtime["ports"].items():
        managed = {
            **runtime["managed_ini"],
            "Mods": "duelcore",
            "DefaultPort": ports["game"],
            "UDPPort": ports["udp"],
            "RCONPort": ports["rcon"],
            "RCONPassword": RCON_SECRET,
        }
        write_ini(zomboid_home / "Server" / f"duel{side}.ini", managed)
        sandbox = zomboid_home / "Server" / f"duel{side}_SandboxVars.lua"
        sandbox.write_text(SANDBOX, encoding="utf-8")
        write_sandbox(sandbox, table["day_length"], runtime["sandbox_start"])
    return {"A": caps_data(table), "B": caps_data(table)}


def check(zomboid_home, table, runtime, caps) -> list[str]:
    builds = {"A": BUILD, "B": BUILD}
    return verify_contract(zomboid_home, table, table["day_length"], runtime, caps, builds)


def alter_ini(zomboid: Path, side: str, **values) -> None:
    write_ini(zomboid / "Server" / f"duel{side}.ini", values)


def mods(zomboid, runtime, caps):
    alter_ini(zomboid, "B", Mods="autre")


def pause_empty(zomboid, runtime, caps):
    alter_ini(zomboid, "A", PauseEmpty="true")


def rcon_port(zomboid, runtime, caps):
    alter_ini(zomboid, "B", RCONPort=runtime["ports"]["A"]["rcon"])


def day_length(zomboid, runtime, caps):
    sandbox = zomboid / "Server" / "duelB_SandboxVars.lua"
    write_sandbox(sandbox, 7, runtime["sandbox_start"])


def caps_mod(zomboid, runtime, caps):
    caps["B"]["mod"] = "0.2.0"


def caps_effect(zomboid, runtime, caps):
    caps["A"]["effects"].remove("spawn_pop")


@pytest.mark.parametrize(
    "alter, key",
    [
        (mods, "Mods"),
        (pause_empty, "PauseEmpty"),
        (rcon_port, "RCONPort"),
        (day_length, "DayLength"),
        (caps_mod, "mod"),
        (caps_effect, "spawn_pop"),
    ],
)
def test_each_mismatch_refused(zomboid_home, runtime, table, conforming, alter, key):
    assert check(zomboid_home, table, runtime, conforming) == []

    alter(zomboid_home, runtime, conforming)
    failures = check(zomboid_home, table, runtime, conforming)

    assert len(failures) == 1
    assert key in failures[0]
    assert RCON_SECRET not in failures[0]


def test_empty_caps_refuses_both_camps(zomboid_home, table, runtime):
    """Sans entrée ni fichier, rien n'est certifié : l'absence se juge camp par camp."""
    failures = check(zomboid_home, table, runtime, {})

    for side in ("A", "B"):
        assert any(f"réglages de {side} illisibles" in failure for failure in failures)
        assert any(f"caps de {side} non reçu" in failure for failure in failures)


def test_absent_camp_is_still_checked(zomboid_home, table, runtime, conforming):
    alter_ini(zomboid_home, "B", Mods="autre")

    failures = check(zomboid_home, table, runtime, {"A": conforming["A"]})

    assert any("caps de B non reçu" in failure for failure in failures)
    assert any("Mods sur B" in failure for failure in failures)


def test_absent_camp_files_refused(zomboid_home, table, runtime, conforming):
    (zomboid_home / "Server" / "duelB.ini").unlink()

    failures = check(zomboid_home, table, runtime, {"A": conforming["A"], "B": conforming["B"]})

    assert len(failures) == 1
    assert "réglages de B illisibles" in failures[0]


class FrozenClock:
    """Instant courant fourni par le test : seule l'attente des doubles reste réelle."""

    def __init__(self) -> None:
        self.now = L_S
        self.on_next_read: Callable[[], None] | None = None

    def __call__(self) -> float:
        if self.on_next_read is not None:
            action, self.on_next_read = self.on_next_read, None
            action()
        return self.now


class SteppedWall:
    """Heure murale du banc : l'instant du test, plus le saut que le test lui impose."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.step_s = 0.0

    def __call__(self) -> float:
        return self.clock() + self.step_s


class PumpPasses:
    """Remplace `match.pump` : compte ses passes, sans rien changer à ce qu'elles font."""

    def __init__(self, pump: Callable[[int], str | None]) -> None:
        self.pump = pump
        self.begun = 0
        self.done = 0

    def __call__(self, now_ms: int) -> str | None:
        self.begun += 1
        try:
            return self.pump(now_ms)
        finally:
            self.done += 1


class Lobby:
    """Vrai `Engine` réel mené au lobby sur les doubles, à un instant fourni par le test.

    `pump` tourne dans son fil : rien de ce que le test écrit n'est traité au retour de
    l'écriture. Un effet se lit par `wait_for`, et une absence d'effet après `sync_pump`.
    """

    def __init__(self, rig, preset: str | None, failed_sides: tuple[str, ...] = ()) -> None:
        self.rig = rig
        self.clock = FrozenClock()
        self.wall = SteppedWall(self.clock)
        self.engine = rig.build()
        if failed_sides:
            # Échec déjà constaté sur ce couple : `test_proof_survives_to_the_next_match` l'établit.
            self.engine._proofs.failed_sides = lambda pair, hook: list(failed_sides)
        self.engine._clock = self.wall
        self.engine._monotonic = self.clock
        catalog = self.engine.effects
        data = {
            "build": BUILD,
            "mod": "0.1.0",
            "protocol": 1,
            "hooks": {HOOK: True},
            "functions": {},
            "atoms": list(catalog["atoms"]),
            "effects": list(catalog["effects"]),
        }
        self.mods = {side: FakeMod(rig.zomboid / "Lua", side) for side in SIDES}
        for side, mod in self.mods.items():
            (rig.caps_dir / side).write_bytes(mod.line("caps", data))
        chosen = {"preset": preset} if preset else {}
        ok, message = self.engine.action("start", {**chosen, "password": PLAYER_PASSWORD})
        assert ok, message
        state = rig.wait("lobby", "error")
        assert state["phase"] == "lobby", state["error"]
        self.match_id = state["match_id"]

    def records(self, type_: str) -> list[dict]:
        return [r for r in self.rig.journal(self.match_id) if r["type"] == type_]

    def phases(self) -> list[str]:
        return [r["data"]["phase"] for r in self.records("state") if "phase" in r["data"]]

    def state(self) -> dict:
        return self.engine.snapshot()

    def step(self, step_id: str) -> dict:
        return next(item for item in self.state()["steps"] if item["id"] == step_id)

    def wait_for(
        self,
        predicate: Callable[[dict], Any],
        timeout_s: float = WAIT_S,
        expected: str | None = None,
    ) -> dict:
        """Rend l'état du moteur dès que `predicate` y tient, ou échoue en disant ce qu'il voit."""
        return self.rig.until(predicate, timeout_s, expected)

    def sync_pump(self) -> None:
        """Attend deux passes entières : tout ce qui précède l'appel est traité et publié.

        La première a pu lire l'horloge avant l'appel ; la seconde l'a lue après. Hors des
        phases où `pump` tourne, il n'y a plus de passe à attendre.
        """
        match = self.engine._match
        if match is None:
            return
        if not isinstance(match.pump, PumpPasses):
            match.pump = PumpPasses(match.pump)
        passes = match.pump
        target = passes.begun + 2
        self.wait_for(
            lambda s: passes.done >= target or s["phase"] not in PUMPED_PHASES,
            expected=f"{target} passes de `pump` finies",
        )

    def at(self, seconds_after_l: float) -> None:
        """Avance l'horloge ; au retour, aucune passe ne date plus rien à l'instant d'avant."""
        self.clock.now = L_S + seconds_after_l
        self.sync_pump()

    def settle(self) -> None:
        """Avant le match, attend qu'une passe entière ait suivi une ligne de l'instant courant."""
        line = json.loads(self.mods["A"].beat(players=self.present("A")))
        self.wait_for(
            lambda _: any(r["data"] == line for r in self.records("event")),
            expected=f"ligne journalisée : {line}",
        )
        self.sync_pump()

    def present(self, side: str) -> tuple[str, ...]:
        player = self.state()["players"][side]
        return (player,) if player else ()

    def wait_detail(self, text: str) -> None:
        self.rig.until(lambda _: text in self.step("contract")["detail"])

    def join(self, side: str) -> int:
        """Connecte le joueur du camp et rend le `seq` de sa sonde."""
        mod = self.mods[side]
        mod.beat(players=(PLAYERS[side],))
        self.rig.until(lambda _: mod.in_path.read_bytes())
        return json.loads(mod.in_path.read_bytes().splitlines()[-1])["seq"]

    def join_and_resolve(self) -> None:
        """Mène à `L` : les deux joueurs sont là et les deux sondes résolues."""
        for side in SIDES:
            self.mods[side].cmd_result(self.join(side), "applied", spawned=1)


def crash(lobby: Lobby, side: str) -> None:
    """Tue l'arbre du serveur : pour le moteur, une sortie que personne n'a demandée."""
    kill_tree(lobby.engine._match.pair.servers[side])


def crash_and_hold(lobby: Lobby, side: str) -> threading.Event:
    """Provoque la sortie et retient le fil de relance jusqu'à l'événement rendu.

    Seul ce fil passe par l'instance : `ServerPair` arrête par `ServerProcess.stop`.
    """
    server = lobby.engine._match.pair.servers[side]
    stop, release = server.stop, threading.Event()

    def held() -> bool:
        release.wait(WAIT_S)
        return stop()

    server.stop = held
    crash(lobby, side)
    lobby.rig.until(lambda s: s["servers"][side] == "restarting")
    return release


def state_entered_at(lobby: Lobby, phase: str) -> int:
    """La phase se publie avant de se journaliser : son `state` s'attend."""
    lobby.wait_for(lambda _: phase in lobby.phases(), expected=f"phase {phase} au journal")
    return next(r["ts_ms"] for r in lobby.records("state") if r["data"].get("phase") == phase)


def test_an_idle_stop_claimed_as_the_countdown_is_decided_opens_no_duel(engine_rig, monkeypatch):
    """Revue A1 (P9F4) : l'arrêt engagé entre la décision du décompte et son entrée l'emporte.

    `claim_idle_stop` passe dans la fenêtre même : après `_rules_refusal`, avant le changement
    d'état. Mutation : retirer le test de `_stop` dans `_enter_countdown` ouvre le décompte
    d'un service qui s'arrête.
    """
    lobby = Lobby(engine_rig, None)
    engine, match = lobby.engine, lobby.engine._match
    rules_refusal = match._rules_refusal
    claimed: list[bool] = []

    def claim_in_the_window():
        refusal = rules_refusal()
        with engine.lock:
            engine._watched_at = float("-inf")
        claimed.append(engine.claim_idle_stop())
        return refusal

    monkeypatch.setattr(match, "_rules_refusal", claim_in_the_window)
    lobby.join_and_resolve()
    lobby.wait_for(lambda _: claimed, expected="l'arrêt engagé au moment du décompte")

    assert engine.shutdown()
    assert claimed[0] is True
    assert engine.snapshot()["phase"] not in ("countdown", "running")
    assert "countdown" not in lobby.phases()


def test_lobby_does_not_wait_for_a_wound(engine_rig):
    """Sans preuve ni échec connus : décompte à `L`, table entière, aucun atome retiré."""
    lobby = Lobby(engine_rig, None)
    assert lobby.state()["config"]["preset"] == WITH_ATOM
    lobby.join_and_resolve()

    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state_entered_at(lobby, "countdown") == L_S * MS
    assert ATOM in state["capabilities"]["active_atoms"]
    assert state["capabilities"]["withdrawn"] == []
    assert state["capabilities"]["disabled_rules"] == []
    assert lobby.records("atom_withdrawn") == []
    assert lobby.engine._match._played_table == lobby.engine._match.table


def test_hook_seen_on_one_side_withdraws_nothing(engine_rig):
    """Un hook muet sur un camp n'est pas un échec : le relevé de santé reste le signal."""
    lobby = Lobby(engine_rig, WITH_ATOM)
    lobby.mods["A"].hook_seen()
    lobby.join_and_resolve()

    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state["capabilities"]["withdrawn"] == []
    assert state["capabilities"]["A"]["hooks"][HOOK]["seen"]
    assert not state["capabilities"]["B"]["hooks"][HOOK]["seen"]


def test_known_failure_withdraws_at_l_and_is_announced(engine_rig):
    """Un échec constaté retire l'atome à `L`, sans attente, et le lobby l'annonce avant."""
    lobby = Lobby(engine_rig, WITH_ATOM, failed_sides=("B",))
    lobby.wait_detail(f"{ATOM} sera retiré")

    lobby.join_and_resolve()
    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state_entered_at(lobby, "countdown") == L_S * MS
    assert state["capabilities"]["withdrawn"] == [ATOM]
    assert state["capabilities"]["disabled_rules"] == ["hurt_supply"]
    assert ATOM not in state["capabilities"]["active_atoms"]
    assert "zombie_kill" in state["capabilities"]["active_atoms"]
    # Ce retrait ne conclut rien sur l'architecture.
    assert state["capabilities"]["fallback"] is None
    assert lobby.step("contract")["state"] == "ok"
    (withdrawn,) = lobby.records("atom_withdrawn")
    assert withdrawn["data"] == {"atom": ATOM, "hook": HOOK, "sides": ["B"]}
    assert withdrawn["ts_ms"] == L_S * MS
    assert withdrawn["mt"] is None


def test_signal_seen_in_the_lobby_clears_a_known_failure(engine_rig):
    lobby = Lobby(engine_rig, WITH_ATOM, failed_sides=("B",))
    lobby.wait_detail(f"{ATOM} sera retiré")

    lobby.mods["B"].hook_seen()
    lobby.join_and_resolve()
    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state["capabilities"]["withdrawn"] == []


def test_table_without_the_atom_publishes_the_same_withdrawal(engine_rig):
    lobby = Lobby(engine_rig, WITHOUT_ATOM, failed_sides=("B",))
    lobby.join_and_resolve()

    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state["capabilities"]["withdrawn"] == [ATOM]
    assert ATOM not in state["capabilities"]["active_atoms"]

    # Un `hook_seen` tardif ne réactive rien, même publié comme vu.
    lobby.mods["B"].hook_seen()
    lobby.rig.until(lambda s: s["capabilities"]["B"]["hooks"][HOOK]["seen"])
    assert lobby.state()["capabilities"]["withdrawn"] == [ATOM]
    assert ATOM not in lobby.state()["capabilities"]["active_atoms"]


def test_decision_outlives_contract_step_and_restart(engine_rig):
    lobby = Lobby(engine_rig, WITH_ATOM)

    assert lobby.step("contract")["state"] == "ok"
    lobby.at(300)
    lobby.settle()
    assert lobby.state()["phase"] == "lobby"
    assert lobby.records("atom_withdrawn") == []
    assert lobby.state()["capabilities"]["active_atoms"] is None

    lobby.at(0)
    lobby.join_and_resolve()
    state = engine_rig.wait("countdown", "error")
    assert state["phase"] == "countdown", state["error"]
    active = state["capabilities"]["active_atoms"]
    assert ATOM in active

    lobby.at(engine_rig.runtime["countdown_s"])
    state = engine_rig.wait("running", "error")
    assert state["phase"] == "running", state["error"]

    # Le `boot` suivant publie ses `caps`, sans `hook_seen` : la décision reste.
    crash_and_hold(lobby, "A").set()
    engine_rig.until(lambda s: s["servers"]["A"] == "up")
    mod = lobby.mods["A"]
    assert mod.read_session()["boot"] == 2
    mod.caps(atoms=["zombie_kill"], hooks={HOOK: False})
    engine_rig.until(lambda s: not s["capabilities"]["A"]["hooks"][HOOK]["registered"])
    assert lobby.state()["capabilities"]["active_atoms"] == active
    assert lobby.records("atom_withdrawn") == []


def window(table_atoms: list[str] = ("zombie_kill", ATOM)) -> ContractWindow:
    opened = ContractWindow(load_effects())
    for side in SIDES:
        opened.on_caps(side, {"atoms": list(table_atoms)})
    return opened


def test_window_never_closes_before_l():
    opened = window()
    for side in SIDES:
        opened.on_hook_seen(side, HOOK)

    assert not opened.due

    opened.reach_lobby_ready()
    assert opened.due


def test_window_closes_at_l_without_any_proof():
    opened = window()
    opened.reach_lobby_ready()

    assert opened.due
    opened.close({})
    assert opened.withdrawn == []
    assert opened.active_atoms == ["damage_taken", "zombie_kill"]
    assert not opened.due


def test_only_a_known_failure_withdraws():
    opened = window()
    opened.close({HOOK: ["B"]})

    assert opened.withdrawn == [{"atom": ATOM, "hook": HOOK, "sides": ["B"]}]
    assert opened.active_atoms == ["zombie_kill"]


def test_window_opens_at_first_caps():
    opened = ContractWindow(load_effects())

    opened.on_hook_seen("A", HOOK)
    assert opened.seen["A"] == set()

    opened.on_caps("B", {"atoms": [ATOM]})
    opened.on_hook_seen("A", HOOK)
    assert opened.seen["A"] == {HOOK}


def test_atom_missing_from_one_caps_is_not_active():
    opened = window()
    opened.on_caps("B", {"atoms": ["zombie_kill"]})

    opened.close({})

    assert opened.active_atoms == ["zombie_kill"]
    assert opened.withdrawn == []


def test_a_wound_or_a_hook_detects_the_side():
    opened = window()
    assert not opened.detected("A", HOOK)

    opened.on_wound("A", ATOM)
    opened.on_hook_seen("B", HOOK)

    assert opened.detected("A", HOOK)
    assert opened.detected("B", HOOK)
