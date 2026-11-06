"""Ce qu'on sait de la détection des blessures, par couple (build du jeu, empreinte du mod).

La preuve répond à une question sur le jeu et le mod, pas sur un match : elle s'établit une fois
et se relit à chaque lancement. `proven` dit qu'un signal a tiré : `hook_seen` ou `damage_taken`.
`failed` dit qu'un joueur est mort sans qu'aucun signal n'ait rien vu : seul ce constat retire
l'atome. Un couple inconnu ne retire rien, et un signal vu efface un échec. Fichier absent, illisible
ou écriture impossible : le match se joue comme sur un couple inconnu, et `report` le dit.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .protocol import SIDES

PROOFS_NAME = "hook_proofs.json"
PROVEN = "proven"
FAILED = "failed"
# Un couple pèse quelques centaines d'octets : au-delà, le fichier n'est pas le nôtre.
MAX_PROOF_BYTES = 65536
MAX_PAIRS = 32


class HookProofs:
    """Le fichier se relit à chaque question : il est petit, et le service n'y écrit qu'ici."""

    def __init__(self, path: Path, report: Callable[[str], None]) -> None:
        self._path = path
        self._report = report

    def failed_sides(self, pair: str, hook: str) -> list[str]:
        """Rend les camps de l'échec constaté, vide s'il n'y en a pas."""
        record = self._load().get(pair, {}).get(hook, {})
        return list(record.get("sides", ())) if record.get("status") == FAILED else []

    def prove(self, pair: str, hook: str) -> None:
        if self._load().get(pair, {}).get(hook, {}).get("status") != PROVEN:
            self._store(pair, hook, {"status": PROVEN})

    def fail(self, pair: str, hook: str, side: str) -> None:
        """Consigne un échec, sauf sur un couple prouvé : ce hook y a déjà tiré."""
        record = self._load().get(pair, {}).get(hook, {})
        if record.get("status") == PROVEN:
            return
        sides = sorted({*record.get("sides", ()), side})
        self._store(pair, hook, {"status": FAILED, "sides": sides})

    def _load(self) -> dict[str, dict[str, Mapping[str, Any]]]:
        try:
            if self._path.stat().st_size > MAX_PROOF_BYTES:
                raise ValueError(f"plus de {MAX_PROOF_BYTES} octets")
            loaded = json.loads(self._path.read_bytes())
            if not _well_formed(loaded):
                raise ValueError("forme inattendue")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self._report(f"preuves de hook ignorées, {self._path.name} : {exc}")
            return {}
        return loaded

    def _store(self, pair: str, hook: str, record: Mapping[str, Any]) -> None:
        """Remplace le fichier d'un bloc ; le couple touché passe en dernier, les plus vieux sortent."""
        known = self._load()
        hooks = {**known.pop(pair, {}), hook: record}
        pairs = [*known.items(), (pair, hooks)][-MAX_PAIRS:]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(dir=self._path.parent, prefix=f".{PROOFS_NAME}.")
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                # Sans tri : l'ordre du fichier est l'ancienneté que la prochaine écriture relit.
                json.dump(dict(pairs), out, ensure_ascii=False)
            os.replace(name, self._path)
        except OSError as exc:
            self._report(f"preuve de hook non enregistrée, {self._path.name} : {exc.strerror}")


def _well_formed(loaded: Any) -> bool:
    if not isinstance(loaded, dict):
        return False
    hooks = [hooks for hooks in loaded.values() if isinstance(hooks, dict)]
    if len(hooks) != len(loaded):
        return False
    return all(_well_formed_record(record) for by_hook in hooks for record in by_hook.values())


def _well_formed_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("status") == FAILED:
        sides = record.get("sides")
        return isinstance(sides, list) and all(side in SIDES for side in sides)
    return record.get("status") == PROVEN
