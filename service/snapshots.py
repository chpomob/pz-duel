"""Snapshots des mondes `duelA` et `duelB` : capture, restauration, passe de création.

Un snapshot couvre `Saves/Multiplayer/duelX/` et `db/duelX.db`, sous `duel-snapshots/`.
Il n'apparaît sous son nom que par un `rename` final : sa présence vaut donc sa complétude.

Capture et restauration portent sur toute la paire. Tout est copié avant le premier
`rename` : une panne de copie, d'un côté comme de l'autre, ne touche à aucun monde, et le
service ne se retrouve jamais avec deux mondes d'origine différente.

Une passe de création pose un marqueur avant de lancer les serveurs et ne le retire qu'une
fois sa capture faite. Un monde trouvé avec ce marqueur vient donc d'une passe inachevée,
même si le service a été tué : il n'est jamais capturé, il est invalidé. Sans marqueur, un
monde existant reste celui de l'utilisateur, et il est capturé.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

from .platforms import Platform, current, replace_file, sync_disk
from .preflight import check_ports
from .rules import load_runtime
from .servers import ServerPair, ServerProcess, server_name

SNAPSHOTS_DIR = "duel-snapshots"
WORLDS_DIR = Path("Saves") / "Multiplayer"
DB_DIR = "db"
SNAPSHOT_WORLD = "world"
STAGED = "new"
REPLACED = "old"
CREATION_MARKER = ".creation-en-cours"
SNAPSHOT_MODE = 0o700

Copy = Callable[[Path, Path], None]
Rename = Callable[[Path, Path], None]


class SnapshotError(Exception):
    """Refus ou échec : le message, en français, arrête le lancement tel quel."""


def copy_path(src: Path, dst: Path) -> None:
    """Copie un dossier ou un fichier ; `dst` ne doit pas exister."""
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def remove_path(path: Path) -> None:
    """Retire un dossier ou un fichier, absent compris ; un lien n'est pas suivi."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _switch(moves: Iterable[tuple[Path, Path, Path]], rename: Rename) -> None:
    """Fait les `rename` de `(préparé, cible, ancien)` ; une panne les défait tous."""
    done: list[tuple[Path, Path]] = []
    try:
        for staged, target, replaced in moves:
            if os.path.lexists(target):
                rename(target, replaced)
                done.append((replaced, target))
            rename(staged, target)
            done.append((target, staged))
    except OSError as exc:
        try:
            for moved, origin in reversed(done):
                rename(moved, origin)
        except OSError as undo_exc:
            raise SnapshotError(
                f"bascule interrompue ({exc}) et non défaite ({undo_exc}) : "
                f"{moved} n'a pas retrouvé sa place {origin}"
            ) from undo_exc
        raise


def _discard(stages: Iterable[Path]) -> None:
    for stage in stages:
        shutil.rmtree(stage, ignore_errors=True)


class SnapshotStore:
    """Snapshots sous `zomboid_dir`. `ports` vaut par défaut ceux de `runtime.json`.

    `copy(src, dst)` est injectable pour simuler une panne à mi-copie. `policy` et `platform`
    vont à `replace_file` : voir `service/platforms.py`.
    """

    def __init__(
        self,
        zomboid_dir: str | os.PathLike[str],
        *,
        ports: Mapping[str, Mapping[str, int]] | None = None,
        copy: Copy = copy_path,
        policy: Mapping[str, Any] | None = None,
        platform: Platform | None = None,
    ) -> None:
        self._zomboid_dir = Path(zomboid_dir)
        self._root = self._zomboid_dir / SNAPSHOTS_DIR
        self._marker = self._root / CREATION_MARKER
        self._ports = load_runtime()["ports"] if ports is None else ports
        self._copy = copy
        self._policy = policy
        self._platform = current() if platform is None else platform

    def has(self, side: str) -> bool:
        return self._snapshot(side).is_dir()

    def has_world(self, side: str) -> bool:
        """Vrai dès qu'une trace du monde existe : une moitié de monde n'est pas un vide."""
        return any(os.path.lexists(path) for path in self._world_parts(side))

    def creation_pending(self) -> bool:
        return os.path.lexists(self._marker)

    def begin_creation(self) -> None:
        """Pose le marqueur, durable avant que le premier serveur n'écrive son monde."""
        self._root.mkdir(parents=True, exist_ok=True)
        self._marker.touch()
        sync_disk([self._marker], platform=self._platform)

    def end_creation(self) -> None:
        self._marker.unlink(missing_ok=True)

    def discard_creation(self, pair: ServerPair) -> None:
        """Efface ce qu'une passe inachevée a créé, serveurs arrêtés et ports libres.

        Une passe ne démarre que sans aucun monde : ceux-ci ne peuvent venir que d'elle.
        Le marqueur part en dernier : une panne ici sera reprise au lancement suivant.
        """
        self._require_stopped(pair, "invalidation")
        try:
            for side in pair.servers:
                for part in self._world_parts(side):
                    remove_path(part)
            self.end_creation()
        except OSError as exc:
            raise SnapshotError(f"invalidation impossible : {exc}") from exc

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
        running = [side for side, server in pair.servers.items() if server.running]
        if running:
            raise SnapshotError(f"{action} refusée : serveur {', '.join(running)} en marche")
        game_ports = {side: {"game": self._ports[side]["game"]} for side in pair.servers}
        _, failures = check_ports(game_ports)
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
        Il ne s'efface donc que là où l'ancien est sûrement ailleurs : jamais par défaut.
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
            sync_disk([staged for staged, _, _ in moves], platform=self._platform)
            _switch(moves, partial(replace_file, policy=self._policy, platform=self._platform))
        except SnapshotError:
            # Bascule non défaite : ces dossiers tiennent peut-être le seul ancien monde.
            raise
        except OSError:
            _discard(stages)
            raise
        _discard(stages)


