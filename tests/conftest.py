"""Fixtures communes et règle de `skip` des tests `pz_integration` (D18)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

RUNTIME_PATH = Path(__file__).resolve().parent.parent / "service" / "runtime.json"
ZOMBOID_SUBDIRS = ("Lua", "Server", "mods", "db", "Saves/Multiplayer")


def pytest_collection_modifyitems(config, items):
    """Sans `-m pz_integration`, aucun test marqué ne lance de vrai serveur.

    La règle ne passe pas par `addopts` : un `pytest` nu doit signaler ces tests
    en `skip` avec leur motif, et non les désélectionner en silence.
    """
    if re.search(r"\bpz_integration\b", config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="test du vrai jeu : lancer avec -m pz_integration")
    for item in items:
        if item.get_closest_marker("pz_integration"):
            item.add_marker(skip)


@pytest.fixture
def zomboid_home(tmp_path, monkeypatch):
    """Dossier temporaire tenant lieu de `~/Zomboid`.

    `HOME` est redirigé aussi : un chemin résolu par défaut ne peut pas atteindre
    les vrais mondes de la machine.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "Zomboid"
    for name in ZOMBOID_SUBDIRS:
        (root / name).mkdir(parents=True)
    return root


@pytest.fixture
def runtime():
    """Copie de `service/runtime.json`, modifiable par chaque test."""
    return json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
