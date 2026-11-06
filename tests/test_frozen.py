"""R4 : binaire figé simulé, résolution (AC18, AC19) et démarrage (AC20, AC21, AC22, AC56).

R8 : empaquetage et test de fumée (AC46, AC48), sur un dossier simulé, sans binaire.

`service/paths.py` se résout à l'import : chaque cas tourne dans un sous-processus qui pose
`sys.frozen` et `sys._MEIPASS` avant le premier import de `service` (D-D). Le dossier
d'extraction est une copie marquée : une lecture qui passerait par le dépôt se verrait.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from test_preflight import make_install
from test_stdlib_only import foreign_imports

from service import api, main as main_module, paths, platforms
from service.journal import read_journal

REPO = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO / "service"
MARK = "extraction-marker"
PRESETS = ["demo", "duel_rapide", "hardcore", "pression"]
SUBPROCESS_TIMEOUT_S = 60
WINDOWS_LOCALAPPDATA = r"C:\Users\duel\AppData\Local"

FREEZE = """
import sys
sys.frozen = True
sys._MEIPASS = sys.argv[1]
"""

# Tous les chemins que le service écrit partent de ces racines.
READ_AND_WRITE = """
import http.client, json, threading
from pathlib import Path
from service import api, engine, paths, preflight, rules
from service.journal import Journal

service = engine.Engine("mock")
server = api.make_server(service)
threading.Thread(target=server.serve_forever, daemon=True).start()
connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
connection.request("GET", "/")
page = connection.getresponse().read().decode("utf-8")
server.shutdown()

journal = Journal(service.matches_dir / "m1", lambda: 0)
journal.write("state", {"phase": "idle"})
journal.close()
service._proofs.prove("pair", "hook")
installed = preflight.install_mod(preflight.MOD_SRC, service.zomboid_dir)
paths.log_file().parent.mkdir(parents=True, exist_ok=True)
paths.log_file().write_text("line", encoding="utf-8")

