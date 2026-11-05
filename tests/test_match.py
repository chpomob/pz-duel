"""Lobby réel : joueurs, sondes de spawn, sceau de la table et décompte, sur les doubles."""

from __future__ import annotations

import json
import shutil

import pytest

from test_contract import PLAYER, SIDES, Lobby, MS, L_S, state_entered_at
from test_servers import stopped_for_good
from test_snapshots import engine_rig  # noqa: F401
from service import engine as engine_module
from service.verdict import spawn_status

PRESET = "demo"
PROBE_TIMEOUT_S = 30


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
