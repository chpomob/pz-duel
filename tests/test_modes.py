"""Séparation mock / réel : le mock s'annonce simulé, le réel ne retombe jamais sur le mock."""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from doubles.fake_server import compile_windows_client, plan_windows_client, write_script
from test_preflight import free_ports, make_install, zomboid_home  # noqa: F401

from service import engine as engine_module
from service import main as main_module
from service.api import make_server
from service.engine import ACTIONS, Engine, load_presets
from service.platforms import WINDOWS, current
from service.preflight import MOD_SRC, detect_pz, install_mod
from service.servers import ServerProcess
from service.state import PHASE_LABELS

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


class FakeClient:
    """Processus du client tel que l'engine le suit : vivant jusqu'à `exit`."""

    def __init__(self) -> None:
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def exit(self) -> None:
        self.code = 0


class Launcher:
    """Faux lanceur du client : il note l'appel, et échoue sur `failure`."""

    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[Path, list[str]]] = []
        self.client = FakeClient()

    def __call__(self, script, args, *, cwd) -> FakeClient:
        self.calls.append((script, list(args)))
        if self.failure is not None:
            raise engine_module.ClientLaunchError(self.failure)
        return self.client


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


def client_install(root: Path) -> tuple[Path, Path]:
    """Rend le dossier à passer en `pz_dir` et le client de l'hôte, à l'endroit du jeu."""
    host = current()
    game = root / "ProjectZomboid" / host.game_subdir
    game.mkdir(parents=True, exist_ok=True)
    write_script(game / host.server_script, "", "")
    client = host.client_file(game)
    if host.posix_modes:
        write_script(client, "", "")
    else:
        client.write_bytes(b"MZ")
    return client.parent, client


HOST_CONFIG = {"host_password": "marqueur-hote", "host_address": "127.0.0.1:17261"}


def client_engine(zomboid_home, runtime, pz_dir: Path, launcher) -> Engine:
    engine = real_engine(zomboid_home, runtime, pz_dir=pz_dir, client_launcher=launcher)
    engine.state.config = dict(HOST_CONFIG)
    engine.state.phase = "lobby"
    return engine


def test_open_game_needs_a_prepared_duel(zomboid_home, tmp_path, runtime):
    pz_dir, _ = client_install(tmp_path)
    launcher = Launcher()
    engine = real_engine(zomboid_home, runtime, pz_dir=pz_dir, client_launcher=launcher)

    assert engine.action("open_game")[0] is False
    assert launcher.calls == []


def test_open_game_is_direct_and_leaves_steam_mode(zomboid_home, tmp_path, runtime):
    """Les serveurs tournent en `zomboid.steam=0` : un client en mode Steam n'y entrerait pas."""
    pz_dir, script = client_install(tmp_path)
    launcher = Launcher()
    engine = client_engine(zomboid_home, runtime, pz_dir, launcher)

    ok, message = engine.action("open_game")

    assert ok is True
    assert "vérifiez en jeu la connexion" in message, "le lancement ne prouve pas la connexion"
    assert launcher.calls == [
        (script.resolve(), ["-nosteam", "+connect", "127.0.0.1:17261", "+password", "marqueur-hote"])
    ]
    assert not any("marqueur-hote" in entry["msg"] for entry in engine.snapshot()["logs"])


class PopenSpy:
    """Tient lieu de `subprocess.Popen` : note les options, et le client « reste ouvert »."""

    def __init__(self, refused: int = 0) -> None:
        self.refused = refused
        self.options: list[dict] = []

    def __call__(self, command, **options) -> PopenSpy:
        self.options.append(options)
        if len(self.options) <= self.refused:
            raise PermissionError(13, "Access is denied")
        return self

    def wait(self, timeout=None) -> int:
        raise subprocess.TimeoutExpired("client", timeout)

    def poll(self) -> None:
        return None


def test_open_game_runs_the_client_in_the_detected_game_folder(
    zomboid_home, tmp_path, runtime, monkeypatch
):
    """A1 : sans `cwd`, le client hérite du dossier du service, celui du téléchargement.

    C'est le dossier détecté, celui des serveurs, et non le parent du client : sous Linux,
    ce parent est un cran au-dessus.
    """
    pz_dir, _ = client_install(tmp_path)
    service_folder = tmp_path / "downloads"
    service_folder.mkdir()
    monkeypatch.chdir(service_folder)
    spy = PopenSpy()
    monkeypatch.setattr(subprocess, "Popen", spy)
    engine = real_engine(zomboid_home, runtime, pz_dir=pz_dir)
    engine.state.config = dict(HOST_CONFIG)
    engine.state.phase = "lobby"

    assert engine.action("open_game")[0] is True

    (options,) = spy.options
    assert options["cwd"] == detect_pz(pz_dir, os.environ, Path.home()).path