print(json.dumps({
    "page": page,
    "runtime": rules.load_runtime().get("marker"),
    "effects": rules.load_effects().get("marker"),
    "presets": {preset["id"]: preset["label"] for preset in engine.load_presets()},
    "installed_marker": (installed / "%(mark)s.txt").read_text(encoding="utf-8"),
    "reads": [
        api.UI_DIR, preflight.MOD_SRC, engine.PRESETS_DIR, rules.RUNTIME_PATH, rules.EFFECTS_PATH,
    ],
    "writes": [
        paths.data_dir(), paths.log_file(), engine.MATCHES_DIR, service.matches_dir,
        service._proofs._path, journal.path, service.zomboid_dir, installed,
    ],
}, default=str))
""" % {"mark": MARK}


def run_python(code: str, tmp_path: Path, *args: object, **env: str | None) -> dict:
    """Lance `code` hors du dépôt, et rend le JSON de sa sortie. `None` retire la variable."""
    merged = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1", **env}
    merged = {name: value for name, value in merged.items() if value is not None}
    done = subprocess.run(
        [sys.executable, "-c", code, *map(str, args)],
        cwd=tmp_path,
        env=merged,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def mark_json(path: Path, key: str, value: str) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[key] = value
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def extraction(tmp_path):
    """Dossier d'extraction d'un `--onefile`, dont chaque ressource diffère du dépôt."""
    root = tmp_path / "_MEI123"
    shutil.copytree(REPO / "ui", root / "ui")
    shutil.copytree(REPO / "mods" / "duelcore", root / "mods" / "duelcore")
    shutil.copytree(SERVICE_DIR / "presets", root / "service" / "presets")
    shutil.copy(SERVICE_DIR / "runtime.json", root / "service" / "runtime.json")
    shutil.copy(SERVICE_DIR / "effects.json", root / "service" / "effects.json")

    with open(root / "ui" / "index.html", "a", encoding="utf-8") as page:
        page.write(f"<!-- {MARK} -->")
    (root / "mods" / "duelcore" / f"{MARK}.txt").write_text(MARK, encoding="utf-8")
    for name in PRESETS:
        mark_json(root / "service" / "presets" / f"{name}.json", "label", f"{MARK} {name}")
    mark_json(root / "service" / "runtime.json", "marker", MARK)
    mark_json(root / "service" / "effects.json", "marker", MARK)
    return root


@pytest.fixture
def user_env(tmp_path):
    """Dossiers de l'utilisateur, pour Linux et Windows : rien ne sort de `tmp_path`."""
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(tmp_path / "data"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def test_ac18_resources_read_from_extraction(tmp_path, extraction, user_env):
    seen = run_python(FREEZE + READ_AND_WRITE, tmp_path, extraction, **user_env)

    assert f"<!-- {MARK} -->" in seen["page"]
    assert seen["installed_marker"] == MARK
    assert seen["presets"] == {name: f"{MARK} {name}" for name in PRESETS}
    assert seen["runtime"] == MARK
    assert seen["effects"] == MARK
    assert all(Path(path).is_relative_to(extraction) for path in seen["reads"])


def test_ac18_no_write_inside_extraction(tmp_path, extraction, user_env):
    before = tree_digest(extraction)

    seen = run_python(FREEZE + READ_AND_WRITE, tmp_path, extraction, **user_env)

    assert not [path for path in seen["writes"] if Path(path).is_relative_to(extraction)]
    assert tree_digest(extraction) == before


def test_ac19_matches_dir_with_windows_injected(tmp_path):
    code = FREEZE + """
import json
from service import paths
print(json.dumps({
    "data": str(paths.data_dir("win32")),
    "matches": str(paths.matches_dir("win32")),
    "log": str(paths.log_file("win32")),
}))
"""
    seen = run_python(code, tmp_path, tmp_path, LOCALAPPDATA=WINDOWS_LOCALAPPDATA)

    assert seen == {
        "data": WINDOWS_LOCALAPPDATA + r"\PZDuel",
        "matches": WINDOWS_LOCALAPPDATA + r"\PZDuel\matches",
        "log": WINDOWS_LOCALAPPDATA + r"\PZDuel\pz-duel.log",
    }


def test_ac19_default_platform_is_sys_platform(tmp_path):
    # `pathlib` d'abord : il choisit sa classe de chemin à l'import.
    code = FREEZE + """
import json, pathlib
sys.platform = "win32"
from service import paths
print(json.dumps(str(paths.matches_dir())))
"""
    seen = run_python(code, tmp_path, tmp_path, LOCALAPPDATA=WINDOWS_LOCALAPPDATA)

    assert seen == WINDOWS_LOCALAPPDATA + r"\PZDuel\matches"


def test_windows_data_dir_without_localappdata(tmp_path):
    code = FREEZE + """
import json
from service import paths
print(json.dumps(str(paths.data_dir("win32"))))
"""
    seen = run_python(code, tmp_path, tmp_path, LOCALAPPDATA=None, USERPROFILE=r"C:\Users\duel")

    assert seen == r"C:\Users\duel\AppData\Local\PZDuel"


LINUX_MATCHES = """
import json
from service import paths
print(json.dumps(str(paths.matches_dir("linux"))))
"""


def test_linux_frozen_matches_dir_follows_xdg(tmp_path, user_env):
    seen = run_python(FREEZE + LINUX_MATCHES, tmp_path, tmp_path, **user_env)

    assert Path(seen) == Path(user_env["XDG_DATA_HOME"]) / "pz-duel" / "matches"


@pytest.mark.parametrize("xdg", [None, "", "relative/data"])
def test_linux_frozen_matches_dir_without_usable_xdg(tmp_path, user_env, xdg):
    env = {**user_env, "XDG_DATA_HOME": xdg}

    seen = run_python(FREEZE + LINUX_MATCHES, tmp_path, tmp_path, **env)

    assert Path(seen) == Path(user_env["HOME"]) / ".local" / "share" / "pz-duel" / "matches"


def test_ac19_sources_keep_repo_matches(tmp_path, user_env):
    code = """
import json
from service import api, engine, paths, preflight, rules
print(json.dumps({
    "frozen": paths.is_frozen(),
    "root": paths.resource_root(),
    "matches": engine.MATCHES_DIR,
    "windows": paths.matches_dir("win32"),
    "engine": engine.Engine("mock").matches_dir,
    "ui": api.UI_DIR,
    "mod": preflight.MOD_SRC,
    "presets": engine.PRESETS_DIR,
    "runtime": rules.RUNTIME_PATH,
    "effects": rules.EFFECTS_PATH,
}, default=str))
"""
    seen = run_python(code, tmp_path, **user_env)

    assert seen == {
        "frozen": False,
        "root": str(REPO),
        "matches": str(REPO / "matches"),
        "windows": str(REPO / "matches"),
        "engine": str(REPO / "matches"),
        "ui": str(REPO / "ui"),
        "mod": str(REPO / "mods" / "duelcore"),
        "presets": str(SERVICE_DIR / "presets"),
        "runtime": str(SERVICE_DIR / "runtime.json"),
        "effects": str(SERVICE_DIR / "effects.json"),
    }


def test_ac19_journal_outlives_extraction(tmp_path, extraction, user_env):
    code = FREEZE + """
import json
from service.engine import Engine
from service.journal import Journal

journal = Journal(Engine("mock").matches_dir / "m1", lambda: 0)
journal.write("state", {"phase": "idle"})
journal.close()
print(json.dumps(str(journal.path)))
"""
    path = Path(run_python(code, tmp_path, extraction, **user_env))

    shutil.rmtree(extraction)

    assert not extraction.exists()
    assert path.is_relative_to(user_env["LOCALAPPDATA"])
    assert [record["data"] for record in read_journal(path).records] == [{"phase": "idle"}]


def test_only_paths_derives_from_file():
    users = [
        path.name
        for path in sorted(SERVICE_DIR.rglob("*.py"))
        if "__file__" in path.read_text(encoding="utf-8")
    ]

    assert users == ["paths.py"]


def test_paths_imports_nothing_from_service():
    tree = ast.parse((SERVICE_DIR / "paths.py").read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
    roots = [
        alias.name.split(".")[0]
        for node in imports
        if isinstance(node, ast.Import)
        for alias in node.names
    ] + [node.module.split(".")[0] for node in imports if isinstance(node, ast.ImportFrom)]

    assert not [node for node in imports if isinstance(node, ast.ImportFrom) and node.level]
    assert SERVICE_DIR.name not in roots


# Démarrage : `main()` tourne dans un sous-processus, sur son fil principal -------------------

SERVICE_UP_S = 30
POLL_S = 0.02
PROBE_S = 0.2
SHORT_WAIT_S = 1
STOPPERS = 8
LOG_RELATIVE = Path("pz-duel") / "pz-duel.log"
LOG_RELATIVE_WINDOWS = Path("PZDuel") / "pz-duel.log"
UNENCODABLE = "→✓"  # ni l'un ni l'autre n'existe en cp1252

# Un fichier `stop` tient lieu de signal : `send_signal` tuerait le processus sous Windows.
DRIVER = """
import io, json, signal, sys, threading, time
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out = Path(config["out"])
if config["frozen"]:
    sys.frozen = True
    sys._MEIPASS = config["extraction"]

from service import main as main_module, platforms
from service.engine import Engine

if config["wait_s"] is not None:
    main_module.load_runtime = lambda: {"instance_wait_s": config["wait_s"]}


def append(name, text):
    with open(out / name, "a", encoding="utf-8") as file:
        file.write(text + "\\n")


take_lock = platforms.take_instance_lock


def take_and_tell(path):
    lock = take_lock(path)
    append("locks.txt", "held" if lock is None else "taken")
    return lock


platforms.take_instance_lock = take_and_tell


def make_engine(mode, pz_dir):
    append("engines.txt", mode)
    if config["fail"]:
        raise RuntimeError("injected startup failure")
    return Engine(mode, zomboid_dir=out / "Zomboid", pz_dir=pz_dir, matches_dir=out / "matches")


def browser(url):
    append("browser.txt", url)
    if config["browser"] == "raise":
        raise OSError("no browser here")
    return config["browser"] == "open"


def watch_stop():
    while not (out / "stop").exists():
        time.sleep(0.02)
    signal.raise_signal(signal.SIGINT)


threading.Thread(target=watch_stop, daemon=True).start()
if config["streams"] == "none":
    sys.stdout = sys.stderr = None
elif config["streams"] == "cp1252":
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
main_module.main(config["argv"], make_engine, browser=browser)
"""


def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(ready, timeout_s: float = SERVICE_UP_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if value := ready():
            return value
        time.sleep(POLL_S)
    raise AssertionError("condition jamais atteinte")


def get(port: int, path: str = "/api/state") -> http.client.HTTPResponse | None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=SERVICE_UP_S)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response
    except OSError:
        return None
    finally:
        connection.close()


def api_state(port: int) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=SERVICE_UP_S)
    try:
        connection.request("GET", "/api/state")
        return json.loads(connection.getresponse().read())
    finally:
        connection.close()


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class Launch:
    """Un `main()` dans son processus, ses traces dans `out`."""

    def __init__(self, root: Path, env: dict, name: str = "first", **config) -> None:
        self.out = root / name
        self.out.mkdir()
        self.port = config.get("port") or pick_port()
        settings = {
            "out": str(self.out),
            "frozen": True,
            "extraction": str(REPO),
            "argv": ["--port", str(self.port), *config.pop("args", ())],
            "browser": "open",
            "streams": None,
            "fail": False,
            "wait_s": None,
            **{key: value for key, value in config.items() if key != "port"},
        }
        config_path = self.out / "config.json"
        config_path.write_text(json.dumps(settings), encoding="utf-8")
        self.stdout = self.out / "stdout.txt"
        with open(self.stdout, "wb") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-c", DRIVER, str(config_path)],
                cwd=self.out,
                env={
                    **os.environ, **env, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
                stdout=output,
                stderr=subprocess.STDOUT,
            )  # fmt: skip

    @property
    def engines(self) -> list[str]:
        return lines(self.out / "engines.txt")

    @property
    def opened(self) -> list[str]:
        return lines(self.out / "browser.txt")

    @property
    def lock_attempts(self) -> list[str]:
        return lines(self.out / "locks.txt")

    def served_port(self) -> int:
        """Le port que le service annonce, qui n'est pas forcément celui demandé."""
        said = self.stdout.read_text(encoding="utf-8")
        found = re.search(r"interface : http://127\.0\.0\.1:(\d+)/", said)
        return int(found[1]) if found else 0

    def wait_up(self, port: int | None = None) -> None:
        def up() -> bool:
            assert self.process.poll() is None, self.stdout.read_text(encoding="utf-8")
            response = get(self.port if port is None else port)
            return response is not None and response.status == http.client.OK

        wait_for(up)

    def wait_exit(self) -> int:
        return self.process.wait(SUBPROCESS_TIMEOUT_S)

    def stop(self) -> int:
        (self.out / "stop").touch()
        return self.wait_exit()


@pytest.fixture
def launch(tmp_path, user_env):
    """Lance des `main()` et n'en laisse aucun derrière le test."""
    launched: list[Launch] = []

    def start(name: str = "first", **config) -> Launch:
        launched.append(Launch(tmp_path, user_env, name, **config))
        return launched[-1]

    yield start
    for running in launched:
        running.process.kill()
        running.process.wait(SUBPROCESS_TIMEOUT_S)


@pytest.fixture
def log_file(tmp_path):
    relative = LOG_RELATIVE_WINDOWS if sys.platform == "win32" else LOG_RELATIVE
    return tmp_path / "data" / relative


# AC20 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frozen, args, mode",
    [(True, [], "real"), (True, ["--mode", "mock"], "mock"), (False, [], "mock")],
)
def test_ac20_default_mode(launch, frozen, args, mode):
    running = launch(frozen=frozen, args=["--no-browser", *args])

    running.wait_up()

    assert running.engines == [mode]
    assert api_state(running.port)["mode"] == mode
    assert running.stop() == 0


