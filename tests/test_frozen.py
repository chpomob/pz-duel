"""R4 : résolution des ressources et des données en mode figé simulé (AC18, AC19).

`service/paths.py` se résout à l'import : chaque cas tourne dans un sous-processus qui pose
`sys.frozen` et `sys._MEIPASS` avant le premier import de `service` (D-D). Le dossier
d'extraction est une copie marquée : une lecture qui passerait par le dépôt se verrait.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
