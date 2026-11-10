"""Premier lancement par l'API : contrôle sans écriture, installation, choix du dossier (R5)."""

from __future__ import annotations

import http.client
import inspect
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

import pytest
from doubles.fake_server import FakeRcon
from test_match import Duel
from test_modes import forbidden_factory, settle, step
from test_preflight import (  # noqa: F401
    FailingMoves,
    assert_no_terminal,
    free_ports,
    make_install,
    player_mod,
    zomboid_home,
)
from test_servers import Service, stopped_for_good, until
from test_snapshots import engine_rig  # noqa: F401

from service import api as api_module
from service import engine as engine_module
from service import main as main_module
from service import paths
from service.api import make_server
from service.engine import ACTIONS, STOP_INCOMPLETE, Engine
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
from service.servers import STATUS_DOWN, STOP_RCON

JSON_TYPE = "application/json"
INSTALL_ROUTE = "/api/setup/install-mod"
FOLDER_ROUTE = "/api/setup/pz-dir"
WRITE_ROUTES = (INSTALL_ROUTE, FOLDER_ROUTE)
QUIT_ROUTE = "/api/quit"
UI_DIR = Path(engine_module.__file__).parent.parent / "ui"
# Filet d'un test bloqué, au-delà de tout budget d'arrêt : c'est le test qui mesure le budget.
MAIN_EXIT_S = 30
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


def test_failed_restore_reaches_the_ui_with_the_kept_folder(
    healthy, serve, game, monkeypatch
):
    """B1 : l'avis s'efface, le journal reste ; les deux disent où le mod du joueur est sauvé."""
    mod = player_mod(healthy)
    FailingMoves(monkeypatch, allowed=1)
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)

    status, answer = request(port, INSTALL_ROUTE, {})

    (kept,) = healthy.glob(f".{MOD_ID}-*/previous")
    assert (kept / "retouche.txt").read_text(encoding="utf-8") == "du joueur"
    assert status == 409
    assert answer["ok"] is False
    assert answer["message"].startswith("Could not install the mod: ")
    assert str(kept) in answer["message"]
    assert str(mod) in answer["message"]
    assert_no_terminal(answer["message"], healthy)
    assert any(str(kept) in entry["msg"] for entry in engine.snapshot()["logs"])


def test_failed_install_answers_without_trace(healthy, serve, game, monkeypatch):
    def refuse(*_, **__):
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

    def slow_install(*args, **options):
        entered.set()
        assert release.wait(WAIT_S)
        return install_mod(*args, **options)

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

    def slow_install(*args, **options):
        entered.set()
        assert release.wait(WAIT_S)
        return install_mod(*args, **options)

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


# Sortie par l'UI ------------------------------------------------------------------------------


class MainRun:
    """`main()` sur son fil : le test garde le sien pour parler à l'API."""

    def __init__(self, engine: Engine, servers: list, shutdowns: list) -> None:
        self.servers = servers
        self.shutdowns = shutdowns
        self.code: int | str | None = None
        argv = ["--mode", engine.mode, "--no-browser", "--port", "0"]
        self.thread = threading.Thread(target=self.run, args=(argv, engine), daemon=True)
        self.thread.start()

    def run(self, argv: list[str], engine: Engine) -> None:
        try:
            main_module.main(argv, lambda mode, pz_dir: engine)
            self.code = 0
        except SystemExit as exc:
            self.code = exc.code

    @property
    def port(self) -> int:
        deadline = time.monotonic() + WAIT_S
        while not self.servers:
            assert time.monotonic() < deadline, "le service n'a jamais écouté"
            time.sleep(POLL_S)
        return self.servers[-1].server_address[1]

    def wait(self) -> int | str | None:
        self.thread.join(MAIN_EXIT_S)
        assert not self.thread.is_alive(), "main() n'a pas rendu la main"
        return self.code