# AC21 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("frozen", [True, False])
@pytest.mark.parametrize("streams", ["none", "cp1252"])
def test_ac21_serves_without_usable_streams(launch, tmp_path, frozen, streams):
    # Le message de détection cite le dossier : il ne s'encode pas en cp1252.
    install = make_install(tmp_path / f"pz {UNENCODABLE}")

    running = launch(
        frozen=frozen,
        streams=streams,
        args=["--no-browser", "--mode", "mock", "--pz-dir", str(install)],
    )

    running.wait_up()
    assert running.stop() == 0


def test_ac21_injected_failure_leaves_a_line_and_an_exit_code(launch, log_file):
    running = launch(fail=True, streams="none", args=["--no-browser"])

    assert running.wait_exit() != 0
    assert "injected startup failure" in log_file.read_text(encoding="utf-8")


def test_ac21_sys_exit_message_reaches_the_log(launch, tmp_path, log_file):
    absent = tmp_path / "no-such-game"

    running = launch(streams="none", args=["--no-browser", "--pz-dir", str(absent)])

    assert running.wait_exit() != 0
    assert running.engines == []
    assert absent.name in log_file.read_text(encoding="utf-8")


def test_ac21_usage_error_reaches_the_log(launch, log_file):
    running = launch(streams="none", args=["--mode", "nonsense"])

    assert running.wait_exit() != 0
    assert "nonsense" in log_file.read_text(encoding="utf-8")


def test_ac21_unusable_log_keeps_the_exit_code(launch, tmp_path):
    (tmp_path / "data").write_text("a file where the data folder should be", encoding="utf-8")

    running = launch(fail=True, streams="none", args=["--no-browser"])

    assert running.wait_exit() != 0


