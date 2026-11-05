"""Orchestration d'un duel, en mode simulé ou réel.

En mock, aucun serveur PZ n'est lancé : la machine à états, les latences, les événements
et le score sont simulés pour valider l'expérience utilisateur, et tout ce qui ressemble
à une vérification s'annonce « simulé ». En réel, le mode demandé est tenu de bout en
bout : un échec finit en `error`, jamais dans le parcours simulé.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import random
import shutil
import socket
import string
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from .journal import Journal, read_journal
from .preflight import run_preflight
from .protocol import PROTOCOL_VERSION, SIDES
from .rules import (
    MS_PER_S,
    RESTARTING,
    MatchCore,
    TableError,
    load_effects,
    load_runtime,
    load_table,
    rules_hash,
)
from .servers import (
    LINE_BREAK,
    STATUS_ERROR,
    STATUS_UP,
    Redactor,
    ServerPair,
    ServerProcess,
    apply_status,
    gen_secret,
    mods_setting,
    server_name,
    verify_contract,
    write_ini,
    write_sandbox,
)
from .snapshots import SnapshotError, SnapshotStore, prepare_worlds
from .state import MatchState
from .transport import Anomaly, CommandChannel, OutReader
from .verdict import (
    APPLIED,
    BEAT,
    CAPS,
    CMD_RESULT,
    COMMAND,
    EVENT,
    HOOK_SEEN,
    INSUFFICIENT,
    KO,
    MATCH_END,
    MIN_SPAWNED,
    OK,
    PROBE_KIND,
    UNKNOWN,
    combined_spawn,
    fallback,
    publish_capabilities,
    verdict,
)

PRESETS_DIR = os.path.join(os.path.dirname(__file__), "presets")
MATCHES_DIR = Path(__file__).resolve().parent.parent / "matches"

ACTIONS = {"start", "cancel", "rematch", "simulate_join", "open_game"}
MODE_MOCK = "mock"
MODE_REAL = "real"
MODES = (MODE_MOCK, MODE_REAL)
STEAM_BINARY = "steam"
# L'hôte joue sur la machine du serveur A.
CLIENT_HOST = "127.0.0.1"
# L'invité joue sur le serveur B.
GUEST_SIDE = "B"
LUA_DIR = "Lua"
SERVER_DIR = "Server"
FIRST_BOOT = 1
FIRST_CMD_FLOOR = 0
STOP_INCOMPLETE = "arrêt incomplet : des processus de serveur subsistent"
# La fonction de jeu que `probe_spawn` met à l'épreuve dans `DuelCore.lua`.
PROBE_FUNCTION = "addZombiesInOutfit"
ATOM_WITHDRAWN = "atom_withdrawn"
STEP_CONTRACT = "contract"
STEP_LOBBY = "lobby"
STATUS_SUSPECT = "suspect"
OUTCOME_FORFEIT = "forfeit"
OUTCOME_INVALIDATED = "invalidated"
# Le `state` d'entrée en `running` ouvre le tick 0.
FIRST_MT = 0
DRIFT_DIGITS = 6
SECONDS_PER_MINUTE = 60

LOGGER = logging.getLogger(__name__)


class ActionRefused(Exception):
    """Refus d'une action : le message français part tel quel vers l'appelant."""


def load_presets(
    runtime: Mapping[str, Any] | None = None,
    effects: Mapping[str, Any] | None = None,
    report: Callable[[str], None] = LOGGER.error,
) -> list[dict]:
    """Rend les presets valides. Un preset invalide est écarté et signalé à `report`."""
    runtime = load_runtime() if runtime is None else runtime
    effects = load_effects() if effects is None else effects
    out = []
    for name in sorted(os.listdir(PRESETS_DIR)):
        stem, extension = os.path.splitext(name)
        if extension != ".json":
            continue
        try:
            with open(os.path.join(PRESETS_DIR, name), encoding="utf-8") as fh:
                raw = json.load(fh)
            out.append(load_table(raw, effects, runtime["limits"], file_stem=stem))
        except (OSError, ValueError) as exc:
            report(f"preset {name} écarté : {exc}")
    return out


def launch_steam(args: Sequence[str]) -> bool:
    """Lance Steam avec `args`, détaché du service. Rend faux si Steam est introuvable."""
    steam = shutil.which(STEAM_BINARY)
    if steam is None:
        return False
    try:
        subprocess.Popen(
            [steam, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def make_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(5))


def _check_ini_value(label: str, value: Any) -> None:
    """Refuse ce qui injecterait une clé dans le `.ini`, sans jamais citer la valeur."""
    if not isinstance(value, str) or LINE_BREAK.search(value) or value.startswith("="):
        raise ActionRefused(
            f"{label} refusé : texte sans retour à la ligne et sans « = » en tête attendu"
        )


class ContractWindow:
    """Fenêtre de contrat : ce que les camps ont prouvé, jusqu'à une décision figée.

    Ouverte au premier `caps`. `reach_lobby_ready` fixe `L`. `close` décide une fois pour
    tout le match : `caps` et `hook_seen` tardifs restent publiés, sans rien réactiver.
    `seen` est ce qui se publie, `_proven` ce qui décide : un `hook_seen` lu après
    `L` + `hook_window_s` entre dans le premier seulement, même si `close` n'a pas encore eu
    lieu, car `pump` lit `out` avant de juger l'échéance.
    """

    def __init__(
        self, table: Mapping[str, Any], catalog: Mapping[str, Any], hook_window_s: float
    ) -> None:
        self._proofs = {
            atom: spec["hook_proof"]
            for atom, spec in catalog["atoms"].items()
            if "hook_proof" in spec
        }
        self._required = sorted(
            {self._proofs[atom] for atom in table["atoms"] if atom in self._proofs}
        )
        self._window_s = hook_window_s
        self.caps: dict[str, Mapping[str, Any]] = {}
        self.seen: dict[str, set[str]] = {side: set() for side in SIDES}
        self._proven: dict[str, set[str]] = {side: set() for side in SIDES}
        self.ready_ms: int | None = None
        self.active_atoms: list[str] | None = None
        self.withdrawn: list[dict[str, Any]] = []

    @property
    def closed(self) -> bool:
        return self.active_atoms is not None

    def on_caps(self, side: str, data: Mapping[str, Any]) -> None:
        self.caps[side] = data

    def on_hook_seen(self, side: str, hook: str, now_ms: int) -> None:
        if not self.caps:
            return
        self.seen[side].add(hook)
        if self.ready_ms is None or now_ms <= self._deadline_ms():
            self._proven[side].add(hook)

    def reach_lobby_ready(self, now_ms: int) -> None:
        if self.ready_ms is None:
            self.ready_ms = now_ms

    def _missing(self, hook: str) -> list[str]:
        return [side for side in SIDES if hook not in self._proven[side]]

    def _deadline_ms(self) -> int:
        return self.ready_ms + self._window_s * MS_PER_S

    def due(self, now_ms: int) -> bool:
        """La clôture suit toujours `L` : un hook vu avant n'en dispense pas."""
        if self.closed or self.ready_ms is None:
            return False
        if not any(self._missing(hook) for hook in self._required):
            return True
        return now_ms >= self._deadline_ms()

    def close(self) -> None:
        """Retient les atomes communs aux deux `caps`, moins ceux dont le hook n'est pas prouvé."""
        common = set.intersection(*(set(self.caps[side]["atoms"]) for side in SIDES))
        for atom in sorted(common & self._proofs.keys()):
            hook = self._proofs[atom]
            if missing := self._missing(hook):
                self.withdrawn.append({"atom": atom, "hook": hook, "sides": missing})
        self.active_atoms = sorted(common - {item["atom"] for item in self.withdrawn})

    def detail(self, now_ms: int) -> str:
        """Détail de l'étape `contract` (D17), vide tant qu'aucun hook n'est attendu."""
        if self.closed:
            summary = f"atomes actifs : {', '.join(self.active_atoms)}"
            retired = [item["atom"] for item in self.withdrawn]
            return f"{summary} ; retirés : {', '.join(retired)}" if retired else summary
        waited = [
            f"hook {hook} attendu sur {', '.join(self._missing(hook))}"
            for hook in self._required
            if self._missing(hook)
        ]
        if not waited:
            return ""
        if self.ready_ms is None:
            limit = f"{self._window_s} s à compter de l'arrivée des deux joueurs"
        else:
            left_s = math.ceil((self._deadline_ms() - now_ms) / MS_PER_S)
            limit = f"temps restant : {left_s} s"
        return f"{' ; '.join(waited)} ; {limit}"


