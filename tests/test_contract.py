"""Contrat : écarts statiques refusés et nommés, puis fenêtre de preuve des hooks."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from doubles.fake_mod import FakeMod
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


class Lobby:
    """Vrai `Engine` réel mené au lobby sur les doubles, à un instant fourni par le test."""

    def __init__(self, rig, preset: str | None) -> None:
        self.rig = rig
        self.clock = FrozenClock()
        self.engine = rig.build()
        self.engine._clock = self.clock
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

    def at(self, seconds_after_l: float) -> None:
        self.clock.now = L_S + seconds_after_l

    def settle(self) -> None:
        """Attend qu'une passe ait lu une ligne écrite à l'instant courant."""
        line = json.loads(self.mods["A"].beat(players=self.present("A")))
        self.rig.until(lambda _: any(r["data"] == line for r in self.records("event")))
        time.sleep(self.engine.runtime["tick_ms"] / MS * 3)

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
    """Tue le groupe du serveur : pour le moteur, une sortie que personne n'a demandée."""
    os.killpg(lobby.engine._match.pair.servers[side]._process.pid, signal.SIGKILL)


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
    return next(r["ts_ms"] for r in lobby.records("state") if r["data"].get("phase") == phase)


def withdrawn_at_window_end(lobby: Lobby) -> None:
    lobby.mods["A"].hook_seen()
    lobby.join_and_resolve()
    lobby.wait_detail(f"hook {HOOK} attendu sur B ; temps restant : 120 s")

    lobby.at(119)
    lobby.wait_detail("temps restant : 1 s")
    assert lobby.state()["phase"] == "lobby"

    lobby.at(120)
    state = lobby.rig.wait("countdown", "error")
    assert state["phase"] == "countdown", state["error"]
    assert state_entered_at(lobby, "countdown") == (L_S + 120) * MS
    assert state["capabilities"]["withdrawn"] == [ATOM]
    assert ATOM not in state["capabilities"]["active_atoms"]
    assert "zombie_kill" in state["capabilities"]["active_atoms"]
    # Ce retrait ne conclut rien sur l'architecture.
    assert state["capabilities"]["fallback"] is None
    assert lobby.step("contract")["state"] == "ok"
    (withdrawn,) = lobby.records("atom_withdrawn")
    assert withdrawn["data"] == {"atom": ATOM, "hook": HOOK, "sides": ["B"]}
    assert withdrawn["ts_ms"] == (L_S + 120) * MS
    assert withdrawn["mt"] is None


def late_hook_read_with_the_deadline_pass(lobby: Lobby) -> None:
    lobby.mods["A"].hook_seen()
    lobby.join_and_resolve()
    lobby.wait_detail(f"hook {HOOK} attendu sur B ; temps restant : 120 s")

    def miss_the_deadline() -> None:
        # Sur le fil du moteur, juste avant `pump` : la passe qui constate l'échéance lit le hook.
        lobby.at(120.001)
        lobby.mods["B"].hook_seen()

    lobby.clock.on_next_read = miss_the_deadline
    state = lobby.rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert ATOM in state["capabilities"]["withdrawn"]
    (withdrawn,) = lobby.records("atom_withdrawn")
    assert withdrawn["data"] == {"atom": ATOM, "hook": HOOK, "sides": ["B"]}
    # Le hook tardif reste publié comme vu, sans compter dans la décision.
    assert state["capabilities"]["B"]["hooks"][HOOK]["seen"]


def published_without_the_atom(lobby: Lobby) -> None:
    lobby.mods["A"].hook_seen()
    lobby.join_and_resolve()

    state = lobby.rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state_entered_at(lobby, "countdown") == L_S * MS
    assert state["capabilities"]["withdrawn"] == [ATOM]
    assert ATOM not in state["capabilities"]["active_atoms"]
    assert "zombie_kill" in state["capabilities"]["active_atoms"]
    (withdrawn,) = lobby.records("atom_withdrawn")
    assert withdrawn["data"] == {"atom": ATOM, "hook": HOOK, "sides": ["B"]}

    # Un `hook_seen` tardif ne réactive rien, même publié comme vu.
    lobby.mods["B"].hook_seen()
    lobby.rig.until(lambda s: s["capabilities"]["B"]["hooks"][HOOK]["seen"])
    assert lobby.state()["capabilities"]["withdrawn"] == [ATOM]
    assert ATOM not in lobby.state()["capabilities"]["active_atoms"]


def seen_on_both_sides(lobby: Lobby) -> None:
    lobby.join_and_resolve()
    lobby.wait_detail(f"hook {HOOK} attendu sur A, B ; temps restant : 120 s")

    lobby.at(5)
    lobby.wait_detail("temps restant : 115 s")
    lobby.mods["A"].hook_seen()
    lobby.wait_detail(f"hook {HOOK} attendu sur B ; temps restant : 115 s")
    assert lobby.state()["phase"] == "lobby"
    lobby.mods["B"].hook_seen()

    state = lobby.rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state_entered_at(lobby, "countdown") == (L_S + 5) * MS
    assert ATOM in state["capabilities"]["active_atoms"]
    assert state["capabilities"]["withdrawn"] == []
    assert lobby.records("atom_withdrawn") == []