@pytest.mark.parametrize(
    "stream",
    [
        io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict"),
        None,
    ],
    ids=["cp1252", "none"],
)
def test_report_never_raises(monkeypatch, stream):
    monkeypatch.setattr(sys, "stdout", stream)

    main_module.report(f"PZ Duel {UNENCODABLE}")

    if stream is not None:
        assert b"PZ Duel \\u2192\\u2713" in stream.buffer.getvalue()


def test_report_survives_a_closed_stream(monkeypatch):
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stdout", closed)

    main_module.report("PZ Duel")


# AC56 ---------------------------------------------------------------------------------------


def test_ac56_opens_the_interface_once(launch):
    running = launch()

    running.wait_up()
    url = wait_for(lambda: running.opened)

    assert url == [f"http://127.0.0.1:{running.port}/"]
    assert get(running.port, "/").status == http.client.OK
    assert running.stop() == 0
    assert len(running.opened) == 1


def test_ac56_no_browser_never_opens(launch):
    running = launch(args=["--no-browser"])

    running.wait_up()

    assert running.stop() == 0
    assert running.opened == []


@pytest.mark.parametrize("outcome", ["refuse", "raise"])
def test_ac56_failed_opening_keeps_the_service(launch, log_file, outcome):
    running = launch(browser=outcome)

    running.wait_up()
    wait_for(lambda: running.opened)
    url = f"http://127.0.0.1:{running.port}/"

    def told() -> bool:
        return log_file.read_text(encoding="utf-8").count(url) >= 2

    wait_for(told)
    assert get(running.port).status == http.client.OK
    assert running.process.poll() is None
    assert running.stop() == 0


def test_open_interface_reports_the_outcome():
    assert main_module.open_interface("http://u/", lambda url: True) is True
    assert main_module.open_interface("http://u/", lambda url: False) is False


def test_open_interface_uses_the_default_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(main_module.webbrowser, "open", lambda url: opened.append(url) or True)

    assert main_module.open_interface("http://u/") is True
    assert opened == ["http://u/"]


# AC22 ---------------------------------------------------------------------------------------


class ForeignHandler(http.server.BaseHTTPRequestHandler):
    server_header = "Foreign/1.0"
    body = b'{"phase": "idle"}'

    def version_string(self) -> str:
        return self.server_header

    def log_message(self, fmt, *args) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(http.client.OK)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


def foreign_server(server_header: str = ForeignHandler.server_header, body=ForeignHandler.body):
    handler = type("Handler", (ForeignHandler,), {"server_header": server_header, "body": body})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_ac22_second_launch_opens_the_running_service(launch):
    first = launch(args=["--no-browser"])
    first.wait_up()

    second = launch("second", port=first.port)

    assert second.wait_exit() == 0
    assert second.opened == [f"http://127.0.0.1:{first.port}/"]
    assert second.engines == []
    assert get(first.port).status == http.client.OK
    assert first.stop() == 0


def test_ac22_service_is_found_on_a_later_port(launch):
    foreign = foreign_server()
    port = foreign.server_address[1]
    try:
        first = launch(port=port, args=["--no-browser"])
        wait_for(lambda: first.engines)
        second_port = wait_for(lambda: main_module.find_service(port, PROBE_S))

        second = launch("second", port=port)

        assert second.wait_exit() == 0
        assert second.opened == [f"http://127.0.0.1:{second_port}/"]
        assert second.engines == []
        assert first.stop() == 0
    finally:
        foreign.shutdown()
        foreign.server_close()


def test_ac22_service_outside_the_candidates_is_found_by_its_recorded_port(launch):
    first = launch(args=["--port", "0", "--no-browser"])
    served = wait_for(first.served_port)
    first.wait_up(served)

    second = launch("second", port=first.port)

    assert second.wait_exit() == 0
    assert second.opened == [f"http://127.0.0.1:{served}/"]
    assert second.engines == []
    assert first.stop() == 0


@pytest.mark.parametrize("recorded", ["not a port", "", "99999", "-1", "dead"])
def test_stale_recorded_port_leaves_a_normal_start(launch, log_file, recorded):
    path = log_file.parent / main_module.INSTANCE_PORT
    path.parent.mkdir(parents=True)
    path.write_text(str(pick_port()) if recorded == "dead" else recorded, encoding="ascii")

    running = launch(args=["--no-browser"])

    running.wait_up()
    assert running.engines == ["real"]
    assert path.read_text(encoding="ascii") == str(running.port)
    assert running.stop() == 0


def test_unwritable_port_record_keeps_the_service(launch, log_file):
    (log_file.parent / main_module.INSTANCE_PORT).mkdir(parents=True)

    running = launch(args=["--no-browser"])

    running.wait_up()
    assert f"Could not record port {running.port}" in log_file.read_text(encoding="utf-8")
    assert running.stop() == 0


def test_ac22_second_launch_honours_no_browser(launch):
    first = launch(args=["--no-browser"])
    first.wait_up()

    second = launch("second", port=first.port, args=["--no-browser"])

    assert second.wait_exit() == 0
    assert second.opened == []
    assert first.stop() == 0


def test_ac22_foreign_port_leads_to_the_next_one(launch):
    foreign = foreign_server()
    port = foreign.server_address[1]
    try:
        running = launch(port=port)

        url = wait_for(lambda: running.opened)[0]
        served = int(url.rstrip("/").rsplit(":", 1)[1])

        assert port < served < port + main_module.PORT_SPAN
        assert running.engines == ["real"]
        running.wait_up(served)
        assert running.stop() == 0
    finally:
        foreign.shutdown()
        foreign.server_close()


def test_simultaneous_launches_build_one_engine(launch):
    port = pick_port()

    runs = [launch(f"run{index}", port=port) for index in range(3)]

    wait_for(lambda: sum(run.process.poll() == 0 for run in runs) == len(runs) - 1)
    winner = next(run for run in runs if run.process.poll() is None)
    served = wait_for(lambda: winner.opened)
    assert sum(len(run.engines) for run in runs) == 1
    assert winner.engines == ["real"]
    assert all(run.opened == served for run in runs)
    assert winner.stop() == 0