def _feed_line(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """Rend le texte et le camp d'une ligne du fil de match, `None` si elle n'y figure pas."""
    data, side = record["data"], record["side"]
    if record["type"] == COMMAND:
        command = data[COMMAND]
        units = f" × {command['n']}" if "n" in command else ""
        return f"{command['kind']}{units} vers {side}", side
    if record["type"] == EVENT and side is not None and data["kind"] not in (BEAT, CAPS, HOOK_SEEN):
        return f"{side} : {data['kind']}", side
    return None


class _Probe(NamedTuple):
    """`probe_spawn` d'un camp : `cause` dit pourquoi elle a échoué."""

    seq: int
    deadline_ms: int
    status: str = UNKNOWN
    cause: str = ""


class _Refusal(NamedTuple):
    """Arrêt du lancement : l'étape à mettre en `error` et le message."""

    step: str
    message: str


class RealMatch:
    """Un lancement réel, de la préparation à l'arrêt, dans l'ordre de D23.

    Le constructeur tire `match_id` et les secrets et arme le `Redactor` : la passe de
    création écrit déjà `server_X.log`. `on_status` suit `ServerPair` ; `stop` est l'événement
    d'annulation du moteur. `shutdown` est le seul chemin d'arrêt : `cancel`, erreur et fin.
    `on_state` applique une modification à l'état du moteur, sous son verrou.

    `_core` naît à l'entrée en `running` : c'est lui qui journalise alors tout ce qui compte
    pour le rejeu. `result` reste nul jusqu'à l'écriture de `match_end`.

    Une relance en match tourne sur son propre fil, qui ne touche que le serveur : son issue
    revient par `_relaunched`, que `pump` lit, seul à parler au cœur et aux canaux.
    `_relaunch_lock` ordonne le `start` de ce fil et `shutdown` : aucun serveur ne part
    une fois l'arrêt engagé.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        table: Mapping[str, Any],
        *,
        runtime: Mapping[str, Any],
        effects: Mapping[str, Any],
        zomboid_dir: Path,
        pz_dir: str | os.PathLike[str],
        matches_dir: Path,
        clock: Callable[[], float],
        stop: threading.Event,
        server_factory: Callable[..., ServerProcess],
        on_status: Callable[[str, str, str], None],
        on_state: Callable[[Callable[[MatchState], None]], None],
    ) -> None:
        self.config = config
        self.table = table
        self.rules_hash = rules_hash(table)
        self.runtime = runtime
        self.effects = effects
        self.zomboid_dir = zomboid_dir
        self.match_id = uuid.uuid4().hex
        self.match_dir = matches_dir / self.match_id
        self.match_dir.mkdir(parents=True)
        admin_password, self._rcon_password = gen_secret(), gen_secret()
        self.redactor = Redactor([admin_password, self._rcon_password])
        self.poll_s = runtime["tick_ms"] / MS_PER_S
        self._clock = clock
        self._stop = stop
        self._on_status = on_status
        self._on_state = on_state
        self.window = ContractWindow(table, effects, runtime["hook_window_s"])
        self.players: dict[str, str | None] = dict.fromkeys(SIDES)
        self._probes: dict[str, _Probe] = {}
        self._last_cmd_seq = FIRST_CMD_FLOOR
        self._countdown_end_ms: int | None = None
        self._core: MatchCore | None = None
        self.result: Mapping[str, Any] | None = None
        self._attribution: str | None = None
        # Instants de réception : `t_ms` date les événements, jamais l'absence (D13).
        self._beat_ms: dict[str, int] = {}
        self._seen_ms: dict[str, int] = {}
        self._suspects: set[str] = set()
        self._first_age: dict[str, float] = {}
        self._last_age: dict[str, float] = {}
        self._drift_s: float | None = None
        self._threats: deque[float] = deque()
        # Commandes écrites sans `cmd_result` lu : ce qu'un redémarrage rendrait perdu.
        self._unresolved: dict[str, set[int]] = {side: set() for side in SIDES}
        self._restarts = dict.fromkeys(SIDES, 0)
        # Camps en relance ou perdus : ni sortie, ni silence, ni absence n'y sont jugés.
        self._unavailable: set[str] = set()
        self._relaunched: queue.SimpleQueue[tuple[str, str | None]] = queue.SimpleQueue()
        self._relaunch_lock = threading.Lock()
        self._closing = threading.Event()
        cfg = {
            **runtime,
            "pz_dir": pz_dir,
            "zomboid_dir": zomboid_dir,
            "admin_password": admin_password,
            "rcon_password": self._rcon_password,
        }
        servers = {
            side: server_factory(side, cfg, self.match_dir, self.redactor) for side in SIDES
        }
        self.pair = ServerPair(servers, runtime, self._publish_status)
        self._store = SnapshotStore(zomboid_dir, ports=runtime["ports"])
        lua_dir = zomboid_dir / LUA_DIR
        self._lua_dir = lua_dir
        self._channels = {side: CommandChannel(side, lua_dir, clock=clock) for side in SIDES}
        self._readers = {
            side: OutReader(channel.out_path, side, effects)
            for side, channel in self._channels.items()
        }
        self._journal: Journal | None = None
        self._shutdown_lock = threading.Lock()
        self._stopped: bool | None = None

    def prepare(self) -> None:
        """Amène les mondes à leur origine, rejoue la préparation, ouvre le journal.

        Le second `_prepare_launch` efface aussi ce que la passe de création a laissé :
        `caps`, `out` et toute réécriture du `.ini` par PZ. Les serveurs sont alors arrêtés.
        """
        prepare_worlds(self.pair, self._store, prepare_launch=self._prepare_launch)
        self._prepare_launch()
        self._journal = Journal(self.match_dir, lambda: self._clock() * MS_PER_S)
        self._journal.write(
            "match_start",
            {
                "match_id": self.match_id,
                "preset": self.table,
                "rules_hash": self.rules_hash,
                "ports": self.runtime["ports"],
                "protocol": PROTOCOL_VERSION,
                "runtime": self.runtime,
                "effects": self.effects,
                "duration_s": self.config["duration_s"],
                "day_length": self.config["day_length"],
            },
        )

    def _prepare_launch(self) -> None:
        """Idempotent, mêmes secrets à chaque appel, et jamais avec un serveur en marche.

        Un `SandboxVars` absent n'existe pas encore : PZ le crée à la passe de création.
        S'il manque encore au lancement du match, la relecture du contrat refuse.
        """
        server_dir = self.zomboid_dir / SERVER_DIR
        self._lua_dir.mkdir(parents=True, exist_ok=True)
        for side, channel in self._channels.items():
            name = server_name(side)
            ports = self.runtime["ports"][side]
            managed = {
                "DefaultPort": ports["game"],
                "UDPPort": ports["udp"],
                "RCONPort": ports["rcon"],
                "RCONPassword": self._rcon_password,
                "Password": self.config["password"],
                "Mods": mods_setting(self.table),
                **self.runtime["managed_ini"],
            }
            write_ini(server_dir / f"{name}.ini", managed)
            try:
                write_sandbox(
                    server_dir / f"{name}_SandboxVars.lua",
                    self.config["day_length"],
                    self.runtime["sandbox_start"],
                )
            except FileNotFoundError:
                pass
            channel.write_session(self.match_id, FIRST_BOOT, FIRST_CMD_FLOOR)
            channel.reset()

    def launch(self) -> bool:
        """Démarre les serveurs du match ; rend faux sur échec (`pair.failure`) ou annulation."""
        return self.pair.start() and self.pair.wait_ready()

    def wait_caps(self) -> dict[str, Mapping[str, Any] | None]:
        """Lit `out` jusqu'aux `caps` des deux camps, `contract_timeout_s`, annulation ou panne."""
        deadline = time.monotonic() + self.runtime["contract_timeout_s"]
        while True:
            self._poll_out(self._now_ms())
            done = len(self.window.caps) == len(SIDES)
            if done or time.monotonic() >= deadline or self._any_exited():
                break
            if self._stop.wait(self.poll_s):
                break
        return {side: self.window.caps.get(side) for side in SIDES}

    def pump(self, now_ms: int) -> str | None:
        """Une passe à l'instant `now_ms` ; rend le motif d'un arrêt, `result` dit la fin.

        Seul ce fil écrit sur les canaux. L'absence de heartbeat n'est une panne qu'en
        `running` : un serveur vide ne tique pas (H3). L'entrée en `running` précède la
        lecture de `out` : ce que le mod a écrit depuis `t0_ms` doit atteindre le cœur.

        Les sorties se relèvent avant cette lecture, qui devient la dernière de ce serveur :
        relevées après, un `cmd_result` écrit juste avant la sortie resterait non lu, et sa
        commande deviendrait un faux `lost_on_restart`.
        """
        exited = self._exited_sides()
        if exited and self._core is None:
            return self.lost_server()
        if self._countdown_over(now_ms):
            self._enter_running(now_ms)
        self._poll_out(now_ms)
        if self._core is not None:
            self._pump_match(now_ms, exited)
            return None
        self._poll_acks()
        refusal = self._advance(now_ms)
        self._publish(now_ms)
        if refusal is None:
            return None
        self._on_state(lambda state: state.set_step(refusal.step, STATUS_ERROR, refusal.message))
        return refusal.message

    def _countdown_over(self, now_ms: int) -> bool:
        end_ms = self._countdown_end_ms
        return self._core is None and end_ms is not None and now_ms >= end_ms

    def _enter_running(self, now_ms: int) -> None:
        """Ouvre le tick 0 : `t0_ms` est le seul relevé de l'horloge du service dans le match.

        Le `state` s'écrit sans le filet de `_record_state` : sans `t0_ms`, rien ne se rejoue.
        """
        self._journal.write("state", {"phase": "running", "t0_ms": now_ms}, None, FIRST_MT)
        duration_s = self.config["duration_s"]
        self._core = MatchCore(self.table, self.runtime, self.effects, now_ms, duration_s)
        self._core.reserve_seq(self._last_cmd_seq)

        def change(state: MatchState) -> None:
            state.phase = "running"
            state.started_at = now_ms / MS_PER_S
            state.clock = {"remaining_s": duration_s, "drift_s": None}
            state.log("match lancé")
            state.event("le duel commence")

        self._on_state(change)

    def _pump_match(self, now_ms: int, exited: Sequence[str]) -> None:
        """Juge l'instant, avance le cœur, puis écrit ce qu'il rend : le tick reste son ordre.

        `out` est déjà lu : une fin soumise ici passe après les événements de son tick.
        """
        self._take_relaunches(now_ms)
        for side in exited:
            self._on_server_exit(side)
        self._watch_beats(now_ms)
        end = self._drift_end() or self._absence_end(now_ms)
        if end is not None:
            self._core.submit_end(end)
        records = self._core.advance(now_ms)
        self._write_records(records)
        self._poll_acks()
        self._publish(now_ms)
        self._publish_match(records)

    def _exited_sides(self) -> list[str]:
        return [
            side
            for side, server in self.pair.servers.items()
            if side not in self._unavailable and server.exited_unexpectedly()
        ]

    def _on_server_exit(self, side: str) -> None:
        """Applique la procédure de redémarrage, ou perd le serveur au-delà du plafond.

        `out` a été lu après la sortie : `_unresolved` ne garde que de vraies pertes. L'âge
        retiré suspend la dérive jusqu'au premier heartbeat du nouveau `boot` : un âge figé
        pendant la relance ne mesure rien, et c'est après le retour que la dérive tranche.
        """
        server = self.pair.servers[side]
        cause = f"sortie inattendue, code {server.exit_code}"
        self._unavailable.add(side)
        if self._restarts[side] >= self.runtime["max_server_restarts"]:
            self._lose_server(side, cause)
            return
        self._restarts[side] += 1
        lost = self._channels[side].prepare_restart(sorted(self._unresolved[side]))
        self._unresolved[side].clear()
        self._suspects.discard(side)
        self.players[side] = None
        self._last_age.pop(side, None)
        self._on_status(side, RESTARTING, self._server_detail(side, cause))
        self._core.submit_state(side, RESTARTING)
        for anomaly in lost:
            self._core.submit_lost(anomaly.data["seq"])
        message = f"serveur {side} : {cause}, relance du même monde"

        def change(state: MatchState) -> None:
            state.log(message, "warn")
            state.event(message, side)

        self._on_state(change)
        threading.Thread(
            target=self._relaunch, args=(side,), name=f"duel-relaunch-{side}", daemon=True
        ).start()

    def _lose_server(self, side: str, cause: str) -> None:
        self._on_status(side, STATUS_ERROR, self._server_detail(side, cause))
        reason = f"serveur {side} perdu : {cause}"
        self._core.submit_end({"outcome": OUTCOME_INVALIDATED, "winner": None, "reason": reason})

    def _server_detail(self, side: str, cause: str) -> str:
        return "\n".join([f"serveur {side} : {cause}", *self.pair.servers[side].log_tail()])

    def _relaunch(self, side: str) -> None:
        """Corps du fil de relance : rien de ce qui dure ne doit retenir `pump`."""
        try:
            failure = self._restart_server(side)
        except Exception as exc:
            # Un fil mort en silence laisserait le camp en relance jusqu'à la fin du match.
            failure = f"relance impossible ({self.redactor.redact_exception(exc)})"
        if not self._closing.is_set():
            self._relaunched.put((side, failure))

    def _restart_server(self, side: str) -> str | None:
        """Relance le même monde et attend sa disponibilité ; rend le motif d'un échec.

        Le script parti, son `java` peut lui survivre : `stop` vide le groupe, que `start`
        refuserait. Une sortie pendant la relance est la deuxième sortie du serveur.
        """
        server = self.pair.servers[side]
        if not server.stop():
            return "relance impossible : des processus de l'ancien serveur subsistent"
        with self._relaunch_lock:
            if self._closing.is_set():
                return None
            server.start()
        timeout_s = self.runtime["server_start_timeout_s"]
        deadline = time.monotonic() + timeout_s
        while not self._closing.wait(self.poll_s):
            if server.exit_code is not None:
                return f"sortie inattendue pendant la relance, code {server.exit_code}"
            if server.ini_failure:
                return server.ini_failure
            if server.ready:
                return None
            if time.monotonic() >= deadline:
                return f"pas de disponibilité après {timeout_s} s"
        return None

    def _take_relaunches(self, now_ms: int) -> None:
        """Reçoit l'issue des relances : la grâce et le silence repartent de zéro à `up`."""
        while True:
            try:
                side, failure = self._relaunched.get_nowait()
            except queue.Empty:
                return
            if failure is not None:
                self._lose_server(side, failure)
                continue
            self._unavailable.discard(side)
            self._beat_ms[side] = self._seen_ms[side] = now_ms
            self._on_status(side, STATUS_UP, "serveur relancé")
            self._core.submit_state(side, STATUS_UP)

    def _watch_beats(self, now_ms: int) -> None:
        """Passe `suspect` un camp muet depuis `beat_timeout_s`, et `up` un camp revenu."""
        timeout_ms = self.runtime["beat_timeout_s"] * MS_PER_S
        for side in SIDES:
            if side in self._unavailable:
                continue
            silent = now_ms - self._beat_ms[side] >= timeout_ms
            if silent == (side in self._suspects):
                continue
            if silent:
                self._suspects.add(side)
                self.players[side] = None
                status, detail = STATUS_SUSPECT, f"aucun heartbeat depuis {timeout_ms // MS_PER_S} s"
            else:
                self._suspects.discard(side)
                status, detail = STATUS_UP, "heartbeat revenu"
            self._on_status(side, status, detail)
            self._core.submit_state(side, status)

    def _absence_end(self, now_ms: int) -> dict[str, Any] | None:
        """Rend la fin due à une absence, comptée depuis le dernier heartbeat du joueur.

        Un camp en relance n'est pas jugé : sa grâce reste suspendue jusqu'à son retour.
        """
        grace_s = self.table["disconnect_grace_s"]
        absent = [
            side
            for side in SIDES
            if side not in self._unavailable
            and now_ms - self._seen_ms[side] >= grace_s * MS_PER_S
        ]
        if not absent:
            return None
        if len(absent) == len(SIDES):
            reason = f"les deux joueurs absents depuis {grace_s} s"
            return {"outcome": OUTCOME_INVALIDATED, "winner": None, "reason": reason}
        [side] = absent
        winner = next(other for other in SIDES if other != side)
        reason = f"forfait du camp {side}, absent depuis {grace_s} s"
        return {"outcome": OUTCOME_FORFEIT, "winner": winner, "reason": reason}

    def _drift_end(self) -> dict[str, Any] | None:
        """Recalcule la dérive en secondes réelles et rend la fin due à son dépassement.

        Elle compare les avances depuis le début du match, une fois `out` lu des deux côtés :
        A est lu avant B, et juger entre les deux prendrait un retard de lecture pour une
        dérive. L'arrondi précède la comparaison : `(3 + 1,6) − 3` vaut 1,5999… et le seuil
        est strict. Un camp en relance n'est pas jugé, même s'il tique déjà : il n'est pas
        encore disponible.
        """
        if self._unavailable or len(self._last_age) < len(SIDES):
            return None
        first, second = (self._last_age[s] - self._first_age[s] for s in SIDES)
        minutes = self.runtime["day_length_minutes"][str(self.config["day_length"])]
        scale = minutes * self.runtime["drift_factor"]
        self._drift_s = round((first - second) * scale, DRIFT_DIGITS)
        limit_s = self.runtime["max_drift_s"]
        if abs(self._drift_s) <= limit_s:
            return None
        reason = f"dérive de {self._drift_s} s, au-delà de {limit_s} s"
        return {"outcome": OUTCOME_INVALIDATED, "winner": None, "reason": reason}

    def _write_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        """Journalise dans l'ordre du cœur ; une commande part une fois journalisée."""
        for record in records:
            if record["type"] == MATCH_END:
                self._conclude(record)
                continue
            data = record["data"]
            self._journal.write(record["type"], data, record["side"], record["mt"])
            if record["type"] == COMMAND:
                self._channels[record["side"]].write(data[COMMAND])
                self._unresolved[record["side"]].add(data[COMMAND]["seq"])
                self._threats.append(record["mt"])

    def _conclude(self, record: Mapping[str, Any]) -> None:
        """Écrit `match_end` avec le verdict relu sur le journal, qui contient tout le match.

        Une attribution `ko` ne touche ni `outcome` ni `winner` : elle s'ajoute à `reason`.
        """
        report = verdict(read_journal(self._journal.path).records)
        kills = report["attribution"]
        status, branch = kills["status"], report["fallback"]
        result = dict(record["data"]["result"])
        note = None
        if status == KO:
            note = (
                f"attribution {status} : {kills['attributed']} kills attribués "
                f"sur {kills['reference']}, branche de repli {branch}"
            )
            result["reason"] = f"{result['reason']} ; {note}"
        data = {
            **record["data"],
            "result": result,
            "attribution": status,
            "spawn": report["spawn"],
            "fallback": branch,
        }
        self._journal.write(MATCH_END, data, record["side"], record["mt"])
        self._attribution = status
        self.result = result
        score = record["data"]["score"]

        def change(state: MatchState) -> None:
            state.result = result
            state.log(f"match terminé — A {score['A']} / B {score['B']} : {result['reason']}")
            state.event(f"fin du match — {result['reason']}")
            if note is not None:
                state.log(note, "warn")

        self._on_state(change)

    def _publish_match(self, records: Sequence[Mapping[str, Any]]) -> None:
        core = self._core
        mt = core.mt or FIRST_MT
        window_s = self.runtime["rate_window_s"]
        while self._threats and self._threats[0] <= mt - window_s:
            self._threats.popleft()
        threats = round(len(self._threats) * SECONDS_PER_MINUTE / window_s, 1)
        clock = {
            "remaining_s": math.ceil(core.duration_s - mt),
            "drift_s": self._drift_s,
        }
        score = dict(core.scores)
        feed = [line for line in map(_feed_line, records) if line is not None]

        def change(state: MatchState) -> None:
            state.score = score
            state.clock = clock
            state.threats_per_min = threats
            for text, side in feed:
                state.event(text, side)

        self._on_state(change)

    def _now_ms(self) -> int:
        return int(self._clock() * MS_PER_S)

    def _line_mt(self) -> float | None:
        """`mt` d'une ligne écrite hors d'un tick : le dernier tick clos, nul avant le match."""
        if self._core is None:
            return None
        mt = self._core.mt
        return FIRST_MT if mt is None else mt

    def _poll_out(self, now_ms: int) -> None:
        for side, reader in self._readers.items():
            events, anomalies = reader.poll()
            self._record_anomalies(anomalies)
            for event in events:
                self._take_event(side, event, now_ms)

    def _record_anomalies(self, anomalies: Sequence[Anomaly]) -> None:
        for anomaly in anomalies:
            self._journal.write(anomaly.type, anomaly.data, anomaly.side, self._line_mt())

    def _take_event(self, side: str, event: Mapping[str, Any], now_ms: int) -> None:
        """Avant le match, l'événement se journalise ici ; ensuite, le cœur l'écrit à son tick."""
        kind, data = event["kind"], event["data"]
        if self._core is None:
            self._journal.write(CMD_RESULT if kind == CMD_RESULT else EVENT, event, event["side"])
        else:
            self._core.submit_event(event)
        if kind == CAPS:
            self.window.on_caps(side, data)
        elif kind == HOOK_SEEN:
            self.window.on_hook_seen(side, data["hook"], now_ms)
        elif kind == BEAT and event["boot"] == self._channels[side].boot:
            # Le `java` d'un `boot` révolu peut survivre à son script : il n'atteste plus rien.
            self._take_beat(side, data, now_ms)
        elif kind == CMD_RESULT:
            self._unresolved[side].discard(data["seq"])
            self._take_probe_result(side, data, now_ms)

    def _take_beat(self, side: str, data: Mapping[str, Any], now_ms: int) -> None:
        self.players[side] = next(iter(data["players"]), None)
        self._beat_ms[side] = now_ms
        if self.players[side] is not None:
            self._seen_ms[side] = now_ms
        if self._core is not None:
            self._first_age.setdefault(side, data["age"])
            self._last_age[side] = data["age"]

    def _take_probe_result(self, side: str, result: Mapping[str, Any], now_ms: int) -> None:
        probe = self._probes.get(side)
        if probe is None or probe.seq != result["seq"] or probe.status != UNKNOWN:
            return
        status, spawned = result["status"], result.get("spawned", 0)
        if now_ms > probe.deadline_ms:
            cause = f"résultat reçu après {self.runtime['probe_timeout_s']} s"
        elif status != APPLIED:
            reason = self.redactor.redact(str(result.get("reason", "")))
            cause = f"résultat {status} {reason}".rstrip()
        elif spawned < MIN_SPAWNED:
            cause = f"{spawned} zombie créé"
        else:
            self._probes[side] = probe._replace(status=OK)
            return
        self._probes[side] = probe._replace(status=KO, cause=cause)

    def _poll_acks(self) -> None:
        for channel in self._channels.values():
            self._record_anomalies(channel.poll_ack())
            self._record_anomalies(channel.check_ack_timeout(self.runtime["ack_timeout_s"]))

    def _advance(self, now_ms: int) -> _Refusal | None:
        if self._countdown_end_ms is not None:
            self._tick_countdown(self._countdown_end_ms - now_ms)
            return None
        self._send_probes(now_ms)
        if refusal := self._failed_probes(now_ms):
            return refusal
        both_here = all(self.players.values())
        if both_here and self._probes_resolved():
            self.window.reach_lobby_ready(now_ms)
        if self.window.due(now_ms) and (refusal := self._close_window()):
            return refusal
        if self.window.closed and both_here:
            return self._enter_countdown(now_ms)
        return None

    def _send_probes(self, now_ms: int) -> None:
        """Une sonde par camp à sa première connexion, hors du cœur : `mt` nul, `seq` réservé."""
        for side, player in self.players.items():
            if player is None or side in self._probes:
                continue
            self._last_cmd_seq += 1
            seq = self._last_cmd_seq
            command = {
                "v": PROTOCOL_VERSION,
                "seq": seq,
                "kind": PROBE_KIND,
                **self.runtime["probe_spawn"],
            }
            self._journal.write(COMMAND, {COMMAND: command, "latency_ms": None}, side)
            self._channels[side].write(command)
            deadline_ms = now_ms + self.runtime["probe_timeout_s"] * MS_PER_S
            self._probes[side] = _Probe(seq, deadline_ms)

    def _probes_resolved(self) -> bool:
        return all(side in self._probes and self._probes[side].status == OK for side in SIDES)

    def _failed_probes(self, now_ms: int) -> _Refusal | None:
        for side, probe in self._probes.items():
            if probe.status == UNKNOWN and now_ms >= probe.deadline_ms:
                cause = f"aucun résultat après {self.runtime['probe_timeout_s']} s"
                self._probes[side] = probe._replace(status=KO, cause=cause)
        failed = [
            f"camp {side}, fonction {PROBE_FUNCTION} : {probe.cause}"
            for side, probe in self._probes.items()
            if probe.status == KO
        ]
        if not failed:
            return None
        branch = fallback(KO, INSUFFICIENT)
        message = f"sonde de spawn en échec, {' ; '.join(failed)} ; branche de repli {branch}"
        return _Refusal(STEP_LOBBY, message)

    def _close_window(self) -> _Refusal | None:
        """Fige la décision, puis revalide la table sur les seuls atomes actifs."""
        self.window.close()
        for withdrawal in self.window.withdrawn:
            self._journal.write(ATOM_WITHDRAWN, withdrawal)
        try:
            load_table(
                self.table,
                self.effects,
                self.runtime["limits"],
                file_stem=self.table["id"],
                active_atoms=self.window.active_atoms,
            )
        except TableError as exc:
            causes = [
                f"atome {item['atom']} retiré, hook {item['hook']} non vu sur "
                + ", ".join(item["sides"])
                for item in self.window.withdrawn
                if item["atom"] in self.table["atoms"]
            ]
            return _Refusal(STEP_CONTRACT, "contrat refusé : " + " ; ".join(causes or [str(exc)]))
        return None

    def _rules_refusal(self) -> _Refusal | None:
        """Recontrôle le hash sur le fichier du preset, tel qu'il est à cet instant."""
        path = os.path.join(PRESETS_DIR, f"{self.table['id']}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                found = rules_hash(json.load(fh))
        except (OSError, ValueError) as exc:
            cause = self.redactor.redact_exception(exc)
            message = f"preset illisible, hash {self.rules_hash} non recontrôlé : {cause}"
            return _Refusal(STEP_LOBBY, message)
        if found != self.rules_hash:
            message = f"preset modifié : hash {found}, scellé {self.rules_hash}"
            return _Refusal(STEP_LOBBY, message)
        return None

    def _enter_countdown(self, now_ms: int) -> _Refusal | None:
        if refusal := self._rules_refusal():
            return refusal
        # La décision se publie avant la phase : `countdown` ne se lit jamais sans elle.
        self._publish(now_ms)
        countdown_s = self.runtime["countdown_s"]
        self._countdown_end_ms = now_ms + countdown_s * MS_PER_S

        def change(state: MatchState) -> None:
            state.set_step(STEP_LOBBY, "ok", "les deux joueurs sont connectés")
            state.clock = {
                "remaining_s": self.config["duration_s"],
                "drift_s": None,
                "countdown": countdown_s,
            }
            state.log("décompte")

        self._on_state(change)
        self.enter_phase("countdown")
        return None

    def _tick_countdown(self, left_ms: int) -> None:
        def change(state: MatchState) -> None:
            state.clock["countdown"] = math.ceil(left_ms / MS_PER_S)

        self._on_state(change)

    def _capabilities(self) -> dict[str, Any]:
        window = self.window
        spawn = {
            side: self._probes[side].status if side in self._probes else UNKNOWN
            for side in SIDES
        }
        published = {
            side: publish_capabilities(window.caps.get(side), window.seen[side]) for side in SIDES
        }
        return {
            **published,
            "active_atoms": window.active_atoms,
            "withdrawn": [item["atom"] for item in window.withdrawn],
            "attribution": self._attribution,
            "spawn": spawn,
            # L'attribution ne se mesure qu'en fin de match : rien à en conclure avant.
            "fallback": fallback(combined_spawn(spawn), self._attribution or INSUFFICIENT),
        }

    def _publish(self, now_ms: int) -> None:
        capabilities = self._capabilities()
        players = dict(self.players)
        detail = self.window.detail(now_ms)

        def change(state: MatchState) -> None:
            state.capabilities = capabilities
            state.players = players
            state.set_step(STEP_CONTRACT, "ok", detail)

        self._on_state(change)

    def verify(self, caps: Mapping[str, Mapping[str, Any] | None]) -> list[str]:
        builds = {side: server.build for side, server in self.pair.servers.items()}
        failures = verify_contract(
            self.zomboid_dir,
            self.table,
            self.config["day_length"],
            self.runtime,
            caps,
            builds,
        )
        return [self.redactor.redact(failure) for failure in failures]

    def _any_exited(self) -> bool:
        return any(server.exited_unexpectedly() for server in self.pair.servers.values())

    def lost_server(self) -> str | None:
        """Hors `running`, une sortie non demandée est une erreur : rend son message expurgé."""
        for side, server in self.pair.servers.items():
            if server.exited_unexpectedly():
                detail = self._server_detail(side, f"sortie inattendue, code {server.exit_code}")
                self._on_status(side, STATUS_ERROR, detail)
                return detail
        return None

    def enter_phase(self, phase: str) -> None:
        """Le journal s'écrit hors du verrou : un `fsync` ne doit pas figer `/api/state`."""

        def change(state: MatchState) -> None:
            state.phase = phase

        self._on_state(change)
        self.record_phase(phase)

    def record_phase(self, phase: str) -> None:
        self._record_state({"phase": phase}, None)

    def _publish_status(self, side: str, status: str, detail: str) -> None:
        self._on_status(side, status, detail)
        self._record_state({"server": status}, side)

    def _record_state(self, data: Mapping[str, Any], side: str | None) -> None:
        """Journalise un `state`, rien avant `match_start`.

        Ces lignes accompagnent aussi l'arrêt : une panne du journal, disque plein compris,
        est consignée sans jamais empêcher d'arrêter les serveurs.
        """
        journal = self._journal
        if journal is None:
            return
        try:
            journal.write("state", data, side, self._line_mt())
        except (OSError, ValueError) as exc:
            LOGGER.error("journal : %s", self.redactor.redact_exception(exc))

    def shutdown(self) -> bool:
        """Arrête les serveurs une seule fois, depuis n'importe quel fil, verrou du moteur libre.

        Un appel concurrent attend la fin de l'arrêt et en rend le résultat.
        """
        with self._shutdown_lock:
            if self._stopped is None:
                with self._relaunch_lock:
                    self._closing.set()
                self._stopped = self.pair.close()
            return self._stopped

    def close(self) -> None:
        """Ferme le journal : à n'appeler qu'après `shutdown`, plus rien n'y écrit."""
        if self._journal is not None:
            self._journal.close()
            self._journal = None


class Engine:
    """Un duel à la fois. `clock` rend l'heure murale en secondes."""

    def __init__(
        self,
        mode: str = MODE_MOCK,
        *,
        zomboid_dir: str | os.PathLike[str] | None = None,
        pz_dir: str | os.PathLike[str] | None = None,
        runtime: Mapping[str, Any] | None = None,
        effects: Mapping[str, Any] | None = None,
        clock: Callable[[], float] | None = None,
        matches_dir: str | os.PathLike[str] | None = None,
        steam_launcher: Callable[[Sequence[str]], bool] | None = None,
        server_factory: Callable[..., ServerProcess] | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode inconnu : {mode}")
        self.mode = mode
        self.zomboid_dir = Path.home() / "Zomboid" if zomboid_dir is None else Path(zomboid_dir)
        self.pz_dir = pz_dir
        self.runtime = load_runtime() if runtime is None else runtime
        self.effects = load_effects() if effects is None else effects
        self.matches_dir = MATCHES_DIR if matches_dir is None else Path(matches_dir)
        self._clock = time.time if clock is None else clock
        self._steam_launcher = launch_steam if steam_launcher is None else steam_launcher
        self._server_factory = ServerProcess if server_factory is None else server_factory
        self.lock = threading.RLock()
        self.state = MatchState(mode=mode)
        self.state.reset_steps()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._match: RealMatch | None = None
        backend = "serveurs réels" if self.real else "backend simulé"
        self.state.log(f"service démarré ({backend})")
        self._presets = load_presets(
            self.runtime, self.effects, lambda message: self.state.log(message, "error")
        )

    @property
    def real(self) -> bool:
        return self.mode == MODE_REAL

    # -- lecture ---------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            return self.state.to_dict()

    def presets(self) -> list[dict]:
        return self._presets

    def preset(self, preset_id: str) -> dict:
        for p in self._presets:
            if p["id"] == preset_id:
                return p
        if self.real:
            raise ActionRefused(f"preset inconnu : {preset_id}")
        if not self._presets:
            raise ActionRefused("aucun preset valide")
        return self._presets[0]

    # -- actions ---------------------------------------------------------
    def action(self, name: str, payload: dict | None = None) -> tuple[bool, str]:
        payload = payload or {}
        if name not in ACTIONS:
            return False, f"action inconnue: {name}"
        with self.lock:
            phase = self.state.phase
            if name == "start":
                if phase not in ("idle", "over", "error"):
                    return False, f"impossible de démarrer en phase {phase}"
                try:
                    self._begin(payload)
                except ActionRefused as exc:
                    return False, str(exc)
                return True, "démarrage"
            if name == "rematch":
                if phase not in ("over", "error"):
                    return False, "revanche possible seulement après un match"
                cfg = dict(self.state.config)
                try:
                    # En réel, l'invitation déjà donnée reste valable.
                    self._begin(cfg, cfg.get("code") if self.real else None)
                except ActionRefused as exc:
                    return False, str(exc)
                return True, "revanche"
            if name == "cancel":
                if phase in ("idle", "over"):
                    return False, "rien à annuler"
                self._stop.set()
                self.state.log("annulation demandée", "warn")
                if self._match is not None:
                    # Hors du verrou : `on_status` le prend, et un arrêt dure.
                    threading.Thread(
                        target=self._match.shutdown, name="duel-cancel", daemon=True
                    ).start()
                return True, "annulation"
            if name == "simulate_join":
                if self.real:
                    return False, "action indisponible avec de vrais serveurs"
                if phase != "lobby":
                    return False, "personne à faire entrer (phase %s)" % phase
                if self.state.players.get("B"):
                    return False, "l'adversaire est déjà là"
                self.state.players["B"] = "invité (sim)"
                self.state.log("adversaire simulé connecté")
                self.state.event("adversaire connecté", "B")
                return True, "invité connecté"
            if name == "open_game":
                if self.real:
                    return self._open_game_real()
                self.state.log("lancement du client PZ demandé (simulé)")
                return True, "client PZ lancé (simulé)"
        return False, "action non traitée"

    def _open_game_real(self) -> tuple[bool, str]:
        """Connecte le client de l'hôte au serveur A. La commande n'est jamais journalisée."""
        password = self.state.config.get("password")
        if not password:
            return False, "aucun duel préparé : démarrez un duel avant d'ouvrir le jeu"
        port = self.runtime["ports"]["A"]["game"]
        args = [
            "-applaunch",
            str(self.runtime["steam_app_id"]),
            "+connect",
            f"{CLIENT_HOST}:{port}",
            "+password",
            password,
        ]
        if not self._steam_launcher(args):
            message = "Steam introuvable : lancez Project Zomboid à la main"
            self.state.log(message, "warn")
            return False, message
        self.state.log("client PZ lancé par Steam")
        return True, "client PZ lancé par Steam"

    # -- moteur ----------------------------------------------------------
    def _begin(self, payload: dict, code: str | None = None) -> None:
        """`code` vient de la revanche seule : le `payload` d'un `start` n'en fixe aucun."""
        preset = self.preset(payload.get("preset", "duel_rapide"))
        if self.real:
            for label, key in (("pseudo", "pseudo"), ("mot de passe", "password")):
                if payload.get(key):
                    _check_ini_value(label, payload[key])
        duration = int(payload.get("duration_s") or preset["duration_s"])
        duration = max(30, min(3600, duration))
        cfg = {
            "pseudo": (payload.get("pseudo") or "hôte")[:20],
            "preset": preset["id"],
            "preset_label": preset["label"],
            "duration_s": duration,
            "day_length": int(payload.get("day_length") or preset["day_length"]),
            "code": code or make_code(),
            "summary": preset["summary"],
        }
        cfg["password"] = payload.get("password") or cfg["code"]
        self.state = MatchState(mode=self.mode, config=cfg)
        self.state.reset_steps()
        # Dès ici et non dans le fil : un second `start` ne doit pas passer entre les deux.
        self.state.phase = "preflight"
        self._stop.clear()
        self.state.log(f"préparation du duel « {preset['label']} » ({duration}s)")
        run = self._run_real if self.real else self._run
        self._thread = threading.Thread(
            target=self._guarded, args=(run,), name="duel-engine", daemon=True
        )
        self._thread.start()

    def _guarded(self, run: Callable[[], None]) -> None:
        try:
            run()
        except Exception as exc:  # pragma: no cover - garde-fou
            self._fail(f"erreur interne: {exc}")

    def _fail(self, message: str) -> None:
        with self.lock:
            self.state.phase = "error"
            self.state.error = message
            self.state.log(message, "error")

    def _run_real(self) -> None:
        with self.lock:
            self.state.phase = "preflight"
            self.state.set_step("preflight", "running")
            self.state.log("préflight : installation, ports, mémoire, mod, réglages, table")
            table = self.preset(self.state.config["preset"])
        result = run_preflight(
            table,
            runtime=self.runtime,
            effects=self.effects,
            pz_dir=self.pz_dir,
            zomboid_dir=self.zomboid_dir,
        )
        if self._aborted():
            return
        with self.lock:
            self.state.set_step("preflight", "ok" if result.ok else "error", result.detail)
        if not result.ok:
            self._fail("préflight en échec : " + " ; ".join(result.failures))
            return
        match = self._open_match(table, result.values["pz_dir"])
        if match is None:
            self._cancel_state()
            return
        try:
            failure = self._drive_match(match)
        except SnapshotError as exc:
            failure = match.redactor.redact(str(exc))
        except Exception as exc:
            # Toute panne, même imprévue, doit passer par `_finish_match` et son arrêt.
            failure = f"lancement impossible : {match.redactor.redact_exception(exc)}"
        self._finish_match(match, failure)

    def _open_match(self, table: Mapping[str, Any], pz_dir: str) -> RealMatch | None:
        """Crée le match sous le verrou : `cancel` voit soit `_match`, soit `_stop` ici relu."""
        with self.lock:
            if self._stop.is_set():
                return None
            self._match = RealMatch(
                self.state.config,
                table,
                runtime=self.runtime,
                effects=self.effects,
                zomboid_dir=self.zomboid_dir,
                pz_dir=pz_dir,
                matches_dir=self.matches_dir,
                clock=self._clock,
                stop=self._stop,
                server_factory=self._server_factory,
                on_status=self._on_server_status,
                on_state=self._change_state,
            )
            self.state.match_id = self._match.match_id
            self.state.config["rules_hash"] = self._match.rules_hash
            self.state.phase = "starting"
            self.state.log("préparation des mondes et des réglages")
            return self._match

    def _on_server_status(self, side: str, status: str, detail: str) -> None:
        with self.lock:
            apply_status(self.state, side, status, detail)

    def _change_state(self, change: Callable[[MatchState], None]) -> None:
        with self.lock:
            change(self.state)

    def _drive_match(self, match: RealMatch) -> str | None:
        """Mène le match jusqu'à sa fin : `cancel` ou une panne en sort plus tôt.

        Rend le motif d'échec, `None` si annulé ou fini, ce que dit `match.result`.
        """
        match.prepare()
        match.record_phase("starting")
        if self._stop.is_set():
            return None
        if not match.launch():
            return match.pair.failure
        match.enter_phase("contract")
        with self.lock:
            self.state.set_step("contract", "running")
        caps = match.wait_caps()
        if self._stop.is_set():
            return None
        if lost := match.lost_server():
            return lost
        if failures := match.verify(caps):
            message = "contrat refusé : " + " ; ".join(failures)
            with self.lock:
                self.state.set_step("contract", "error", message)
            return message
        self._open_lobby(match)
        while not self._stop.wait(match.poll_s):
            if failure := match.pump(int(self._clock() * MS_PER_S)):
                return failure
            if match.result is not None:
                break
        return None

    def _open_lobby(self, match: RealMatch) -> None:
        with self.lock:
            self.state.set_step("contract", "ok", "contrat vérifié sur A et B")
            self.state.set_step("lobby", "running", "en attente des joueurs")
            self.state.invite = {
                "ip": local_ip(),
                "port": self.runtime["ports"][GUEST_SIDE]["game"],
                "password": self.state.config["password"],
                "code": self.state.config["code"],
            }
            self.state.log("lobby ouvert, invitation prête")
        match.enter_phase("lobby")

    def _finish_match(self, match: RealMatch, failure: str | None) -> None:
        """Toute sortie passe ici : arrêt d'abord, puis état final, puis fermeture du journal."""
        match.enter_phase("stopping")
        stopped = match.shutdown()
        with self.lock:
            self._match = None
        if stopped and match.result is not None:
            # Une annulation venue pendant l'arrêt n'efface pas un match déjà jugé.
            match.enter_phase("over")
            match.close()
            return
        if not stopped:
            failure = STOP_INCOMPLETE
        elif self._stop.is_set():
            failure = None
        if failure is None:
            self._cancel_state()
            match.record_phase("idle")
        else:
            self._fail(failure)
            match.record_phase("error")
        match.close()

    def _run(self) -> None:
        self._phase_preflight()
        if self._aborted():
            return
        self._phase_starting()
        if self._aborted():
            return
        self._phase_contract()
        if self._aborted():
            return
        self._phase_lobby()
        if self._aborted():
            return
        self._phase_running()

    def _aborted(self) -> bool:
        if self._stop.is_set():
            self._cancel_state()
            return True
        return False

    def _cancel_state(self) -> None:
        with self.lock:
            self.state.phase = "idle"
            self.state.servers = {"A": "down", "B": "down"}
            self.state.players = {"A": None, "B": None}
            self.state.clock = {"remaining_s": 0, "drift_s": None}
            self.state.invite = {}
            for step in self.state.steps:
                if step["state"] == "running":
                    step["state"] = "pending"
                    step["detail"] = "annulé"
            self.state.set_step("lobby", "pending", "annulé")
            self.state.log("duel annulé", "warn")

    def _sleep(self, seconds: float) -> bool:
        end = self._clock() + seconds
        while self._clock() < end:
            if self._stop.is_set():
                return False
            time.sleep(0.05)
        return True

    def _phase_preflight(self) -> None:
        with self.lock:
            self.state.phase = "preflight"
            self.state.set_step("preflight", "running")
            self.state.log("préflight (simulé)")
        self._sleep(1.2)
        with self.lock:
            self.state.set_step("preflight", "ok", "aucun contrôle exécuté (simulé)")

    def _phase_starting(self) -> None:
        with self.lock:
            self.state.phase = "starting"
            self.state.set_step("serverA", "running")
            self.state.log("démarrage serveur A (simulé)")
        if not self._sleep(2.4):
            return self._cancel_state()
        with self.lock:
            self.state.servers["A"] = "up"
            self.state.set_step("serverA", "ok", "démarré (simulé)")
            self.state.set_step("serverB", "running")
            self.state.log("démarrage serveur B (simulé)")
        if not self._sleep(3.6):
            return self._cancel_state()
        with self.lock:
            self.state.servers["B"] = "up"
            self.state.set_step("serverB", "ok", "démarré (simulé)")
            self.state.log("les deux serveurs sont prêts (simulé)")

    def _phase_contract(self) -> None:
        with self.lock:
            self.state.phase = "contract"
            self.state.set_step("contract", "running")
        if not self._sleep(0.8):
            return self._cancel_state()
        with self.lock:
            self.state.set_step("contract", "ok", "contrat non vérifié (simulé)")

    def _phase_lobby(self) -> None:
        with self.lock:
            self.state.phase = "lobby"
            self.state.set_step("lobby", "running", "en attente de l'adversaire")
            self.state.invite = {
                "ip": local_ip(),
                "port": self.runtime["ports"]["A"]["game"],
                "password": self.state.config["password"],
                "code": self.state.config["code"],
            }
            self.state.log("lobby ouvert, invitation prête (simulé)")
        # l'hôte rejoint son propre serveur
        if not self._sleep(1.2):
            return self._cancel_state()
        with self.lock:
            self.state.players["A"] = self.state.config["pseudo"] + " (toi)"
            self.state.log("hôte connecté au serveur A (simulé)")
        # attente de l'adversaire (bouton « simuler » en mode mock)
        while True:
            with self.lock:
                if self.state.players.get("B"):
                    break
            if not self._sleep(0.3):
                return self._cancel_state()
        with self.lock:
            self.state.set_step("lobby", "ok", "les deux joueurs sont connectés")
            self.state.phase = "countdown"
            self.state.clock = {"remaining_s": self.state.config["duration_s"], "drift_s": 0}
            self.state.log("décompte")
        for n in (3, 2, 1):
            with self.lock:
                self.state.clock["countdown"] = n
            if not self._sleep(1.0):
                return self._cancel_state()
        with self.lock:
            self.state.clock.pop("countdown", None)

    def _phase_running(self) -> None:
        cfg = self.state.config
        with self.lock:
            self.state.phase = "running"
            self.state.started_at = self._clock()
            self.state.clock = {"remaining_s": cfg["duration_s"], "drift_s": 0}
            self.state.log("match lancé (simulé)")
            self.state.event("le duel commence")
        remaining = cfg["duration_s"]
        kills = {"A": 0, "B": 0}
        while remaining > 0:
            if not self._sleep(1.0):
                return self._cancel_state()
            remaining -= 1
            with self.lock:
                self.state.clock["remaining_s"] = remaining
                self.state.clock["drift_s"] = round(random.uniform(-2, 2), 1)
                if remaining % 2 == 0:
                    for side, other in (("A", "B"), ("B", "A")):
                        n = random.choice([0, 0, 1, 1, 2, 3])
                        if n:
                            kills[side] += n
                            self.state.score[side] += n * 10
                            self.state.event(
                                f"{side} tue {n} zombie{'s' if n > 1 else ''} → "
                                f"{n * 2} chez {other}",
                                side,
                            )
                    self.state.threats_per_min = round(random.uniform(1.5, 6.5), 1)
        with self.lock:
            self.state.phase = "over"
            a, b = self.state.score["A"], self.state.score["B"]
            verdict = "égalité" if a == b else ("victoire A" if a > b else "victoire B")
            self.state.log(f"match terminé — A {a} / B {b} ({verdict}, simulé)")
            self.state.event(f"fin du match — {verdict}")
