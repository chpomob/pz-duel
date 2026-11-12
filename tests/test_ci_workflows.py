"""Workflows de CI et de release (R8), par lecture textuelle ; `yaml` vérifie seulement qu'ils se chargent.

Chaque exigence est une entrée de `ci_problems` ou de `release_problems`. Les mutants
ci-dessous prouvent que ces lectures voient le défaut, y compris une étape seulement commentée.

La documentation (R9) se lit de même : `start_problems` pour la section de démarrage des README,
`limits_problems` pour `limites-connues`, chaque preuve citée y désignant un nom qui existe.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
TESTS_YML = WORKFLOWS / "tests.yml"
RELEASE_YML = WORKFLOWS / "release.yml"
BUILD_SPEC = REPO / "packaging" / "pz-duel.spec"
RUNTIME = REPO / "service" / "runtime.json"
LIMITS_DIR = REPO / "docs" / "campaigns" / "phase2"

SUITE_PYTHONS = ["3.12", "3.13", "3.14"]
BUILD_PYTHON = "3.13"
REQUIREMENTS = "packaging/requirements-build.txt"
# Sans `--require-hashes`, un paquet substitué entrerait dans un binaire que `.sha256` attesterait.
REQUIREMENTS_INSTALL = f"pip install --require-hashes --only-binary :all: -r {REQUIREMENTS}"
PINNED = re.compile(r"^([A-Za-z0-9][\w.-]*)==([\w.]+)(?: ; [^\\]+)? \\$")
HASH = re.compile(r"^    --hash=sha256:[0-9a-f]{64}( \\)?$")
SPEC_PATH = "packaging/pz-duel.spec"
SMOKE_PATH = "packaging/smoke_test.py"
# Les suites dont dépend la publication : ce sont celles de `tests.yml`, à l'identique.
RELEASE_SUITES = ["pytest", "pytest-windows"]
RELEASE_JOB = "release"

STEP_INDENT = 6
KEY = re.compile(r"([\w-]+):")
CONDITION = re.compile(r"^\s*(?:- )?if:", re.MULTILINE)
SWALLOWED = re.compile(r"continue-on-error|\|\|\s*(?:true|:|exit 0)|set \+e")
# `\binstaller\b` et non `installer` : `PyInstaller` construit le binaire, il n'installe rien.
# Une action ne s'épingle qu'à un commit complet : un tag (`@v4`) ou un préfixe se déplacent.
PINNED_ACTION = re.compile(r"uses: [\w.-]+/[\w./-]+@[0-9a-f]{40}")
FORBIDDEN = re.compile(
    r"signtool|osslsigncode|codesign|cosign|\bgpg\b"
    r"|\binstaller\b|\bnsis\b|makensis|inno ?setup|\biscc\b|\bwix\b|\.msi\b"
    r"|workshop|steamcmd",
    re.IGNORECASE,
)


def code_lines(text: str) -> list[str]:
    """Les lignes utiles : une étape commentée n'existe pas."""
    lines = []
    for line in text.splitlines():
        kept = re.sub(r"(^|\s+)#.*$", "", line).rstrip()
        if kept:
            lines.append(kept)
    return lines


def depth(line: str) -> int:
    return len(line) - len(line.lstrip())


def blocks(lines: list[str], indent: int) -> dict[str, list[str]]:
    """Les clés de mapping à cette indentation, chacune avec ses lignes, la sienne en tête."""
    found: dict[str, list[str]] = {}
    current = None
    for line in lines:
        match = KEY.match(line.strip()) if depth(line) == indent else None
        if match:
            current = found.setdefault(match[1], [])
        if current is not None:
            current.append(line)
    return found


