"""Journal JSONL d'un match : `<dossier du match>/journal.jsonl`.

`Journal` attribue `n` et `ts_ms`, et chaque ligne part vers le système en un seul
`write`. Le fichier est créé par son `Journal` : un second écrivain repartirait de
`n`=1 dans le même fichier. Ce module ne lit pas l'horloge lui-même, car le rejeu
importe `read_journal` et ne doit pas dépendre du temps système.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from .protocol import dump_line

JOURNAL_VERSION = 1
JOURNAL_NAME = "journal.jsonl"
FIRST_N = 1
SYNCED_TYPE = "state"
# Comparés sans casse à une partie de la clé : `RCONPassword` et `admin_password`
# tombent aussi. Le mot de passe joueur reste hors du journal, comme les deux autres.
FORBIDDEN_FIELDS = ("password",)


class JournalError(ValueError):
    """Écriture refusée ou journal illisible ; le message ne cite jamais une valeur."""


class JournalContent(NamedTuple):
    """`incomplete` porte les octets de la dernière ligne coupée, sinon `None`."""

    records: list[dict[str, Any]]
    incomplete: bytes | None


def _forbidden_key(value: Any) -> str | None:
    """Rend la première clé interdite, à toute profondeur de `value`."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if any(field in name.lower() for field in FORBIDDEN_FIELDS):
                return name
            if (found := _forbidden_key(item)) is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            if (found := _forbidden_key(item)) is not None:
                return found
    return None


class Journal:
    """Écrivain unique d'un journal, partagé entre `pump` et le fil HTTP.

    `clock` rend l'heure murale en millisecondes. Une ligne refusée ou impossible à
    sérialiser ne consomme pas de `n`. Le verrou couvre `n`, `ts_ms` et l'écriture,
    `fsync` compris : l'ordre des lignes est celui de `n`.

    Une ligne coupée par une panne d'écriture reste au plus en fin de fichier, où
    `read_journal` la sépare : la ligne suivante la retire avant de s'écrire.
    """

    def __init__(self, dir: str | os.PathLike[str], clock: Callable[[], float]) -> None:
        directory = Path(dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / JOURNAL_NAME
        self._clock = clock
        self._lock = threading.Lock()
        self._next_n = FIRST_N
        self._end = 0
        self._torn = False
        self._file = open(self.path, "xb", buffering=0)

    def write(
        self,
        type: str,
        data: Mapping[str, Any],
        side: str | None = None,
        mt: float | None = None,
    ) -> dict[str, Any]:
        """Ajoute une ligne et rend l'enregistrement écrit.

        Un `state` est suivi d'un `fsync` : un changement de phase survit à une coupure.
        """
        if (key := _forbidden_key(data)) is not None:
            raise JournalError(f"champ interdit au journal : `{key}` (type `{type}`)")
        with self._lock:
            record = {
                "v": JOURNAL_VERSION,
                "n": self._next_n,
                "ts_ms": int(self._clock()),
                "mt": mt,
                "type": type,
                "side": side,
                "data": data,
            }
            line = dump_line(record)
            self._append(line)
            self._next_n += 1
            if type == SYNCED_TYPE:
                os.fsync(self._file.fileno())
        return record

    def _append(self, line: bytes) -> None:
        """Écrit `line` après la dernière ligne entière ; sous le verrou de `write`."""
        if self._torn:
            os.ftruncate(self._file.fileno(), self._end)
            self._file.seek(self._end)
            self._torn = False
        self._torn = True
        written = self._file.write(line)
        if written != len(line):
            raise OSError(f"écriture partielle du journal : {written} octets sur {len(line)}")
        self._torn = False
        self._end += written

    def close(self) -> None:
        with self._lock:
            self._file.close()


def _decode(line: bytes) -> dict[str, Any] | None:
    try:
        record = json.loads(line.decode("utf-8"))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def read_journal(path: str | os.PathLike[str]) -> JournalContent:
    """Lit un journal et sépare sa dernière ligne si elle est incomplète.

    La lecture reste en octets jusqu'au découpage : une coupure peut tomber au milieu
    d'un caractère. Une dernière ligne sans `\\n`, ou illisible, n'est pas une erreur.
    Une ligne illisible ailleurs ne vient pas d'une coupure : elle lève `JournalError`.
    """
    *lines, tail = Path(path).read_bytes().split(b"\n")
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        record = _decode(line)
        if record is not None:
            records.append(record)
        elif number == len(lines) and not tail:
            return JournalContent(records, line)
        else:
            raise JournalError(f"ligne {number} du journal illisible")
    return JournalContent(records, tail or None)