def test_silent_lock_holder_ends_the_loser_with_an_error(launch, log_file):
    lock = platforms.take_instance_lock(log_file.parent / main_module.INSTANCE_LOCK)
    try:
        loser = launch(wait_s=SHORT_WAIT_S, streams="none")

        assert loser.wait_exit() != 0
        assert loser.engines == []
        assert loser.opened == []
        assert "did not answer" in log_file.read_text(encoding="utf-8")
    finally:
        os.close(lock)


def test_lock_released_by_a_dead_winner_goes_to_the_waiting_loser(launch, log_file):
    lock = platforms.take_instance_lock(log_file.parent / main_module.INSTANCE_LOCK)
    loser = launch(args=["--no-browser"])
    wait_for(lambda: loser.lock_attempts)
    assert loser.engines == []

    os.close(lock)

    loser.wait_up()
    assert loser.engines == ["real"]
    assert loser.stop() == 0


def test_lock_is_held_as_long_as_the_service_runs(launch, log_file):
    running = launch(args=["--no-browser"])
    running.wait_up()
    path = log_file.parent / main_module.INSTANCE_LOCK

    assert platforms.take_instance_lock(path) is None

    assert running.stop() == 0
    lock = platforms.take_instance_lock(path)
    assert lock is not None
    os.close(lock)


@pytest.mark.parametrize(
    "server_header, body",
    [
        ("Foreign/1.0", b'{"phase": "idle"}'),
        ("PZduel/0.1", b"<html>not json</html>"),
        ("PZduel/0.1", b"[1, 2]"),
        ("PZduel/0.1", b'{"other": 1}'),
    ],
    ids=["header", "not-json", "not-object", "no-phase"],
)
def test_lookalike_is_not_a_service(server_header, body):
    server = foreign_server(server_header, body)
    try:
        assert not main_module.is_service(server.server_address[1], SUBPROCESS_TIMEOUT_S)
    finally:
        server.shutdown()
        server.server_close()


def test_probe_recognises_the_real_header_and_state():
    server = foreign_server(api.Handler.server_version, b'{"phase": "idle"}')
    try:
        assert main_module.is_service(server.server_address[1], SUBPROCESS_TIMEOUT_S)
    finally:
        server.shutdown()
        server.server_close()


def test_probe_gives_up_on_a_silent_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        started = time.monotonic()

        assert not main_module.is_service(listener.getsockname()[1], PROBE_S)

    assert time.monotonic() - started < SUBPROCESS_TIMEOUT_S


@pytest.mark.parametrize(
    "head",
    [
        b"HTTP/1.0 200 OK\r\n",
        b"HTTP/1.0 200 OK\r\nServer: PZduel/0.1\r\nContent-Length: 1000\r\n\r\n",
    ],
    ids=["head", "body"],
)
def test_probe_deadline_holds_against_a_dripping_listener(head):
    done = threading.Event()

    def drip(listener: socket.socket) -> None:
        peer, _ = listener.accept()
        with peer:
            try:
                peer.sendall(head)
                while not done.wait(PROBE_S / 4):
                    peer.sendall(b"x")
            except OSError:
                pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        dripper = threading.Thread(target=drip, args=(listener,), daemon=True)
        dripper.start()
        started = time.monotonic()
        try:
            assert not main_module.is_service(listener.getsockname()[1], PROBE_S)
            assert time.monotonic() - started < SHORT_WAIT_S
        finally:
            done.set()
            dripper.join(SUBPROCESS_TIMEOUT_S)


def test_probe_past_the_last_port_is_no_service():
    assert not main_module.is_service(1 << 16, PROBE_S)


# Depuis les sources (H3) ----------------------------------------------------------------------


def test_sources_keep_messages_and_write_nothing_to_the_data_dir(launch, tmp_path):
    running = launch(frozen=False, args=["--no-browser"])

    running.wait_up()
    assert running.stop() == 0

    said = running.stdout.read_text(encoding="utf-8")
    assert f"PZ Duel — interface : http://127.0.0.1:{running.port}/" in said
    assert "Arrêt demandé : fermeture des serveurs" in said
    assert not (tmp_path / "data").exists()


def test_sources_start_beside_a_running_service(launch):
    first = launch(frozen=False, args=["--no-browser"])
    first.wait_up()

    second = launch("second", frozen=False, port=first.port, args=["--no-browser"])

    served = wait_for(second.served_port)
    assert served != first.port
    second.wait_up(served)
    assert second.stop() == 0
    assert second.engines == ["mock"]
    assert second.lock_attempts == []
    assert first.stop() == 0


def test_sources_let_a_startup_exception_through(launch, tmp_path):
    running = launch(frozen=False, fail=True, args=["--no-browser"])

    assert running.wait_exit() != 0
    assert "RuntimeError: injected startup failure" in running.stdout.read_text(encoding="utf-8")
    assert not (tmp_path / "data").exists()


# Signaux et arrêt --------------------------------------------------------------------------


def test_stop_signals_follow_the_platform():
    other = signal.SIGBREAK if sys.platform == "win32" else signal.SIGTERM

    assert main_module.STOP_SIGNALS == (signal.SIGINT, other)


class StopSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.done = threading.Event()

    def shutdown(self) -> None:
        self.calls.append("shutdown")
        if len(self.calls) == 2:
            self.done.set()


def test_stop_function_is_idempotent_across_threads_and_signals(monkeypatch):
    handlers = {}
    monkeypatch.setattr(signal, "signal", handlers.__setitem__)
    spy = StopSpy()

    stop = main_module.stop_on_signal(spy, spy)

    threads = [threading.Thread(target=stop) for _ in range(STOPPERS)]
    for thread in threads:
        thread.start()
    for signum, handler in handlers.items():
        handler(signum, None)
    for thread in threads:
        thread.join(SUBPROCESS_TIMEOUT_S)

    assert set(handlers) == set(main_module.STOP_SIGNALS)
    assert spy.done.wait(SUBPROCESS_TIMEOUT_S)
    stop()
    assert spy.calls == ["shutdown", "shutdown"]