@pytest.fixture
def run_main(monkeypatch):
    """Rend de quoi lancer `main()`. Hors du fil principal, aucun signal ne se pose."""
    monkeypatch.setattr(main_module, "STOP_SIGNALS", ())
    servers: list = []
    shutdowns: list = []

    def recording_server(engine: Engine, **kwargs):
        server = make_server(engine, **kwargs)
        shutdown = server.shutdown
        server.shutdown = lambda: shutdowns.append(1) or shutdown()
        servers.append(server)
        return server

    monkeypatch.setattr(main_module, "make_server", recording_server)
    runs: list[MainRun] = []

    def start(engine: Engine) -> MainRun:
        runs.append(MainRun(engine, servers, shutdowns))
        return runs[-1]

    yield start
    for run in runs:
        if run.thread.is_alive():
            run.servers[-1].on_quit()
            run.thread.join(MAIN_EXIT_S)


def running_duel(rig) -> Duel:
    duel = Duel(rig)
    duel.start_match()
    return duel


def stop_budget_s(rig) -> float:
    return rig.runtime["stop_timeout_s"] + rig.runtime["kill_grace_s"]


def quit_service(port: int) -> None:
    status, answer = request(port, QUIT_ROUTE, {})
    assert status == 200 and answer["ok"] is True


def test_ac28_quit_during_a_real_match_goes_through_the_full_stop(engine_rig, run_main):  # noqa: F811
    duel = running_duel(engine_rig)
    match = duel.engine._match
    servers = match.pair.servers.values()
    assert len(servers) == 2 and all(server.running for server in servers)
    run = run_main(duel.engine)
    port = run.port

    started = time.monotonic()
    quit_service(port)
    code = run.wait()

    assert code == 0
    assert time.monotonic() - started < stop_budget_s(engine_rig)
    assert not any(server.running for server in servers)
    assert stopped_for_good(engine_rig)
    assert match._journal is None
    phases = [r["data"]["phase"] for r in duel.records("state") if "phase" in r["data"]]
    assert phases[-3:] == ["running", "stopping", "idle"]


def test_ac28_quit_ends_the_service_process_with_code_zero(engine_rig, tmp_path):  # noqa: F811
    """Un vrai processus : le code est celui que voit le système, et aucun signal n'y aide."""
    engine_rig.build()
    groups_dir = tmp_path / "no-groups"
    groups_dir.mkdir()
    service = Service(engine_rig, groups_dir)
    try:
        service.start_duel()
        until(lambda: service.state()["phase"] in ("lobby", "error"))
        state = service.state()
        assert state["phase"] == "lobby", state["error"]

        started = time.monotonic()
        quit_service(urlparse(service.url).port)
        code = service.stop()
        elapsed_s = time.monotonic() - started
    finally:
        service.kill()

    assert code == 0
    assert elapsed_s < stop_budget_s(engine_rig)
    records = engine_rig.journal(state["match_id"])
    down = {r["side"] for r in records if r["data"].get("server") == STATUS_DOWN}
    assert down == {"A", "B"}
    phases = [r["data"]["phase"] for r in records if "phase" in r["data"]]
    assert phases[-2:] == ["stopping", "idle"]


def test_ac11_quit_stops_the_servers_through_rcon_first(engine_rig, run_main):  # noqa: F811
    """Sur `windows-latest`, c'est AC11 par la sortie de l'UI : aucun signal n'y existe."""
    duel = running_duel(engine_rig)
    servers = duel.engine._match.pair.servers
    run = run_main(duel.engine)

    with ExitStack() as stack:
        for side, server in servers.items():
            # Le mot de passe est tiré par match : le double du script ne peut pas le connaître.
            port = server._cfg["ports"][side]["rcon"]
            password = server._cfg["rcon_password"]
            rcon = FakeRcon(password, on_quit=server._write_quit, port=port)
            stack.enter_context(rcon)
        quit_service(run.port)
        assert run.wait() == 0

    assert [server.stop_stage for server in servers.values()] == [STOP_RCON, STOP_RCON]
    assert stopped_for_good(engine_rig)


