"""Premier lancement par l'API : contrôle sans écriture, installation, choix du dossier (R5)."""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import threading
import time
from functools import partial
from pathlib import Path

import pytest
from test_modes import forbidden_factory, settle, step
from test_preflight import assert_no_terminal, free_ports, make_install, zomboid_home  # noqa: F401

from service import engine as engine_module
from service import paths
from service.api import make_server
from service.engine import ACTIONS, Engine
from service.platforms import LINUX, WINDOWS, current
from service.preflight import (
    ABSENT,
    CHOOSE_FOLDER,
    INSTALL_MOD,
    MOD_ID,
    MOD_SRC,
    PRESENT,
    RETRY,
    PreflightResult,
    install_mod,
    mod_fingerprint,
)

JSON_TYPE = "application/json"
INSTALL_ROUTE = "/api/setup/install-mod"
FOLDER_ROUTE = "/api/setup/pz-dir"
WRITE_ROUTES = (INSTALL_ROUTE, FOLDER_ROUTE)
POLL_S = 0.01
WAIT_S = 5
HELD_S = 0.05
THREADS = 4
ENOUGH_RAM_MB = 1 << 20
TOO_MUCH_RAM_MB = 1 << 40
BUSY_PHASES = ("preflight", "starting", "contract", "lobby", "running", "over", "error")


def tree(root: Path) -> dict:
    """Noms et contenus : un fichier ajouté, retiré ou réécrit autrement s'y voit."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def request(port: int, path: str, body: dict | None = None, **headers: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT_S)
    try:
        sent = {"Host": f"127.0.0.1:{port}", "Content-Type": JSON_TYPE} | headers
        method, data = ("GET", None) if body is None else ("POST", json.dumps(body).encode())
        conn.request(method, path, data, sent)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def setup(port: int) -> dict:
    status, answer = request(port, "/api/setup")
    assert status == 200
    return answer


def write_body(route: str, folder: Path | str = "") -> dict:
    return {"folder": str(folder)} if route == FOLDER_ROUTE else {}


@pytest.fixture
def game(tmp_path) -> Path:
    return make_install(tmp_path / "game")


@pytest.fixture
def healthy(zomboid_home, runtime, monkeypatch):  # noqa: F811
    """Une machine sans défaut : chaque test y casse ce qu'il mesure."""
    monkeypatch.delenv("PZ_INSTALL_DIR", raising=False)
    monkeypatch.setattr(
        engine_module,
        "run_preflight",
        partial(engine_module.run_preflight, ram_reader=lambda: ENOUGH_RAM_MB),
    )
    runtime["ports"] = free_ports(runtime)
    install_mod(MOD_SRC, zomboid_home)
    (zomboid_home / "Server" / "duelA.ini").write_bytes(b"Public=false\r\n")
    (zomboid_home / "Saves" / "Multiplayer" / "world.bin").write_bytes(bytes(range(256)))
    return zomboid_home


@pytest.fixture
def serve(tmp_path, runtime):
    """Démarre un moteur et rend `(engine, port)`."""
    servers = []

    def start(mode: str = "real", **kwargs) -> tuple[Engine, int]:
        kwargs.setdefault("server_factory", forbidden_factory)
        kwargs.setdefault("matches_dir", tmp_path / "matches")
        engine = Engine(mode, runtime=runtime, **kwargs)
        server = make_server(engine)
        threading.Thread(target=server.serve_forever, args=(POLL_S,), daemon=True).start()
        servers.append(server)
        return engine, server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_healthy_machine_answers_ok_without_writing(healthy, serve, game):
    _, port = serve(zomboid_dir=healthy, pz_dir=game)
    before = tree(healthy)
    answer = setup(port)
    assert answer["ok"] is True and answer["measured"] is True
    assert answer["pz"] == PRESENT
    assert answer["defects"] == []
    assert answer["values"]["mod_fingerprint"] == answer["expected_mod_fingerprint"]
    assert tree(healthy) == before


def test_result_built_without_defects_keeps_its_shape():
    result = PreflightResult(("panne",), {}, "détail")
    assert result.defects == ()
    assert result.failures == ("panne",) and not result.ok