# API : le port ne se partage pas ------------------------------------------------------------


class RecordingSocket:
    def __init__(self) -> None:
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def bind(self, address) -> None:
        self.address = address

    def getsockname(self):
        return self.address

    def close(self) -> None:
        return


def test_api_server_never_reuses_an_address():
    assert api.BoundedHTTPServer.allow_reuse_address is False

    server = api.make_server(None)
    try:
        with pytest.raises(OSError):
            api.make_server(None, port=server.server_address[1])
    finally:
        server.server_close()


def test_api_server_is_exclusive_where_the_option_exists(monkeypatch):
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)  # valeur de Winsock
    server = api.BoundedHTTPServer(("127.0.0.1", 0), api.Handler, bind_and_activate=False)
    real, server.socket = server.socket, RecordingSocket()
    real.close()

    server.server_bind()

    assert server.socket.options == [(socket.SOL_SOCKET, -5, 1)]


# Verrou d'instance ---------------------------------------------------------------------------


def test_lock_excludes_a_second_holder_and_ends_with_its_descriptor(tmp_path):
    path = tmp_path / "data" / main_module.INSTANCE_LOCK

    first = platforms.take_instance_lock(path)

    assert first is not None
    assert platforms.take_instance_lock(path) is None
    os.close(first)
    second = platforms.take_instance_lock(path)
    assert second is not None
    os.close(second)


def fake_msvcrt(error: OSError | None = None) -> types.SimpleNamespace:
    calls = []

    def locking(descriptor, mode, size):
        calls.append((mode, size))
        if error is not None:
            raise error

    return types.SimpleNamespace(locking=locking, LK_NBLCK=2, calls=calls)


def test_windows_lock_goes_through_msvcrt_without_waiting(tmp_path):
    module = fake_msvcrt()

    lock = platforms.take_instance_lock(
        tmp_path / "instance.lock", platform=platforms.WINDOWS, module=module
    )

    assert lock is not None
    os.close(lock)
    assert module.calls == [(module.LK_NBLCK, platforms.LOCK_BYTES)]


@pytest.mark.parametrize(
    "error",
    [PermissionError(errno.EACCES, "held"), OSError(0, "Error")],
    ids=["windows", "wine"],
)
def test_windows_held_lock_is_no_error(tmp_path, error):
    lock = platforms.take_instance_lock(
        tmp_path / "instance.lock", platform=platforms.WINDOWS, module=fake_msvcrt(error)
    )

    assert lock is None


def fake_fcntl(error: OSError) -> types.SimpleNamespace:
    def flock(descriptor, operation):
        raise error

    return types.SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4)


def test_posix_held_lock_is_no_error(tmp_path):
    module = fake_fcntl(BlockingIOError(errno.EWOULDBLOCK, "held"))

    lock = platforms.take_instance_lock(
        tmp_path / "instance.lock", platform=platforms.LINUX, module=module
    )

    assert lock is None


def test_posix_other_lock_failure_is_not_taken_for_a_rival(tmp_path):
    module = fake_fcntl(OSError(errno.ENOLCK, "no locks"))

    with pytest.raises(OSError, match="no locks"):
        platforms.take_instance_lock(
            tmp_path / "instance.lock", platform=platforms.LINUX, module=module
        )


# -- Empaquetage et test de fumée (AC46, AC48) -------------------------------------------------

PACKAGING = REPO / "packaging"
SMOKE = PACKAGING / "smoke_test.py"
ENTRY = PACKAGING / "pz_duel_entry.py"
SPEC = PACKAGING / "pz-duel.spec"
REQUIREMENTS = PACKAGING / "requirements-build.txt"
SMOKE_TIMEOUT_S = 180
FAKE_PARENT = 4321

# Le processus qui porte le service, avec ou sans l'attente du parent de `pz_duel_entry.py`.
SIMULATED_SERVICE = """
import os, runpy, sys
sys.frozen = True
sys._MEIPASS = {root!r}
sys.path.insert(0, {repo!r})
with open({pids!r}, "a", encoding="ascii") as pids:
    pids.write(f"{{os.getpid()}}\\n")
{start}
"""
THROUGH_ENTRY = "runpy.run_path({entry!r}, run_name='__main__')"
WITHOUT_ENTRY = "from service.main import main; main()"
# Le chargeur d'un `--onefile` : il relance, attend et transmet le code.
SIMULATED_LOADER = """
import subprocess, sys
child = subprocess.Popen([sys.executable, {service!r}, *sys.argv[1:]])
sys.exit(child.wait() + {code_shift})
"""


def load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def smoke():
    return load_module(SMOKE, "smoke_test_under_test")


@pytest.fixture(scope="module")
def entry():
    return load_module(ENTRY, "pz_duel_entry_under_test")


def read_spec() -> dict:
    """Exécute `pz-duel.spec` contre des doubles : ce que PyInstaller recevrait, sans lui."""
    calls: dict = {}

    def analysis(scripts, **options):
        calls["analysis"] = {"scripts": scripts, **options}
        return types.SimpleNamespace(pure="pure", scripts="scripts", binaries="bin", datas="data")

    def pyz(*args):
        calls["pyz"] = args
        return "archive"

    def exe(*args, **options):
        calls["exe"] = {"args": args, **options}

    source = SPEC.read_text(encoding="utf-8")
    exec(  # noqa: S102 - fichier du dépôt
        compile(source, str(SPEC), "exec"),
        {"SPECPATH": str(PACKAGING), "Analysis": analysis, "PYZ": pyz, "EXE": exe},
    )
    return calls


