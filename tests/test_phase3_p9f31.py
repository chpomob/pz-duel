"""P9F31 : une release de test roulante depuis `master`, et un binaire qui dit sa version.

`service/build_info.py` relit l'empreinte déposée à la construction (`build_id`), `--version`
l'imprime sans ouvrir de port, `/api/state` la publie sous `build`, et le pied du lobby l'affiche.
Le job `test-build` de `tests.yml` publie au tag fixe `test`, après les deux suites, sur un push
de `master` seulement. Chaque test cite la mutation qui le fait échouer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_ci_workflows import (
    RELEASE_YML,
    SPEC_PATH,
    TEST_BUILD_JOB,
    TESTS_YML,
    ci_problems,
    job_steps,
    parse,
    replaced,
)
from test_modes import FastClock, call

from service import build_info, main as service_main
from service.api import make_server
from service.build_info import build_id, describe, make_stamp, read_stamp
from service.engine import Engine
from service.state import MatchState

REPO = Path(__file__).resolve().parent.parent
UI_SCRIPT = REPO / "ui" / "app.js"
ENTRY = REPO / "packaging" / "pz_duel_entry.py"
SMOKE = REPO / "packaging" / "smoke_test.py"
SHA = "53068e1" + "a" * 33
STAMP = {"id": "master-53068e1", "sha": SHA, "built_at": "2026-11-17T09:30:00Z"}
DEV = {"id": "dev", "sha": None, "built_at": None}
TIMEOUT_S = 60


def frozen_with(monkeypatch, root: Path, stamp: object | None = None, raw: bytes | None = None):
    """Binaire simulé : `sys.frozen`, et `root` comme dossier d'extraction (`_MEIPASS`)."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    target = root / "packaging" / "_build_stamp.json"
    if stamp is not None or raw is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw if raw is not None else json.dumps(stamp).encode("utf-8"))
    return target


# -- build_id ---------------------------------------------------------------------------------


def test_build_id_reads_the_stamp_of_the_binary(monkeypatch, tmp_path):
    """Figé, l'empreinte embarquée est rendue telle quelle. Mutation : `build_id` qui rend
    toujours `dev_build()` (lecture retirée) ; un autre nom de fichier que `_build_stamp.json`."""
    frozen_with(monkeypatch, tmp_path, STAMP)

    assert build_id() == STAMP


def test_build_id_is_dev_from_the_sources_even_beside_a_stamp(monkeypatch, tmp_path):
    """Depuis les sources, `dev`, même si un fichier d'empreinte traîne à la racine. Mutation :
    retirer `if not paths.is_frozen()` rend l'empreinte trouvée."""
    frozen_with(monkeypatch, tmp_path, STAMP)
    monkeypatch.delattr(sys, "frozen")
    monkeypatch.setattr(build_info.paths, "resource_root", lambda: tmp_path)

    assert build_id() == DEV


def test_build_id_is_dev_when_the_binary_has_no_stamp(monkeypatch, tmp_path):
    """Figé sans fichier : `dev`, sans exception. Mutation : ôter le `try` de `read_stamp`."""
    frozen_with(monkeypatch, tmp_path)

    assert build_id() == DEV


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff\xfe\x00garbage",
        b"[]",
        b"null",
        json.dumps({"sha": SHA}).encode(),
        json.dumps({**STAMP, "id": "<img src=x onerror=alert(1)>"}).encode(),
        json.dumps({**STAMP, "id": "x" * 65}).encode(),
        json.dumps({**STAMP, "id": 7}).encode(),
        json.dumps({**STAMP, "sha": "C:\\Users\\alice"}).encode(),
        json.dumps({**STAMP, "built_at": "yesterday"}).encode(),
        b" " * (build_info.STAMP_MAX + 1) + json.dumps(STAMP).encode(),
    ],
    ids=[
        "truncated", "not_utf8", "list", "null", "no_id", "markup_id", "long_id", "id_not_text",
        "path_as_sha", "bad_time", "oversized",
    ],
)
def test_build_id_survives_an_unreadable_stamp(monkeypatch, tmp_path, raw):
    """Illisible ou mal formée, l'empreinte devient `dev` : jamais d'exception, jamais un texte
    arbitraire dans le lobby. Mutations : retirer le `try` ; retirer `SAFE_ID`, `FULL_SHA` ou
    `BUILT_AT` ; ne plus borner la lecture à `STAMP_MAX`."""
    frozen_with(monkeypatch, tmp_path, raw=raw)

    assert build_id() == DEV


def test_build_id_survives_a_stamp_that_is_a_folder(monkeypatch, tmp_path):
    """Un dossier à la place du fichier lève `IsADirectoryError` (ou `PermissionError` sous
    Windows) : `dev`. Mutation : n'attraper que `FileNotFoundError`."""
    frozen_with(monkeypatch, tmp_path).mkdir(parents=True)

    assert build_id() == DEV


