"""Transport par fichiers du contrat v1 : lecture de `out`.

`OutReader` suit un fichier `duel_X_out.txt` en mode binaire : l'offset compte des
octets, jamais des caractères, et ne franchit que des lignes complètes. Une ligne
n'arrête jamais la lecture : elle devient un événement ou une anomalie.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from .protocol import SIDE_MISMATCH, ProtocolError, parse_event

BAD_LINE = "bad_line"
DUP = "dup"
SEQ_GAP = "seq_gap"


@dataclass(frozen=True)
class Anomaly:
    """Anomalie de transport : `type` est le type de journal, `side` le camp du fichier."""

    type: str
    side: str
    data: Mapping[str, Any]


class PollResult(NamedTuple):
    events: list[Mapping[str, Any]]
    anomalies: list[Anomaly]


class OutReader:
    """Lit les nouvelles lignes de `out` et garde offset et couple `(boot, seq)`.

    Une ligne passe, dans l'ordre : le contrat (`parse_event`), le camp du fichier,
    puis la déduplication. Seule une ligne acceptée déplace `last`, si bien qu'une
    ligne rejetée ne peut ni masquer ni créer un doublon.
    """

    def __init__(self, path: str | os.PathLike[str], side: str, catalog: Mapping[str, Any]) -> None:
        self.path = Path(path)
        self.side = side
        self.catalog = catalog
        self.offset = 0
        self.last: tuple[int, int] | None = None

    def poll(self) -> PollResult:
        """Traite les lignes complètes ajoutées ; le reste attend la lecture suivante."""
        result = PollResult([], [])
        chunk = self._read_from_offset()
        complete = chunk[: chunk.rfind(b"\n") + 1]
        for line in complete.split(b"\n")[:-1]:
            self._take(line, result)
            self.offset += len(line) + 1
        return result

    def _read_from_offset(self) -> bytes:
        try:
            with open(self.path, "rb") as handle:
                if os.fstat(handle.fileno()).st_size < self.offset:
                    self.offset = 0
                handle.seek(self.offset)
                return handle.read()
        except FileNotFoundError:
            # Un fichier disparu vaut un fichier vide : celui qui le remplacera se lit
            # depuis son début, et la déduplication écarte ce qui serait déjà vu.
            self.offset = 0
            return b""

    def _take(self, line: bytes, result: PollResult) -> None:
        try:
            event = parse_event(line, self.catalog)
        except ProtocolError as exc:
            kind = SIDE_MISMATCH if exc.code == SIDE_MISMATCH else BAD_LINE
            self._report(result, kind, offset=self.offset, code=exc.code, detail=exc.detail)
            return
        if event["src"] != self.side:
            detail = f"`src` {event['src']!r} dans le fichier du camp {self.side!r}"
            self._report(result, SIDE_MISMATCH, offset=self.offset, code=SIDE_MISMATCH, detail=detail)
            return
        boot, seq = event["boot"], event["seq"]
        if self.last is not None:
            if (boot, seq) <= self.last:
                self._report(result, DUP, boot=boot, seq=seq)
                return
            last_boot, last_seq = self.last
            if boot == last_boot and seq > last_seq + 1:
                self._report(result, SEQ_GAP, boot=boot, after=last_seq, seq=seq, missing=seq - last_seq - 1)
        self.last = (boot, seq)
        result.events.append(event)

    def _report(self, result: PollResult, kind: str, **data: Any) -> None:
        result.anomalies.append(Anomaly(kind, self.side, data))