@pytest.mark.parametrize(
    ("kind", "action"),
    [("pz", CHOOSE_FOLDER), ("mod", INSTALL_MOD), ("ram", RETRY), ("port", RETRY)],
)
def test_ac23_each_defect_names_its_action_and_writes_nothing(
    healthy, serve, game, runtime, kind, action
):
    pz_dir = game.parent / "absent" if kind == "pz" else game
    if kind == "mod":
        shutil.rmtree(healthy / "mods" / MOD_ID)
    if kind == "ram":
        runtime["min_free_ram_mb"] = TOO_MUCH_RAM_MB
    _, port = serve(zomboid_dir=healthy, pz_dir=pz_dir)
    before = tree(healthy)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
        if kind == "port":
            holder.bind(("", runtime["ports"]["A"]["game"]))
        answer = setup(port)

    assert answer["ok"] is False
    (defect,) = answer["defects"]
    assert (defect["kind"], defect["action"]) == (kind, action)
    assert defect["message"]
    assert answer["pz"] == (ABSENT if kind == "pz" else PRESENT)
    assert tree(healthy) == before


def test_every_defect_is_reported_at_once(healthy, serve, game, runtime):
    shutil.rmtree(healthy / "mods" / MOD_ID)
    runtime["min_free_ram_mb"] = TOO_MUCH_RAM_MB
    _, port = serve(zomboid_dir=healthy, pz_dir=game.parent / "absent")
    assert [defect["kind"] for defect in setup(port)["defects"]] == ["pz", "ram", "mod"]


def test_missing_default_table_is_a_defect_not_a_crash(healthy, serve, game):
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    engine._presets = []
    (defect,) = setup(port)["defects"]
    assert (defect["kind"], defect["action"]) == ("table", RETRY)


def test_ac24_install_route_lays_the_mod_and_leaves_other_mods(healthy, serve, game):
    shutil.rmtree(healthy / "mods" / MOD_ID)
    other = healthy / "mods" / "autre"
    other.mkdir()
    (other / "mod.info").write_bytes(b"name=autre\n")
    before = tree(other)
    _, port = serve(zomboid_dir=healthy, pz_dir=game)
    assert setup(port)["values"]["mod_fingerprint"] is None

    status, answer = request(port, INSTALL_ROUTE, {})

    assert status == 200 and answer["ok"] is True
    state = setup(port)
    assert state["ok"] is True
    assert state["values"]["mod_fingerprint"] == mod_fingerprint(MOD_SRC)
    assert state["expected_mod_fingerprint"] == mod_fingerprint(MOD_SRC)
    assert tree(other) == before
    assert sorted(path.name for path in (healthy / "mods").iterdir()) == ["autre", MOD_ID]


def test_install_route_replaces_an_outdated_mod(healthy, serve, game):
    (healthy / "mods" / MOD_ID / "ancien.txt").write_bytes(b"x")
    _, port = serve(zomboid_dir=healthy, pz_dir=game)
    (defect,) = setup(port)["defects"]
    assert defect["action"] == INSTALL_MOD
    assert request(port, INSTALL_ROUTE, {})[0] == 200
    assert setup(port)["ok"] is True


def test_failed_install_answers_without_trace(healthy, serve, game, monkeypatch):
    def refuse(*_):
        raise PermissionError(13, "denied by the disk")

    monkeypatch.setattr(engine_module, "install_mod", refuse)
    _, port = serve(zomboid_dir=healthy, pz_dir=game)
    status, answer = request(port, INSTALL_ROUTE, {})
    assert status == 409
    assert answer == {"ok": False, "message": "Could not install the mod: denied by the disk"}


def test_ac25_valid_folder_serves_the_next_check_and_the_next_start(healthy, serve, game):
    engine, port = serve(zomboid_dir=healthy, pz_dir=game.parent / "absent")
    assert setup(port)["pz"] == ABSENT

    status, answer = request(port, FOLDER_ROUTE, {"folder": str(game)})

    assert status == 200 and answer["ok"] is True
    state = setup(port)
    assert state["pz"] == PRESENT
    assert state["values"]["pz_dir"] == str(game.resolve())
    assert state["ok"] is True
    assert request(port, "/api/action", {"action": "start", "preset": "demo"})[0] == 200
    assert step(settle(engine), "preflight")["state"] == "ok"


def test_ac25_invalid_folder_is_refused_and_the_previous_state_stays(
    healthy, serve, game, tmp_path
):
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    empty = tmp_path / "empty"
    empty.mkdir()

    status, answer = request(port, FOLDER_ROUTE, {"folder": str(empty)})

    assert status == 409 and answer["ok"] is False
    assert answer["message"]
    assert engine.pz_dir == game
    assert setup(port)["pz"] == PRESENT