@pytest.mark.parametrize(
    "preset, scenario",
    [
        (WITH_ATOM, withdrawn_at_window_end),
        (WITH_ATOM, late_hook_read_with_the_deadline_pass),
        (WITHOUT_ATOM, published_without_the_atom),
        (WITH_ATOM, seen_on_both_sides),
    ],
)
def test_unseen_hook_withdraws_atom(engine_rig, preset, scenario):
    assert engine_rig.runtime["hook_window_s"] == 120

    scenario(Lobby(engine_rig, preset))


def test_default_preset_launches_without_the_proof(engine_rig):
    """Sans preset choisi, `duel_rapide` atteint le décompte même si aucune blessure n'arrive."""
    lobby = Lobby(engine_rig, None)
    assert lobby.state()["config"]["preset"] == WITH_ATOM
    lobby.join_and_resolve()
    lobby.wait_detail(f"hook {HOOK} attendu sur A, B ; temps restant : 120 s")

    lobby.at(engine_rig.runtime["hook_window_s"])
    state = engine_rig.wait("countdown", "error")

    assert state["phase"] == "countdown", state["error"]
    assert state["capabilities"]["withdrawn"] == [ATOM]


def test_hook_window_outlives_contract_step(engine_rig):
    lobby = Lobby(engine_rig, WITH_ATOM)

    assert lobby.step("contract")["state"] == "ok"
    lobby.wait_detail(f"hook {HOOK} attendu sur A, B")
    lobby.at(300)
    lobby.settle()
    assert lobby.state()["phase"] == "lobby"
    assert lobby.records("atom_withdrawn") == []
    assert lobby.state()["capabilities"]["active_atoms"] is None

    lobby.at(0)
    for mod in lobby.mods.values():
        mod.hook_seen()
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
    lobby.at(engine_rig.runtime["hook_window_s"] * 2)
    lobby.settle()
    assert lobby.state()["capabilities"]["active_atoms"] == active
    assert lobby.records("atom_withdrawn") == []


def window(table_atoms: list[str]) -> ContractWindow:
    opened = ContractWindow({"atoms": table_atoms}, load_effects(), 120)
    for side in SIDES:
        opened.on_caps(side, {"atoms": ["zombie_kill", ATOM]})
    return opened


def test_window_never_closes_before_l():
    opened = window(["zombie_kill", ATOM])
    for side in SIDES:
        opened.on_hook_seen(side, HOOK, 0)

    assert not opened.due(0)

    opened.reach_lobby_ready(10)
    assert opened.due(10)


def test_registered_hook_without_proof_withdraws_atom():
    """`caps` publie l'enregistrement : seul un `hook_seen` du hook requis le prouve."""
    opened = ContractWindow({"atoms": [ATOM]}, load_effects(), 120)
    for side in SIDES:
        opened.on_caps(side, {"atoms": [ATOM], "hooks": {HOOK: True}})
        opened.on_hook_seen(side, "OnHitZombie", 0)
    opened.on_hook_seen("A", HOOK, 0)
    opened.reach_lobby_ready(0)

    assert not opened.due(119_999)
    assert opened.due(120_000)
    opened.close()
    assert opened.withdrawn == [{"atom": ATOM, "hook": HOOK, "sides": ["B"]}]
    assert opened.active_atoms == []


def test_window_opens_at_first_caps():
    opened = ContractWindow({"atoms": [ATOM]}, load_effects(), 120)

    opened.on_hook_seen("A", HOOK, 0)
    assert opened.seen["A"] == set()

    opened.on_caps("B", {"atoms": [ATOM]})
    opened.on_hook_seen("A", HOOK, 0)
    assert opened.seen["A"] == {HOOK}


def test_window_keeps_first_l():
    opened = window([ATOM])
    opened.reach_lobby_ready(1000)
    opened.reach_lobby_ready(5000)

    assert not opened.due(120_999)
    assert opened.due(121_000)


def test_atom_missing_from_one_caps_is_not_active():
    opened = window(["zombie_kill"])
    opened.on_caps("B", {"atoms": ["zombie_kill"]})
    opened.on_hook_seen("A", HOOK, 0)
    opened.on_hook_seen("B", HOOK, 0)

    opened.close()

    assert opened.active_atoms == ["zombie_kill"]
    assert opened.withdrawn == []


def test_hook_after_deadline_is_published_but_proves_nothing():
    opened = window([ATOM])
    opened.reach_lobby_ready(1000)
    opened.on_hook_seen("A", HOOK, 121_000)
    opened.on_hook_seen("B", HOOK, 121_001)

    assert opened.seen["B"] == {HOOK}
    assert opened.due(121_001)
    opened.close()

    assert opened.withdrawn == [{"atom": ATOM, "hook": HOOK, "sides": ["B"]}]
    assert ATOM not in opened.active_atoms