@pytest.fixture
def bundle(tmp_path):
    """Dossier d'extraction que donneraient les `datas` de la spec, ni plus ni moins."""
    root = tmp_path / "_MEI456"
    for source, destination in read_spec()["analysis"]["datas"]:
        target = root / destination
        if Path(source).is_dir():
            shutil.copytree(source, target)
        else:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)
    return root


def simulated_binary(
    tmp_path: Path,
    root: Path,
    *args: str,
    through_entry: bool = True,
    loader: bool = True,
    code_shift: int = 0,
) -> list[str]:
    """Rend une commande bâtie comme le binaire : un chargeur, puis le service figé."""
    start = THROUGH_ENTRY.format(entry=str(ENTRY)) if through_entry else WITHOUT_ENTRY
    service = tmp_path / "simulated_service.py"
    service.write_text(
        SIMULATED_SERVICE.format(
            root=str(root), repo=str(REPO), pids=str(tmp_path / "pids.txt"), start=start
        ),
        encoding="utf-8",
    )
    if not loader:
        return [sys.executable, str(service), *args]
    script = tmp_path / "simulated_loader.py"
    script.write_text(
        SIMULATED_LOADER.format(service=str(service), code_shift=code_shift), encoding="utf-8"
    )
    return [sys.executable, str(script), *args]


def run_smoke(*command: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SMOKE), *command],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env},
        capture_output=True,
        text=True,
        timeout=SMOKE_TIMEOUT_S,
    )


def assert_none_left(smoke, tmp_path: Path) -> None:
    pids = [int(line) for line in lines(tmp_path / "pids.txt")]
    assert pids
    wait_for(lambda: not set(pids) & set(smoke.processes()))


@pytest.mark.parametrize("loader", [True, False], ids=["two_processes", "one_process"])
def test_ac46_smoke_passes_on_a_complete_folder(smoke, tmp_path, bundle, loader):
    outer = tmp_path / "outer"
    outer.mkdir()

    done = run_smoke(
        *simulated_binary(tmp_path, bundle, loader=loader),
        HOME=str(outer),
        USERPROFILE=str(outer),
        LOCALAPPDATA=str(outer),
        XDG_DATA_HOME=str(outer),
    )

    assert done.returncode == 0, done.stderr
    assert "Smoke test passed" in done.stdout
    assert list(outer.iterdir()) == []
    assert len(lines(tmp_path / "pids.txt")) == 2
    assert_none_left(smoke, tmp_path)


def test_ac46_smoke_fails_without_ui(smoke, tmp_path, bundle):
    shutil.rmtree(bundle / "ui")

    done = run_smoke(*simulated_binary(tmp_path, bundle))

    assert done.returncode == 1
    assert "GET / returned 404" in done.stderr
    assert "Smoke test passed" not in done.stdout
    assert_none_left(smoke, tmp_path)


def drop_ui_script(root: Path) -> None:
    (root / "ui" / "app.js").unlink()


def change_ui_page(root: Path) -> None:
    with open(root / "ui" / "index.html", "a", encoding="utf-8") as page:
        page.write("<!-- stale -->")


def drop_preset(root: Path) -> None:
    (root / "service" / "presets" / "demo.json").unlink()


def change_mod(root: Path) -> None:
    (root / "mods" / "duelcore" / "extra.txt").write_text("stale", encoding="utf-8")


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (drop_ui_script, "GET /app.js returned 404"),
        (change_ui_page, "/ does not return ui/index.html"),
        (drop_preset, "presets listed"),
        (change_mod, "embedded mod fingerprint"),
    ],
    ids=["ui_file", "ui_content", "preset", "mod"],
)
def test_ac46_smoke_fails_on_an_incomplete_folder(tmp_path, bundle, damage, message):
    damage(bundle)

    done = run_smoke(*simulated_binary(tmp_path, bundle))

    assert done.returncode == 1
    assert message in done.stderr


def test_ac46_smoke_fails_outside_real_mode(tmp_path, bundle):
    done = run_smoke(*simulated_binary(tmp_path, bundle, "--mode", "mock"))

    assert done.returncode == 1
    assert "mode 'mock'" in done.stderr


def test_ac46_smoke_fails_on_a_bad_exit_code(tmp_path, bundle):
    done = run_smoke(*simulated_binary(tmp_path, bundle, code_shift=3))

    assert done.returncode == 1
    assert "exit code 3" in done.stderr


def test_smoke_shows_the_log_of_a_binary_that_stops_at_startup(tmp_path, bundle):
    (bundle / "service" / "runtime.json").write_text("{", encoding="utf-8")

    done = run_smoke(*simulated_binary(tmp_path, bundle))

    assert done.returncode == 1
    assert "before answering" in done.stderr
    assert "log: " in done.stderr
    assert "Startup failed" in done.stderr


def test_forced_end_of_the_parent_ends_the_child_through_the_entry(tmp_path, bundle):
    done = run_smoke(*simulated_binary(tmp_path, bundle))

    assert done.returncode == 0, done.stderr
    assert re.search(r"Forced end passed: [1-9]", done.stdout)


def test_forced_end_of_the_parent_catches_a_surviving_child(smoke, tmp_path, bundle):
    """Sans l'attente du parent, l'enfant d'un `--onefile` survit : le passage 2 doit le voir."""
    done = run_smoke(*simulated_binary(tmp_path, bundle, through_entry=False))

    assert done.returncode == 1
    assert "Normal run passed" in done.stdout
    assert "survivors" in done.stderr
    assert_none_left(smoke, tmp_path)


def test_smoke_without_a_command_is_a_usage_error():
    assert run_smoke().returncode == 2


def test_smoke_reports_a_command_that_cannot_start(tmp_path):
    done = run_smoke(str(tmp_path / "absent.exe"))

    assert done.returncode == 1
    assert "could not start" in done.stderr


def test_descendants_cover_grandchildren_and_ignore_the_rest(smoke, monkeypatch):
    table = {
        10: smoke.Process(1, "root"),
        11: smoke.Process(10, "child"),
        12: smoke.Process(11, "grandchild"),
        13: smoke.Process(13, "own parent"),
        20: smoke.Process(1, "other"),
    }
    monkeypatch.setattr(smoke, "processes", lambda: table)

    assert set(smoke.descendants(10)) == {11, 12}