def test_incomplete_stop_keeps_its_failure_code(engine_rig, run_main):  # noqa: F811
    duel = running_duel(engine_rig)
    duel.engine._match.pair.close = lambda: False
    run = run_main(duel.engine)

    quit_service(run.port)

    assert run.wait() == STOP_INCOMPLETE


def test_second_quit_answers_and_stops_once(run_main, runtime, tmp_path):
    engine = Engine("mock", runtime=runtime, matches_dir=tmp_path / "matches")
    entered, release = threading.Event(), threading.Event()
    stops = []
    stop_service = engine._stop_service

    def held_stop() -> bool:
        stops.append(1)
        entered.set()
        assert release.wait(WAIT_S)
        return stop_service()

    engine._stop_service = held_stop
    run = run_main(engine)
    port = run.port

    quit_service(port)
    assert entered.wait(WAIT_S)
    quit_service(port)
    release.set()

    assert run.wait() == 0
    assert len(stops) == 1
    assert len(run.shutdowns) == 1


@pytest.mark.parametrize(
    "header", [{"Host": "evil.example"}, {"Origin": "http://evil.example"}], ids=["host", "origin"]
)
def test_ac27_foreign_quit_is_refused_and_the_service_stays(run_main, runtime, tmp_path, header):
    engine = Engine("mock", runtime=runtime, matches_dir=tmp_path / "matches")
    run = run_main(engine)
    port = run.port

    status, answer = request(port, QUIT_ROUTE, {}, **header)

    assert status == 403 and answer["ok"] is False
    assert request(port, "/api/state")[0] == 200
    assert run.thread.is_alive()
    assert engine._stopped is None
    quit_service(port)
    assert run.wait() == 0


@pytest.mark.parametrize("real", [True, False], ids=["reel", "simule"])
def test_setup_state_hashes_the_mod_with_no_lock_held(healthy, monkeypatch, real):
    """Depuis un autre fil : `lock` est réentrant, et un disque lent figerait `snapshot`."""
    engine = Engine(mode="real" if real else "mock")
    free = []

    def lock_is_free(lock) -> bool:
        taken = []

        def attempt() -> None:
            taken.append(lock.acquire(blocking=False))
            if taken[0]:
                lock.release()

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join()
        return taken[0]

    def fingerprint(root):
        free.append((lock_is_free(engine.lock), lock_is_free(engine._setup_lock)))
        return "empreinte"

    monkeypatch.setattr("service.engine.mod_fingerprint", fingerprint)

    assert engine.setup_state()["expected_mod_fingerprint"] == "empreinte"
    assert free == [(True, True)]


# -- UI : lecture statique, le rendu se vérifie à la main ---------------------


def ui_script() -> str:
    return (UI_DIR / "app.js").read_text("utf-8")


def test_ac26_can_launch_commands_the_disabled_attribute():
    script = ui_script()

    assert "const canLaunch = (setup) => Boolean(setup && setup.ok);" in script
    (button,) = [line for line in script.splitlines() if 'data-action="start"' in line]
    assert "Launch duel" in button
    assert "${canLaunch(setup) ? '' : 'disabled'}" in button


def test_setup_screen_handles_each_action_of_the_service():
    script = ui_script()

    for action in (INSTALL_MOD, CHOOSE_FOLDER):
        assert f"defect.action === '{action}'" in script
    assert f"d.action !== '{RETRY}'" in script
    for kind in ("pz", "mod", "ram", "port"):
        assert re.search(rf"setupRow\('[^']+', '{kind}'", script)
    for label, action in (
        ("Install mod", "install-mod"),
        ("Choose folder", "choose-folder"),
        ("Check again", "retry"),
    ):
        assert f'data-action="{action}"' in script and label in script
        assert f"a === '{action}'" in script