def parse(text: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    top = blocks(code_lines(text), 0)
    return top, blocks(top.get("jobs", [])[1:], 2)


def job_steps(job: list[str]) -> list[str]:
    body = blocks(job[1:], 4).get("steps", [])[1:]
    steps: list[list[str]] = []
    for line in body:
        if depth(line) == STEP_INDENT and line.lstrip().startswith("- "):
            steps.append([])
        if steps:
            steps[-1].append(line)
    return ["\n".join(step).replace("\\\n", " ") for step in steps]


def job_value(job: list[str], key: str) -> str | None:
    lines = blocks(job[1:], 4).get(key)
    return lines[0].split(":", 1)[1].strip() if lines else None


def first_step(steps: list[str], needle: str) -> int | None:
    return next((index for index, step in enumerate(steps) if needle in step), None)


def in_order(indexes: list[int | None]) -> bool:
    return None not in indexes and indexes == sorted(set(indexes))


def matrix_pythons(job: list[str]) -> list[str]:
    line = next((line for line in job if line.strip().startswith("python:")), "")
    return re.findall(r"3\.\d+", line)


def pin_problems(workflow: str, text: str) -> list[str]:
    """Chaque `uses:` est épinglé à 40 caractères hexadécimaux, sinon c'est un défaut."""
    return [
        f"{workflow}: action not pinned to a full commit: {line.strip(' -')}"
        for line in code_lines(text)
        if "uses:" in line and not PINNED_ACTION.fullmatch(line.strip(" -"))
    ]


def checkout_problems(name: str, steps: list[str], windows: bool) -> list[str]:
    problems = []
    checkout = first_step(steps, "actions/checkout@")
    if checkout is None:
        return [f"{name}: no checkout"]
    if "fetch-depth: 0" not in steps[checkout]:
        problems.append(f"{name}: checkout without fetch-depth: 0")
    if windows:
        autocrlf = first_step(steps, "git config --global core.autocrlf false")
        if not in_order([autocrlf, checkout]):
            problems.append(f"{name}: autocrlf is not disabled before the checkout")
    return problems


def build_problems(name: str, job: list[str], exe: str) -> tuple[list[str], list[str]]:
    """La chaîne commune à la CI et à la release : les problèmes et les étapes du job."""
    steps = job_steps(job)
    problems = checkout_problems(name, steps, windows=True)
    if job_value(job, "runs-on") != "windows-latest":
        problems.append(f"{name}: not on windows-latest")
    python = first_step(steps, "actions/setup-python@")
    if python is None or f'python-version: "{BUILD_PYTHON}"' not in steps[python]:
        problems.append(f"{name}: not built with Python {BUILD_PYTHON}")
    chain = [
        python,
        first_step(steps, REQUIREMENTS_INSTALL),
        first_step(steps, f"PyInstaller {SPEC_PATH}"),
        first_step(steps, f"{SMOKE_PATH} "),
    ]
    if not in_order(chain):
        problems.append(f"{name}: requirements, build and smoke test are missing or out of order")
    elif exe not in steps[chain[-1]]:
        problems.append(f"{name}: the smoke test does not run {exe}")
    body = "\n".join(job)
    if CONDITION.search(body):
        problems.append(f"{name}: a condition can bypass the chain")
    if SWALLOWED.search(body):
        problems.append(f"{name}: a failure can be swallowed")
    return problems, steps


def exe_name() -> str:
    return re.search(r'name="([^"]+)"', BUILD_SPEC.read_text(encoding="utf-8"))[1] + ".exe"


def ci_problems(text: str) -> list[str]:
    top, jobs = parse(text)
    problems = pin_problems("tests", text)
    if "pull_request" not in blocks(top.get("on", [])[1:], 2):
        problems.append("tests: no pull_request trigger")

    suites = {name: job for name, job in jobs.items() if any("pytest" in line for line in job)}
    for runner in ("ubuntu-latest", "windows-latest"):
        matching = [
            (name, job) for name, job in suites.items() if job_value(job, "runs-on") == runner
        ]
        if len(matching) != 1:
            problems.append(f"tests: expected one pytest job on {runner}")
            continue
        name, job = matching[0]
        if matrix_pythons(job) != SUITE_PYTHONS:
            problems.append(f"{name}: Python matrix is not {SUITE_PYTHONS}")
        steps = job_steps(job)
        if first_step(steps, "python -m pytest") is None:
            problems.append(f"{name}: pytest is not run")
        problems += checkout_problems(name, steps, windows=runner == "windows-latest")

    builders = [
        (name, job) for name, job in jobs.items() if any(SPEC_PATH in line for line in job)
    ]
    if len(builders) != 1:
        return problems + ["tests: expected one build job"]
    problems += build_problems(builders[0][0], builders[0][1], exe_name())[0]
    return problems


def publish_problems(step: str, exe: str) -> list[str]:
    script = step.split("run:", 1)[-1]
    problems = []
    if "shell: bash" not in step:
        problems.append("publish: not in bash")
    if "GH_TOKEN: ${{ github.token }}" not in step:
        problems.append("publish: no GH_TOKEN")
    if "${{" in script:
        problems.append("publish: expression expanded inside the script")

    view = script.find('if ! gh release view "$GITHUB_REF_NAME"')
    create = script.find('gh release create "$GITHUB_REF_NAME"')
    close = re.compile(r"^\s*fi$", re.MULTILINE).search(script, max(create, 0))
    upload = script.find('gh release upload "$GITHUB_REF_NAME"')
    if min(view, create, upload) < 0 or close is None:
        return problems + ["publish: gh release view, create or upload is missing"]
    if not view < create < close.start() < upload:
        problems.append("publish: create must be guarded by view and precede the upload")

    create_line = script[create : close.start()]
    for option in ("--verify-tag", '--title "$GITHUB_REF_NAME"', "--notes ", "${prerelease[@]}"):
        if option not in create_line:
            problems.append(f"publish: create without {option}")
    if not re.search(r"\*-\*\) prerelease=\(--prerelease\)", script):
        problems.append("publish: a tag with a dash is not a prerelease")

    upload_line = script[upload:]
    for option in (f'{exe}"', f'{exe}.sha256"', "--clobber"):
        if option not in upload_line:
            problems.append(f"publish: upload without {option}")
    return problems


def uses_lines(text: str) -> set[str]:
    return {line.strip(" -") for line in code_lines(text) if "uses:" in line}


def gate_problems(text: str, jobs: dict[str, list[str]]) -> list[str]:
    """Un tag ne publie qu'après les suites des pull requests, sous Linux et sous Windows."""
    problems = []
    reference_text = TESTS_YML.read_text(encoding="utf-8")
    reference = parse(reference_text)[1]
    for suite in RELEASE_SUITES:
        if jobs[suite] != reference[suite]:
            problems.append(f"release: {suite} differs from tests.yml")
    needs = job_value(jobs[RELEASE_JOB], "needs") or ""
    if sorted(re.findall(r"[\w-]+", needs)) != RELEASE_SUITES:
        problems.append(f"release: the publication does not need {RELEASE_SUITES}")
    # Une action épinglée d'un seul côté le serait à moitié : les deux fichiers vont ensemble.
    if uses_lines(text) != uses_lines(reference_text):
        problems.append("release: actions are not at the same revisions as tests.yml")
    return problems


def release_problems(text: str) -> list[str]:
    top, jobs = parse(text)
    exe = exe_name()
    problems = pin_problems("release", text)

    trigger = blocks(top.get("on", [])[1:], 2)
    tags = [line for line in trigger.get("push", []) if line.strip().startswith("tags:")]
    if list(trigger) != ["push"] or len(trigger["push"]) != 2 or not tags:
        problems.append("release: the only trigger must be a tag push")
    elif re.findall(r'"([^"]+)"', tags[0]) != ["v*"]:
        problems.append("release: tags are not v*")

    # Lecture seule pour tout le workflow : `contents: write` n'appartient qu'au job qui publie.
    if [line.strip() for line in top.get("permissions", [])] != ["permissions:", "contents: read"]:
        problems.append("release: workflow permissions are not exactly contents: read")
    if any("write" in line for line in top.get("permissions", [])):
        problems.append("release: write permissions belong to the publishing job")
    # Un seul job publie, et ses étapes ordonnent la publication : le reste n'est que sa porte.
    if sorted(jobs) != sorted([RELEASE_JOB, *RELEASE_SUITES]):
        return problems + ["release: expected the publishing job and the two test suites only"]
    problems += gate_problems(text, jobs)
    name, job = RELEASE_JOB, jobs[RELEASE_JOB]
    if [line.strip() for line in blocks(job[1:], 4).get("permissions", [])] != [
        "permissions:",
        "contents: write",
    ]:
        problems.append(f"{name}: permissions are not exactly contents: write")

    chain_problems, steps = build_problems(name, job, exe)
    problems += chain_problems
    if FORBIDDEN.search("\n".join(code_lines(text))):
        problems.append("release: signing, installer or Workshop step")

    smoke = first_step(steps, f"{SMOKE_PATH} ")
    checksum = first_step(steps, f"sha256sum {exe} > {exe}.sha256")
    publish = first_step(steps, "gh release")
    if not in_order([smoke, checksum, publish]):
        return problems + ["release: expected smoke test, then checksum, then publication"]
    if publish != len(steps) - 1:
        problems.append("release: the publication must be the last step")
    if sum("gh release" in step for step in steps) != 1:
        problems.append("release: gh release is used outside the publication")
    return problems + publish_problems(steps[publish], exe)


def swap_steps(text: str, first: str, second: str) -> str:
    """Échange 2 étapes nommées, dans l'ordre où le fichier les donne."""
    pattern = r"(      - name: {}\n.*?)(?=^      (?:- |# )|\Z)"
    one = re.search(pattern.format(first), text, re.DOTALL | re.MULTILINE)
    two = re.search(pattern.format(second), text, re.DOTALL | re.MULTILINE)
    return (
        text[: one.start()]
        + two[1]
        + text[one.end() : two.start()]
        + one[1]
        + text[two.end() :]
    )


def replaced(old: str, new: str, count: int = 1):
    def mutate(text: str) -> str:
        assert old in text
        return text.replace(old, new, count)

    return mutate


SMOKE_RUN = '        run: python packaging/smoke_test.py "$RUNNER_TEMP/dist/pz-duel.exe"\n'
PUBLISH_NAME = "      - name: Publish\n"
AUTOCRLF_STEP = "      - run: git config --global core.autocrlf false\n"
CHECKOUT_PIN = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
SETUP_PYTHON_PIN = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
CHECKOUT_STEP = f"      - uses: {CHECKOUT_PIN} # v4.4.0\n        with:\n          fetch-depth: 0\n"
WORKFLOW_READ = "permissions:\n  contents: read\n"
BUILD_JOB = "  build-windows:\n"


def in_build_job(old: str, new: str):
    def mutate(text: str) -> str:
        head, tail = text.split(BUILD_JOB)
        assert old in tail
        return head + BUILD_JOB + tail.replace(old, new, 1)

    return mutate


TESTS_MUTANTS = {
    "no_pull_request": replaced("  pull_request:\n", ""),
    "suite_on_linux_loses_a_version": replaced('["3.12", "3.13", "3.14"]', '["3.12", "3.13"]'),
    "suite_on_windows_loses_a_version": lambda text: text[::-1]
    .replace('["3.12", "3.13", "3.14"]'[::-1], '["3.13", "3.14"]'[::-1], 1)[::-1],
    "linux_job_removed": replaced("runs-on: ubuntu-latest", "runs-on: macos-latest"),
    "windows_suite_shallow": replaced("          fetch-depth: 0\n", "", 2),
    "build_shallow": in_build_job("          fetch-depth: 0\n", "          fetch-depth: 1\n"),
    "build_keeps_autocrlf": in_build_job(AUTOCRLF_STEP, ""),
    "build_autocrlf_after_checkout": in_build_job(
        AUTOCRLF_STEP + CHECKOUT_STEP, CHECKOUT_STEP + AUTOCRLF_STEP
    ),
    "build_without_requirements": in_build_job(
        "-r packaging/requirements-build.txt", "pyinstaller"
    ),
    "build_without_hashes": in_build_job("--require-hashes ", ""),
    "build_from_sources": in_build_job("--only-binary :all: ", ""),
    "build_on_another_python": in_build_job('"3.13"', '"3.14"'),
    "smoke_commented_out": in_build_job(SMOKE_RUN, "        # " + SMOKE_RUN.lstrip()),
    "smoke_on_another_file": in_build_job("pz-duel.exe", "other.exe"),
    "smoke_may_fail": in_build_job(SMOKE_RUN, SMOKE_RUN + "        continue-on-error: true\n"),
    "build_on_linux": in_build_job("windows-latest", "ubuntu-latest"),
    # A2 : la règle d'épinglage vaut pour `tests.yml` aussi, toutes actions comprises.
    "action_on_a_moving_tag": replaced(CHECKOUT_PIN, "actions/checkout@v4"),
    "action_on_a_short_sha": replaced(SETUP_PYTHON_PIN, SETUP_PYTHON_PIN[:-33]),
    "action_on_a_branch": in_build_job(CHECKOUT_PIN, "actions/checkout@main"),
    "action_in_upper_case": replaced(
        CHECKOUT_PIN, "actions/checkout@" + CHECKOUT_PIN[-40:].upper()
    ),
}

RELEASE_MUTANTS = {
    "tag_filter_widened": replaced('tags: ["v*"]', 'tags: ["*"]'),
    "also_on_branches": replaced("    tags:", '    branches: ["master"]\n    tags:'),
    "manual_trigger": replaced("  push:\n", "  workflow_dispatch:\n  push:\n"),
    "write_for_the_whole_workflow": replaced(
        "jobs:\n", "permissions:\n  contents: write\n\njobs:\n"
    ),
    "no_write_permission": replaced("      contents: write\n", "      contents: read\n"),
    "keeps_autocrlf": replaced(AUTOCRLF_STEP, ""),
    "shallow": replaced("          fetch-depth: 0\n", ""),
    "another_python": replaced('"3.13"', '"3.12"'),
    "smoke_commented_out": replaced(SMOKE_RUN, "        # " + SMOKE_RUN.lstrip()),
    "smoke_may_fail": replaced(SMOKE_RUN, SMOKE_RUN + "        continue-on-error: true\n"),
    "smoke_failure_swallowed": replaced('pz-duel.exe"\n      #', 'pz-duel.exe" || true\n      #'),
    "publish_always": replaced(PUBLISH_NAME, PUBLISH_NAME + "        if: always()\n"),
    "publish_on_failure": replaced(
        PUBLISH_NAME, PUBLISH_NAME + "        if: failure() || success()\n"
    ),
    "checksum_before_smoke": lambda text: swap_steps(text, "Smoke test", "Checksum"),
    "publish_before_checksum": lambda text: swap_steps(text, "Checksum", "Publish"),
    "publish_in_powershell": lambda text: text[::-1]
    .replace("shell: bash"[::-1], "shell: pwsh"[::-1], 1)[::-1],
    "no_token": replaced("GH_TOKEN: ${{ github.token }}", "OTHER: ${{ github.token }}"),
    "tag_expanded_in_script": replaced(
        'gh release view "$GITHUB_REF_NAME"', 'gh release view "${{ github.ref_name }}"'
    ),
    "no_verify_tag": replaced(" --verify-tag", ""),
    "no_clobber": replaced(" --clobber", ""),
    "no_prerelease": replaced("*-*) prerelease=(--prerelease) ;;", "*-*) prerelease=() ;;"),
    "prerelease_unused": replaced('              "${prerelease[@]}"\n', "              --latest\n"),
    "create_unguarded": replaced(
        'if ! gh release view "$GITHUB_REF_NAME" > /dev/null; then', "if true; then"
    ),
    "create_commented_out": replaced("  gh release create", "  # gh release create"),
    "upload_commented_out": replaced("  gh release upload", "  # gh release upload"),
    "upload_before_create": replaced(
        "          if ! gh release view",
        '          gh release upload "$GITHUB_REF_NAME" x --clobber\n'
        "          if ! gh release view",
    ),
    "upload_without_checksum": replaced('"$RUNNER_TEMP/dist/pz-duel.exe.sha256" ', ""),
    "publish_without_tests": replaced("    needs: [pytest, pytest-windows]\n", ""),
    "publish_without_linux_tests": replaced("needs: [pytest, pytest-windows]", "needs: [pytest-windows]"),
    "publish_without_windows_tests": replaced("needs: [pytest, pytest-windows]", "needs: [pytest]"),
    "publish_needs_commented_out": replaced("    needs: [", "    # needs: ["),
    "publish_despite_failed_tests": replaced(
        "    needs: [pytest, pytest-windows]\n",
        "    needs: [pytest, pytest-windows]\n    if: always()\n",
    ),
    "linux_tests_removed": lambda text: text[: text.index("  pytest:\n")]
    + text[text.index("  pytest-windows:\n") :],
    "windows_tests_removed": lambda text: text[: text.index("  pytest-windows:\n")],
    "tests_on_one_system_only": replaced("runs-on: ubuntu-latest", "runs-on: windows-latest"),
    "tests_lose_a_version": replaced('["3.12", "3.13", "3.14"]', '["3.13"]'),
    "tests_may_fail": replaced(
        "      - run: python -m pytest -q\n",
        "      - run: python -m pytest -q\n        continue-on-error: true\n",
    ),
    "tests_swallowed": replaced("python -m pytest -q\n", "python -m pytest -q || true\n"),
    "tests_never_run": replaced("python -m pytest -q -rs", "python -m pytest --co -q"),
    "actions_pinned_by_half": replaced(CHECKOUT_PIN, "actions/checkout@" + "0" * 40),
    # A2 : un tag se déplace, un préfixe de commit aussi, une branche encore plus.
    "release_action_on_a_moving_tag": replaced(CHECKOUT_PIN, "actions/checkout@v4", 3),
    "release_action_on_a_short_sha": replaced(SETUP_PYTHON_PIN, SETUP_PYTHON_PIN[:-33], 3),
    "release_action_on_a_branch": replaced(CHECKOUT_PIN, "actions/checkout@main", 3),
    "no_workflow_permissions": replaced(WORKFLOW_READ, ""),
    "workflow_write_instead_of_read": replaced(WORKFLOW_READ, "permissions:\n  contents: write\n"),
    "workflow_write_all": replaced(WORKFLOW_READ, "permissions: write-all\n"),
    "workflow_reads_and_writes_packages": replaced(
        WORKFLOW_READ, WORKFLOW_READ + "  packages: write\n"
    ),
    "second_job": replaced("    steps:\n", "    steps:\n      - run: echo\n  other:\n    steps:\n"),
    "signtool": replaced(SMOKE_RUN, SMOKE_RUN + "      - run: signtool sign x\n"),
    "nsis_installer": replaced(SMOKE_RUN, SMOKE_RUN + "      - run: makensis pz-duel.nsi\n"),
    "inno_setup": replaced(SMOKE_RUN, SMOKE_RUN + "      - run: iscc pz-duel.iss\n"),
    "installer_step": replaced(SMOKE_RUN, SMOKE_RUN + "      - name: Build installer\n"),
    "workshop": replaced(SMOKE_RUN, SMOKE_RUN + "      - run: steamcmd +workshop_build_item x\n"),
}


def yaml_problems(name: str, text: str) -> list[str]:
    # GitHub refuse un fichier illisible en entier : le run est créé sans aucun job.
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark is not None else "unknown line"
        return [f"{name}: invalid YAML at {where}: {getattr(error, 'problem', error)}"]
    if not isinstance(loaded, dict) or not isinstance(loaded.get("jobs"), dict):
        return [f"{name}: YAML loads but has no `jobs` mapping"]
    return []


@pytest.mark.parametrize("workflow", [TESTS_YML, RELEASE_YML], ids=lambda path: path.name)
def test_workflow_is_valid_yaml(workflow):
    assert yaml_problems(workflow.name, workflow.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("workflow", [TESTS_YML, RELEASE_YML], ids=lambda path: path.name)
def test_unquoted_colon_value_in_workflow_is_seen(workflow):
    text = workflow.read_text(encoding="utf-8")
    quoted = f'run: "python -m {REQUIREMENTS_INSTALL}"'
    mutated = text.replace(quoted, f"run: python -m {REQUIREMENTS_INSTALL}")
    line = text[: text.index(quoted)].count("\n") + 1

    assert mutated != text
    assert yaml_problems(workflow.name, mutated) == [
        f"{workflow.name}: invalid YAML at line {line}: mapping values are not allowed here"
    ]


def test_ac45_tests_workflow_covers_windows_and_the_binary():
    assert ci_problems(TESTS_YML.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("mutant", TESTS_MUTANTS, ids=str)
def test_ac45_defect_in_tests_workflow_is_seen(mutant):
    text = TESTS_YML.read_text(encoding="utf-8")
    mutated = TESTS_MUTANTS[mutant](text)

    assert mutated != text
    assert ci_problems(mutated) != []


def test_ac47_release_workflow_publishes_only_after_the_smoke_test():
    assert release_problems(RELEASE_YML.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("mutant", RELEASE_MUTANTS, ids=str)
def test_ac47_defect_in_release_workflow_is_seen(mutant):
    text = RELEASE_YML.read_text(encoding="utf-8")
    mutated = RELEASE_MUTANTS[mutant](text)

    assert mutated != text
    assert release_problems(mutated) != []


@pytest.mark.parametrize(
    ("line", "pinned"),
    [
        (f"      - uses: {CHECKOUT_PIN} # v4.4.0", True),
        (f"      - uses: {SETUP_PYTHON_PIN}", True),
        ("      - uses: actions/checkout@v4", False),
        ("      - uses: actions/checkout@v4.4.0", False),
        ("      - uses: actions/checkout@main", False),
        ("      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af67726", False),
        ("      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af6772620", False),
        ("      - uses: actions/checkout@11D5960A326750D5838078E36CF38B85AF677262", False),
        ("      - uses: actions/checkout", False),
        ("      - uses: ./local-action", False),
        ("      # uses: actions/checkout@v4", True),  # commentée, l'étape n'existe pas
    ],
)
def test_a2_every_action_is_pinned_to_a_full_commit(line, pinned):
    assert (pin_problems("w", f"jobs:\n  j:\n    steps:\n{line}\n") == []) is pinned


@pytest.mark.parametrize("path", [TESTS_YML, RELEASE_YML], ids=lambda path: path.name)
def test_a2_shipped_workflows_pin_every_action(path):
    text = path.read_text(encoding="utf-8")
    assert pin_problems(path.stem, text) == []
    assert uses_lines(text) == {f"uses: {CHECKOUT_PIN}", f"uses: {SETUP_PYTHON_PIN}"}


def test_a2_release_workflow_reads_and_only_the_publishing_job_writes():
    top, jobs = parse(RELEASE_YML.read_text(encoding="utf-8"))
    assert [line.strip() for line in top["permissions"]] == ["permissions:", "contents: read"]
    writers = [name for name, job in jobs.items() if any("write" in line for line in job)]
    assert writers == [RELEASE_JOB]


def test_ac47_comments_do_not_trip_the_forbidden_words():
    text = RELEASE_YML.read_text(encoding="utf-8")

    assert release_problems(text + "      # signtool, installer, Workshop: never here\n") == []


def test_release_builds_exactly_like_the_pull_requests():
    """Le binaire publié sort de la construction que chaque pull request a éprouvée."""

    def chain(path: Path) -> list[str]:
        _, jobs = parse(path.read_text(encoding="utf-8"))
        job = next(job for job in jobs.values() if any(SPEC_PATH in line for line in job))
        return [
            step for step in job_steps(job)
            if "gh release" not in step and "sha256sum" not in step
        ]

    assert chain(RELEASE_YML) == chain(TESTS_YML)


def requirement_problems(text: str) -> list[str]:
    """Chaque paquet de la construction : version exacte, puis au moins une empreinte."""
    problems, current, hashes = [], None, 0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith(" "):
            if current is None or not HASH.match(line):
                problems.append(f"malformed hash line: {line.strip()}")
            hashes += 1
            continue
        if current is not None and hashes == 0:
            problems.append(f"{current}: no hash")
        pinned = PINNED.match(line)
        if pinned is None:
            problems.append(f"not pinned with hashes: {line}")
            current = None
        else:
            current, hashes = pinned[1], 0
    if current is not None and hashes == 0:
        problems.append(f"{current}: no hash")
    return problems


def test_build_requirements_are_pinned_with_their_hashes():
    text = (REPO / REQUIREMENTS).read_text(encoding="utf-8")
    assert requirement_problems(text) == []
    names = {match[1].lower() for match in map(PINNED.match, text.splitlines()) if match}
    # PyInstaller et tout ce qu'il tire sous Windows : `--require-hashes` refuserait un oubli.
    assert names == {
        "pyinstaller", "altgraph", "packaging", "pyinstaller-hooks-contrib", "setuptools",
        "pefile", "pywin32-ctypes",
    }


@pytest.mark.parametrize(
    "mutant",
    [
        lambda text: text.replace("pyinstaller==6.22.3", "pyinstaller>=6.22.3", 1),
        lambda text: text.replace(
            "altgraph==0.17.5 \\\n    --hash=", "altgraph==0.17.5 \\\n    #", 1
        ),
        lambda text: text + "requests\n",
        lambda text: text.replace("--hash=sha256:", "--hash=sha256:x", 1),
    ],
    ids=["range", "hash_removed", "unpinned_added", "bad_hash"],
)
def test_requirement_mutants_are_caught(mutant):
    text = (REPO / REQUIREMENTS).read_text(encoding="utf-8")
    assert mutant(text) != text
    assert requirement_problems(mutant(text)) != []


def test_build_and_release_install_the_same_hashed_file():
    for path in (TESTS_YML, RELEASE_YML):
        text = path.read_text(encoding="utf-8")
        assert text.count(REQUIREMENTS) == text.count(REQUIREMENTS_INSTALL) == 1, path.name


# ---------------------------------------------------------------- documentation (R9)
EN, FR = "en", "fr"
LANGUAGES = [EN, FR]
README = {EN: REPO / "README.md", FR: REPO / "README.fr.md"}
LIMITS = {EN: LIMITS_DIR / "limites-connues.md", FR: LIMITS_DIR / "limites-connues.fr.md"}
START_HEADING = {EN: "## Getting started on Windows", FR: "## Démarrage sous Windows"}
UNPROVEN_HEADING = {EN: "## Not proven on the real game", FR: "## Non prouvé sur le vrai jeu"}
GUEST_SIDE = "B"

# Ce que le joueur ne doit jamais avoir à taper, ni un bloc de code où le coller.
TERMINAL = re.compile(
    r"```|^\s*[$>]\s+\S+\s+-|--[a-z]"
    r"|\b(?:python3?|pip|pytest|git|curl|wget|netsh|sudo|chmod|powershell|pwsh|cmd"
    r"|taskkill|regedit)\b",
    re.IGNORECASE | re.MULTILINE,
)
START_TERMS = {
    EN: {
        "smartscreen": r"SmartScreen",
        "firewall": r"Windows Firewall",
        "share port": r"share port",
        "bat ban": r"Do not post or distribute the `\.bat`",
        "rematch keeps": r"Rematch[^\n]*\n?[^\n]*`\.bat` you already sent stays valid",
        "new duel stales": r"new duel can make a `\.bat` you already sent stale",
        "send again": r"send the new `\.bat` again",
    },
    FR: {
        "smartscreen": r"SmartScreen",
        "firewall": r"pare-feu Windows",
        "share port": r"port de partage",
        "bat ban": r"Ne publiez pas et ne diffusez pas le `\.bat`",
        "rematch keeps": r"Revanche[^\n]*\n?[^\n]*`\.bat` déjà envoyé reste valide",
        "new duel stales": r"nouveau duel peut périmer un `\.bat` déjà envoyé",
        "send again": r"renvoyer le nouveau `\.bat`",
    },
}
UNPROVEN_TERMS = {
    EN: {
        "eight arguments": r"`StartServer64\.bat` passing on its 8 arguments",
        "steam value": r"`-Dzomboid\.steam` against a `-nosteam` client",
        "end of script": r"end-of-script behaviour",
        "guest connection": r"A guest actually connecting",
        "internet reach": r"Reaching an entered address from the Internet",
        "not tested": r"service does not test an entered address",
    },
    FR: {
        "eight arguments": r"transmission par `StartServer64\.bat` de ses 8 arguments",
        "steam value": r"`-Dzomboid\.steam` face à un client `-nosteam`",
        "end of script": r"comportement de fin de script",
        "guest connection": r"connexion effective d'un invité",
        "internet reach": r"joignabilité depuis Internet d'une adresse saisie",
        "not tested": r"service ne teste pas une adresse saisie",
    },
}
# P5, P11, P13, P14 et P15 ont relevé ce que `limites-connues` doit garder. `os.sync` n'y est
# plus un blocage depuis P18F1 : il reste la raison pour laquelle `sync_disk`, sous Windows, ne
# synchronise que fichier par fichier. `test_disk_sync.py` suit l'usage de ce nom dans `service/`.
LIMITS_TERMS = {
    EN: {
        "windows disk sync": r"`os\.sync`",
        "powershell": r"constrained language mode",
        "mark of the web": r"Mark of the Web",
        "antivirus": r"Antivirus",
        "visible password": r"password is visible",
        "nt resume": r"`NtResumeProcess`",
        "mei folder": r"`_MEI\*`",
        "rebind": r"TIME_WAIT",
        "error phase": r"`error` phase",
    },
    FR: {
        "windows disk sync": r"`os\.sync`",
        "powershell": r"mode de langage restreint",
        "mark of the web": r"Mark of the Web",
        "antivirus": r"antivirus",
        "visible password": r"mot de passe client est visible",
        "nt resume": r"`NtResumeProcess`",
        "mei folder": r"`_MEI\*`",
        "rebind": r"TIME_WAIT",
        "error phase": r"phase `error`",
    },
}
CITATION = re.compile(r"`([\w./-]+\.py)::(\w+)`")
CI_RUN = re.compile(r"https://github\.com/chpomob/pz-duel/actions/runs/\d+")
# Ni un chemin de fichier, ni le motif `test_windows_*`.
BARE_TEST = re.compile(r"(?<![/\w])test_\w+(?![\w*])")


def section(text: str, heading: str) -> str:
    """Le corps d'une section de niveau 2, vide si elle manque."""
    lines = text.splitlines()
    if heading not in lines:
        return ""
    start = lines.index(heading) + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def missing_terms(body: str, terms: dict[str, str]) -> list[str]:
    # Le passage à la ligne d'un paragraphe ne doit pas faire disparaître un terme.
    flat = re.sub(r"\s*\n\s*", " ", body)
    return [name for name, term in terms.items() if not re.search(term, flat)]


def shipped_ports() -> dict[str, int]:
    runtime = json.loads(RUNTIME.read_text(encoding="utf-8"))
    ports = runtime["ports"][GUEST_SIDE]
    return {"game": ports["game"], "udp": ports["udp"], "share": runtime["share_port"]}


def start_problems(text: str, language: str, ports: dict[str, int]) -> list[str]:
    body = section(text, START_HEADING[language])
    if not body:
        return ["start: no getting started section"]
    problems = [f"start: {name} missing" for name in missing_terms(body, START_TERMS[language])]
    udp = f"\\*\\*{ports['game']}\\*\\*\\D+\\*\\*{ports['udp']}\\*\\*\\D+\\*\\*UDP\\*\\*"
    if not re.search(udp, body):
        problems.append("start: game ports are not given in UDP")
    if not re.search(f"\\*\\*{ports['share']}\\*\\*\\D+\\*\\*TCP\\*\\*", body):
        problems.append("start: share port is not given in TCP")
    if LIMITS[language].name not in body:
        problems.append("start: no link to the known limits")
    command = TERMINAL.search(body)
    if command:
        problems.append(f"start: terminal command {command[0]!r}")
    return problems


# P19F6 : la seule vérification qui tienne se fait hors du fichier, avant de l'ouvrir.
GUEST_HEADING = {EN: "### Guest", FR: "### Invité"}
HOST_HEADING = {EN: "### Host", FR: "### Hôte"}
CERTUTIL = "`certutil -hashfile pz-duel-join.bat SHA256`"
OPENS_FILE = {
    EN: r"double-click|\bopen (?:it|the file|the `\.bat`)",
    FR: r"double-clique|\bouvr\w* (?:le|-le)",
}
REFUSES = {
    EN: r"Do not open the file if the result does not start with that code",
    FR: r"N'ouvrez pas le fichier si le résultat ne commence pas par ce code",
}
READ_ALOUD = {
    EN: r"read it aloud[^.]*separately from the link",
    FR: r"lisez-le à voix haute[^.]*séparément du lien",
}


def steps_of(text: str, heading: str) -> list[str]:
    """Les étapes numérotées d'une sous-section, chacune sur une ligne."""
    lines = text.splitlines()
    if heading not in lines:
        return []
    body = []
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("#"):
            break
        body.append(line)
    flat = re.sub(r"\n(?!\d+\. )\s*", " ", "\n".join(body))
    return [line for line in flat.splitlines() if re.match(r"\d+\. ", line)]


def guest_check_problems(text: str, language: str) -> list[str]:
    guest = steps_of(text, GUEST_HEADING[language])
    host = steps_of(text, HOST_HEADING[language])
    if not guest or not host:
        return ["guest: no host or guest steps"]
    problems = []
    checked = False
    for step in guest:
        if CERTUTIL in step and re.search(REFUSES[language], step):
            checked = True
        elif re.search(OPENS_FILE[language], step, re.IGNORECASE) and not checked:
            problems.append(f"guest: opens the file before the certutil check: {step[:40]}")
    if not checked:
        problems.append("guest: no certutil check")
    if not any(re.search(READ_ALOUD[language], step) for step in host):
        problems.append("host: the code is not read aloud, apart from the link")
    return problems


@pytest.mark.parametrize("language", LANGUAGES)
def test_guest_checks_the_file_with_certutil_before_opening_it(language):
    assert guest_check_problems(read(README[language]), language) == []


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "mutant", ["unchanged", "no_check", "check_after_opening", "no_refusal", "not_aloud"]
)
def test_guest_check_mutants_are_caught(language, mutant):
    """`unchanged` passe par la même remise en lignes : un mutant n'est pas pris par accident."""
    text = read(README[language])
    guest = steps_of(text, GUEST_HEADING[language])
    check = next(step for step in guest if CERTUTIL in step)
    flat = re.sub(r"\s*\n\s*", " ", text)
    if mutant == "unchanged":
        mutated = flat
    elif mutant == "no_check":
        mutated = flat.replace(CERTUTIL, "`dir`")
    elif mutant == "check_after_opening":
        opening = next(step for step in guest if step is not check and re.search(
            OPENS_FILE[language], step, re.IGNORECASE))
        swapped = flat.replace(check[3:], "@@").replace(opening[3:], check[3:])
        mutated = swapped.replace("@@", opening[3:])
    elif mutant == "no_refusal":
        mutated = re.sub(REFUSES[language], "Go on", flat)
    else:
        mutated = re.sub(READ_ALOUD[language], "send it", flat)
    # Remettre les titres sur leur ligne : `steps_of` les cherche seuls.
    for heading in (GUEST_HEADING[language], HOST_HEADING[language]):
        mutated = mutated.replace(f" {heading} ", f"\n{heading}\n")
    mutated = re.sub(r" (\d+\. \*\*)", r"\n\1", mutated)
    mutated = re.sub(r" (#{2,3} )", r"\n\1", mutated)

    if mutant == "unchanged":
        assert guest_check_problems(mutated, language) == []
    else:
        assert mutated != flat
        assert guest_check_problems(mutated, language) != []


def defined_names(path: Path) -> set[str]:
    """Toutes les fonctions du fichier, y compris dans un bloc `if sys.platform == "win32"`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return {node.name for node in ast.walk(tree) if isinstance(node, kinds)}


def citation_problems(text: str) -> list[str]:
    problems = []
    for file, name in CITATION.findall(text):
        path = REPO / file
        if not path.is_file():
            problems.append(f"limits: {file} does not exist")
        elif name not in defined_names(path):
            problems.append(f"limits: {file} has no {name}")
    # Un nom sans son fichier échapperait au contrôle : il ne compte pas comme une preuve.
    bare_names = BARE_TEST.findall(CITATION.sub("", text))
    return problems + [f"limits: {name} is cited without its file" for name in bare_names]


def limits_problems(text: str, language: str) -> list[str]:
    unproven = section(text, UNPROVEN_HEADING[language])
    if not unproven:
        return ["limits: no list of what is not proven"]
    names = missing_terms(unproven, UNPROVEN_TERMS[language])
    problems = [f"limits: {name} missing from what is not proven" for name in names]
    names = missing_terms(text, LIMITS_TERMS[language])
    problems += [f"limits: {name} missing" for name in names]
    for point in re.split(r"^### ", unproven, flags=re.MULTILINE)[1:]:
        if not CITATION.search(point) and not CI_RUN.search(point):
            problems.append(f"limits: no proof cited for {point.splitlines()[0]}")
    return problems + citation_problems(text)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def flat_sub(pattern: str, text: str) -> str:
    """Efface un terme, y compris coupé en deux lignes par le paragraphe."""
    return re.sub(pattern.replace(" ", r"\s+"), "effaced", text)


@pytest.mark.parametrize("language", LANGUAGES)
def test_ac50_ac61_readme_has_the_getting_started_section(language):
    assert start_problems(read(README[language]), language, shipped_ports()) == []


@pytest.mark.parametrize(
    ("language", "term"),
    [(language, term) for language in LANGUAGES for term in START_TERMS[language]],
)
def test_ac50_ac61_missing_term_is_seen(language, term):
    text = read(README[language])
    mutated = flat_sub(START_TERMS[language][term], text)

    assert mutated != text
    assert f"start: {term} missing" in start_problems(mutated, language, shipped_ports())


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("key", ["game", "udp", "share"])
def test_ac50_ports_follow_the_runtime(language, key):
    ports = {**shipped_ports(), key: 1}

    assert start_problems(read(README[language]), language, ports) != []


@pytest.mark.parametrize("language", LANGUAGES)
def test_ac50_term_outside_the_section_does_not_count(language):
    mutated = read(README[language]).replace(START_HEADING[language], "## Elsewhere")

    assert start_problems(mutated, language, shipped_ports()) == [
        "start: no getting started section"
    ]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "command",
    [
        "```bash\nstart\n```",
        "Run python -m service.main first.",
        "Use netsh advfirewall to open the port.",
        "Open PowerShell as administrator.",
        "Start it with --mode real.",
        "$ pz-duel -x",
    ],
)
def test_ac50_terminal_command_in_the_section_is_seen(language, command):
    text = read(README[language])
    heading = START_HEADING[language]
    mutated = text.replace(heading, f"{heading}\n\n{command}\n")

    assert any("terminal" in item for item in start_problems(mutated, language, shipped_ports()))


@pytest.mark.parametrize("language", LANGUAGES)
def test_ac51_limits_list_what_is_not_proven(language):
    assert limits_problems(read(LIMITS[language]), language) == []


@pytest.mark.parametrize(
    ("language", "term"),
    [(language, term) for language in LANGUAGES for term in UNPROVEN_TERMS[language]],
)
def test_ac51_missing_point_is_seen(language, term):
    text = read(LIMITS[language])
    mutated = flat_sub(UNPROVEN_TERMS[language][term], text)

    assert mutated != text
    assert f"limits: {term} missing from what is not proven" in limits_problems(mutated, language)


@pytest.mark.parametrize(
    ("language", "term"),
    [(language, term) for language in LANGUAGES for term in LIMITS_TERMS[language]],
)
def test_ac51_finding_of_an_earlier_step_is_kept(language, term):
    text = read(LIMITS[language])
    mutated = flat_sub(LIMITS_TERMS[language][term], text)

    assert mutated != text
    assert f"limits: {term} missing" in limits_problems(mutated, language)


@pytest.mark.parametrize("language", LANGUAGES)
def test_ac51_point_without_a_proof_is_seen(language):
    text = read(LIMITS[language])
    unproven = section(text, UNPROVEN_HEADING[language])
    mutated = text.replace(unproven, CITATION.sub("no proof", unproven))

    assert any("no proof cited" in item for item in limits_problems(mutated, language))


@pytest.mark.parametrize(
    ("citation", "expected"),
    [
        ("`tests/test_share.py::test_that_does_not_exist`", "has no test_that_does_not_exist"),
        ("`tests/test_gone.py::test_anything`", "tests/test_gone.py does not exist"),
        ("`test_windows_command_keeps_its_eight_arguments`", "cited without its file"),
    ],
)
def test_ac51_cited_proof_must_name_something_that_exists(citation, expected):
    assert any(expected in item for item in citation_problems(citation))


def test_ac51_win32_block_counts_as_a_definition():
    names = defined_names(REPO / "tests" / "test_guest_launcher.py")

    assert "test_windows_launcher_installs_the_mod_and_connects" in names


def test_ac51_both_languages_cite_the_same_proofs():
    cited = {language: set(CITATION.findall(read(LIMITS[language]))) for language in LANGUAGES}

    assert cited[EN] == cited[FR]


def test_p9f17_gitattributes_forces_lf_for_every_file():
    """Le dépôt impose LF à l'extraction, sur tout poste (P9F17).

    Mutation : `.gitattributes` retiré, ou sa règle `*` sans `eol=lf`, échoue.
    """
    attributes = REPO / ".gitattributes"
    assert attributes.is_file()
    rules = [line.split() for line in attributes.read_text("utf-8").splitlines()]
    star = [rule[1:] for rule in rules if rule and rule[0] == "*"]
    assert star and "eol=lf" in star[0] and "text=auto" in star[0]
