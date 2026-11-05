"""AC62 : `service/` n'importe que la stdlib et reste en syntaxe Python 3.11."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent / "service"
MIN_SYNTAX = (3, 11)


def foreign_imports(tree: ast.AST) -> list[str]:
    """Rend les modules racines importés hors stdlib et hors paquet `service`."""
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.append(node.module.split(".")[0])
    allowed = sys.stdlib_module_names | {SERVICE_DIR.name}
    return [root for root in roots if root not in allowed]


def test_imports():
    assert foreign_imports(ast.parse("import requests\nfrom yaml import load")) == [
        "requests",
        "yaml",
    ]
    assert not foreign_imports(ast.parse("import json\nfrom . import state"))

    paths = sorted(SERVICE_DIR.rglob("*.py"))
    assert paths

    offenders = {}
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path), feature_version=MIN_SYNTAX)
        if found := foreign_imports(tree):
            offenders[str(path.relative_to(SERVICE_DIR))] = found
    assert not offenders