def test_setup_is_not_measured_at_the_pace_of_the_poll():
    """Une mesure lie les ports : à 700 ms, elle gênerait le service qu'elle contrôle."""
    script = ui_script()
    poll = script[script.index("async function poll()") : script.index("async function refreshSetup")]

    assert "/api/setup" not in poll
    assert "if (S.phase !== setupPhase && SETTLED_PHASES.includes(S.phase)) refreshSetup();" in poll


def test_answer_without_a_phase_keeps_the_state_and_says_unreachable():
    """Un 403, un 500 ou un 503 portent du JSON : ni `S` ni le badge ne doivent le croire."""
    script = ui_script()
    poll = script[script.index("async function poll()") : script.index("async function refreshSetup")]

    check = "if (!state || typeof state.phase !== 'string') return unreachable();"
    assert poll.index("await api('/api/state')") < poll.index(check) < poll.index("S = state;")
    assert poll.index("S = state;") < poll.index("'service : ok'")
    assert poll.count("S = ") == 1
    assert "'service injoignable'" in script[: script.index("async function poll()")]


def test_error_screen_can_run_the_checks_again():
    """Hors d'`idle`, `/api/setup` rend la dernière mesure : sans ce départ, l'UI resterait bloquée."""
    script = ui_script()
    error = script[script.index("function renderError()") : script.index("function renderQuit()")]

    assert 'data-action="rematch"' in error


def lobby_ports_ui(script: str) -> tuple[str, str, str]:
    """`(portsBlock, prefillAddress, inviteCard)` : les trois morceaux de l'ouverture des ports."""
    block = script[script.index("function portsBlock()") : script.index("// Tout vient de `invite`")]
    prefill = script[script.index("function prefillAddress()") : script.index("function portsBlock()")]
    invite = script[script.index("function inviteCard()") : script.index("function renderLive()")]
    return block, prefill, invite


def test_phase3_ac27_lobby_has_the_open_ports_button_only_in_a_real_duel():
    script = ui_script()
    block, _, invite = lobby_ports_ui(script)

    # Le libelle vient de l'API : ports, protocoles et bail de `ports_opening`.
    named = script[script.index("function portsNamed(") : script.index("function leaseText(")]
    assert "(po.ports || []).map((p) => `${p.port}/${p.protocol}`)" in named
    assert "const po = S.ports_opening || {};" in block
    assert "if (Number.isFinite(po.lease_s) && po.lease_s > 0) portsLease = po.lease_s;" in block
    assert "`Open ports ${portsNamed(po)}${portsLease ? ` for ${leaseText(portsLease)}` : ''}`" in block
    leases = script[script.index("function leaseText(") : script.index("// Heure locale")]
    assert "`${s / 3600} h`" in leases
    (button,) = [line for line in block.splitlines() if 'data-action="open-ports"' in line]
    assert "${busy ? 'disabled' : ''}" in button and "${esc(label)}" in button
    assert "const busy = po.status === 'in_progress';" in block
    assert "busy ? 'Opening ports…'" in block
    # Le bloc n'existe que dans la branche `real ?` du champ d'adresse : absent en mode simule.
    branch = invite[invite.index("${real ? `<label class=\"field address\">") :]
    branch = branch[: branch.index("` : ''}") + len("` : ''}")]
    assert "${portsBlock()}" in branch
    assert invite.count("portsBlock()") == 1
    assert script.count("${portsBlock()}") == 1
    assert "const real = S.mode === 'real';" in invite
    # Le clic passe par le meme `api()` que `set-address`.
    assert "if (a === 'open-ports') { return answered(await api('/api/ports/open', {})); }" in script
    assert "/api/ports/open" in inspect.getsource(api_module)
    css = (UI_DIR / "style.css").read_text("utf-8")
    for variant in ("mapped", "no_gateway", "refused", "invalid_config"):
        assert f".ports.{variant}" in css


