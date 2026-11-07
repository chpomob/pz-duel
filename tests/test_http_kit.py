"""A9 : le domaine ne passe plus par la couche HTTP, et le module commun ne dépend de personne."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO / "service"
SUBPROCESS_TIMEOUT_S = 60


def package_imports(name: str) -> set[str]:
    """Les modules de `service` que ce fichier importe, à n'importe quelle profondeur."""
    tree = ast.parse((SERVICE_DIR / f"{name}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[1] for alias in node.names if "service." in alias.name}
        elif isinstance(node, ast.ImportFrom) and node.level:
            found |= {node.module} if node.module else {alias.name for alias in node.names}
    return found


def loaded_with(module: str) -> set[str]:
    script = f"import sys, service.{module}; print(*sys.modules)"
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    return set(done.stdout.split())


def test_common_module_depends_on_nothing_of_the_service():
    assert package_imports("http_kit") == set()


@pytest.mark.parametrize("module", ["engine", "share"])
def test_domain_does_not_import_the_http_layer(module):
    assert "api" not in package_imports(module)
    assert "http_kit" in package_imports(module)
    assert "service.api" not in loaded_with(module)


def test_http_layer_does_not_import_the_domain():
    assert {"engine", "share"}.isdisjoint(package_imports("api"))
    assert "service.engine" not in loaded_with("api")


@pytest.mark.parametrize("first", ["http_kit", "api", "share", "engine", "main"])
def test_no_import_order_meets_a_cycle(first):
    assert f"service.{first}" in loaded_with(first)
