"""Workflows de CI et de release (R8), par lecture textuelle : la stdlib n'a pas de `yaml`.

Chaque exigence est une entrée de `ci_problems` ou de `release_problems`. Les mutants
ci-dessous prouvent que ces lectures voient le défaut, y compris une étape seulement commentée.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
TESTS_YML = WORKFLOWS / "tests.yml"
RELEASE_YML = WORKFLOWS / "release.yml"
BUILD_SPEC = REPO / "packaging" / "pz-duel.spec"

SUITE_PYTHONS = ["3.12", "3.13", "3.14"]
BUILD_PYTHON = "3.13"
REQUIREMENTS = "packaging/requirements-build.txt"
SPEC_PATH = "packaging/pz-duel.spec"
SMOKE_PATH = "packaging/smoke_test.py"

STEP_INDENT = 6
KEY = re.compile(r"([\w-]+):")
CONDITION = re.compile(r"^\s*(?:- )?if:", re.MULTILINE)
SWALLOWED = re.compile(r"continue-on-error|\|\|\s*(?:true|:|exit 0)|set \+e")
# `\binstaller\b` et non `installer` : `PyInstaller` construit le binaire, il n'installe rien.
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
        first_step(steps, f"pip install -r {REQUIREMENTS}"),
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
    problems = []
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


def release_problems(text: str) -> list[str]:
    top, jobs = parse(text)
    exe = exe_name()
    problems = []

    trigger = blocks(top.get("on", [])[1:], 2)
    tags = [line for line in trigger.get("push", []) if line.strip().startswith("tags:")]
    if list(trigger) != ["push"] or len(trigger["push"]) != 2 or not tags:
        problems.append("release: the only trigger must be a tag push")
    elif re.findall(r'"([^"]+)"', tags[0]) != ["v*"]:
        problems.append("release: tags are not v*")

    if "permissions" in top:
        problems.append("release: permissions belong to the job")
    if len(jobs) != 1:
        return problems + ["release: expected one job, so that steps order the publication"]
    name, job = next(iter(jobs.items()))
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
CHECKOUT_STEP = "      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n"
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
    "build_on_another_python": in_build_job('"3.13"', '"3.14"'),
    "smoke_commented_out": in_build_job(SMOKE_RUN, "        # " + SMOKE_RUN.lstrip()),
    "smoke_on_another_file": in_build_job("pz-duel.exe", "other.exe"),
    "smoke_may_fail": in_build_job(SMOKE_RUN, SMOKE_RUN + "        continue-on-error: true\n"),
    "build_on_linux": in_build_job("windows-latest", "ubuntu-latest"),
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
    "second_job": replaced("    steps:\n", "    steps:\n      - run: echo\n  other:\n    steps:\n"),
    "signtool": replaced(SMOKE_RUN, SMOKE_RUN + "      - run: signtool sign x\n"),
    "nsis_installer": replaced(SMOKE_RUN, SMOKE_RUN + "      - run: makensis pz-duel.nsi\n"),
    "inno_setup": replaced(SMOKE_RUN, SMOKE_RUN + "      - run: iscc pz-duel.iss\n"),
    "installer_step": replaced(SMOKE_RUN, SMOKE_RUN + "      - name: Build installer\n"),
    "workshop": replaced(SMOKE_RUN, SMOKE_RUN + "      - run: steamcmd +workshop_build_item x\n"),
}


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