def test_phase3_ac28_prefill_manual_instruction_and_expiry_in_the_lobby():
    script = ui_script()
    block, prefill, _ = lobby_ports_ui(script)

    # Pre-remplissage : seulement un `mapped` avec adresse, une fois, et seulement si le champ est vide.
    assert "if (!po || po.status !== 'mapped' || !po.address || po.expires_at === portsPrefilled) return;" in prefill
    assert "if (addressDraft === '') addressDraft = po.address;" in prefill
    assert [line.strip() for line in prefill.splitlines() if "addressDraft =" in line] == [
        "if (addressDraft === '') addressDraft = po.address;"
    ]
    assert "api(" not in prefill  # l'invitation ne change qu'au clic sur "Use this address" (R5)
    assert '<input id="f-address" value="${esc(addressDraft)}"' in script
    poll = script[script.index("async function poll()") : script.index("async function refreshSetup")]
    assert poll.index("S = state;") < poll.index("prefillAddress();") < poll.index("render();")
    assert "if (out.state) { S = out.state; prefillAddress(); render(); }" in script
    # `no_gateway` / `refused` / `invalid_config` : raison et consigne manuelle de l'API (AC15).
    failed = block[block.index("['no_gateway', 'refused', 'invalid_config'].includes(po.status)") :]
    assert "${esc(portsReason(po))}" in failed
    assert "${po.manual ? `<p class=\"hint\">${esc(po.manual)}</p>` : ''}" in failed
    # `mapped` : l'heure d'expiration vient de `expires_at` (epoch), en heure locale du navigateur.
    mapped = block[block.index("if (po.status === 'mapped')") : block.index("} else if")]
    assert "${esc(expiryText(po.expires_at))}" in mapped
    assert "po.address_note" in mapped
    expiry = script[script.index("function expiryText(") : script.index("function portsReason(")]
    assert "new Date(expiresAt * 1000)" in expiry


def phase2_ui_script() -> str | None:
    """`ui/app.js` a la base de la phase 2 (`3b5f407`), ou `None` sans historique git."""
    try:
        out = subprocess.run(
            ["git", "show", "3b5f407:ui/app.js"],
            cwd=UI_DIR.parent,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.decode("utf-8")


def test_phase3_ac29_added_ui_lines_are_english():
    script = ui_script()
    block, prefill, _ = lobby_ports_ui(script)
    ports = script[script.index("// ---------------------------------------------------------------- ports") :]
    ports = ports[: ports.index("// Tout vient de `invite`")]
    forbidden = re.compile(r"[éèàç]|adresse|ouvrir|échec", re.IGNORECASE)

    base = phase2_ui_script()
    if base is None:
        # Sans historique, au moins le code propre a la phase.
        added = ports.splitlines()
    else:
        before = set(base.splitlines())
        added = [line for line in script.splitlines() if line not in before]
        assert all(line in added for line in block.splitlines() if line.strip() and line not in before)
        assert any("data-action=\"open-ports\"" in line for line in added)
    assert added
    assert [line for line in added if forbidden.search(line)] == []


def test_ui_only_calls_routes_of_the_service():
    routes = set(re.findall(r"'(/api/[a-z/-]+)'", ui_script()))

    assert {QUIT_ROUTE, *WRITE_ROUTES, "/api/setup", "/api/invite/address"} <= routes
    source = inspect.getsource(api_module)
    assert [route for route in routes if f'"{route}"' not in source] == []


def test_quit_button_reaches_the_quit_route_and_its_end_screen():
    script = ui_script()
    page = (UI_DIR / "index.html").read_text("utf-8")

    assert re.search(r'<button[^>]*data-action="quit"[^>]*>Quit</button>', page)
    assert "if (a === 'quit') { return quit(); }" in script
    assert f"await api('{QUIT_ROUTE}', {{}})" in script
    assert "if (quitting) return renderQuit();" in script
    assert "PZ Duel has stopped" in script
