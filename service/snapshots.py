"""Snapshots des mondes `duelA` et `duelB` : capture, restauration, passe de création.

Un snapshot couvre `Saves/Multiplayer/duelX/` et `db/duelX.db`, sous `duel-snapshots/`.
Il n'apparaît sous son nom que par un `rename` final : sa présence vaut donc sa complétude.

Capture et restauration portent sur toute la paire. Tout est copié avant le premier
`rename` : une panne de copie, d'un côté comme de l'autre, ne touche à aucun monde, et le
service ne se retrouve jamais avec deux mondes d'origine différente.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from functools import partial
from pathlib import Path

from .preflight import _check_ports
from .rules import load_runtime
from .servers import ServerPair, ServerProcess, server_name

SNAPSHOTS_DIR = "duel-snapshots"
WORLDS_DIR = Path("Saves") / "Multiplayer"
DB_DIR = "db"
SNAPSHOT_WORLD = "world"
STAGED = "new"
REPLACED = "old"
SNAPSHOT_MODE = 0o700

Copy = Callable[[Path, Path], None]


class SnapshotError(Exception):
    """Refus ou échec : le message, en français, arrête le lancement tel quel."""


def copy_path(src: Path, dst: Path) -> None:
    """Copie un dossier ou un fichier ; `dst` ne doit pas exister."""
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _is_running(server: ServerProcess) -> bool:
    """Le groupe de processus décide, pas le script : `java` peut lui survivre.

    `ServerProcess` n'expose rien de public pour cela : `exit_code` vaut `None` aussi bien
    pour un serveur jamais lancé que pour un serveur qui tourne, et `stop` l'arrêterait.
    """
    return server._process is not None and server._group_alive()


def _switch(moves: Iterable[tuple[Path, Path, Path]]) -> None:
    """Fait les `rename` de `(préparé, cible, ancien)` ; une panne les défait tous."""
    done: list[tuple[Path, Path]] = []
    try:
        for staged, target, replaced in moves:
            if os.path.lexists(target):
                os.rename(target, replaced)
                done.append((replaced, target))
            os.rename(staged, target)
            done.append((target, staged))
    except OSError as exc:
        try:
            for moved, origin in reversed(done):
                os.rename(moved, origin)
        except OSError as undo_exc:
            raise SnapshotError(
                f"bascule interrompue ({exc}) et non défaite ({undo_exc}) : "
                f"{moved} n'a pas retrouvé sa place {origin}"
            ) from undo_exc
        raise


class SnapshotStore:
    """Snapshots sous `zomboid_dir`. `ports` vaut par défaut ceux de `runtime.json`.

    `copy(src, dst)` est injectable pour simuler une panne à mi-copie.
    """

    def __init__(
        self,
        zomboid_dir: str | os.PathLike[str],
        *,
        ports: Mapping[str, Mapping[str, int]] | None = None,
        copy: Copy = copy_path,
    ) -> None:
        self._zomboid_dir = Path(zomboid_dir)
        self._root = self._zomboid_dir / SNAPSHOTS_DIR
        self._ports = load_runtime()["ports"] if ports is None else ports
        self._copy = copy

    def has(self, side: str) -> bool:
        return self._snapshot(side).is_dir()

    def has_world(self, side: str) -> bool:
        """Vrai dès qu'une trace du monde existe : une moitié de monde n'est pas un vide."""
        return any(os.path.lexists(path) for path in self._world_parts(side))

    def capture(self, pair: ServerPair) -> None:
        """Capture les deux mondes, serveurs arrêtés, sans jamais écraser un snapshot."""
        self._require_stopped(pair, "capture")
        for side in pair.servers:
            if os.path.lexists(self._snapshot(side)):
                raise SnapshotError(f"capture refusée : un snapshot de {side} existe déjà")
            missing = [p.name for p in self._world_parts(side) if not os.path.lexists(p)]
            if missing:
                names = ", ".join(missing)
                raise SnapshotError(f"capture refusée : monde {side} incomplet, manque {names}")
        items = [(self._snapshot(side), partial(self._fill, side)) for side in pair.servers]
        try:
            self._install(items)
        except OSError as exc:
            raise SnapshotError(f"capture impossible : {exc}") from exc

    def restore(self, pair: ServerPair) -> None:
        """Rend aux deux mondes le contenu de leur snapshot, serveurs arrêtés et ports libres."""
        self._require_stopped(pair, "restauration")
        items = []
        for side in pair.servers:
            if not self.has(side):
                raise SnapshotError(f"restauration refusée : aucun snapshot de {side}")
            world, database = self._world_parts(side)
            snapshot = self._snapshot(side)
            items.append((world, partial(self._copy, snapshot / SNAPSHOT_WORLD)))
            items.append((database, partial(self._copy, snapshot / database.name)))
        try:
            self._install(items)
        except OSError as exc:
            raise SnapshotError(f"restauration impossible, monde intact : {exc}") from exc

    def _snapshot(self, side: str) -> Path:
        return self._root / server_name(side)

    def _world_parts(self, side: str) -> tuple[Path, Path]:
        name = server_name(side)
        return (
            self._zomboid_dir / WORLDS_DIR / name,
            self._zomboid_dir / DB_DIR / f"{name}.db",
        )

    def _require_stopped(self, pair: ServerPair, action: str) -> None:
        running = [side for side, server in pair.servers.items() if _is_running(server)]
        if running:
            raise SnapshotError(f"{action} refusée : serveur {', '.join(running)} en marche")
        game_ports = {side: {"game": self._ports[side]["game"]} for side in pair.servers}
        _, failures = _check_ports(game_ports)
        if failures:
            raise SnapshotError(f"{action} refusée : {' '.join(failures)}")

    def _fill(self, side: str, staged: Path) -> None:
        staged.mkdir(SNAPSHOT_MODE)
        world, database = self._world_parts(side)
        self._copy(world, staged / SNAPSHOT_WORLD)
        self._copy(database, staged / database.name)

    def _install(self, items: Iterable[tuple[Path, Callable[[Path], None]]]) -> None:
        """Prépare chaque cible à côté d'elle, puis bascule le tout.

        Le dossier temporaire partage le parent de sa cible, donc son système de fichiers.
        Il reçoit aussi l'ancien contenu, qui ne disparaît qu'avec lui, après la bascule.
        """
        stages: list[Path] = []
        try:
            moves = []
            for target, fill in items:
                target.parent.mkdir(parents=True, exist_ok=True)
                stage = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}."))
                stages.append(stage)
                fill(stage / STAGED)
                moves.append((stage / STAGED, target, stage / REPLACED))
            # Sans cela, une coupure pourrait garder le `rename` et perdre les données.
            os.sync()
            _switch(moves)
        except SnapshotError:
            # Bascule non défaite : ces dossiers tiennent peut-être le seul ancien monde.
            stages.clear()
            raise
        finally:
            for stage in stages:
                shutil.rmtree(stage, ignore_errors=True)


def prepare_worlds(
    pair: ServerPair, store: SnapshotStore, *, prepare_launch: Callable[[], None]
) -> None:
    """Amène les deux mondes à une même origine, ou refuse le lancement (`SnapshotError`).

    `prepare_launch` n'est appelé que par la passe de création : l'appelant le rejoue
    après ce retour, sur tous les chemins (D23). Les serveurs sont alors arrêtés.
    """
    sides = list(pair.servers)
    snapshots = [side for side in sides if store.has(side)]
    worlds = [side for side in sides if store.has_world(side)]
    if len(snapshots) == len(sides):
        store.restore(pair)
    elif snapshots:
        raise SnapshotError(
            f"lancement refusé : snapshot présent seulement pour {', '.join(snapshots)}"
        )
    elif len(worlds) == len(sides):
        store.capture(pair)
    elif worlds:
        raise SnapshotError(
            f"lancement refusé : monde présent seulement pour {', '.join(worlds)}, sans "
            "snapshot ; une passe de création interrompue laisse aussi cet état"
        )
    else:
        _creation_pass(pair, store, prepare_launch)


def _creation_pass(
    pair: ServerPair, store: SnapshotStore, prepare_launch: Callable[[], None]
) -> None:
    prepare_launch()
    ready = pair.start() and pair.wait_ready()
    # Aussi après un échec ou une annulation : au retour, plus aucun serveur ne tourne.
    stopped = pair.stop()
    if not ready:
        raise SnapshotError(f"passe de création en échec : {pair.failure or 'interrompue'}")
    if not stopped:
        raise SnapshotError("passe de création en échec : arrêt des serveurs incomplet")
    store.capture(pair)
