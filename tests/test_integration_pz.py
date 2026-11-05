"""Tests du vrai jeu (`pz_integration`) et preuve de leur `skip` quand PZ est absent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from service.preflight import ABSENT, ENV_VAR, SCRIPT, detect_pz

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def pz_dir() -> Path:
    """Dossier du jeu détecté ; sans lui, le test est en `skip` avec le motif de la détection."""
    detection = detect_pz(None, os.environ, Path.home())
    if detection.path is None:
        pytest.skip(detection.message)
    return detection.path


@pytest.mark.pz_integration
def test_detection_finds_install(pz_dir):
    assert os.access(pz_dir / SCRIPT, os.X_OK)


def test_detection_reports_absent(tmp_path):
    home = tmp_path / "home"
    empty_install = tmp_path / "install"
    home.mkdir()
    empty_install.mkdir()

    for install_dir in ("", str(empty_install)):
        env = {**os.environ, "HOME": str(home), ENV_VAR: install_dir}
        assert detect_pz(None, env, home).status == ABSENT

        # `-m pz_integration` écarte ce test-ci : l'enfant ne se relance pas lui-même.
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-m",
                "pz_integration",
                "-rs",
                "-p",
                "no:cacheprovider",
                "tests/test_integration_pz.py",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        assert "PZ absent" in child.stdout
        assert "skipped" in child.stdout
        assert " passed" not in child.stdout