def test_chosen_folder_no_longer_depends_on_the_working_directory(
    healthy, serve, game, monkeypatch
):
    _, port = serve(zomboid_dir=healthy, pz_dir=game.parent / "absent")
    monkeypatch.chdir(game.parent)
    assert request(port, FOLDER_ROUTE, {"folder": game.name})[0] == 200
    monkeypatch.chdir(healthy)
    assert setup(port)["pz"] == PRESENT


@pytest.mark.parametrize("folder", ["", 5, None, ["x"]], ids=repr)
def test_folder_that_is_no_text_is_refused_without_reaching_the_disk(
    healthy, serve, game, folder
):
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    status, answer = request(port, FOLDER_ROUTE, {"folder": folder})
    assert status == 400 and answer["ok"] is False
    assert engine.pz_dir == game


@pytest.mark.parametrize("folder", ["\ud800", "a\x00b", "x" * 5000, "~inconnu-du-systeme/pz"])
def test_hostile_folder_is_a_plain_refusal(healthy, serve, game, folder):
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    status, answer = request(port, FOLDER_ROUTE, {"folder": folder})
    assert status == 409 and answer["ok"] is False
    assert engine.pz_dir == game


def test_ac26_refused_start_gives_the_same_defect_as_the_state_route(healthy, serve, game):
    shutil.rmtree(healthy / "mods" / MOD_ID)
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    expected = setup(port)["defects"]
    assert [defect["kind"] for defect in expected] == ["mod"]

    assert request(port, "/api/action", {"action": "start", "preset": "demo"})[0] == 200
    state = settle(engine)

    assert state["phase"] == "error"
    assert expected[0]["message"] in state["error"]
    assert setup(port)["defects"] == expected


@pytest.mark.parametrize("route", WRITE_ROUTES)
@pytest.mark.parametrize(
    "header", [{"Host": "evil.example"}, {"Origin": "http://evil.example"}], ids=["host", "origin"]
)
def test_ac27_foreign_request_is_refused_and_writes_nothing(
    healthy, serve, game, tmp_path, route, header
):
    shutil.rmtree(healthy / "mods" / MOD_ID)
    other = make_install(tmp_path / "other")
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    before = tree(healthy)
    status, _ = request(port, route, write_body(route, other), **header)
    assert status == 403
    assert tree(healthy) == before
    assert engine.pz_dir == game


@pytest.mark.parametrize("route", WRITE_ROUTES)
@pytest.mark.parametrize("phase", BUSY_PHASES)
def test_ac27_busy_engine_refuses_and_writes_nothing(
    healthy, serve, game, tmp_path, route, phase
):
    shutil.rmtree(healthy / "mods" / MOD_ID)
    other = make_install(tmp_path / "other")
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    engine.state.phase = phase
    before = tree(healthy)
    status, answer = request(port, route, write_body(route, other))
    assert status == 409 and answer["ok"] is False
    assert tree(healthy) == before
    assert engine.pz_dir == game


@pytest.mark.parametrize("route", WRITE_ROUTES)
def test_mock_refuses_the_writes_and_touches_nothing(healthy, serve, game, route):
    shutil.rmtree(healthy / "mods" / MOD_ID)
    engine, port = serve("mock", zomboid_dir=healthy)
    before = tree(healthy)
    status, answer = request(port, route, write_body(route, game))
    assert status == 409 and answer["ok"] is False
    assert tree(healthy) == before
    assert engine.pz_dir is None


@pytest.mark.parametrize("route", WRITE_ROUTES)
def test_stopping_service_refuses_the_writes(healthy, serve, game, tmp_path, route):
    shutil.rmtree(healthy / "mods" / MOD_ID)
    other = make_install(tmp_path / "other")
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    assert engine.shutdown()
    before = tree(healthy)
    assert request(port, route, write_body(route, other))[0] == 409
    assert tree(healthy) == before
    assert engine.pz_dir == game


def forbid_measures(monkeypatch) -> None:
    def explode(*_, **__):
        raise AssertionError("mesure interdite : elle lierait les ports")

    monkeypatch.setattr(engine_module, "run_preflight", explode)


def test_mock_answers_ok_without_measuring(serve, tmp_path, monkeypatch):
    forbid_measures(monkeypatch)
    _, port = serve("mock", zomboid_dir=tmp_path)
    answer = setup(port)
    assert answer["ok"] is True and answer["measured"] is False
    assert answer["defects"] == []
    assert answer["expected_mod_fingerprint"] == mod_fingerprint(MOD_SRC)


