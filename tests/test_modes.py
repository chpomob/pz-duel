"""Séparation mock / réel : le mock s'annonce simulé, le réel ne retombe jamais sur le mock."""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_preflight import free_ports, make_install

from service import engine as engine_module
from service import main as main_module
from service.api import make_server
from service.engine import ACTIONS, Engine, load_presets
from service.preflight import MOD_SRC, install_mod
from service.servers import ServerProcess

SIMULATED = "simulé"
CHECK_STEPS = ("preflight", "serverA", "serverB", "contract")
STATE_KEYS = {
    "version",
    "phase",
    "phase_label",
    "mode",
    "config",
    "steps",
    "servers",
    "players",
    "invite",
    "clock",
    "score",
    "threats_per_min",
    "events",
    "error",
    "logs",
}
NEW_STATE_KEYS = {"match_id", "capabilities", "result"}
PRESET_KEYS = {"id", "label", "tagline", "summary", "duration_s", "day_length"}
WAIT_S = 10
JSON_HEADERS = {"Content-Type": "application/json"}


class FastClock:
    """Avance d'une seconde à chaque lecture : les latences du mock passent en un instant."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1
        return self.now


class Launcher:
    def __init__(self, found: bool = True) -> None:
        self.found = found
        self.calls: list[list[str]] = []

    def __call__(self, args) -> bool:
        self.calls.append(list(args))
        return self.found


def forbidden_factory(*args, **kwargs):
    raise AssertionError("aucun serveur ne doit être construit ici")


def wait_phase(engine: Engine, *phases: str) -> dict:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        state = engine.snapshot()
        if state["phase"] in phases:
            return state
        time.sleep(0.01)
    raise AssertionError(f"phase {phases} non atteinte : {engine.snapshot()['phase']}")


def settle(engine: Engine) -> dict:
    """Attend la fin du fil du moteur, qui s'arrête seul en réel."""
    if engine._thread is not None:
        engine._thread.join(WAIT_S)
        assert not engine._thread.is_alive()
    return engine.snapshot()


def interrupt(engine: Engine) -> dict:
    """Interrompt le parcours mock, qui sinon attend l'adversaire ou la fin du match."""
    engine._stop.set()
    return settle(engine)


def step(state: dict, step_id: str) -> dict:
    return next(item for item in state["steps"] if item["id"] == step_id)


def state_text(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False).lower()


def real_engine(zomboid_home: Path, runtime: dict, **kwargs) -> Engine:
    kwargs.setdefault("pz_dir", zomboid_home / "absent")
    kwargs.setdefault("server_factory", forbidden_factory)
    return Engine("real", zomboid_dir=zomboid_home, runtime=runtime, **kwargs)


def test_mock_never_claims_real(runtime):
    engine = Engine("mock", runtime=runtime, clock=FastClock())
    assert SIMULATED in engine.snapshot()["logs"][0]["msg"]

    assert engine.action("start", {"preset": "demo"})[0]
    state = wait_phase(engine, "lobby")
    assert state["mode"] == "mock"
    deadline = time.monotonic() + WAIT_S
    while not state["players"]["A"] and time.monotonic() < deadline:
        state = engine.snapshot()

    for step_id in CHECK_STEPS:
        assert SIMULATED in step(state, step_id)["detail"]
    preflight = step(state, "preflight")["detail"]
    assert "Go" not in preflight
    assert not re.search(r"\d", preflight)

    ports = [str(port) for side in runtime["ports"].values() for port in side.values()]
    for line in [item["detail"] for item in state["steps"]] + [
        entry["msg"] for entry in state["logs"]
    ]:
        assert not any(port in line for port in ports)

    # Le parcours vu par l'UI ne change pas : invitation, simulation de l'adversaire.
    assert state["invite"]["port"] == runtime["ports"]["A"]["game"]
    assert engine.action("simulate_join")[0]
    assert wait_phase(engine, "over")["mode"] == "mock"


def test_real_never_falls_back(zomboid_home, runtime):
    engine = real_engine(zomboid_home, runtime)
    assert not any(SIMULATED in entry["msg"] for entry in engine.snapshot()["logs"])

    assert engine.action("start", {"preset": "demo"})[0]
    assert engine.snapshot()["mode"] == "real"
    assert engine.action("simulate_join")[0] is False
    state = settle(engine)

    assert state["mode"] == "real"
    assert state["phase"] == "error"
    assert "PZ absent" in state["error"]
    assert step(state, "preflight")["state"] == "error"
    assert step(state, "serverA")["state"] == "pending"
    assert state["servers"] == {"A": "down", "B": "down"}
    assert state["score"] == {"A": 0, "B": 0}
    assert state["invite"] == {}
    assert SIMULATED not in state_text(state)
    assert engine.action("simulate_join")[0] is False