def prepare_worlds(
    pair: ServerPair, store: SnapshotStore, *, prepare_launch: Callable[[], bool | None]
) -> None:
    """Amène les deux mondes à une même origine, ou refuse le lancement (`SnapshotError`).

    `prepare_launch` n'est appelé que par la passe de création : l'appelant le rejoue
    après ce retour, sur tous les chemins (D23). Les serveurs sont alors arrêtés.
    Il rend `False` quand un fichier qu'il doit régler manque encore (le `SandboxVars`
    d'une installation neuve) : voir `_creation_pass`.
    """
    sides = list(pair.servers)
    snapshots = [side for side in sides if store.has(side)]
    worlds = [side for side in sides if store.has_world(side)]
    if len(snapshots) == len(sides):
        # Un marqueur ici dit seulement qu'une coupure a suivi la capture : elle est complète.
        store.end_creation()
        store.restore(pair)
    elif snapshots:
        raise SnapshotError(
            f"lancement refusé : snapshot présent seulement pour {', '.join(snapshots)}"
        )
    elif store.creation_pending():
        store.discard_creation(pair)
        raise SnapshotError(
            "lancement refusé : monde laissé par une passe de création interrompue, invalidé "
            "sans capture ; relancez le duel pour recréer les deux mondes"
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
    pair: ServerPair, store: SnapshotStore, prepare_launch: Callable[[], bool | None]
) -> None:
    """Crée les deux mondes avec les réglages du match, puis les capture.

    Un monde garde ce que son `SandboxVars` disait à sa création (date de départ,
    population…). Sur une installation neuve, ce fichier n'existe pas encore : PZ l'écrit
    à son premier départ, avec ses défauts. Une passe d'amorçage le fait donc naître, son
    monde est invalidé sans capture, et la vraie passe crée les mondes une fois réglés (P9F15).
    """
    if prepare_launch() is False:
        _run_pass(pair, store, " (amorçage du sandbox)")
        store.discard_creation(pair)
        if prepare_launch() is False:
            raise SnapshotError(
                "lancement refusé : réglages du monde toujours inapplicables après "
                "l'amorçage du sandbox (SandboxVars absent)"
            )
    _run_pass(pair, store, "")
    try:
        store.capture(pair)
    except SnapshotError as exc:
        store.discard_creation(pair)
        raise SnapshotError(f"{exc} ; monde partiel invalidé") from exc
    store.end_creation()


def _run_pass(pair: ServerPair, store: SnapshotStore, stage: str) -> None:
    """Démarre puis arrête la paire sous le marqueur ; sur échec, invalide et refuse.

    L'amorçage est une étape de la passe de création : son échec en porte le nom.
    """
    label = f"passe de création en échec{stage}"
    store.begin_creation()
    ready = pair.start() and pair.wait_ready()
    # Aussi après un échec ou une annulation : au retour, plus aucun serveur ne tourne.
    stopped = pair.stop()
    failure = None if ready else f"{label} : {pair.failure or 'interrompue'}"
    if not stopped:
        # Rien ne s'efface sous un serveur : le marqueur reste, le lancement suivant reprend.
        raise SnapshotError(failure or f"{label} : arrêt des serveurs incomplet")
    if failure is not None:
        store.discard_creation(pair)
        raise SnapshotError(f"{failure} ; monde partiel invalidé")
