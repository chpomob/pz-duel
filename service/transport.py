"""Transport par fichiers du contrat v1 : `out`, `in`, `ack` et `session`.

`OutReader` suit un fichier `duel_X_out.txt` en mode binaire : l'offset compte des
octets, jamais des caractères, et ne franchit que des lignes complètes. Une ligne
n'arrête jamais la lecture : elle devient un événement ou une anomalie. Une passe lit
`POLL_MAX` octets et `POLL_RECORDS` lignes au plus, et une ligne de plus de `RECORD_MAX`
octets ne s'accumule pas. `drain` enchaîne `DRAIN_PASSES` de ces passes au plus : au-delà,
`behind` dit à qui doit décider sur tout ce qui est écrit que sa lecture n'est pas finie.

`CommandChannel` porte le sens service → mod et la garantie « au plus une fois ».
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from .protocol import (
    PROTOCOL_VERSION,
    SIDE_MISMATCH,
    ProtocolError,
    dump_line,
    parse_ack,
    parse_event,
    parse_session,
)

BAD_LINE = "bad_line"
DUP = "dup"
SEQ_GAP = "seq_gap"
ACK_AHEAD = "ack_ahead"
ACK_FOREIGN_BOOT = "ack_foreign_boot"
ACK_TIMEOUT = "ack_timeout"
LOST_ON_RESTART = "lost_on_restart"
LINE_TOO_LONG = "line_too_long"
# Une ligne du contrat tient en quelques centaines d'octets : ce plafond n'arrête qu'une panne.
RECORD_MAX = 1 << 16
# Doit dépasser `RECORD_MAX` : une ligne admise se voit alors entière à la passe qui la commence.
POLL_MAX = 1 << 20
# Une passe de lignes du contrat en compte moins : ce plafond n'arrête qu'une rafale de `\n`.
POLL_RECORDS = 1 << 14
# Le travail d'un `drain`, donc d'une passe de `pump` : le retard se résorbe sur les suivantes.
DRAIN_PASSES = 4


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

    Une ligne trop longue se signale une fois puis se jette jusqu'à son `\\n`, sans rien
    garder : `offset` la franchit au fil des passes, et elle n'est jamais relue.
    """

    def __init__(self, path: str | os.PathLike[str], side: str, catalog: Mapping[str, Any]) -> None:
        self.path = Path(path)
        self.side = side
        self.catalog = catalog
        self.offset = 0
        self.last: tuple[int, int] | None = None
        self._skipping = False
        self._goal = 0

    @property
    def behind(self) -> bool:
        """Vrai si le dernier `drain` a épuisé son budget avant la fin de ce qu'il visait."""
        return self._goal > 0

    def poll(self) -> PollResult:
        """Traite les lignes complètes ajoutées ; le reste attend la lecture suivante."""
        result = PollResult([], [])
        chunk = self._read_from_offset()
        if self._skipping:
            chunk = self._skip_long_line(chunk)
        start = 0
        for _ in range(POLL_RECORDS):
            end = chunk.find(b"\n", start)
            if end < 0:
                break
            if end - start > RECORD_MAX:
                self._report_long_line(result)
            else:
                self._take(chunk[start:end], result)
            self.offset += end - start + 1
            start = end + 1
        else:
            return result  # Plafond atteint : le reste se relit à la passe suivante.
        tail = len(chunk) - start
        if tail > RECORD_MAX:
            self._report_long_line(result)
            self.offset += tail
            self._skipping = True
        return result

    def drain(self) -> Iterator[PollResult]:
        """Rend passe par passe ce que `out` contient, `DRAIN_PASSES` passes au plus.

        Chaque lot a les bornes de `poll`, et l'appel entier celles de son budget, quelle que
        soit la taille du fichier. Budget épuisé, `behind` reste vrai et l'appel suivant
        reprend vers le même repère : la taille vue par le premier. Ainsi, un écrivain qui
        continue ne retient pas le lecteur, même sur plusieurs appels. Une passe sans progrès
        clôt aussi la lecture : il ne reste alors qu'une ligne inachevée.
        """
        self._goal = self._goal or self._size()
        for _ in range(DRAIN_PASSES):
            before = self.offset
            yield self.poll()
            if self.offset >= self._goal or self.offset == before:
                self._goal = 0
                return

    def aim_at_end(self) -> None:
        """Fait viser au prochain `drain` la fin de `out` : un repère ancien en ignorerait."""
        self._goal = 0

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _skip_long_line(self, chunk: bytes) -> bytes:
        """Jette `chunk` jusqu'au `\\n` de la ligne trop longue et rend ce qui le suit."""
        end = chunk.find(b"\n")
        if end < 0:
            self.offset += len(chunk)
            return b""
        self.offset += end + 1
        self._skipping = False
        return chunk[end + 1 :]

    def _report_long_line(self, result: PollResult) -> None:
        detail = f"ligne de plus de {RECORD_MAX} octets écartée"
        self._report(result, BAD_LINE, offset=self.offset, code=LINE_TOO_LONG, detail=detail)

    def _read_from_offset(self) -> bytes:
        try:
            with open(self.path, "rb") as handle:
                if os.fstat(handle.fileno()).st_size < self.offset:
                    self.offset = 0
                    self._skipping = False
                handle.seek(self.offset)
                return handle.read(POLL_MAX)
        except FileNotFoundError:
            # Un fichier disparu vaut un fichier vide : celui qui le remplacera se lit
            # depuis son début, et la déduplication écarte ce qui serait déjà vu.
            self.offset = 0
            self._skipping = False
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


