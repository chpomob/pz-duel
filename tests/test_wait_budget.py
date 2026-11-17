"""P9F29 : la patience des tests vient de `PZDUEL_TEST_WAIT_S`, et d'un seul endroit.

Les budgets d'attente étaient écrits en dur, un par fichier (3, 5 ou 10 s), calibrés pour la
machine de développement ; un runner Windows partagé les dépassait (run #79). `doubles.waiting`
lit désormais la variable, et chaque fichier qui attend un état produit ailleurs y prend son
budget. Mutation : rendre `wait_budget_s` constant (ignorer la variable) fait tomber
`test_budget_follows_the_environment` et `test_every_waiting_file_takes_its_budget_from_the_environment`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from doubles import waiting
from doubles.waiting import DEFAULT_WAIT_S, WAIT_ENV, wait_budget_s, wait_until

REPO = Path(__file__).resolve().parent.parent
TESTS_YML = REPO / ".github" / "workflows" / "tests.yml"
CI_WAIT_S = 60.0
# Les fichiers qui prennent `WAIT_S` à la source : la valeur lue doit y arriver telle quelle.
WAITING_FILES = (
    "test_api_security",
    "test_setup_flow",
    "test_share",
    "test_modes",
    "test_snapshots",
    "test_contract",
    "test_match",
    "test_announce",
    "test_upnp",
    "test_secrets",
    "test_servers",
)
SUBPROCESS_TIMEOUT_S = 120


def test_budget_follows_the_environment():
    assert wait_budget_s({WAIT_ENV: "60"}) == 60.0
    assert wait_budget_s({WAIT_ENV: " 2.5 "}) == 2.5
    assert wait_budget_s({}) == DEFAULT_WAIT_S
    assert wait_budget_s({WAIT_ENV: ""}) == DEFAULT_WAIT_S


def test_development_default_is_unchanged():
    """Sans la variable, la machine de développement garde ses 10 s d'avant P9F29."""
    assert DEFAULT_WAIT_S == 10.0


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "inf", "nan"])
def test_unreadable_budget_is_refused(raw):
    """Une faute de frappe ne devient ni le défaut en silence, ni une attente sans fin."""
    with pytest.raises(ValueError, match=WAIT_ENV):
        wait_budget_s({WAIT_ENV: raw})


def test_module_budget_is_the_one_read_from_the_environment():
    assert waiting.WAIT_S == wait_budget_s(os.environ)


def test_wait_until_without_a_delay_uses_the_budget(monkeypatch):
    monkeypatch.setattr(waiting, "WAIT_S", 0.03)
    with pytest.raises(AssertionError, match=r"^attendu en 0\.03 s : jamais ;"):
        wait_until(lambda: False, what="jamais")


def test_every_waiting_file_takes_its_budget_from_the_environment():
    """Un sous-processus importe chaque fichier avec une valeur que rien d'autre ne produit :
    un littéral resté dans l'un d'eux s'y verrait."""
    program = (
        "import json, sys\n"
        "sys.path.insert(0, 'tests')\n"
        "import importlib\n"
        "from doubles import waiting\n"
        f"names = {list(WAITING_FILES)!r}\n"
        "seen = {name: importlib.import_module(name).WAIT_S for name in names}\n"
        "seen['doubles.waiting'] = waiting.WAIT_S\n"
        "print(json.dumps(seen))\n"
    )
    env = {**os.environ, WAIT_ENV: "42.5"}
    done = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen == {name: 42.5 for name in [*WAITING_FILES, "doubles.waiting"]}


def test_ci_gives_every_job_sixty_seconds():
    """Au niveau du workflow, la variable vaut pour tous les jobs de `tests.yml`."""
    workflow = yaml.safe_load(TESTS_YML.read_text(encoding="utf-8"))
    assert float(workflow["env"][WAIT_ENV]) == CI_WAIT_S
    for name, job in workflow["jobs"].items():
        assert WAIT_ENV not in job.get("env", {}), f"{name} redéfinit le budget"