def test_every_launch_attempt_keeps_the_game_folder(tmp_path, monkeypatch):
    """Un Job qui refuse la sortie provoque un second essai : il doit garder le dossier."""
    spy = PopenSpy(refused=1)
    monkeypatch.setattr(subprocess, "Popen", spy)

    engine_module.launch_client(tmp_path / WINDOWS.client_program, [], WINDOWS, cwd=tmp_path)

    assert [options["cwd"] for options in spy.options] == [tmp_path, tmp_path]


def test_open_game_without_the_client_fails_explicitly(zomboid_home, tmp_path, runtime):
    """Binaire absent : aucun lancement, ni de relais qui se dirait réussi sans rien constater."""
    pz_dir, script = client_install(tmp_path)
    script.unlink()
    launcher = Launcher()
    engine = client_engine(zomboid_home, runtime, pz_dir, launcher)

    ok, message = engine.action("open_game")

    assert ok is False
    assert launcher.calls == []
    assert f"client PZ introuvable ({current().client_program} absent)" in message
    assert "à la main avec -nosteam, puis rejoignez 127.0.0.1:17261" in message
    assert "marqueur-hote" not in message
    assert engine.snapshot()["logs"][-1]["level"] == "warn"


def test_open_game_reports_a_refused_launch(zomboid_home, tmp_path, runtime):
    pz_dir, _ = client_install(tmp_path)
    launcher = Launcher("options refusées")
    engine = client_engine(zomboid_home, runtime, pz_dir, launcher)

    ok, message = engine.action("open_game")

    assert ok is False
    assert len(launcher.calls) == 1
    assert message.startswith("options refusées : lancez PZ à la main")


def test_open_game_keeps_the_engine_free_while_launching(zomboid_home, tmp_path, runtime):
    """Le lanceur attend le démarrage : ni `/api/state` ni le match n'attendent avec lui."""
    pz_dir, _ = client_install(tmp_path)
    free: list[bool] = []

    def probe():
        acquired = engine.lock.acquire(timeout=WAIT_S)
        free.append(acquired)
        if acquired:
            engine.lock.release()

    def launcher(script, args, *, cwd):
        other = threading.Thread(target=probe)
        other.start()
        other.join()
        return FakeClient()

    engine = client_engine(zomboid_home, runtime, pz_dir, launcher)

    assert engine.action("open_game")[0] is True
    assert free == [True]


@pytest.mark.parametrize(
    "phase", sorted(set(PHASE_LABELS) - {"lobby"}), ids=lambda phase: phase
)
def test_open_game_is_refused_outside_the_lobby(zomboid_home, tmp_path, runtime, phase):
    """`config` survit à la fin du duel : sans garde, le client viserait un serveur arrêté."""
    pz_dir, _ = client_install(tmp_path)
    launcher = Launcher()
    engine = client_engine(zomboid_home, runtime, pz_dir, launcher)
    engine.state.phase = phase

    ok, message = engine.action("open_game")

    assert ok is False
    assert message == f"impossible d'ouvrir le jeu en phase {phase} : il s'ouvre au lobby"
    assert launcher.calls == []


def test_open_game_is_refused_outside_the_lobby_in_mock_too(runtime):
    engine = Engine("mock", runtime=runtime, clock=FastClock())

    assert engine.action("open_game")[0] is False


def test_second_open_game_is_refused_while_the_first_is_starting(zomboid_home, tmp_path, runtime):
    """Deux appels rapprochés : le premier regarde encore le démarrage, hors du verrou."""
    pz_dir, _ = client_install(tmp_path)
    starting, release = threading.Event(), threading.Event()
    calls: list[list[str]] = []

    def launcher(script, args, *, cwd):
        calls.append(list(args))
        starting.set()
        release.wait(WAIT_S)
        return FakeClient()

    engine = client_engine(zomboid_home, runtime, pz_dir, launcher)
    first: list[tuple[bool, str]] = []
    thread = threading.Thread(target=lambda: first.append(engine.action("open_game")))
    thread.start()
    assert starting.wait(WAIT_S)

    ok, message = engine.action("open_game")
    release.set()
    thread.join(WAIT_S)

    assert ok is False
    assert message == "client PZ déjà lancé : rejoignez 127.0.0.1:17261 depuis celui-ci"
    assert first[0][0] is True
    assert len(calls) == 1