@pytest.mark.parametrize("phase", BUSY_PHASES)
def test_busy_engine_gives_the_last_state_without_binding_a_port(
    healthy, serve, game, monkeypatch, phase
):
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    last = setup(port)
    engine.state.phase = phase
    forbid_measures(monkeypatch)
    assert setup(port) == last


def test_busy_engine_without_a_measure_never_claims_ok(healthy, serve, game, monkeypatch):
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    engine.state.phase = "lobby"
    forbid_measures(monkeypatch)
    answer = setup(port)
    assert answer["ok"] is False and answer["measured"] is False
    assert answer["pz"] is None


def test_concurrent_checks_never_overlap(healthy, serve, game, monkeypatch):
    """Deux préflights simultanés se verraient l'un l'autre comme un port occupé."""
    measure = engine_module.run_preflight
    running: list[int] = []
    overlaps: list[int] = []

    def spy(*args, **kwargs):
        running.append(1)
        overlaps.append(len(running))
        time.sleep(HELD_S)
        running.pop()
        return measure(*args, **kwargs)

    monkeypatch.setattr(engine_module, "run_preflight", spy)
    _, port = serve(zomboid_dir=healthy, pz_dir=game)
    threads = [threading.Thread(target=setup, args=(port,)) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT_S)
    assert overlaps == [1] * THREADS


def test_start_preflight_waits_for_a_running_install(healthy, serve, game, monkeypatch):
    shutil.rmtree(healthy / "mods" / MOD_ID)
    entered, release = threading.Event(), threading.Event()

    def slow_install(*args):
        entered.set()
        assert release.wait(WAIT_S)
        return install_mod(*args)

    monkeypatch.setattr(engine_module, "install_mod", slow_install)
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    installing = threading.Thread(target=request, args=(port, INSTALL_ROUTE, {}))
    installing.start()
    assert entered.wait(WAIT_S)

    assert engine.action("start", {"preset": "demo"})[0]
    time.sleep(HELD_S)
    assert step(engine.snapshot(), "preflight")["state"] == "running"
    release.set()
    installing.join(WAIT_S)

    assert step(settle(engine), "preflight")["state"] == "ok"


def test_shutdown_waits_for_a_running_install(healthy, serve, game, monkeypatch):
    """Le fil de la requête est un démon : sortir avant lui laisserait `mods/` sans `duelcore`."""
    shutil.rmtree(healthy / "mods" / MOD_ID)
    entered, release = threading.Event(), threading.Event()

    def slow_install(*args):
        entered.set()
        assert release.wait(WAIT_S)
        return install_mod(*args)

    monkeypatch.setattr(engine_module, "install_mod", slow_install)
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)
    installing = threading.Thread(target=request, args=(port, INSTALL_ROUTE, {}))
    installing.start()
    assert entered.wait(WAIT_S)

    stopping = threading.Thread(target=engine.shutdown)
    stopping.start()
    stopping.join(HELD_S)
    held = stopping.is_alive()
    release.set()
    stopping.join(WAIT_S)

    assert held and not stopping.is_alive()
    assert tree(healthy / "mods" / MOD_ID) == tree(MOD_SRC)
    installing.join(WAIT_S)


def test_ac29_actions_keep_their_five_names():
    assert ACTIONS == {"start", "cancel", "rematch", "simulate_join", "open_game"}


@pytest.mark.parametrize("platform", [LINUX, WINDOWS], ids=["linux", "windows"])
def test_ac29_frozen_failure_messages_cite_no_terminal(
    healthy, serve, tmp_path, runtime, monkeypatch, platform
):
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    shutil.rmtree(healthy / "mods" / MOD_ID)
    runtime["min_free_ram_mb"] = TOO_MUCH_RAM_MB
    empty = tmp_path / "empty"
    empty.mkdir()
    shown: list[str] = []
    engine, port = serve(zomboid_dir=healthy, pz_dir=empty, platform=platform)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
        holder.bind(("", runtime["ports"]["A"]["game"]))
        defects = setup(port)["defects"]
        shown += [defect["message"] for defect in defects]
        shown.append(request(port, FOLDER_ROUTE, {"folder": str(empty)})[1]["message"])
        if platform is LINUX and current().posix_modes:
            locked = make_install(tmp_path / "locked", mode=0o644)
            shown.append(request(port, FOLDER_ROUTE, {"folder": str(locked)})[1]["message"])
        assert engine.action("start", {"preset": "demo"})[0]
        shown.append(settle(engine)["error"])
    assert {defect["kind"] for defect in defects} == {"pz", "port", "ram", "mod"}
    for message in shown:
        assert_no_terminal(message, tmp_path)