def test_a_reused_pid_is_no_survivor(smoke, monkeypatch):
    monkeypatch.setattr(smoke, "processes", lambda: {11: smoke.Process(1, "notepad")})

    assert smoke.still_alive({11: smoke.Process(10, "pz-duel")}) == {}


def test_entry_imports_main_absolutely():
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]

    assert all(node.level == 0 for node in imports)
    assert "service.main" in {node.module for node in imports}


def test_entry_ends_the_service_after_its_parent(entry, monkeypatch):
    events = []
    monkeypatch.setattr(entry, "log_line", lambda text: events.append("log"))

    entry.end_with_parent(FAKE_PARENT, lambda pid: events.append(("wait", pid)), events.append)

    assert events == [("wait", FAKE_PARENT), "log", entry.FORCED_END_CODE]


def fake_winapi(error: OSError | None = None) -> types.SimpleNamespace:
    calls = []

    def open_process(access, inherit, pid):
        calls.append(("open", access, inherit, pid))
        if error is not None:
            raise error
        return "handle"

    def wait(handle, timeout):
        calls.append(("wait", handle, timeout))

    return types.SimpleNamespace(
        calls=calls,
        SYNCHRONIZE=0x00100000,
        INFINITE=0xFFFFFFFF,
        OpenProcess=open_process,
        WaitForSingleObject=wait,
    )


def test_entry_waits_on_the_windows_handle_of_its_parent(entry, monkeypatch):
    winapi = fake_winapi()
    monkeypatch.setitem(sys.modules, "_winapi", winapi)

    entry.wait_for_parent_windows(FAKE_PARENT)

    assert winapi.calls == [
        ("open", winapi.SYNCHRONIZE, False, FAKE_PARENT),
        ("wait", "handle", winapi.INFINITE),
    ]


def test_entry_takes_an_unopenable_windows_parent_for_gone(entry, monkeypatch):
    winapi = fake_winapi(OSError(errno.EINVAL, "no such process"))
    monkeypatch.setitem(sys.modules, "_winapi", winapi)

    entry.wait_for_parent_windows(FAKE_PARENT)

    assert [call[0] for call in winapi.calls] == ["open"]


def test_entry_polls_for_a_new_posix_parent(entry, monkeypatch):
    parents = iter([FAKE_PARENT, FAKE_PARENT, 1])
    naps = []
    monkeypatch.setattr(entry.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(entry.time, "sleep", naps.append)

    entry.wait_for_parent_posix(FAKE_PARENT)

    assert naps == [entry.PARENT_POLL_S] * 2


class ThreadSpy:
    started: list = []

    def __init__(self, target, args, name, daemon):
        self.record = (target, args, daemon)

    def start(self):
        self.started.append(self.record)


@pytest.fixture
def thread_spy(entry, monkeypatch):
    monkeypatch.setattr(ThreadSpy, "started", [])
    monkeypatch.setattr(entry.threading, "Thread", ThreadSpy)
    return ThreadSpy


def test_entry_does_not_watch_from_the_sources(entry, thread_spy):
    entry.watch_parent()

    assert thread_spy.started == []


@pytest.mark.parametrize(
    ("name", "wait"),
    [("win32", "wait_for_parent_windows"), ("linux", "wait_for_parent_posix")],
)
def test_entry_watches_the_right_way_once_frozen(entry, thread_spy, monkeypatch, name, wait):
    monkeypatch.setattr(entry.paths, "is_frozen", lambda: True)
    monkeypatch.setattr(entry.os, "getppid", lambda: FAKE_PARENT)
    monkeypatch.setattr(sys, "platform", name)

    entry.watch_parent()

    assert thread_spy.started == [
        (entry.end_with_parent, (FAKE_PARENT, getattr(entry, wait), os._exit), True)
    ]


def test_entry_keeps_the_service_when_the_parent_cannot_be_read(entry, thread_spy, monkeypatch):
    logged = []

    def unreadable():
        raise OSError(errno.ENOSYS, "not implemented")

    monkeypatch.setattr(entry.paths, "is_frozen", lambda: True)
    monkeypatch.setattr(entry.os, "getppid", unreadable)
    monkeypatch.setattr(entry, "log_line", logged.append)

    entry.watch_parent()

    assert thread_spy.started == []
    assert len(logged) == 1


def test_spec_builds_a_windowed_onefile_without_upx():
    calls = read_spec()

    assert calls["analysis"]["scripts"] == [str(ENTRY)]
    assert calls["analysis"]["pathex"] == [str(REPO)]
    assert calls["exe"]["args"][:4] == ("archive", "scripts", "bin", "data")
    assert calls["exe"]["console"] is False
    assert calls["exe"]["upx"] is False
    assert not calls["exe"].get("exclude_binaries")


def test_spec_embeds_every_resource_where_the_service_reads_it():
    datas = {
        (Path(source).relative_to(REPO).as_posix(), destination)
        for source, destination in read_spec()["analysis"]["datas"]
    }

    assert datas == {
        ("ui", "ui"),
        ("mods/duelcore", "mods/duelcore"),
        ("service/presets", "service/presets"),
        ("service/runtime.json", "service"),
        ("service/effects.json", "service"),
    }


def test_ac48_pyinstaller_stays_out_of_service():
    files = [
        path
        for path in SERVICE_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]

    assert files
    assert [path for path in files if b"pyinstaller" in path.read_bytes().lower()] == []


def test_ac48_build_requirement_is_pinned():
    entries = [
        line
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert entries == ["pyinstaller==6.22.3"]


@pytest.mark.parametrize("path", [SMOKE, ENTRY], ids=["smoke", "entry"])
def test_ac48_packaging_scripts_need_nothing_but_the_stdlib(path):
    assert foreign_imports(ast.parse(path.read_text(encoding="utf-8"))) == []