def test_make_stamp_names_each_kind_of_build():
    """`master-<sha court>` sur une branche, le nom du tag sur un tag, des caractères sûrs pour
    une pull request, `local` hors de la CI. Mutation : ne plus distinguer le tag ; ne plus
    nettoyer le `/` de `123/merge`."""
    now = datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)
    push = {"GITHUB_SHA": SHA, "GITHUB_REF_NAME": "master", "GITHUB_REF_TYPE": "branch"}
    tag = {"GITHUB_SHA": SHA, "GITHUB_REF_NAME": "v1.0.0", "GITHUB_REF_TYPE": "tag"}
    pull = {"GITHUB_SHA": SHA, "GITHUB_REF_NAME": "123/merge", "GITHUB_REF_TYPE": "branch"}

    assert make_stamp(push, now) == STAMP
    assert make_stamp(tag, now)["id"] == "v1.0.0"
    assert make_stamp(pull, now)["id"] == "123-merge-53068e1"
    assert make_stamp({}, now) == {"id": "local", "sha": None, "built_at": "2026-11-17T09:30:00Z"}


def test_make_stamp_carries_no_secret_and_reads_back(tmp_path):
    """Trois clés publiques, rien de l'environnement d'autre : ni chemin, ni utilisateur, ni
    jeton ; `read_stamp` relit l'empreinte à l'identique. Mutation : ajouter à l'empreinte une
    variable de l'environnement (`RUNNER_TEMP`, `USERNAME`, `GITHUB_TOKEN`)."""
    env = {
        "GITHUB_SHA": SHA,
        "GITHUB_REF_NAME": "master",
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_TOKEN": "ghs_secret-token",
        "USERNAME": "alice",
        "RUNNER_TEMP": "D:\\a\\_temp",
        "HOME": "/home/alice",
    }
    stamp = make_stamp(env)
    path = tmp_path / "_build_stamp.json"
    path.write_text(json.dumps(stamp), encoding="utf-8")
    text = path.read_text(encoding="utf-8")

    assert set(stamp) == {"id", "sha", "built_at"}
    for secret in ("ghs_", "alice", "_temp", "/home"):
        assert secret not in text
    assert read_stamp(path) == stamp


# -- --version --------------------------------------------------------------------------------


def no_port(*args, **kwargs):
    raise AssertionError("--version opened a port or the engine")


def test_version_prints_the_identity_without_a_port(monkeypatch, tmp_path, capsys):
    """Figé, `--version` imprime `pz-duel master-53068e1 (commit …, built …)` et rend la main :
    ni verrou d'instance, ni moteur, ni port. Mutation : retirer la branche `if args.version`
    (le service démarre) ; la placer après `claim_instance`."""
    frozen_with(monkeypatch, tmp_path, STAMP)
    monkeypatch.setattr(service_main, "claim_instance", no_port)
    monkeypatch.setattr(service_main, "free_port", no_port)
    monkeypatch.setattr(service_main, "make_server", no_port)

    service_main.main(["--version"], make_engine=no_port, browser=no_port)

    assert capsys.readouterr().out == (
        f"pz-duel master-53068e1 (commit {SHA}, built 2026-11-17T09:30:00Z)\n"
    )
    assert describe(DEV) == "pz-duel dev"


# Le binaire : `pz_duel_entry.py` figé, un socket qui refuse tout `bind`.
VERSION_SERVICE = """
import runpy, socket, sys
sys.frozen = True
sys._MEIPASS = {root!r}
sys.path.insert(0, {repo!r})
def refuse(*args):
    raise SystemExit("--version opened a port")
socket.socket.bind = refuse
socket.socket.connect = refuse
sys.argv = ["pz-duel.exe", "--version"]
runpy.run_path({entry!r}, run_name="__main__")
"""


def test_version_of_the_binary_exits_zero_through_the_entry(tmp_path):
    """Par le point d'entrée du binaire : code 0, une ligne, aucun `bind` ni `connect` (la sonde
    d'instance en ferait un). Mutation : retirer la branche `--version` de `pz_duel_entry.py`
    ou de `service/main.py` (la sonde se connecte, le service se lie)."""
    root = tmp_path / "_MEI1"
    target = root / "packaging" / "_build_stamp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(STAMP), encoding="utf-8")
    script = VERSION_SERVICE.format(root=str(root), repo=str(REPO), entry=str(ENTRY))
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path), "XDG_DATA_HOME": str(tmp_path)}

    done = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=TIMEOUT_S,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [describe(STAMP)]