def test_real_launches_after_successful_preflight(zomboid_home, tmp_path, runtime):
    """Le script vide sort aussitôt : l'échec vient du lancement, plus d'un arrêt transitoire."""
    runtime["ports"] = free_ports(runtime)
    runtime["min_free_ram_mb"] = 0
    install_mod(MOD_SRC, zomboid_home)
    engine = real_engine(
        zomboid_home,
        runtime,
        pz_dir=make_install(tmp_path / "pz"),
        matches_dir=tmp_path / "matches",
        server_factory=ServerProcess,
    )

    assert engine.action("start", {"preset": "demo"})[0]
    state = settle(engine)

    assert state["phase"] == "error"
    assert "passe de création en échec" in state["error"]
    preflight = step(state, "preflight")
    assert preflight["state"] == "ok"
    assert "RAM disponible" in preflight["detail"]
    assert step(state, "serverA")["state"] == "error"
    assert SIMULATED not in state_text(state)


def test_pz_dir_option_reaches_the_real_engine(tmp_path, monkeypatch):
    install = make_install(tmp_path / "pz")
    built: dict = {}

    class Built(Exception):
        pass

    def spy(*args, **kwargs):
        built.update(kwargs)
        raise Built

    monkeypatch.setattr(main_module, "Engine", spy)
    monkeypatch.setattr(sys, "argv", ["main", "--mode", "real", "--pz-dir", str(install)])

    with pytest.raises(Built):
        main_module.main()

    assert built == {"mode": "real", "pz_dir": str(install)}


@pytest.mark.parametrize(
    "field, value",
    [
        ("password", "abc\nRCONPassword=x"),
        ("password", "abc\rdef"),
        ("password", "=abc"),
        ("password", 1234),
        ("pseudo", "hote\nMods=x"),
        ("pseudo", "=hote"),
    ],
)
def test_real_refuses_ini_injection(zomboid_home, runtime, field, value):
    engine = real_engine(zomboid_home, runtime)

    ok, message = engine.action("start", {"preset": "demo", field: value})

    assert ok is False
    assert "refusé" in message
    assert str(value) not in message
    assert engine._thread is None
    assert engine.snapshot()["phase"] == "idle"


@pytest.mark.parametrize(
    "field, value",
    [
        ("day_length", 8),
        ("day_length", 0),
        ("day_length", -1),
        ("day_length", "abc"),
        ("day_length", "1"),
        ("day_length", 1.5),
        ("day_length", True),
        ("day_length", [1]),
        ("day_length", float("inf")),
        ("duration_s", "abc"),
        ("duration_s", 0),
        ("duration_s", 29),
        ("duration_s", 3601),
        ("duration_s", 10**400),
        ("duration_s", 120.5),
        ("duration_s", True),
        ("duration_s", float("nan")),
        ("duration_s", {"s": 1}),
        ("pseudo", 5),
        ("pseudo", ["hôte"]),
        ("password", 5),
        ("preset", ["demo"]),
        ("preset", 5),
    ],
)
@pytest.mark.parametrize("mode", ["mock", "real"])
def test_bad_start_payload_is_refused_before_any_effect(
    mode, field, value, zomboid_home, runtime
):
    engine = Engine(
        mode, zomboid_dir=zomboid_home, runtime=runtime, server_factory=forbidden_factory
    )
    before = engine.snapshot()

    ok, message = engine.action("start", {"preset": "demo", field: value})

    assert not ok and message
    assert engine._thread is None
    assert engine.snapshot() == before


@pytest.mark.parametrize("duration_s", [30, 3600])
def test_duration_bounds_are_accepted(duration_s, runtime):
    engine = Engine("mock", runtime=runtime)
    ok, message = engine.action("start", {"preset": "demo", "duration_s": duration_s})
    assert ok, message
    assert engine.snapshot()["config"]["duration_s"] == duration_s
    interrupt(engine)


@pytest.mark.parametrize("name", [[], {}, 5, None])
def test_action_name_of_a_wrong_type_is_refused(name, runtime):
    ok, message = Engine("mock", runtime=runtime).action(name)
    assert not ok and "action inconnue" in message


def test_second_start_is_refused_at_once(zomboid_home, runtime):
    engine = real_engine(zomboid_home, runtime)

    assert engine.action("start", {"preset": "demo"})[0]
    assert engine.action("start", {"preset": "demo"})[0] is False
    settle(engine)


def test_unknown_preset_refused(zomboid_home, runtime):
    engine = real_engine(zomboid_home, runtime)

    ok, message = engine.action("start", {"preset": "inconnu"})

    assert ok is False
    assert message == "preset inconnu : inconnu"
    assert engine._thread is None
    assert engine.snapshot()["phase"] == "idle"
    assert engine.snapshot()["config"] == {}

    # En mock, le comportement reste celui d'avant : le premier preset.
    mock = Engine("mock", runtime=runtime, clock=FastClock())
    assert mock.action("start", {"preset": "inconnu"})[0]
    assert mock.snapshot()["config"]["preset"] == mock.presets()[0]["id"]
    interrupt(mock)


