"""Le harnais de campagne : pas d'étape validée sans commit, pas de .gitignore écrasé."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "docs/campaigns/phase1/scaffolding/run_campaign.py"
)
STEP = {"id": "P1", "title": "Socle", "block": ""}
EXPECTED = "feat(phase1): P1 — Socle"
PREVIOUS = "feat(phase1): P0 — Précédent"
IGNORED = b"__pycache__/\n"


def load_harness():
    spec = importlib.util.spec_from_file_location("run_campaign_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sh(repo: Path, *args: str) -> str:
    done = subprocess.run(args, cwd=repo, text=True, capture_output=True, check=True)
    return done.stdout.strip()


def commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(message, encoding="utf-8")
    sh(repo, "git", "add", name)
    sh(repo, "git", "commit", "-q", "-m", message)
    return sh(repo, "git", "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sh(root, "git", "init", "-q", "-b", "master")
    sh(root, "git", "config", "user.name", "test")
    sh(root, "git", "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_bytes(IGNORED)
    sh(root, "git", "add", ".gitignore")
    commit(root, "previous.txt", PREVIOUS)
    return root


@pytest.fixture
def harness(repo, tmp_path, monkeypatch):
    """Le harnais, pointé sur un dépôt jetable et une suite de tests qui passe."""
    module = load_harness()
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\necho '1 passed'\n", encoding="utf-8")
    fake_python.chmod(0o755)
    monkeypatch.setattr(module, "REPO", repo)
    monkeypatch.setattr(module, "PY", str(fake_python))
    monkeypatch.setattr(module, "log", lambda *args, **kwargs: None)
    return module


def head(repo: Path, fmt: str = "%H") -> str:
    return sh(repo, "git", "log", "-1", f"--format={fmt}")


def run_main(harness, monkeypatch, loop) -> int:
    """Déroule `main` sur l'étape P1, `loop` tenant lieu de boucle de code."""
    monkeypatch.setattr(harness, "parse_plan", lambda: [STEP])
    monkeypatch.setattr(harness, "ac_text", dict)
    monkeypatch.setattr(harness, "write_step_spec", lambda step, acs: Path("spec.md"))
    monkeypatch.setattr(harness, "run_loop", loop)
    monkeypatch.setattr(sys, "argv", ["run_campaign.py"])
    return harness.main()


def test_gate_fails_without_new_commit(harness, repo):
    before = head(repo)

    ok, notes = harness.gate(STEP, before)

    assert not ok
    assert "aucun commit" in notes
    assert head(repo) == before
    assert head(repo, "%s") == PREVIOUS


def test_gate_fails_when_head_went_back(harness, repo):
    previous = head(repo)
    before = commit(repo, "step.txt", "commit à ne pas perdre")
    sh(repo, "git", "reset", "-q", "--hard", previous)

    ok, _ = harness.gate(STEP, before)

    assert not ok
    assert head(repo) == previous
    assert head(repo, "%s") == PREVIOUS


def test_main_stops_when_loop_commits_nothing(harness, repo, monkeypatch):
    before = head(repo)

    assert run_main(harness, monkeypatch, lambda step, spec: 0) == 1

    assert head(repo) == before
    assert head(repo, "%s") == PREVIOUS


def test_gate_renames_only_the_new_commit(harness, repo):
    before = head(repo)
    commit(repo, "step.txt", "squash: phase1-p1 — adversarial approved")
    sh(repo, "git", "commit", "-q", "--amend", "-m", "squash: phase1-p1", "-m",
       "Corps du commit.\n\nCo-Authored-By: Quelqu'un <q@example.invalid>")  # fmt: skip

    ok, notes = harness.gate(STEP, before)

    assert ok, notes
    assert head(repo, "%s") == EXPECTED
    assert "Corps du commit." in head(repo, "%b")
    assert "Co-Authored-By: Quelqu'un <q@example.invalid>" in head(repo, "%b")
    assert sh(repo, "git", "rev-parse", "HEAD~1") == before


def test_gate_keeps_a_commit_already_named(harness, repo):
    before = head(repo)
    sha = commit(repo, "step.txt", EXPECTED)

    ok, notes = harness.gate(STEP, before)

    assert ok, notes
    assert head(repo) == sha


def test_require_clean_restores_gitignore_put_back_by_the_loop(harness, repo):
    (repo / ".gitignore").write_bytes(IGNORED + b"matches/\n")
    sh(repo, "git", "add", ".gitignore")
    sh(repo, "git", "commit", "-q", "-m", "l'étape ajoute une entrée")
    (repo / ".gitignore").write_bytes(IGNORED)

    harness.require_clean(IGNORED)

    assert (repo / ".gitignore").read_bytes() == IGNORED + b"matches/\n"
    assert sh(repo, "git", "status", "--porcelain") == ""


def test_require_clean_stops_on_a_new_gitignore_edit(harness, repo):
    edited = IGNORED + b"matches/\n"
    (repo / ".gitignore").write_bytes(edited)

    with pytest.raises(SystemExit, match=r"\.gitignore"):
        harness.require_clean(IGNORED)

    assert (repo / ".gitignore").read_bytes() == edited


def test_require_clean_keeps_gitignore_without_a_reference(harness, repo):
    edited = IGNORED + b"matches/\n"
    (repo / ".gitignore").write_bytes(edited)

    with pytest.raises(SystemExit, match=r"\.gitignore"):
        harness.require_clean()

    assert (repo / ".gitignore").read_bytes() == edited


def test_main_stops_when_a_step_leaves_gitignore_edited(harness, repo, monkeypatch):
    edited = IGNORED + b"matches/\n"

    def loop(step, spec):
        commit(repo, "step.txt", EXPECTED)
        (repo / ".gitignore").write_bytes(edited)
        return 0

    with pytest.raises(SystemExit, match=r"\.gitignore"):
        run_main(harness, monkeypatch, loop)

    assert (repo / ".gitignore").read_bytes() == edited


def test_require_clean_still_stops_on_other_files(harness, repo):
    (repo / "previous.txt").write_text("modifié", encoding="utf-8")

    with pytest.raises(SystemExit, match="previous.txt"):
        harness.require_clean(IGNORED)