class CommandChannel:
    """Écrit `in` et `session`, lit `ack`, pour un camp ; le service est seul écrivain.

    « Au plus une fois » repose sur un fil unique : `write`, `poll_ack` et
    `prepare_restart` tournent tous sur celui de `pump`. Rien ne s'écrit ainsi entre
    la lecture de `ack` et la troncature, et aucun verrou ne le garantit à leur place.
    Avant `prepare_restart`, l'appelant vide `out` : un `cmd_result` déjà écrit mais
    non lu deviendrait sinon un faux `lost_on_restart`.
    """

    def __init__(
        self,
        side: str,
        lua_dir: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        lua_dir = Path(lua_dir)
        self.side = side
        self.out_path = lua_dir / f"duel_{side}_out.txt"
        self.in_path = lua_dir / f"duel_{side}_in.txt"
        self.ack_path = lua_dir / f"duel_{side}_ack.txt"
        self.session_path = lua_dir / f"duel_{side}_session.txt"
        self.match: str | None = None
        self.boot: int | None = None
        self.last_seq = 0
        self._clock = clock
        self._awaiting_ack: dict[int, float] = {}
        self._in_has_lines = False
        self._reported_ack: tuple[int, int] | None = None

    def write(self, command: Mapping[str, Any]) -> None:
        """Ajoute une commande à `in`, `\\n` compris, en un seul `write` suivi d'un `fsync`.

        `last_seq` avance dès le `write` : même si la suite échoue, `cmd_floor` couvrira
        une ligne que le mod a pu lire.
        """
        seq = command["seq"]
        if seq <= self.last_seq:
            # Le mod ignorerait cette ligne sans rien dire.
            raise ValueError(f"`seq` {seq} non croissant : dernier écrit {self.last_seq}")
        line = dump_line(command)
        fd = os.open(self.in_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            written = os.write(fd, line)
            self.last_seq = seq
            self._in_has_lines = True
            self._awaiting_ack[seq] = self._clock()
            if written != len(line):
                raise OSError(f"écriture partielle de `in` : {written} octets sur {len(line)}")
            os.fsync(fd)
        finally:
            os.close(fd)

    def poll_ack(self) -> list[Anomaly]:
        """Lit `ack`, tronque `in` s'il couvre tout ce qui est écrit, rend les anomalies.

        Le mod réécrit `ack` sans atomicité : vide ou illisible, il vaut « pas d'ack »,
        et la lecture suivante le reverra. Comme il reste en place et se relit à chaque
        passage, une même valeur anormale n'est rendue qu'une fois.
        """
        try:
            ack = parse_ack(self.ack_path.read_bytes())
        except (OSError, ProtocolError):
            return []
        boot, seq = ack["boot"], ack["seq"]
        if boot != self.boot:
            return self._report_ack(ACK_FOREIGN_BOOT, boot, seq, current_boot=self.boot)
        if seq > self.last_seq:
            return self._report_ack(ACK_AHEAD, boot, seq, last_seq=self.last_seq)
        self._reported_ack = None
        self._awaiting_ack = {key: at for key, at in self._awaiting_ack.items() if key > seq}
        if seq == self.last_seq and self._in_has_lines:
            self._truncate_in()
        return []

    def check_ack_timeout(self, ack_timeout_s: float) -> list[Anomaly]:
        """Rend une fois chaque commande sans ack depuis `ack_timeout_s` ; rien n'est réémis."""
        now = self._clock()
        expired = [seq for seq, at in self._awaiting_ack.items() if now - at >= ack_timeout_s]
        for seq in expired:
            del self._awaiting_ack[seq]
        return [Anomaly(ACK_TIMEOUT, self.side, {"seq": seq}) for seq in expired]

    def write_session(self, match: str, boot: int, cmd_floor: int) -> None:
        """Remplace `session` d'un bloc et fait de `boot` le `boot` courant.

        Le mod ne traite aucune commande sur une session illisible : le contenu est
        contrôlé avant d'écrire, et un fichier temporaire évite tout état intermédiaire.
        """
        session = {
            "v": PROTOCOL_VERSION,
            "match": match,
            "side": self.side,
            "boot": boot,
            "cmd_floor": cmd_floor,
        }
        parse_session(session)
        temporary = self.session_path.with_name(self.session_path.name + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(dump_line(session))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.session_path)
        self.match = match
        self.boot = boot
        self.last_seq = cmd_floor
        self._awaiting_ack.clear()

    def prepare_restart(self, unresolved_seqs: Iterable[int]) -> list[Anomaly]:
        """Prépare une relance : `in` vidé, session à `boot` + 1, plancher au dernier `seq`.

        `unresolved_seqs` liste les commandes écrites sans `cmd_result` reçu. Elles sont
        rendues en `lost_on_restart` et ne sont jamais réécrites.
        """
        if self.match is None or self.boot is None:
            raise ValueError("redémarrage sans session écrite")
        self._truncate_in()
        self.write_session(self.match, self.boot + 1, self.last_seq)
        return [Anomaly(LOST_ON_RESTART, self.side, {"seq": seq}) for seq in unresolved_seqs]

    def reset(self) -> None:
        """Vide `out`, `in` et `ack` avant un lancement et oublie les commandes écrites.

        La session n'est pas touchée : `prepare_launch` peut l'écrire avant ou après.
        """
        for path in (self.out_path, self.in_path, self.ack_path):
            _truncate(path)
        self.last_seq = 0
        self._awaiting_ack.clear()
        self._in_has_lines = False
        self._reported_ack = None

    def _truncate_in(self) -> None:
        _truncate(self.in_path)
        self._in_has_lines = False

    def _report_ack(self, kind: str, boot: int, seq: int, **data: Any) -> list[Anomaly]:
        if self._reported_ack == (boot, seq):
            return []
        self._reported_ack = (boot, seq)
        return [Anomaly(kind, self.side, {"boot": boot, "seq": seq, **data})]


def _truncate(path: Path) -> None:
    """Ramène un fichier à 0 octet sans le supprimer, en le créant s'il manque."""
    with open(path, "wb") as handle:
        os.fsync(handle.fileno())