def test_invalid_preset_discarded_and_reported(tmp_path, monkeypatch, runtime):
    source = Path(engine_module.PRESETS_DIR)
    shutil.copy(source / "demo.json", tmp_path / "demo.json")
    (tmp_path / "casse.json").write_text("{", encoding="utf-8")
    (tmp_path / "liste.json").write_text("[]", encoding="utf-8")
    raw = json.loads((source / "duel_rapide.json").read_text(encoding="utf-8"))
    raw["surplus"] = 1
    (tmp_path / "duel_rapide.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(engine_module, "PRESETS_DIR", str(tmp_path))

    engine = Engine("mock", runtime=runtime)

    assert [preset["id"] for preset in engine.presets()] == ["demo"]
    errors = [e["msg"] for e in engine.snapshot()["logs"] if e["level"] == "error"]
    assert len(errors) == 3
    for name in ("casse.json", "liste.json", "duel_rapide.json"):
        assert any(name in message for message in errors)


def test_shipped_presets_all_load(runtime):
    reported: list[str] = []

    presets = load_presets(runtime, report=reported.append)

    assert reported == []
    assert {preset["id"] for preset in presets} == {
        "demo",
        "duel_rapide",
        "hardcore",
        "pression",
    }


def test_no_valid_preset_is_a_refusal(tmp_path, monkeypatch, runtime):
    monkeypatch.setattr(engine_module, "PRESETS_DIR", str(tmp_path))
    engine = Engine("mock", runtime=runtime)

    assert engine.action("start") == (False, "aucun preset valide")


def test_open_game_real(zomboid_home, tmp_path, monkeypatch, runtime):
    launcher = Launcher()
    engine = real_engine(zomboid_home, runtime, steam_launcher=launcher)

    assert engine.action("open_game")[0] is False
    assert launcher.calls == []

    assert engine.action("start", {"preset": "demo", "password": "marqueur-joueur"})[0]
    settle(engine)
    ok, _ = engine.action("open_game")

    assert ok is True
    [args] = launcher.calls
    port = runtime["ports"]["A"]["game"]
    assert port == 17261
    assert args == [
        "-applaunch",
        str(runtime["steam_app_id"]),
        "+connect",
        f"127.0.0.1:{port}",
        "+password",
        "marqueur-joueur",
    ]
    assert not any("marqueur-joueur" in e["msg"] for e in engine.snapshot()["logs"])

    # Sans Steam : le lanceur double, puis le vrai lanceur sur un PATH sans `steam`.
    launcher.found = False
    ok, message = engine.action("open_game")
    assert ok is False
    assert "Steam introuvable" in message

    monkeypatch.setenv("PATH", str(tmp_path))
    no_steam = real_engine(zomboid_home, runtime)
    assert no_steam.action("start", {"preset": "demo"})[0]
    settle(no_steam)
    assert no_steam.action("open_game")[0] is False


def test_default_launcher_goes_through_steam(tmp_path, monkeypatch):
    steam = tmp_path / "steam"
    steam.write_text("#!/bin/sh\n", encoding="utf-8")
    steam.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    started: list[list[str]] = []
    monkeypatch.setattr(
        engine_module.subprocess, "Popen", lambda argv, **kwargs: started.append(argv)
    )

    assert engine_module.launch_steam(["+connect", "127.0.0.1:1"]) is True
    assert started == [[str(steam), "+connect", "127.0.0.1:1"]]


def call(url: str, data: dict | None = None) -> tuple[int, dict]:
    body = None if data is None else json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, JSON_HEADERS)) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


@pytest.mark.parametrize("mode", ["mock", "real"])
def test_api_backward_compatible(mode, zomboid_home, runtime):
    if mode == "real":
        engine = real_engine(zomboid_home, runtime, steam_launcher=Launcher())
    else:
        engine = Engine(mode, runtime=runtime, clock=FastClock())
    server = make_server(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api"
    try:
        status, state = call(f"{base}/state")
        assert status == 200
        assert len(STATE_KEYS) == 15
        assert set(state) == STATE_KEYS | NEW_STATE_KEYS
        assert state["mode"] == mode
        assert state["match_id"] is None
        assert state["capabilities"] == {}
        assert state["result"] is None

        _, presets = call(f"{base}/presets")
        assert presets["presets"]
        assert all(PRESET_KEYS <= set(preset) for preset in presets["presets"])

        assert ACTIONS == {"start", "cancel", "rematch", "simulate_join", "open_game"}
        for action in ("start", "simulate_join", "open_game", "cancel", "rematch"):
            status, reply = call(f"{base}/action", {"action": action, "preset": "demo"})
            assert status in (200, 409)
            assert isinstance(reply["ok"], bool)
            assert reply["message"]
            assert not reply["message"].startswith("action inconnue")
            assert STATE_KEYS <= set(reply["state"])
            assert reply["state"]["mode"] == mode
            if action == "start":
                assert reply["ok"] is True
                if mode == "real":
                    settle(engine)

        status, reply = call(f"{base}/action", {"action": "autre"})
        assert status == 409
        assert reply["message"] == "action inconnue: autre"
    finally:
        interrupt(engine)
        server.shutdown()
        server.server_close()
        thread.join(WAIT_S)
