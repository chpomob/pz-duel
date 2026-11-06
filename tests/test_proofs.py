"""Preuves par couple (build, mod) : ce qui se relit d'un match à l'autre."""

from __future__ import annotations

import json

import pytest

from service.proofs import MAX_PAIRS, MAX_PROOF_BYTES, PROOFS_NAME, HookProofs

PAIR = "42.21.0|empreinte"
HOOK = "OnPlayerGetDamage"


@pytest.fixture
def reports() -> list[str]:
    return []


@pytest.fixture
def proofs(tmp_path, reports) -> HookProofs:
    return HookProofs(tmp_path / "matches" / PROOFS_NAME, reports.append)


def test_unknown_pair_has_no_failure(proofs, reports):
    assert proofs.failed_sides(PAIR, HOOK) == []
    assert reports == []


def test_failure_accumulates_sides_and_stays_on_its_pair(proofs):
    proofs.fail(PAIR, HOOK, "B")
    proofs.fail(PAIR, HOOK, "A")

    assert proofs.failed_sides(PAIR, HOOK) == ["A", "B"]
    assert proofs.failed_sides("42.22.0|empreinte", HOOK) == []
    assert proofs.failed_sides("42.21.0|autre", HOOK) == []


def test_a_signal_clears_a_failure_and_outlives_a_later_one(proofs):
    proofs.fail(PAIR, HOOK, "B")
    proofs.prove(PAIR, HOOK)
    assert proofs.failed_sides(PAIR, HOOK) == []

    proofs.fail(PAIR, HOOK, "B")
    assert proofs.failed_sides(PAIR, HOOK) == []


@pytest.mark.parametrize(
    "content",
    [
        b"pas du json",
        b"[]",
        b'{"a": 1}',
        b'{"a": {"h": {"status": "autre"}}}',
        b'{"a": {"h": {"status": "failed"}}}',
        b'{"a": {"h": {"status": "failed", "sides": null}}}',
        b'{"a": {"h": {"status": "failed", "sides": "A"}}}',
        b'{"a": {"h": {"status": "failed", "sides": ["C"]}}}',
        b'{"a": {"h": {"status": "failed", "sides": [["A"]]}}}',
        b" " * (MAX_PROOF_BYTES + 1),
    ],
)
def test_unreadable_file_withdraws_nothing_and_says_so(tmp_path, proofs, reports, content):
    path = tmp_path / "matches" / PROOFS_NAME
    path.parent.mkdir()
    path.write_bytes(content)

    assert proofs.failed_sides(PAIR, HOOK) == []
    assert PROOFS_NAME in reports[0]


def test_write_failure_is_reported_not_raised(tmp_path, reports):
    (tmp_path / "fichier").write_text("x", encoding="utf-8")
    proofs = HookProofs(tmp_path / "fichier" / PROOFS_NAME, reports.append)

    proofs.prove(PAIR, HOOK)

    assert PROOFS_NAME in reports[-1]


def test_file_stays_bounded_and_keeps_the_latest_pairs(tmp_path, proofs):
    for index in range(MAX_PAIRS + 5):
        proofs.fail(f"build{index}|mod", HOOK, "A")

    stored = json.loads((tmp_path / "matches" / PROOFS_NAME).read_text(encoding="utf-8"))
    assert len(stored) == MAX_PAIRS
    assert f"build{MAX_PAIRS + 4}|mod" in stored
    assert "build0|mod" not in stored


def test_eviction_follows_age_not_the_alphabet(tmp_path, proofs):
    """Des couples écrits en ordre alphabétique inverse : le plus vieux sort, pas le premier nommé."""
    names = [f"build{index:02}|mod" for index in range(MAX_PAIRS + 1, 0, -1)]
    for name in names:
        proofs.fail(name, HOOK, "A")

    stored = json.loads((tmp_path / "matches" / PROOFS_NAME).read_text(encoding="utf-8"))
    assert list(stored) == names[-MAX_PAIRS:]