def load_entry():
    """Le point d'entrée comme module, sans exécuter sa branche `__main__`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pz_duel_entry_under_test", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_console_falls_back_to_the_shell_behind_the_onefile_loader():
    """En `--onefile` fenêtré, le parent est le chargeur, sans console : `AttachConsole` doit
    alors viser son parent à lui, le `cmd` du testeur. Mutation : ne garder que
    `ATTACH_PARENT_PROCESS` (rien ne s'affiche depuis `cmd`)."""
    entry = load_entry()
    LOADER, SHELL = 4100, 4000
    tried = []

    def attach(pid):
        tried.append(pid)
        return pid == SHELL

    owners = entry.console_owners(lambda: LOADER, {LOADER: SHELL}.get)

    assert entry.attach_first_console(owners, attach) is True
    assert tried == [entry.ATTACH_PARENT_PROCESS, SHELL]


def test_version_console_stays_with_a_parent_that_has_one():
    """Le parent a une console (binaire non `--onefile`) : on s'y tient, et le grand-parent
    n'est même pas cherché. Mutation : chercher le grand-parent d'avance."""
    entry = load_entry()

    def no_lookup(pid):
        raise AssertionError("grandparent looked up although the parent console was taken")

    owners = entry.console_owners(lambda: 4100, no_lookup)

    assert entry.attach_first_console(owners, lambda pid: True) is True


@pytest.mark.parametrize("lookup", [lambda pid: None, lambda pid: (_ for _ in ()).throw(OSError)])
def test_version_console_gives_up_quietly_without_a_grandparent(lookup):
    """Ni parent ni grand-parent lisible : `False`, pas d'exception (le log reste témoin)."""
    entry = load_entry()
    tried = []

    def attach(pid):
        tried.append(pid)
        return False

    assert entry.attach_first_console(entry.console_owners(lambda: 4100, lookup), attach) is False
    assert tried == [entry.ATTACH_PARENT_PROCESS]


if sys.platform == "win32":

    def test_parent_pid_windows_reads_the_real_parent():
        """L'instantané Toolhelp des processus rend le même parent que `os.getppid`."""
        entry = load_entry()

        assert entry.parent_pid_windows(os.getpid()) == os.getppid()


def test_version_from_the_sources_says_dev():
    """`python -m service.main --version` : `pz-duel dev`, code 0. La valeur neutre."""
    done = subprocess.run(
        [sys.executable, "-m", "service.main", "--version"],
        cwd=REPO,
        capture_output=True,
        encoding="utf-8",
        timeout=TIMEOUT_S,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == "pz-duel dev\n"


# -- /api/state et le lobby -------------------------------------------------------------------


def test_state_carries_the_build_of_the_binary(monkeypatch, tmp_path):
    """L'état d'un binaire figé porte son empreinte. Mutation : retirer `"build"` de `to_dict`."""
    frozen_with(monkeypatch, tmp_path, STAMP)

    assert MatchState().to_dict()["build"] == STAMP


def test_api_state_publishes_build(runtime):
    """`GET /api/state` publie `build` ; depuis les sources, `dev`. Mutations : retirer `"build"`
    de `MatchState.to_dict` ; un `snapshot` du moteur qui le filtre."""
    engine = Engine("mock", runtime=runtime, clock=FastClock())
    server = make_server(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, state = call(f"http://127.0.0.1:{server.server_address[1]}/api/state")
    finally:
        server.shutdown()
        server.server_close()
        engine.shutdown()

    assert status == 200
    assert state["build"] == DEV


def build_foot_script() -> str:
    script = UI_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = script.index("function buildFoot(build) {")
    return script[start : script.index("\n}\n", start) + 3]


def test_lobby_foot_shows_the_build():
    """Le pied du lobby porte `version <id>`, échappé. Mutations : ne plus appeler `buildFoot`
    dans le lobby de `renderLive` ; afficher autre chose que `build.id` ; retirer `esc`."""
    script = UI_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    live = script[script.index("function renderLive() {") :]
    lobby = live[: live.index("renderQr();")]
    assert "${buildFoot(S.build)}" in lobby
    block = build_foot_script()
    assert "${esc(id)}" in block

    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        return
    harness = (
        "const esc = (s) => String(s ?? '').replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
        + block
        + f"\nconsole.log(JSON.stringify([buildFoot({json.dumps(STAMP)}),"
        + " buildFoot({id: '<b>'}), buildFoot(undefined)]));\n"
    )
    done = subprocess.run(
        [node, "-e", harness], capture_output=True, encoding="utf-8", timeout=30, check=True
    )
    shown, hostile, missing = json.loads(done.stdout)
    assert ">version master-53068e1</p>" in shown
    assert "<b>" not in hostile and "&lt;b&gt;" in hostile
    assert missing == ""


# -- test de fumée ----------------------------------------------------------------------------


def test_smoke_rejects_a_state_whose_build_differs_from_version():
    """Le test de fumée compare `--version` et `/api/state`. Mutation : `check_build` retiré de
    `run`, ou qui ne compare plus `build.id`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("smoke_p9f31", SMOKE)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    session = types.SimpleNamespace(get_json=lambda path: {"build": {"id": "dev"}})

    with pytest.raises(smoke.SmokeFailure, match="master-53068e1"):
        smoke.check_build(session, "master-53068e1")
    smoke.check_build(session, "dev")
    source = SMOKE.read_text(encoding="utf-8")
    run = source[source.index("def run(command") :]
    assert "build = check_version(command)" in run
    assert "check_build(session, build)" in run


# -- le garde-fou du workflow -----------------------------------------------------------------

TEST_BUILD = f"  {TEST_BUILD_JOB}:\n"
CONDITION_LINE = "    if: github.ref == 'refs/heads/master' && github.event_name == 'push'\n"
NEEDS_LINE = "    needs: [pytest, pytest-windows]\n"
PUBLISH_NAME = "      - name: Publish\n"


def in_test_build(old: str, new: str):
    def mutate(text: str) -> str:
        head, tail = text.split(TEST_BUILD)
        assert old in tail
        return head + TEST_BUILD + tail.replace(old, new, 1)

    return mutate


TEST_BUILD_MUTANTS = {
    "no_condition": in_test_build(CONDITION_LINE, ""),
    "condition_commented_out": in_test_build(CONDITION_LINE, "    # " + CONDITION_LINE.lstrip()),
    "any_branch": in_test_build("github.ref == 'refs/heads/master' && ", ""),
    "another_branch": in_test_build("refs/heads/master", "refs/heads/dev"),
    "pull_requests_too": in_test_build(" && github.event_name == 'push'", ""),
    "or_instead_of_and": in_test_build(" && ", " || "),
    "pull_request_event": in_test_build("== 'push'", "== 'pull_request'"),
    "no_needs": in_test_build(NEEDS_LINE, ""),
    "needs_commented_out": in_test_build(NEEDS_LINE, "    # " + NEEDS_LINE.lstrip()),
    "needs_linux_only": in_test_build("[pytest, pytest-windows]", "[pytest]"),
    "needs_windows_only": in_test_build("[pytest, pytest-windows]", "[pytest-windows]"),
    "publish_always": in_test_build(PUBLISH_NAME, PUBLISH_NAME + "        if: always()\n"),
    "smoke_skipped": in_test_build(
        '        run: python packaging/smoke_test.py "$RUNNER_TEMP/dist/pz-duel.exe"\n',
        '        run: echo skipped\n',
    ),
    "another_tag": in_test_build("gh release upload test ", "gh release upload nightly "),
    "no_clobber": in_test_build(" --clobber", ""),
    "never_edited": in_test_build("gh release edit test ", "echo "),
    "not_prerelease": in_test_build('--notes "$notes" --prerelease', '--notes "$notes"'),
    "notes_without_subject": in_test_build('"$subject" "${built_at%)}"', '"${built_at%)}"'),
    "tag_left_behind": in_test_build('git push --force origin "$GITHUB_SHA:refs/tags/test"', ""),
    "job_reads_only": in_test_build("      contents: write\n", "      contents: read\n"),
    "workflow_writes": replaced("permissions:\n  contents: read\n", "permissions: write-all\n"),
    "job_removed": lambda text: text[: text.index("  # P9F31")],
}


def test_test_build_job_publishes_only_after_both_suites_on_master():
    assert ci_problems(TESTS_YML.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("mutant", TEST_BUILD_MUTANTS, ids=str)
def test_test_build_guard_sees_the_drift(mutant):
    """Chaque mutant retire une garantie du job : le garde-fou doit le voir. Mutation du
    garde-fou : `rolling_build_problems` qui ne lit plus `needs` ou la condition `master`."""
    text = TESTS_YML.read_text(encoding="utf-8")
    mutated = TEST_BUILD_MUTANTS[mutant](text)

    assert mutated != text
    assert ci_problems(mutated) != []


def test_test_build_builds_exactly_like_the_release():
    """Même chaîne que `release.yml` et que la construction des pull requests : seules les
    étapes d'empreinte et de publication diffèrent. Mutation : une autre spec, un autre Python,
    une étape de plus avant le test de fumée."""

    def chain(path: Path, name: str | None = None) -> list[str]:
        _, jobs = parse(path.read_text(encoding="utf-8"))
        if name is None:
            name = next(
                job for job, lines in jobs.items()
                if job != TEST_BUILD_JOB and any(SPEC_PATH in line for line in lines)
            )
        return [
            step for step in job_steps(jobs[name])
            if "gh release" not in step and "sha256sum" not in step
        ]

    assert chain(TESTS_YML, TEST_BUILD_JOB) == chain(RELEASE_YML) == chain(TESTS_YML)