def test_open_game_waits_for_the_client_to_exit_before_relaunching(zomboid_home, tmp_path, runtime):
    pz_dir, _ = client_install(tmp_path)
    launcher = Launcher()
    engine = client_engine(zomboid_home, runtime, pz_dir, launcher)

    assert engine.action("open_game")[0] is True
    assert engine.action("open_game")[0] is False
    assert len(launcher.calls) == 1

    launcher.client.exit()

    assert engine.action("open_game")[0] is True
    assert len(launcher.calls) == 2


def test_failed_open_game_leaves_the_next_attempt_free(zomboid_home, tmp_path, runtime):
    pz_dir, _ = client_install(tmp_path)
    launcher = Launcher("options refusées")
    engine = client_engine(zomboid_home, runtime, pz_dir, launcher)

    assert engine.action("open_game")[0] is False
    launcher.failure = None

    assert engine.action("open_game")[0] is True
    assert len(launcher.calls) == 2


@pytest.fixture(scope="session")
def compiled_client(tmp_path_factory):
    """Le double de `ProjectZomboid64.exe`, un vrai PE. Rien à compiler hors de Windows."""
    if sys.platform != "win32":
        return None
    return compile_windows_client(tmp_path_factory.mktemp("client") / "client.exe")


def write_client(compiled, path: Path, sh_body: str, args_file: Path, **plan) -> Path:
    """Rend un client lançable : ce script sous POSIX, le PE compilé sous Windows."""
    if compiled is None:
        return write_script(path, sh_body, "")
    plan_windows_client(compiled, args_file, **plan)
    return compiled


def test_default_client_launcher_accepts_a_client_that_stays_up(
    tmp_path, monkeypatch, compiled_client
):
    monkeypatch.setattr(engine_module, "CLIENT_START_GRACE_S", 0.05)
    marker = tmp_path / "args"
    script = write_client(
        compiled_client,
        tmp_path / "pz.sh",
        f'echo "$@" > {marker}\nexec sleep 1',
        marker,
        stay_ms=1000,
    )

    process = engine_module.launch_client(script, ["-nosteam", "+connect"], cwd=tmp_path)
    assert process.poll() is None, "l'engine suit ce processus pour refuser un second client"
    deadline = time.monotonic() + WAIT_S
    while not (marker.exists() and marker.read_text(encoding="utf-8")):
        assert time.monotonic() < deadline
    assert marker.read_text(encoding="utf-8") == "-nosteam +connect\n"


@pytest.mark.parametrize("code", [0, 3], ids=["exit-0-du-vrai-script", "refus"])
def test_default_client_launcher_refuses_an_early_exit(
    tmp_path, monkeypatch, code, compiled_client
):
    """`projectzomboid.sh` finit par `exit 0` même quand le client n'a pas tenu."""
    monkeypatch.setattr(engine_module, "CLIENT_START_GRACE_S", WAIT_S)
    script = write_client(
        compiled_client, tmp_path / "pz.sh", f"exit {code}", tmp_path / "args", code=code
    )

    stopped = f"{re.escape(script.name)} s'est arrêté aussitôt \\(code {code}\\)"
    with pytest.raises(engine_module.ClientLaunchError, match=stopped):
        engine_module.launch_client(script, ["+password", "marqueur-hote"], cwd=tmp_path)


def test_default_client_launcher_names_a_missing_binary(tmp_path):
    with pytest.raises(engine_module.ClientLaunchError, match="pz.sh ne démarre pas") as caught:
        engine_module.launch_client(
            tmp_path / "pz.sh", ["+password", "marqueur-hote"], cwd=tmp_path
        )

    assert "marqueur-hote" not in str(caught.value)


def test_default_passwords_are_distinct_and_unrelated_to_the_code(zomboid_home, runtime):
    engine = real_engine(zomboid_home, runtime)

    assert engine.action("start", {"preset": "demo"})[0]
    settle(engine)
    config = engine.state.config

    assert config["password"] != config["host_password"]
    assert config["code"] not in (config["password"], config["host_password"])
    assert min(len(config["password"]), len(config["host_password"])) >= 16


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
        engine = real_engine(zomboid_home, runtime, client_launcher=Launcher())
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
