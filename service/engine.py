"""Orchestration d'un duel, en mode simulé ou réel.

En mock, aucun serveur PZ n'est lancé : la machine à états, les latences, les événements
et le score sont simulés pour valider l'expérience utilisateur, et tout ce qui ressemble
à une vérification s'annonce « simulé ». En réel, le mode demandé est tenu de bout en
bout : un échec finit en `error`, jamais dans le parcours simulé.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import queue
import random
import socket
import string
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict
from functools import partial
from pathlib import Path
from secrets import token_hex
from typing import Any, NamedTuple

from . import paths, upnp
from .guest_launcher import (
    ADDRESS_REFUSED,
    PASSWORD_ALLOWED,
    PASSWORD_REFUSED,
    LauncherRefused,
    build_launcher,
    is_usable_address,
    launcher_code,
    read_mod,
)
from .http_kit import LAUNCHER_NAME
from .journal import Journal, read_journal
from .platforms import Platform, current
from .preflight import (
    ABSENT,
    MOD_SRC,
    PRESENT,
    PreflightResult,
    ModRestoreError,
    detect_pz,
    install_mod,
    mod_fingerprint,
    run_preflight,
)
from .proofs import PROOFS_NAME, HookProofs
from .protocol import PROTOCOL_VERSION, SIDES
from .rules import (
    MS_PER_S,
    PLAYER_DEATH,
    RESTARTING,
    MatchCore,
    TableError,
    idle_quit_seconds,
    load_effects,
    load_runtime,
    load_table,
    narrow_table,
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
    gen_player_password,
    gen_secret,
    mods_setting,
    server_name,
    verify_contract,
    write_ini,
    write_sandbox,
)
from .share import ShareServer
from .snapshots import SnapshotError, SnapshotStore, prepare_worlds
from .state import MatchState
from .transport import Anomaly, CommandChannel, OutReader, PollResult
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
    probe_may_retry,
    publish_capabilities,
    verdict,
)

PRESETS_DIR = str(paths.resource_root() / "service" / "presets")
MATCHES_DIR = paths.matches_dir()

ACTIONS = {"start", "cancel", "rematch", "simulate_join", "open_game"}
DEFAULT_PRESET = "duel_rapide"
MODE_MOCK = "mock"
MODE_REAL = "real"
MODES = (MODE_MOCK, MODE_REAL)
# `start-server.sh` fixe `-Dzomboid.steam=0` : le client doit quitter le mode Steam lui aussi.
NO_STEAM_FLAG = "-nosteam"
# Un client qui tient ce délai a démarré : un refus d'options sort bien avant.
CLIENT_START_GRACE_S = 2
# L'hôte joue sur la machine du serveur A.
HOST_SIDE = "A"
CLIENT_HOST = "127.0.0.1"
# L'invité joue sur le serveur B.
GUEST_SIDE = "B"
PLAYER_REFUSED = "player_refused"
EVENT_REFUSED = "event_refused"
# Faits du serveur, sans auteur : ils arrivent avant toute connexion.
SERVER_KINDS = (BEAT, CAPS, CMD_RESULT, HOOK_SEEN)
LUA_DIR = "Lua"
SERVER_DIR = "Server"
FIRST_BOOT = 1
FIRST_CMD_FLOOR = 0
STOP_INCOMPLETE = "arrêt incomplet : des processus de serveur subsistent"
# La fonction de jeu que `probe_spawn` met à l'épreuve dans `DuelCore.lua`.
PROBE_FUNCTION = "addZombiesInOutfit"
ATOM_WITHDRAWN = "atom_withdrawn"
TABLE_SEALED = "table_sealed"
WITHDRAWAL_REASON = "blessure jamais détectée sur ce build et ce mod"
EARLY_DEATH = "early_death"
OUTCOME_DEATH = "death"
STEP_CONTRACT = "contract"
STEP_LOBBY = "lobby"
STATUS_SUSPECT = "suspect"
OUTCOME_FORFEIT = "forfeit"
OUTCOME_INVALIDATED = "invalidated"
# Le `state` d'entrée en `running` ouvre le tick 0.
FIRST_MT = 0
DRIFT_DIGITS = 6
SECONDS_PER_MINUTE = 60
MIN_DURATION_S = 30
MAX_DURATION_S = 3600
VERDICT_FAILED = "verdict_failed"
# 128 bits (AC38).
SHARE_TOKEN_BYTES = 16
SCOPE_LAN = "lan"
SCOPE_UNVERIFIED = "unverified"
SCOPE_NO_ADDRESS = "no_address"
SCOPE_MOCK = "mock"
SCOPE_LAN_TEXT = (
    "Detected address: this invitation only works on your local network. "
    "For a remote guest, enter your public address."
)
SCOPE_UNVERIFIED_TEXT = (
    "Unverified address: PZ Duel cannot test it. For a remote guest, open UDP ports {game} "
    "and {udp} to play and TCP port {share} for the link."
)
SCOPE_NO_ADDRESS_TEXT = "No usable address was detected: enter an address your guest can reach."
SCOPE_MOCK_TEXT = "Simulated duel: there is no link and no launcher."
REMATCH_NOTE = "rematch_note"
REMATCH_NOTE_TEXT = (
    "A launcher you already sent stays valid for a rematch. "
    "A new duel can make it stale: send the new one then."
)
SHARE_TEXT = "share_text"
SHARE_UNAVAILABLE_TEXT = (
    "The share link is unavailable: TCP port {port} could not be opened. "
    "Save the launcher and send it to your guest."
)
# Arrêt sur inactivité (P9F4, resserré en P9F5) : seules ces phases, où rien n'est en cours,
# l'autorisent. De `preflight` à la fin du `lobby`, un lancement attend sans onglet ouvert (le
# premier duel réel a été coupé en `preflight`) ; pendant un duel, l'hôte regarde le jeu.
IDLE_QUIT_PHASES = ("idle", "over", "error")
# `open_game` attend l'invité (P9F5) : le monde de l'hôte reste figé jusqu'à son arrivée.
GUEST_SIDE = "B"
GUEST_NOT_HERE = (
    "impossible d'ouvrir le jeu : l'adversaire n'est pas encore connecté. Le jeu s'ouvre dès "
    "que l'invité est identifié, pour que ton monde ne tourne pas seul en l'attendant."
)
# Le lobby s'ouvre depuis `contract` : l'écouteur naît juste avant lui, sans lui survivre.
SHARE_PHASES = ("contract", "lobby")
# Formes neutres de `ports_opening` ; les autres statuts viennent de `service/upnp.py`.
PORTS_IDLE = "idle"
PORTS_IN_PROGRESS = "in_progress"
UDP, TCP = upnp.PROTOCOLS
# Ce que `invite` ne dit que pendant le lobby (AC60).
LOBBY_ONLY = (REMATCH_NOTE, SHARE_TEXT)
INVITE_CLOSED = "invitation unavailable: it only exists in the lobby of a real duel"
LAUNCHER_CODE_TEXT = (
    "Read this code aloud to your guest, never next to the link. "
    "Their launcher does not show it: it asks them to type it, and stops if it does not match. "
    "A launcher that does not ask for a code must not be run: the file was replaced. "
    "Before opening the file, they run: certutil -hashfile {name} SHA256. "
    "The result must start with this code, or the file was replaced and must not be opened. "
    "This invitation is meant for a trusted network: "
    "the code is the only protection against a replaced file."
)

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


class ClientLaunchError(Exception):
    """Client non lancé : le message dit la cause, sans jamais citer les arguments."""


def _spawn_detached(command: Sequence[str], cwd: Path, platform: Platform) -> subprocess.Popen:
    *first, last = platform.client_popen_attempts()
    streams = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    for attempt in first:
        try:
            return subprocess.Popen(command, cwd=cwd, **streams, **attempt)
        except PermissionError:
            continue
    return subprocess.Popen(command, cwd=cwd, **streams, **last)


def launch_client(
    script: Path, args: Sequence[str], platform: Platform | None = None, *, cwd: Path
) -> subprocess.Popen:
    """Lance le client, détaché du service, et rend son processus démarré.

    `cwd` est le dossier de jeu détecté, celui des serveurs : sans lui, le client hériterait
    du dossier du service, celui du téléchargement pour un binaire double-cliqué. Ce n'est pas
    toujours le parent de `script` : sous Linux, le client est un cran au-dessus.

    Le script Linux garde le client au premier plan et finit toujours par `exit 0` : le code
    ne prouve rien, seul compte qu'il tourne encore au bout de `CLIENT_START_GRACE_S`.
    """
    platform = current() if platform is None else platform
    try:
        process = _spawn_detached([str(script), *args], cwd, platform)
    except OSError as exc:
        raise ClientLaunchError(f"{script.name} ne démarre pas ({exc.strerror})") from exc
    try:
        code = process.wait(CLIENT_START_GRACE_S)
    except subprocess.TimeoutExpired:
        return process
    raise ClientLaunchError(f"{script.name} s'est arrêté aussitôt (code {code}), options refusées ?")


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


def _text_option(payload: Mapping[str, Any], key: str, label: str) -> str:
    """Rend un texte facultatif du `payload`, vide s'il manque."""
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise ActionRefused(f"{label} refusé : texte attendu")
    return value or ""


def _duration_of(payload: Mapping[str, Any], preset: Mapping[str, Any]) -> int:
    """Une durée demandée sort du refus ; seule celle du preset, déjà chargé, se ramène."""
    value = payload.get("duration_s")
    if value is None:
        return max(MIN_DURATION_S, min(MAX_DURATION_S, preset["duration_s"]))
    if type(value) is not int or not MIN_DURATION_S <= value <= MAX_DURATION_S:
        raise ActionRefused(
            f"durée refusée : entier de {MIN_DURATION_S} à {MAX_DURATION_S} s attendu"
        )
    return value


def _redacted(redactor: Redactor, value: Any) -> Any:
    """Copie une valeur JSON sans aucun secret, à toute profondeur."""
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, Mapping):
        return {_redacted(redactor, k): _redacted(redactor, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(redactor, item) for item in value]
    return value


class ContractWindow:
    """Fenêtre de contrat : ce que les camps ont montré, jusqu'à une décision figée.

    Ouverte au premier `caps`, elle se ferme à `L`, que `reach_lobby_ready` fixe : aucune
    blessure n'est attendue, car `damage_taken` vient du relevé de santé du mod, et son hook ne
    fait que le confirmer. `close` décide une fois pour tout le match, sur l'échec déjà constaté
    pour ce build et ce mod : `caps` et `hook_seen` tardifs restent publiés, sans rien réactiver.
    `seen` est ce qui se publie, `detected` dit si le camp a vu la blessure par un signal ou l'autre.
    """

    def __init__(self, catalog: Mapping[str, Any]) -> None:
        self.proofs = {
            atom: spec["hook_proof"]
            for atom, spec in catalog["atoms"].items()
            if "hook_proof" in spec
        }
        self.caps: dict[str, Mapping[str, Any]] = {}
        self.seen: dict[str, set[str]] = {side: set() for side in SIDES}
        self._wounded: dict[str, set[str]] = {side: set() for side in SIDES}
        self.ready = False
        self.active_atoms: list[str] | None = None
        self.withdrawn: list[dict[str, Any]] = []

    @property
    def closed(self) -> bool:
        return self.active_atoms is not None

    @property
    def due(self) -> bool:
        return self.ready and not self.closed

    def on_caps(self, side: str, data: Mapping[str, Any]) -> None:
        self.caps[side] = data

    def on_hook_seen(self, side: str, hook: str) -> None:
        if self.caps:
            self.seen[side].add(hook)

    def on_wound(self, side: str, atom: str) -> None:
        self._wounded[side].add(self.proofs[atom])

    def detected(self, side: str, hook: str) -> bool:
        return hook in self.seen[side] or hook in self._wounded[side]

    def reach_lobby_ready(self) -> None:
        self.ready = True

    def _common_atoms(self) -> set[str]:
        return set.intersection(*(set(self.caps[side]["atoms"]) for side in SIDES))

    def withdrawals(self, failed: Mapping[str, Sequence[str]]) -> list[dict[str, Any]]:
        """Rend ce que `failed` retire : le lobby l'annonce, `close` le fige."""
        return [
            {"atom": atom, "hook": self.proofs[atom], "sides": list(failed[self.proofs[atom]])}
            for atom in sorted(self._common_atoms() & self.proofs.keys())
            if self.proofs[atom] in failed
        ]

    def close(self, failed: Mapping[str, Sequence[str]]) -> None:
        """Retient les atomes communs aux deux `caps`, moins ceux dont `failed` nomme le hook."""
        self.withdrawn = self.withdrawals(failed)
        self.active_atoms = sorted(self._common_atoms() - {item["atom"] for item in self.withdrawn})

    def detail(self, failed: Mapping[str, Sequence[str]]) -> str:
        """Détail de l'étape `contract` (D17) : dès le lobby, il annonce la table qui sera jouée."""
        if self.closed:
            summary = f"atomes actifs : {', '.join(self.active_atoms)}"
            retired = [item["atom"] for item in self.withdrawn]
            return f"{summary} ; retirés : {', '.join(retired)}" if retired else summary
        return " ; ".join(
            f"{atom} sera retiré : {WITHDRAWAL_REASON}"
            for atom, hook in sorted(self.proofs.items())
            if hook in failed
        )


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
    # Posé par un refus passager : l'essai suivant part à cet instant, dans le même délai.
    retry_ms: int | None = None


class _Refusal(NamedTuple):
    """Arrêt du lancement : l'étape à mettre en `error` et le message."""

    step: str
    message: str


class MatchSecrets(NamedTuple):
    """Secrets d'un match, tirés par le moteur avant `RealMatch` : voir `Engine._guarded`."""

    admin_password: str
    rcon_password: str


class RealMatch:
    """Un lancement réel, de la préparation à l'arrêt, dans l'ordre de D23.

    Le constructeur tire `match_id` et arme le `Redactor` sur `secrets` : la passe de
    création écrit déjà `server_X.log`. `on_status` suit `ServerPair` ; `stop` est l'événement
    d'annulation du moteur. `shutdown` est le seul chemin d'arrêt : `cancel`, erreur et fin.
    `on_state` applique une modification à l'état du moteur, sous son verrou.

    `_core` naît à l'entrée en `running` : c'est lui qui journalise alors tout ce qui compte
    pour le rejeu. `result` reste nul jusqu'à l'écriture de `match_end`.

    Une relance en match tourne sur son propre fil, qui ne touche que le serveur : son issue
    revient par `_relaunched`, que `pump` lit, seul à parler au cœur et aux canaux.
    `_relaunch_lock` ordonne le `start` de ce fil et `shutdown` : aucun serveur ne part
    une fois l'arrêt engagé.

    Ce que le mod écrit est une sortie de serveur comme une autre : `_poll_out` l'expurge
    à la lecture, avant le journal, le cœur et l'état.

    Deux horloges, sans mélange. `monotonic` cadence tout ce qui se mesure en durée : `now_ms`,
    les ticks, le décompte, le silence, l'absence et les délais d'`ack`. `clock`, l'heure
    murale, date le journal et donne `t0_ms`, seul repère commun avec le `t_ms` du mod. Un
    saut de l'heure murale ne touche ainsi ni le rythme ni les minuteries.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        table: Mapping[str, Any],
        *,
        secrets: MatchSecrets,
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
        proofs: HookProofs,
        mod_print: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.table = table
        self.preset_hash = rules_hash(table)
        # Seule source de vérité : le cœur la joue, `rules_hash` en dérive, l'UI la publie.
        # Le lobby la suit au fil des échecs connus ; `_close_window` la fige avant le décompte.
        self._played_table: Mapping[str, Any] = table
        self.runtime = runtime
        self.effects = effects
        self.zomboid_dir = zomboid_dir
        self.match_id = uuid.uuid4().hex
        self.match_dir = matches_dir / self.match_id
        self.match_dir.mkdir(parents=True)
        self._rcon_password = secrets.rcon_password
        self.redactor = Redactor(secrets)
        self.poll_s = runtime["tick_ms"] / MS_PER_S
        self._clock = clock
        self._monotonic = monotonic
        # Ce qui sépare `now_ms` de l'échelle de `t0_ms`, fixé une fois à l'entrée en `running`.
        self._t0_shift_ms = 0
        self._stop = stop
        self._on_status = on_status
        self._on_state = on_state
        self.window = ContractWindow(effects)
        self._proofs = proofs
        self._mod_print = mod_print
        # Échecs constatés pour ce build et ce mod, relus une fois les `caps` connus.
        self._failed_hooks: dict[str, list[str]] | None = None
        self._proven_hooks: set[str] = set()
        self._disabled_rules: list[str] = []
        self.players: dict[str, str | None] = dict.fromkeys(SIDES)
        # Nom de connexion lié à chaque camp, pour tout le match, relances comprises.
        self._identities: dict[str, str] = {}
        self._refused_players: set[tuple[str, str]] = set()
        self._lost_lines = 0
        self._probes: dict[str, _Probe] = {}
        self._last_cmd_seq = FIRST_CMD_FLOOR
        self._countdown_end_ms: int | None = None
        self._core: MatchCore | None = None
        # Morts lues avant `running` : le cœur, qui n'existait pas, les reçoit à sa naissance.
        self._early_deaths: list[Mapping[str, Any]] = []
        self.result: Mapping[str, Any] | None = None
        self._attribution: str | None = None
        # Instants de réception : `t_ms` date les événements, jamais l'absence (D13).
        self._beat_ms: dict[str, int] = {}
        self._seen_ms: dict[str, int] = {}
        self._suspects: set[str] = set()
        self._first_age: dict[str, float] = {}
        self._last_age: dict[str, float] = {}
        # Écart d'âge A − B acquis avant les relances, et camps sans repère depuis la leur.
        self._banked_gap = 0.0
        self._rebasing: set[str] = set()
        # Camps sans joueur ou muets : leur monde est figé (`PauseEmpty=true`), dérive suspendue.
        self._paused: set[str] = set()
        self._drift_s: float | None = None
        self._threats: deque[float] = deque()
        # Commandes écrites sans `cmd_result` lu : ce qu'un redémarrage rendrait perdu.
        self._unresolved: dict[str, set[int]] = {side: set() for side in SIDES}
        self._restarts = dict.fromkeys(SIDES, 0)
        # Camps en relance ou perdus : ni sortie, ni silence, ni absence n'y sont jugés.
        self._unavailable: set[str] = set()
        self._exits_held: set[str] = set()
        # Fin de l'attente du tampon du mod, posée quand une conclusion devient due.
        self._settle_end_ms: int | None = None
        self._conclusion_aimed = False
        self._relaunched: queue.SimpleQueue[tuple[str, str | None]] = queue.SimpleQueue()
        self._relaunch_lock = threading.Lock()
        self._closing = threading.Event()
        cfg = {
            **runtime,
            "pz_dir": pz_dir,
            "zomboid_dir": zomboid_dir,
            "admin_password": secrets.admin_password,
            "rcon_password": self._rcon_password,
        }
        servers = {
            side: server_factory(side, cfg, self.match_dir, self.redactor) for side in SIDES
        }
        self.pair = ServerPair(servers, runtime, self._publish_status)
        self._store = SnapshotStore(zomboid_dir, ports=runtime["ports"], policy=runtime)
        lua_dir = zomboid_dir / LUA_DIR
        self._lua_dir = lua_dir
        self._channels = {
            side: CommandChannel(side, lua_dir, clock=monotonic, policy=runtime) for side in SIDES
        }
        self._readers = {
            side: OutReader(channel.out_path, side, effects)
            for side, channel in self._channels.items()
        }
        self._journal: Journal | None = None
        self._shutdown_lock = threading.Lock()
        self._stopped: bool | None = None

    @property
    def rules_hash(self) -> str:
        """Le hash de la table jouée : il ne se stocke pas, donc ne peut pas s'en écarter."""
        return rules_hash(self._played_table)

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
                # La table jouée n'est connue qu'à `table_sealed` : il faut les `caps` pour la dire.
                "preset_hash": self.preset_hash,
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
        passwords = {
            HOST_SIDE: self.config["host_password"],
            GUEST_SIDE: self.config["password"],
        }
        self._lua_dir.mkdir(parents=True, exist_ok=True)
        for side, channel in self._channels.items():
            name = server_name(side)
            ports = self.runtime["ports"][side]
            managed = {
                "DefaultPort": ports["game"],
                "UDPPort": ports["udp"],
                "RCONPort": ports["rcon"],
                "RCONPassword": self._rcon_password,
                "Password": passwords[side],
                "Mods": mods_setting(self.table),
                **self.runtime["managed_ini"],
            }
            write_ini(server_dir / f"{name}.ini", managed, policy=self.runtime)
            try:
                write_sandbox(
                    server_dir / f"{name}_SandboxVars.lua",
                    self.config["day_length"],
                    self.runtime["sandbox_start"],
                    policy=self.runtime,
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
        """Une passe à l'instant monotone `now_ms` ; rend le motif d'un arrêt, `result` dit la fin.

        Seul ce fil écrit sur les canaux. L'absence de heartbeat n'est une panne qu'en
        `running` : un serveur vide bat toutes les 10 s, mais rien n'exige ce `beat` avant la
        première connexion. L'entrée en `running` précède la lecture de `out` : ce que le mod
        a écrit depuis `t0_ms` doit atteindre le cœur.

        Les sorties se relèvent avant cette lecture, qui devient la dernière de ce serveur :
        relevées après, un `cmd_result` écrit juste avant la sortie resterait non lu, et sa
        commande deviendrait un faux `lost_on_restart`.

        Une passe lit un budget borné de `out`. Tant qu'il reste du retard, rien ne se juge sur
        ce relevé partiel : les passes suivantes finissent la lecture, puis tranchent. Une fin
        de match se juge en plus sur un repère pris une fois le tampon du mod retombé.
        """
        exited = self._exited_sides()
        if exited and self._core is None:
            return self.lost_server()
        if self._countdown_over(now_ms):
            self._enter_running(now_ms)
        for side in exited:
            self._readers[side].aim_at_end()
        self._poll_out(now_ms)
        if self._core is not None:
            self._settle_conclusion(now_ms)
            self._pump_match(now_ms, exited)
            return None
        self._poll_acks()
        refusal = None if self._reading_behind() else self._advance(now_ms)
        self._publish(now_ms)
        if refusal is None:
            return None
        self._on_state(lambda state: state.set_step(refusal.step, STATUS_ERROR, refusal.message))
        return refusal.message

    def _countdown_over(self, now_ms: int) -> bool:
        end_ms = self._countdown_end_ms
        return self._core is None and end_ms is not None and now_ms >= end_ms

    def _enter_running(self, now_ms: int) -> None:
        """Ouvre le tick 0 : `t0_ms` est le seul relevé de l'heure murale qui règle le match.

        Il se lit ici, et non au lancement : un saut survenu au lobby ne décale pas `te`.
        Le `state` s'écrit sans le filet de `_record_state` : sans `t0_ms`, rien ne se rejoue.
        Une mort déjà lue passe au cœur : le match s'ouvre pour la juger, et rien d'autre.
        """
        t0_ms = int(self._clock() * MS_PER_S)
        self._t0_shift_ms = t0_ms - now_ms
        self._journal.write("state", {"phase": "running", "t0_ms": t0_ms}, None, FIRST_MT)
        duration_s = self.config["duration_s"]
        self._core = MatchCore(self._played_table, self.runtime, self.effects, t0_ms, duration_s)
        self._core.reserve_seq(self._last_cmd_seq)
        for event in self._early_deaths:
            self._core.submit_event(event)

        def change(state: MatchState) -> None:
            state.phase = "running"
            state.started_at = t0_ms / MS_PER_S
            state.clock = {"remaining_s": duration_s, "drift_s": None}
            state.log("match lancé")
            state.event("le duel commence")

        self._on_state(change)

    def _settle_conclusion(self, now_ms: int) -> None:
        """Laisse retomber le tampon du mod, puis relit `out` à sa fin, une seule fois par fin due.

        Le mod vide `out` par lots, sans accord entre A et B : un fait plus ancien que la
        conclusion peut n'être pas encore écrit. Le repère se prend donc une fois l'attente
        finie. Un repère de plus laisserait un écrivain retenir la fin : celui-ci ne bouge plus,
        sauf si la fin s'efface (`_judged_tick`) : la suivante aura sa propre attente.
        """
        if self._conclusion_aimed:
            return
        if self._settle_end_ms is None:
            if not self._core.conclusion_due(now_ms + self._t0_shift_ms):
                return
            self._begin_settle(now_ms)
        if now_ms < self._settle_end_ms:
            return
        self._conclusion_aimed = True
        for reader in self._readers.values():
            reader.aim_at_end()
        self._poll_out(now_ms)

    def _begin_settle(self, now_ms: int) -> None:
        if self._settle_end_ms is None:
            self._settle_end_ms = now_ms + self.runtime["conclusion_settle_ms"]

    def _settling(self) -> bool:
        return self._settle_end_ms is not None and not self._conclusion_aimed

    def _pump_match(self, now_ms: int, exited: Sequence[str]) -> None:
        """Juge l'instant, avance le cœur, puis écrit ce qu'il rend : le tick reste son ordre.

        `out` est déjà lu : une fin soumise ici passe après les événements de son tick.
        En retard de lecture, ou le temps que le tampon du mod retombe, sorties et fins attendent,
        et une sortie reste relevée à la passe suivante. Les ticks ordinaires continuent : ils vident la file du cœur à chaque tour,
        et ce qui arrive après son tick s'y journalise en `late_event`. Rien ne part vers un
        serveur sorti : seul le bilan de ses commandes attend la fin de sa lecture.
        """
        self._take_relaunches(now_ms)
        core_ms = now_ms + self._t0_shift_ms
        if self._reading_behind() or self._settling():
            self._hold_commands_to(exited)
            records = self._core.advance_short_of_end(core_ms)
        else:
            records = self._judged_tick(now_ms, core_ms, exited)
        self._write_records(records)
        self._poll_acks()
        self._publish(now_ms)
        self._publish_match(records)

    def _judged_tick(
        self, now_ms: int, core_ms: int, exited: Sequence[str]
    ) -> list[dict[str, Any]]:
        for side in exited:
            self._on_server_exit(side)
        self._watch_beats(now_ms)
        end = self._drift_end() or self._absence_end(now_ms)
        if end is not None:
            if not self._conclusion_aimed:
                self._begin_settle(now_ms)
                return self._core.advance_short_of_end(core_ms)
            self._core.submit_end(end)
        records = self._core.advance(core_ms)
        if end is None and not self._core.conclusion_due(core_ms):
            # Le joueur absent est revenu pendant l'attente : la fin attendue n'existe plus.
            self._settle_end_ms = None
            self._conclusion_aimed = False
        return records

    def _hold_commands_to(self, exited: Sequence[str]) -> None:
        """Passe en relance, pour le cœur et le journal, les camps sortis pas encore relevés."""
        for side in exited:
            if side not in self._exits_held:
                self._exits_held.add(side)
                self._core.submit_state(side, RESTARTING)

    def _reading_behind(self) -> bool:
        return any(reader.behind for reader in self._readers.values())

    def _exited_sides(self) -> list[str]:
        return [
            side
            for side, server in self.pair.servers.items()
            if side not in self._unavailable and server.exited_unexpectedly()
        ]

    def _on_server_exit(self, side: str) -> None:
        """Applique la procédure de redémarrage, ou perd le serveur au-delà du plafond.

        `out` a été lu après la sortie : `_unresolved` ne garde que de vraies pertes. L'âge
        retiré suspend la dérive : voir `_track_age` pour sa reprise.
        """
        server = self.pair.servers[side]
        cause = f"sortie inattendue, code {server.exit_code}"
        self._unavailable.add(side)
        if self._restarts[side] >= self.runtime["max_server_restarts"]:
            self._lose_server(side, cause)
            return
        self._restarts[side] += 1
        try:
            lost = self._channels[side].prepare_restart(sorted(self._unresolved[side]))
        except OSError as exc:
            # Sans `in` vidé ni session neuve, relancer pourrait rejouer une commande.
            detail = self.redactor.redact_exception(exc)
            self._lose_server(side, f"{cause}, relance impossible : {detail}")
            return
        self._unresolved[side].clear()
        self._suspects.discard(side)
        self.players[side] = None
        self._bank_drift()
        self._last_age.pop(side, None)
        self._rebasing.add(side)
        self._on_status(side, RESTARTING, self._server_detail(side, cause))
        if side in self._exits_held:
            self._exits_held.discard(side)
        else:
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

    def _transport_failed(self, step: str, exc: OSError) -> None:
        """Clôt un match invalidé sur une panne de fichier : `match_end` ferme le journal.

        Le cœur tient le score : laisser l'erreur remonter le perdrait avec le match. Avant
        `running`, il n'y a rien à conclure, et l'erreur reste une panne de lancement.
        """
        if self._core is None:
            raise exc
        reason = f"transport en panne ({step}) : {self.redactor.redact_exception(exc)}"
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
                self._suspend_drift(side)
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

        Elle compare les avances depuis le repère courant, plus l'écart acquis avant les
        relances, une fois `out` lu des deux côtés : A est lu avant B, et juger entre les
        deux prendrait un retard de lecture pour une dérive. L'arrondi précède la
        comparaison : `(3 + 1,6) − 3` vaut 1,5999… et le seuil est strict. Un camp en
        relance n'est pas jugé, même s'il tique déjà : il n'est pas encore disponible.
        """
        if self._unavailable or len(self._last_age) < len(SIDES):
            return None
        gap = self._age_gap() + self._banked_gap
        minutes = self.runtime["day_length_minutes"][str(self.config["day_length"])]
        scale = minutes * self.runtime["drift_factor"]
        self._drift_s = round(gap * scale, DRIFT_DIGITS)
        limit_s = self.runtime["max_drift_s"]
        if abs(self._drift_s) <= limit_s:
            return None
        reason = f"dérive de {self._drift_s} s, au-delà de {limit_s} s"
        return {"outcome": OUTCOME_INVALIDATED, "winner": None, "reason": reason}

    def _age_gap(self) -> float:
        first, second = (self._last_age[s] - self._first_age[s] for s in SIDES)
        return first - second

    def _bank_drift(self) -> None:
        """Met de côté l'écart mesuré avant une relance : le repère va changer, pas lui."""
        if len(self._last_age) == len(SIDES):
            self._banked_gap += self._age_gap()

    def _suspend_drift(self, side: str) -> None:
        """Suspend la dérive d'un camp sans joueur : `PauseEmpty=true` fige son monde.

        L'autre monde continue de tourner : comparer leurs âges compterait l'absence comme
        dérive, et la dérive finirait le match avant la grâce de reconnexion. L'écart acquis
        est mis de côté, comme pour une relance, et un repère neuf s'ouvre au retour du
        joueur. L'absence reste jugée par `_absence_end`, depuis le dernier joueur vu.
        """
        if self._core is None or side in self._paused:
            return
        self._paused.add(side)
        self._bank_drift()
        self._last_age.pop(side, None)

    def _track_age(self, side: str, age: float) -> None:
        """Suit l'âge du monde d'un camp, et repose le repère au retour d'une relance.

        Le monde relancé a perdu son temps d'arrêt et repart de sa dernière sauvegarde : son
        âge ne se compare plus à l'ancien repère. Son premier heartbeat une fois disponible,
        avec son joueur, ouvre donc un repère neuf, pris au dernier âge connu pour l'autre
        camp : un serveur vide est figé (`PauseEmpty=true`, voir `_suspend_drift`). La relance
        ne compte ainsi pas comme dérive.
        """
        if side in self._unavailable:
            return
        if side in self._rebasing:
            self._rebasing.discard(side)
            self._first_age = {**self._last_age, side: age}
        self._first_age.setdefault(side, age)
        self._last_age[side] = age

    def _write_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        """Journalise dans l'ordre du cœur ; une commande part une fois journalisée.

        `match_end` ferme le lot : aucune panne d'une ligne précédente ne doit l'en priver.
        """
        for record in records:
            if record["type"] == MATCH_END:
                self._conclude(record)
                continue
            written = self._write_safely(
                record["type"], record["data"], record["side"], record["mt"]
            )
            if written and record["type"] == COMMAND:
                self._send(record)

    def _send(self, record: Mapping[str, Any]) -> None:
        """Écrit une commande dans `in` ; perdue, le cœur la rend par son `cmd_timeout`."""
        side, command = record["side"], record["data"][COMMAND]
        # Avant l'écriture : une panne après le `write` laisse une ligne que le mod a pu lire.
        self._unresolved[side].add(command["seq"])
        try:
            self._channels[side].write(command)
        except (OSError, ValueError) as exc:
            LOGGER.error("commande %s : %s", command["seq"], self.redactor.redact_exception(exc))
            return
        self._threats.append(record["mt"])

    def _conclude(self, record: Mapping[str, Any]) -> None:
        """Écrit `match_end` avec le verdict relu sur le journal, qui contient tout le match.

        Une attribution `ko` ne touche ni `outcome` ni `winner` : elle s'ajoute à `reason`.
        Le cœur a déjà décidé : ni un verdict incalculable, journalisé à part avec une
        attribution `unknown`, ni une panne du journal ne font perdre le résultat.
        """
        result = dict(record["data"]["result"])
        self._judge_detection(result)
        notes = []
        try:
            report = verdict(read_journal(self._journal.path).records)
        except Exception as exc:
            cause = self.redactor.redact_exception(exc)
            self._write_safely(VERDICT_FAILED, {"cause": cause}, None, record["mt"])
            notes.append(f"verdict non calculé, attribution inconnue : {cause}")
            published = self._capabilities()
            status, spawn, branch = UNKNOWN, published["spawn"], published["fallback"]
        else:
            kills = report["attribution"]
            status, spawn, branch = kills["status"], report["spawn"], report["fallback"]
            self._attribution = status
            if status == KO:
                note = (
                    f"attribution {status} : {kills['attributed']} kills attribués "
                    f"sur {kills['reference']}, branche de repli {branch}"
                )
                result["reason"] = f"{result['reason']} ; {note}"
                notes.append(note)
        data = {
            **record["data"],
            "result": result,
            "attribution": status,
            "spawn": spawn,
            "fallback": branch,
        }
        if not self._write_safely(MATCH_END, data, record["side"], record["mt"]):
            notes.append("fin de match absente du journal : écriture impossible")
        if self._lost_lines:
            notes.append(f"journal incomplet : {self._lost_lines} lignes perdues")
        self.result = result
        score = record["data"]["score"]

        def change(state: MatchState) -> None:
            state.result = result
            state.log(f"match terminé — A {score['A']} / B {score['B']} : {result['reason']}")
            state.event(f"fin du match — {result['reason']}")
            for note in notes:
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
        return int(self._monotonic() * MS_PER_S)

    def _line_mt(self) -> float | None:
        """`mt` d'une ligne écrite hors d'un tick : le dernier tick clos, nul avant le match."""
        if self._core is None:
            return None
        mt = self._core.mt
        return FIRST_MT if mt is None else mt

    def _poll_out(self, now_ms: int) -> None:
        """Lit `out` dans le budget d'une passe : `_reading_behind` dit s'il en reste."""
        for side, reader in self._readers.items():
            for events, anomalies in self._read_passes(side, reader):
                self._record_anomalies(anomalies)
                for event in events:
                    self._take_event(side, _redacted(self.redactor, event), now_ms)

    def _read_passes(self, side: str, reader: OutReader) -> Iterator[PollResult]:
        """Isole les pannes de lecture : celles du journal ne sont pas des pannes de transport."""
        try:
            yield from reader.drain()
        except OSError as exc:
            self._transport_failed(f"lecture de `out` du camp {side}", exc)

    def _record_anomalies(self, anomalies: Sequence[Anomaly]) -> None:
        for anomaly in anomalies:
            data = _redacted(self.redactor, anomaly.data)
            self._write_safely(anomaly.type, data, anomaly.side, self._line_mt())

    def _admitted(self, side: str, event: Mapping[str, Any]) -> bool:
        """Dit si l'événement vient de l'identité du camp, seule à pouvoir peser sur le match.

        `SERVER_KINDS` décrivent le serveur et n'ont pas d'auteur. Le reste exige un camp
        authentifié. Un atome sans camp n'a pas d'auteur non plus : le cœur le journalise
        sans le compter. Un atome de camp sans `player` est refusé, faute de preuve.
        """
        if event["kind"] in SERVER_KINDS:
            return True
        bound = self._identities.get(side)
        if bound is None:
            return False
        return event["side"] is None or event["data"].get("player") == bound

    def _take_event(self, side: str, event: Mapping[str, Any], now_ms: int) -> None:
        """Avant le match, l'événement se journalise ici ; ensuite, le cœur l'écrit à son tick."""
        kind, data = event["kind"], event["data"]
        if not self._admitted(side, event):
            refused = {
                "kind": kind,
                "boot": event["boot"],
                "seq": event["seq"],
                "player": data.get("player"),
            }
            self._record_anomalies([Anomaly(EVENT_REFUSED, side, refused)])
            return
        if self._core is not None:
            self._core.submit_event(event)
        elif kind == PLAYER_DEATH and event["side"] is not None:
            self._take_early_death(event, now_ms)
        else:
            self._write_safely(
                CMD_RESULT if kind == CMD_RESULT else EVENT, event, event["side"], None
            )
        if kind == CAPS:
            self.window.on_caps(side, data)
        elif kind == HOOK_SEEN:
            self.window.on_hook_seen(side, data["hook"])
            if data["hook"] in self.window.proofs.values():
                self._prove(data["hook"])
        elif kind in self.window.proofs and event["side"] is not None:
            self.window.on_wound(event["side"], kind)
            self._prove(self.window.proofs[kind])
        elif kind == BEAT and event["boot"] == self._channels[side].boot:
            # Le `java` d'un `boot` révolu peut survivre à son script : il n'atteste plus rien.
            self._take_beat(side, data, now_ms)
        elif kind == CMD_RESULT:
            self._unresolved[side].discard(data["seq"])
            self._take_probe_result(side, data, now_ms)

    def _take_early_death(self, event: Mapping[str, Any], now_ms: int) -> None:
        """Retient une mort d'avant `running` : l'`event` s'écrira au tick qui la juge.

        Sa trace part dès la lecture : un `cancel` du décompte ne doit pas l'effacer. L'attente
        du tampon part d'ici, et le premier tick décide si elle est finie à `t0`.
        """
        self._early_deaths.append(event)
        self._begin_settle(now_ms)
        data = {"boot": event["boot"], "seq": event["seq"], "t_ms": event["t_ms"]}
        self._write_safely(EARLY_DEATH, data, event["side"], None)
        message = f"camp {event['side']} mort avant le début du match"
        self._on_state(lambda state: state.log(message, "warn"))

    def _proof_pair(self) -> str | None:
        """Rend la clé de `HookProofs`, nulle sans `caps`. `verify` tient les deux camps égaux."""
        caps = next(iter(self.window.caps.values()), None)
        if caps is None:
            return None
        return f"{caps['build']}|{self._mod_print or caps['mod']}"

    def _prove(self, hook: str) -> None:
        """Un signal a tiré : le couple est prouvé, et un échec passé tombe, une fois par match."""
        pair = self._proof_pair()
        if pair is None or hook in self._proven_hooks:
            return
        self._proven_hooks.add(hook)
        self._proofs.prove(pair, hook)
        if self._failed_hooks is not None:
            self._failed_hooks.pop(hook, None)

    def _known_failures(self) -> Mapping[str, Sequence[str]]:
        """Rend les échecs de ce couple, lus une fois : `pump` passe quatre fois par seconde."""
        if self._failed_hooks is not None:
            return self._failed_hooks
        pair = self._proof_pair()
        if pair is None or len(self.window.caps) < len(SIDES):
            return {}
        sides = {hook: self._proofs.failed_sides(pair, hook) for hook in self.window.proofs.values()}
        self._failed_hooks = {
            hook: found
            for hook, found in sides.items()
            if found and hook not in self._proven_hooks
        }
        return self._failed_hooks

    def _judge_detection(self, result: Mapping[str, Any]) -> None:
        """Seule preuve d'échec : un mort que ni le relevé de santé ni le hook n'ont vu blesser."""
        if result["outcome"] != OUTCOME_DEATH:
            return
        dead = next(side for side in SIDES if side != result["winner"])
        pair = self._proof_pair()
        for atom, hook in self.window.proofs.items():
            if atom in self.window.active_atoms and not self.window.detected(dead, hook):
                self._proofs.fail(pair, hook, dead)

    def _take_beat(self, side: str, data: Mapping[str, Any], now_ms: int) -> None:
        self.players[side] = self._identify(side, data["players"])
        self._beat_ms[side] = now_ms
        if self.players[side] is not None:
            self._seen_ms[side] = now_ms
        if self._core is None:
            return
        if self.players[side] is None:
            self._suspend_drift(side)
            return
        if side in self._paused:
            # Le joueur revient : le monde repart d'où il s'était figé, sur un repère neuf.
            self._paused.discard(side)
            self._rebasing.add(side)
        self._track_age(side, data["age"])

    def _identify(self, side: str, names: Sequence[str]) -> str | None:
        """Rend le joueur du camp : le premier nom admis s'y lie, et n'en sert aucun autre."""
        bound = self._identities.get(side)
        if bound in names:
            return bound
        taken = {name for other, name in self._identities.items() if other != side}
        for name in names:
            if bound is None and name not in taken:
                self._identities[side] = name
                return name
            self._refuse_player(side, name)
        return None

    def _refuse_player(self, side: str, name: str) -> None:
        """Un nom refusé ne compte pas comme présent ; il se signale une fois par camp."""
        if (side, name) in self._refused_players:
            return
        self._refused_players.add((side, name))
        self._record_anomalies([Anomaly(PLAYER_REFUSED, side, {"player": name})])
        message = f"joueur {name} ignoré sur le serveur {side} : ce n'est pas l'identité du camp"
        self._on_state(lambda state: state.log(self.redactor.redact(message), "warn"))

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
            if probe_may_retry(result):
                retry_ms = now_ms + self.runtime["probe_retry_s"] * MS_PER_S
                self._probes[side] = probe._replace(cause=cause, retry_ms=retry_ms)
                return
        elif spawned < MIN_SPAWNED:
            cause = f"{spawned} zombie créé"
        else:
            self._probes[side] = probe._replace(status=OK)
            return
        self._probes[side] = probe._replace(status=KO, cause=cause)

    def _poll_acks(self) -> None:
        for side, channel in self._channels.items():
            try:
                self._record_anomalies(channel.poll_ack())
            except OSError as exc:
                self._transport_failed(f"suivi de `ack` du camp {side}", exc)
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
            self.window.reach_lobby_ready()
        if self.window.due and (refusal := self._close_window()):
            return refusal
        if self.window.closed and both_here:
            return self._enter_countdown(now_ms)
        return None

    def _send_probes(self, now_ms: int) -> None:
        """Une sonde par camp à sa première connexion, hors du cœur : `mt` nul, `seq` réservé.

        Un essai suivant garde l'échéance du premier : `probe_timeout_s` borne toute la sonde.
        """
        for side, player in self.players.items():
            probe = self._probes.get(side)
            if player is None or not (probe is None or self._retry_due(probe, now_ms)):
                continue
            self._last_cmd_seq += 1
            seq = self._last_cmd_seq
            command = {
                "v": PROTOCOL_VERSION,
                "seq": seq,
                "kind": PROBE_KIND,
                **self.runtime["probe_spawn"],
            }
            # Non journalisée, elle ne part pas : l'échéance rend la sonde en échec.
            if self._write_safely(COMMAND, {COMMAND: command, "latency_ms": None}, side, None):
                self._channels[side].write(command)
            if probe is None:
                deadline_ms = now_ms + self.runtime["probe_timeout_s"] * MS_PER_S
                self._probes[side] = _Probe(seq, deadline_ms)
            else:
                self._probes[side] = probe._replace(seq=seq, retry_ms=None)

    @staticmethod
    def _retry_due(probe: _Probe, now_ms: int) -> bool:
        return probe.retry_ms is not None and probe.retry_ms <= now_ms < probe.deadline_ms

    def _probes_resolved(self) -> bool:
        return all(side in self._probes and self._probes[side].status == OK for side in SIDES)

    def _failed_probes(self, now_ms: int) -> _Refusal | None:
        for side, probe in self._probes.items():
            if probe.status == UNKNOWN and now_ms >= probe.deadline_ms:
                last = probe.cause or "aucun résultat"
                cause = f"{last} après {self.runtime['probe_timeout_s']} s"
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
        """Fige la décision et scelle la table sans ce qui dépend d'un atome retiré.

        Sans échec constaté, la table jouée est le preset. Seule une table privée de tout atome
        est refusée. Un atome absent de `caps` sans être retiré reste un écart de contrat : la
        revalidation le refuse. Une table que le journal ne porte pas ne se joue pas : le rejeu
        ne la retrouverait pas.
        """
        self.window.close(self._known_failures())
        written = [
            self._write_safely(ATOM_WITHDRAWN, withdrawal, None, None)
            for withdrawal in self.window.withdrawn
        ]
        narrowed = narrow_table(self.table, self._withdrawn_atoms())
        try:
            self._played_table = load_table(
                narrowed,
                self.effects,
                self.runtime["limits"],
                file_stem=self.table["id"],
                active_atoms=self.window.active_atoms,
            )
        except TableError as exc:
            causes = [
                f"atome {item['atom']} retiré, hook {item['hook']} en échec sur "
                + ", ".join(item["sides"])
                for item in self.window.withdrawn
                if item["atom"] in self.table["atoms"] and not narrowed["atoms"]
            ]
            cause = " ; ".join(causes or [self.redactor.redact(str(exc))])
            return _Refusal(STEP_CONTRACT, f"contrat refusé : {cause}")
        self._disabled_rules = self._rules_gone()
        written.append(self._write_safely(TABLE_SEALED, self._seal(), None, None))
        if not all(written):
            message = f"contrat refusé : table {self.rules_hash} non journalisée"
            return _Refusal(STEP_CONTRACT, message)
        self._log_withdrawals()
        return None

    def _withdrawn_atoms(self) -> list[str]:
        return [item["atom"] for item in self.window.withdrawn]

    def _rules_gone(self) -> list[str]:
        kept = {rule["id"] for rule in self._played_table["rules"]}
        return [rule["id"] for rule in self.table["rules"] if rule["id"] not in kept]

    def _announced_table(self) -> dict[str, list[str]]:
        table = self._played_table
        return {"atoms": list(table["atoms"]), "rules": [rule["id"] for rule in table["rules"]]}

    def _seal(self) -> dict[str, Any]:
        return {
            "rules_hash": self.rules_hash,
            "preset_hash": self.preset_hash,
            **self._announced_table(),
            "withdrawn": self._withdrawn_atoms(),
            "disabled_rules": self._disabled_rules,
        }

    def _log_withdrawals(self) -> None:
        lines = [
            f"atome {item['atom']} retiré : {WITHDRAWAL_REASON} "
            f"(hook {item['hook']}, camps {', '.join(item['sides'])})"
            for item in self.window.withdrawn
        ]
        if self._disabled_rules:
            rules = ", ".join(self._disabled_rules)
            lines.append(f"règles désactivées par le retrait d'un atome : {rules}")
        if lines:
            lines.append(f"table jouée : hash {self.rules_hash}")

        def change(state: MatchState) -> None:
            for line in lines:
                state.log(line, "warn")

        self._on_state(change)

    def _rules_refusal(self) -> _Refusal | None:
        """Recontrôle le fichier du preset, tel qu'il est à cet instant, et ce qui en dérive.

        Le preset doit être celui du lancement, et la table jouée ce qu'il donne une fois
        retirés les atomes journalisés : ni règle en plus, ni règle en moins.
        """
        path = os.path.join(PRESETS_DIR, f"{self.table['id']}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            found = rules_hash(raw)
        except (OSError, ValueError) as exc:
            cause = self.redactor.redact_exception(exc)
            message = f"preset illisible, hash {self.preset_hash} non recontrôlé : {cause}"
            return _Refusal(STEP_LOBBY, message)
        if found != self.preset_hash:
            message = f"preset modifié : hash {found}, scellé {self.preset_hash}"
            return _Refusal(STEP_LOBBY, message)
        derived = rules_hash(narrow_table(raw, self._withdrawn_atoms()))
        if derived != self.rules_hash:
            message = f"table jouée {self.rules_hash} différente du preset réduit {derived}"
            return _Refusal(STEP_LOBBY, message)
        return None

    def _enter_countdown(self, now_ms: int) -> _Refusal | None:
        if refusal := self._rules_refusal():
            return refusal
        # La décision se publie avant la phase : `countdown` ne se lit jamais sans elle.
        self._publish(now_ms)
        countdown_s = self.runtime["countdown_s"]
        entered = False

        def change(state: MatchState) -> None:
            # Sous le verrou du moteur, comme `Engine.claim_idle_stop` : un arrêt engagé au
            # lobby n'ouvre jamais de duel, et un duel ouvert n'est jamais tenu pour inactif.
            nonlocal entered
            if self._stop.is_set():
                return
            entered = True
            state.set_step(STEP_LOBBY, "ok", "les deux joueurs sont connectés")
            state.clock = {
                "remaining_s": self.config["duration_s"],
                "drift_s": None,
                "countdown": countdown_s,
            }
            state.log("décompte")
            state.phase = "countdown"

        self._on_state(change)
        if entered:
            self._countdown_end_ms = now_ms + countdown_s * MS_PER_S
            self.record_phase("countdown")
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
            "disabled_rules": self._disabled_rules,
            "table": self._announced_table(),
            "attribution": self._attribution,
            "spawn": spawn,
            # L'attribution ne se mesure qu'en fin de match : rien à en conclure avant.
            "fallback": fallback(combined_spawn(spawn), self._attribution or INSUFFICIENT),
        }

    def _follow_failures(self) -> None:
        """Avant la clôture, la table suit les échecs connus : un signal au lobby peut en lever un."""
        if not self.window.closed:
            withdrawals = self.window.withdrawals(self._known_failures())
            self._played_table = narrow_table(self.table, [item["atom"] for item in withdrawals])
            self._disabled_rules = self._rules_gone()

    def announce_table(self) -> None:
        """Publie la table avant d'ouvrir le lobby : aucun relevé n'y voit celle du preset."""
        self._publish(self._now_ms())

    def _publish(self, now_ms: int) -> None:
        self._follow_failures()
        capabilities = self._capabilities()
        players = dict(self.players)
        detail = self.window.detail(self._known_failures())
        announced = self.rules_hash

        def change(state: MatchState) -> None:
            state.capabilities = capabilities
            state.config["rules_hash"] = announced
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
        if self._journal is not None:
            self._write_safely("state", data, side, self._line_mt())

    def _write_safely(
        self, type_: str, data: Mapping[str, Any], side: str | None, mt: float | None
    ) -> bool:
        """Journalise une ligne qu'une panne du journal ne doit pas interrompre.

        Disque, encodage ou type : la perte se compte et `_conclude` la rapporte.
        """
        try:
            self._journal.write(type_, data, side, mt)
        except (OSError, TypeError, ValueError) as exc:
            self._lost_lines += 1
            LOGGER.error("journal : %s", self.redactor.redact_exception(exc))
            return False
        return True

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
    """Un duel à la fois. `clock` rend l'heure murale en secondes, `monotonic` cadence le match."""

    def __init__(
        self,
        mode: str = MODE_MOCK,
        *,
        zomboid_dir: str | os.PathLike[str] | None = None,
        pz_dir: str | os.PathLike[str] | None = None,
        runtime: Mapping[str, Any] | None = None,
        effects: Mapping[str, Any] | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        matches_dir: str | os.PathLike[str] | None = None,
        client_launcher: Callable[..., subprocess.Popen] | None = None,
        server_factory: Callable[..., ServerProcess] | None = None,
        platform: Platform | None = None,
        upnp_target: tuple[str, int] | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode inconnu : {mode}")
        self.mode = mode
        self.zomboid_dir = Path.home() / "Zomboid" if zomboid_dir is None else Path(zomboid_dir)
        self.pz_dir = pz_dir
        self.runtime = load_runtime() if runtime is None else runtime
        self.effects = load_effects() if effects is None else effects
        self.matches_dir = MATCHES_DIR if matches_dir is None else Path(matches_dir)
        self._proofs = HookProofs(
            self.matches_dir / PROOFS_NAME, LOGGER.warning, policy=self.runtime
        )
        self._clock = time.time if clock is None else clock
        self._monotonic = time.monotonic if monotonic is None else monotonic
        # Arrêt sur inactivité (P9F4) : délai validé (0 = jamais), et le dernier battement, sur
        # l'horloge monotone. Le démarrage en vaut un : sans navigateur, le délai court déjà.
        self._idle_quit_s = idle_quit_seconds(self.runtime)
        self._watched_at = self._monotonic()
        self._platform = current() if platform is None else platform
        self._client_launcher = (
            partial(launch_client, platform=self._platform)
            if client_launcher is None
            else client_launcher
        )
        self._client: subprocess.Popen | None = None
        self._client_starting = False
        self._server_factory = (
            partial(ServerProcess, platform=self._platform)
            if server_factory is None
            else server_factory
        )
        self.lock = threading.RLock()
        # Tenu par toute mesure et toute écriture du premier lancement, sans `self.lock` : deux
        # préflights simultanés se verraient l'un l'autre comme un port occupé, et celui d'un
        # `start` attend ici qu'une installation ait fini, comme `shutdown`.
        self._setup_lock = threading.Lock()
        self._setup_result: PreflightResult | None = None
        self.state = MatchState(mode=mode)
        self.state.reset_steps()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._match: RealMatch | None = None
        self._redactor = Redactor(())
        # Une fois vrai, plus rien ne démarre : le service s'arrête.
        self._closing = False
        self._stop_incomplete = False
        self._shutdown_lock = threading.Lock()
        self._stopped: bool | None = None
        # `local_ip` n'est lu qu'à l'ouverture du lobby : il fait un `connect` (AC58).
        self._detected_ip: str | None = None
        self._share_token: str | None = None
        self._share: ShareServer | None = None
        # Le `.bat` de l'invitation en cours, en mémoire seulement, ou la raison de son absence.
        self._launcher: bytes | None = None
        self._launcher_refusal = INVITE_CLOSED
        # Survit aux lobbys : la revanche garde l'adresse que l'hôte a saisie.
        self._entered_address: str | None = None
        # Ouverture des ports (phase 3) : rien ne part sans un clic de l'hôte au lobby (R2).
        # `None` vise le groupe SSDP ; les tests passent le faux IGD local.
        self._upnp_target = upnp_target
        self._ports_opening: dict[str, Any] = {"status": PORTS_IDLE}
        self._ports_thread: threading.Thread | None = None
        self._ports_cancel = threading.Event()
        # Section `upnp` lue au clic qui a lancé la dernière ouverture : l'arrêt la reprend.
        self._ports_cfg: dict[str, Any] | None = None
        # Suivi de vie des mappings posés (P9F3) : `upnp.Ledger` en est l'unique propriétaire —
        # box, couple, échéance sur l'horloge monotone, état « à nous / peut-être à un tiers »,
        # proposition encore valable, retrait de l'arrêt. Le moteur ne fait que lui passer les
        # résultats et lui demander la vue et le retrait.
        self._ports_ledger = upnp.Ledger(self._monotonic)
        # Vrai dès que l'arrêt a commencé son retrait : une ouverture qui fusionne plus tard
        # dépose ses mappings dans le registre, que l'arrêt vide une seconde fois avant de rendre
        # la main (C1, P9F2).
        self._ports_released = False
        # Les couples de l'ouverture en cours, du clic à la fusion de son résultat ; vide sinon.
        self._ports_pending: list[tuple[int, str]] = []
        # Vrai quand l'arrêt a rendu la main sans cette fusion, en nommant ses couples : plus rien
        # de cette ouverture n'est fusionné ensuite (C1, P9F2).
        self._ports_abandoned = False
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
            state = self.state.to_dict()
            # L'invitation survit au lobby, pas l'annonce (AC60).
            if state["phase"] != "lobby":
                invite = state["invite"].items()
                state["invite"] = {key: value for key, value in invite if key not in LOBBY_ONLY}
            # Présent dans toutes les phases ; l'UI ne le montre qu'au lobby. `invite` n'en
            # reçoit rien : l'adresse proposée reste dans `ports_opening.address` (R5).
            state["ports_opening"] = self._ports_view()
            # Rien qui varie d'un appel à l'autre : l'UI compte le délai depuis son dernier appel
            # réussi, chacun étant un battement. `armed` est faux si l'arrêt ne peut pas tirer.
            state["idle_quit"] = {
                "after_s": self._idle_quit_s,
                "armed": self._idle_left_s() is not None,
                "ports_open": self._ports_held(),
            }
            return state

    # -- arrêt sur inactivité (P9F4) -----------------------------------------
    def heartbeat(self) -> None:
        """Quelqu'un regarde : seul `/api/state` l'appelle, par `main` (P9F4)."""
        with self.lock:
            self._watched_at = self._monotonic()

    def _idle_left_s(self) -> float | None:
        """Secondes avant l'arrêt sur inactivité, `None` s'il ne peut pas tirer. Sous `self.lock`.

        `None` quand `idle_quit_s` vaut 0, et hors d'`IDLE_QUIT_PHASES` (lancement, duel, arrêt) :
        le battement y est alors renouvelé, si bien que le délai repart entier à la fin d'un
        duel ou d'un lancement échoué, même sans navigateur. Négatif ou nul : le délai est échu.
        """
        if self._idle_quit_s == 0:
            return None
        now = self._monotonic()
        if self.state.phase not in IDLE_QUIT_PHASES:
            self._watched_at = now
            return None
        return self._idle_quit_s - (now - self._watched_at)

    def idle_due(self) -> bool:
        """Vrai quand `maintenant - dernier battement >= idle_quit_s`, en `IDLE_QUIT_PHASES`."""
        with self.lock:
            left = self._idle_left_s()
            return left is not None and left <= 0

    def claim_idle_stop(self) -> bool:
        """Engage l'arrêt sur inactivité s'il est dû : rend vrai si c'est fait (revue A1, P9F4).

        Le constat et l'engagement se font sous un même `self.lock`, que prennent aussi
        `heartbeat` et l'entrée en `countdown` : un battement arrivé avant compte, et un duel
        ne peut plus commencer après. `_closing` refuse `start` et `rematch`, `_stop` fait
        renoncer le fil du match au décompte ; l'appelant prend ensuite le chemin de `Quit`.
        """
        with self.lock:
            left = self._idle_left_s()
            if left is None or left > 0:
                return False
            self._closing = True
            self._stop.set()
            return True

    def _ports_held(self) -> bool:
        """Des redirections posées ou en cours de pose, que l'arrêt retirera. Sous `self.lock`."""
        return bool(self._ports_ledger.entries()) or bool(self._ports_pending)

    def presets(self) -> list[dict]:
        return self._presets

    # -- premier lancement -------------------------------------------------
    # Hors de `ACTIONS` : ce ne sont pas des actions de match (H6). Le dossier choisi ne survit
    # pas au service, la spécification ne le demande pas.
    def setup_state(self) -> dict:
        """Mesure sans rien écrire. Hors d'`idle`, rend la dernière mesure sans lier de port.

        Une UI qui sonde pendant un duel lierait sinon les ports du serveur. En mock, rien ne
        se mesure, et `ok` reste vrai pour garder le parcours simulé.
        """
        # Avant tout verrou : lire et hacher le mod figerait `snapshot`, comme dans `_measure`.
        expected = self._expected_fingerprint()
        if not self.real:
            return self._setup_view(None, expected)
        with self._setup_lock:
            if self._setup_refusal() is None:
                self._measure(self._default_table())
            with self.lock:
                return self._setup_view(self._setup_result, expected)

    def install_mod(self) -> tuple[bool, str]:
        with self._setup_lock:
            if refusal := self._setup_refusal():
                return False, refusal
            try:
                install_mod(
                    MOD_SRC, self.zomboid_dir, policy=self.runtime, platform=self._platform
                )
            except ModRestoreError as exc:
                # Le journal garde le chemin du mod sauvé : l'avis de l'UI, lui, disparaît.
                message = f"Could not install the mod: {exc}"
                with self.lock:
                    self.state.log(message, "warn")
                return False, message
            except OSError as exc:
                return False, f"Could not install the mod: {exc.strerror or exc}"
            with self.lock:
                self.state.log("mod installed")
            return True, "Mod installed"

    def set_pz_dir(self, folder: str) -> tuple[bool, str]:
        """Retient le dossier détecté, absolu : il ne dépend plus du dossier courant."""
        with self._setup_lock:
            if refusal := self._setup_refusal():
                return False, refusal
            detection = detect_pz(folder, os.environ, Path.home(), platform=self._platform)
            if detection.path is None:
                return False, detection.message
            with self.lock:
                self.pz_dir = str(detection.path)
                self.state.log(f"game folder set: {detection.path}")
            return True, f"Game folder set: {detection.path}"

    def _setup_refusal(self) -> str | None:
        with self.lock:
            if not self.real:
                return "Not available in simulated mode"
            if self._closing:
                return "The service is stopping"
            if self.state.phase != "idle":
                return f"Not available during phase {self.state.phase}"
        return None

    def _default_table(self) -> Any:
        """Sans table livrée, `{}` fait constater le défaut par le préflight."""
        try:
            return self.preset(DEFAULT_PRESET)
        except ActionRefused:
            return {}

    def _measure(self, table: Any) -> PreflightResult:
        """Appelé `_setup_lock` tenu, `self.lock` libre : un disque lent ne fige pas l'état."""
        with self.lock:
            pz_dir = self.pz_dir
        result = run_preflight(
            table,
            runtime=self.runtime,
            effects=self.effects,
            pz_dir=pz_dir,
            zomboid_dir=self.zomboid_dir,
            platform=self._platform,
        )
        with self.lock:
            self._setup_result = result
        return result

    def _expected_fingerprint(self) -> str | None:
        try:
            return mod_fingerprint(MOD_SRC)
        except OSError:
            return None

    def _setup_view(self, result: PreflightResult | None, expected: str | None) -> dict:
        values = {} if result is None else result.values
        return {
            "ok": not self.real if result is None else result.ok,
            "measured": result is not None,
            "pz": None if result is None else PRESENT if values["pz_dir"] else ABSENT,
            "defects": [] if result is None else [asdict(defect) for defect in result.defects],
            "values": values,
            "expected_mod_fingerprint": expected,
        }

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
        if not isinstance(name, str) or name not in ACTIONS:
            return False, f"action inconnue: {name}"
        if name == "open_game":
            return self._open_game()
        with self.lock:
            phase = self.state.phase
            if self._closing and name in ("start", "rematch"):
                return False, "service en cours d'arrêt"
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
                    code = cfg.get("code") if self.real else None
                    self._begin(cfg, code, cfg.get("host_password"))
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
        return False, "action non traitée"

    def shutdown(self) -> bool:
        """Arrête le service : plus aucun départ, duel en cours clos comme par `cancel`.

        Idempotent et sûr depuis tout fil, sauf celui du duel : un appel concurrent attend
        la fin de l'arrêt. Rend faux si des processus de serveur subsistent.
        """
        with self._shutdown_lock:
            if self._stopped is None:
                self._stopped = self._stop_service()
            return self._stopped

    def _stop_service(self) -> bool:
        """Le verrou reste libre pendant l'arrêt : `on_status` le prend, et un arrêt dure."""
        with self.lock:
            self._closing = True
            self._stop.set()
            match, thread = self._match, self._thread
        self._close_share()
        stopped = match is None or match.shutdown()
        if thread is not None:
            # L'état final et la fermeture du journal sont écrits par ce fil.
            thread.join()
        # `_closing` refuse les suivantes ; celle en cours tourne sur un fil démon de l'API, que
        # la sortie du service couperait entre les deux renommages d'`install_mod`.
        with self._setup_lock:
            pass
        self._release_ports()
        return stopped and not self._stop_incomplete

    def _open_game(self) -> tuple[bool, str]:
        """Ouvre le client depuis le lobby, l'invité identifié : ailleurs, A est arrêté ou la
        partie déjà jouée ; avant l'invité, le monde de l'hôte tournerait seul.

        Le verrou reste libre pendant que `launch_client` regarde le démarrage :
        `_client_starting` retient donc le second appel, jusqu'à ce que `_client` prenne le relais.
        """
        with self.lock:
            phase = self.state.phase
            if phase != "lobby":
                return False, f"impossible d'ouvrir le jeu en phase {phase} : il s'ouvre au lobby"
            # P9F5 (C2) : `PauseEmpty=true` fige le monde A tant que l'hôte n'y est pas ; s'y
            # connecter avant l'invité le ferait tourner seul. L'UI lit la même condition.
            if not self.state.players.get(GUEST_SIDE):
                return False, GUEST_NOT_HERE
            if not self.real:
                self.state.log("lancement du client PZ demandé (simulé)")
                return True, "client PZ lancé (simulé)"
            password = self.state.config["host_password"]
            address = self.state.config["host_address"]
            if self._client_starting or self._client_alive():
                return False, f"client PZ déjà lancé : rejoignez {address} depuis celui-ci"
            self._client_starting = True
        try:
            return self._open_game_real(password, address)
        finally:
            with self.lock:
                self._client_starting = False

    def _client_alive(self) -> bool:
        return self._client is not None and self._client.poll() is None

    def _open_game_real(self, password: str, address: str) -> tuple[bool, str]:
        """Lance le client de l'hôte vers le serveur A. La commande n'est jamais journalisée.

        Les serveurs tournent sans Steam, le client aussi, et par son seul programme : Steam ne
        rendrait aucun constat du lancement. Un succès dit que le client tourne, pas qu'il
        est connecté.
        """
        args = [NO_STEAM_FLAG, "+connect", address, "+password", password]
        found = self._client_install()
        try:
            if found is None:
                program = self._platform.client_program
                raise ClientLaunchError(f"client PZ introuvable ({program} absent)")
            script, folder = found
            client = self._client_launcher(script, args, cwd=folder)
        except ClientLaunchError as exc:
            manual = f"lancez PZ à la main avec {NO_STEAM_FLAG}, puis rejoignez {address}"
            message = f"{exc} : {manual}"
            with self.lock:
                self.state.log(message, "warn")
            return False, message
        message = f"client PZ lancé sans Steam : vérifiez en jeu la connexion à {address}"
        with self.lock:
            self._client = client
            self.state.log(message)
        return True, message

    def _client_install(self) -> tuple[Path, Path] | None:
        """Rend le programme client et le dossier de jeu détecté, ou `None` sans client."""
        folder = detect_pz(self.pz_dir, os.environ, Path.home(), platform=self._platform).path
        if folder is None:
            return None
        script = self._platform.client_file(folder)
        return (script, folder) if os.access(script, os.X_OK) else None

    # -- ouverture des ports (phase 3) ---------------------------------------
    # Hors d'`ACTIONS`, comme l'invitation. Aucun appel réseau sous `self.lock` (AC16) : le fil
    # `ports-open` fait les requêtes, puis reprend le verrou pour publier le résultat.
    def _upnp_ports(self) -> list[tuple[int, str]]:
        """Les trois couples de R3 : les deux UDP du camp B, le TCP de partage."""
        guest = self.runtime["ports"][GUEST_SIDE]
        return [
            (guest["game"], UDP),
            (guest["udp"], UDP),
            (self.runtime["share_port"], TCP),
        ]

    def _ports_view(self) -> dict[str, Any]:
        """Copie de l'état d'ouverture ; le neutre nomme les ports et le bail avant le clic (R8).

        Le registre (`upnp.Ledger`) dit si la dernière proposition `mapped` vaut encore, sur son
        horloge monotone (P9F3) : échue, un `mapped` redevient la forme neutre, marquée `expired`,
        sans l'adresse qu'il proposait (C5) ; quel que soit le dernier résultat (un nouvel essai
        en échec, une ouverture en cours), la vue porte `expired`, pour que l'UI la retire du
        champ (C5, P9F2). `expires_at` (heure murale) n'est que l'heure affichée.
        """
        view = copy.deepcopy(self._ports_opening)
        if self._ports_ledger.proposal() == upnp.PROPOSAL_EXPIRED:
            if view["status"] == upnp.MAPPED:
                view = {"status": PORTS_IDLE, "expired": True}
            else:
                view["expired"] = True
        if view["status"] in (PORTS_IDLE, PORTS_IN_PROGRESS):
            cfg = self.runtime.get("upnp")
            view.setdefault("ports", [{"port": p, "protocol": t} for p, t in self._upnp_ports()])
            view.setdefault("lease_s", cfg.get("lease_s") if isinstance(cfg, dict) else None)
        return view

    def open_ports(self) -> tuple[bool, str]:
        """Lance l'ouverture des trois mappings, à la demande de l'hôte, au lobby d'un duel réel."""
        with self.lock:
            if not self.real:
                return False, "not available in simulated mode"
            if self._closing:
                return False, "the service is stopping"
            phase = self.state.phase
            if phase != "lobby":
                return False, f"only in the lobby, phase {phase}"
            if self._ports_opening["status"] == PORTS_IN_PROGRESS:
                return False, "an opening is already in progress"
            ports = self._upnp_ports()
            cfg = self.runtime.get("upnp")
            invalid = upnp.validate(cfg, ports)
            if invalid is not None:
                # Aucun fil, aucun paquet : la configuration est refusée avant tout (AC13).
                self._ports_opening = invalid
                self.state.log(f"port opening: {upnp.INVALID_CONFIG}", "warn")
                return False, f"invalid configuration: {invalid['key']}"
            assert isinstance(cfg, dict)
            self._ports_cfg = dict(cfg)
            self._ports_opening = {"status": PORTS_IN_PROGRESS}
            self._ports_pending = list(ports)
            click = (self._clock(), self._ports_ledger.clock())
            self.state.log("port opening requested")
            self._ports_thread = threading.Thread(
                target=self._run_opening,
                args=(dict(cfg), ports, click),
                name="ports-open",
                daemon=True,
            )
            self._ports_thread.start()
        return True, "opening ports"

    def _run_opening(
        self, cfg: dict, ports: list[tuple[int, str]], click: tuple[float, float]
    ) -> None:
        """Corps du fil `ports-open` : le réseau hors du verrou, la fusion sous le verrou.

        `click` porte l'heure murale (affichée) et l'heure du registre (décisions) du clic. Une
        fusion qui lève ne laisse ni l'ouverture bloquée en `in_progress` ni ses mappings oubliés
        (revue I3) : `_merge_failed` retient tous ses couples, à vérifier avant tout retrait, et
        publie un statut terminal.
        """
        try:
            result, gateway, cleared = upnp.open_ports(
                cfg, ports, self._upnp_target, self._ports_cancel
            )
        except Exception as exc:  # noqa: BLE001 - `upnp` promet de ne rien lever ; dernier filet
            LOGGER.warning("port opening failed (%s)", type(exc).__name__)
            result = {
                "status": upnp.NO_GATEWAY,
                "reason": upnp.UNREADABLE,
                "ports": [{"port": p, "protocol": t} for p, t in ports],
                "manual": upnp.manual_instruction(ports),
            }
            gateway, cleared = None, []
        with self.lock:
            try:
                self._merge_opening(result, gateway, cleared, cfg, click)
            except Exception as exc:  # noqa: BLE001 - dernier filet : jamais d'ouverture bloquée
                LOGGER.warning("port opening result could not be merged (%s)", type(exc).__name__)
                self._merge_failed(ports, gateway, cfg, click)

    def _lease_terms(self, cfg: dict, click: tuple[float, float]) -> dict[str, Any]:
        wall, started = click
        return {
            "started": started,
            "expires_at": wall + cfg["lease_s"],
            "lease_s": cfg["lease_s"],
            "description": cfg["description"],
        }

    def _merge_opening(
        self,
        result: dict,
        gateway: upnp.Gateway | None,
        cleared: Sequence[tuple[int, str]],
        cfg: dict,
        click: tuple[float, float],
    ) -> None:
        """Publie le résultat et le confie au registre des mappings (P9F3).

        Appelé sous `self.lock`. Le journal d'état ne reçoit que le statut, jamais l'adresse (R5).
        Le registre est servi en premier : quoi qu'il arrive ensuite, les mappings actifs sont
        retenus. S'il a déjà commencé son retrait, l'arrêt le videra une seconde fois ; s'il a déjà
        rendu la main en abandonnant cette ouverture (couples nommés), rien n'est fusionné (C1).

        `expires_at` vaut heure du clic + `lease_s` : une borne conservatrice (C6), jamais
        postérieure à l'expiration réelle d'un mapping, puisque chaque bail ne commence qu'à son
        `AddPortMapping`, après la découverte et les requêtes précédentes. C'est l'heure affichée ;
        les décisions, elles, se prennent sur l'horloge monotone du registre.
        """
        if self._ports_abandoned:
            return
        terms = self._lease_terms(cfg, click)
        self._ports_ledger.record(result, gateway, cleared, **terms)
        self._ports_pending = []
        status = result.get("status")
        view = dict(result)
        if status == upnp.MAPPED:
            view["expires_at"] = terms["expires_at"]
        self._ports_opening = view
        level = "info" if status == upnp.MAPPED else "warn"
        self.state.log(f"port opening: {status}", level)

    def _merge_failed(
        self,
        ports: list[tuple[int, str]],
        gateway: upnp.Gateway | None,
        cfg: dict,
        click: tuple[float, float],
    ) -> None:
        """Filet d'une fusion en échec (revue I3), sous `self.lock`, sans rien qui puisse lever.

        Avec une box, les trois couples sont retenus comme possiblement posés, et le statut
        terminal est un `refused` illisible qui les nomme ; sans box, `no_gateway`. L'issue réelle
        de l'ouverture étant inconnue, ces couples ne sont jamais présumés à nous (revue P9F3 A1) :
        `Ledger.suspect` les fait vérifier auprès de la box avant toute suppression à l'arrêt, et
        laisse intacts ceux qu'une fusion partielle a déjà retenus.
        """
        if self._ports_abandoned:
            return
        self._ports_pending = []
        dicts = [{"port": p, "protocol": t} for p, t in ports]
        if gateway is not None:
            terms = self._lease_terms(cfg, click)
            self._ports_ledger.suspect(
                gateway, ports, expires_at=terms["expires_at"], description=terms["description"]
            )
            self._ports_opening = {
                "status": upnp.REFUSED,
                "failure": upnp.UNREADABLE,
                "ports": dicts,
                "still_active": list(dicts),
                "manual": upnp.manual_instruction(ports),
            }
        else:
            self._ports_opening = {
                "status": upnp.NO_GATEWAY,
                "reason": upnp.UNREADABLE,
                "ports": dicts,
                "manual": upnp.manual_instruction(ports),
            }

    def _report_release(self, released: upnp.Released) -> None:
        """Consigne l'issue d'un retrait : port et protocole seulement, jamais d'adresse (R6)."""
        with self.lock:
            if released.foreign:
                named = ", ".join(f"{port}/{protocol}" for port, protocol in released.foreign)
                message = (
                    f"port mappings left in place at shutdown: {named} "
                    "now belong to another device on the network"
                )
                LOGGER.info(message)
                self.state.log(message)
            if released.failed:
                named = ", ".join(f"{port}/{protocol}" for port, protocol in released.failed)
                message = f"could not remove port mappings at shutdown: {named}"
                LOGGER.warning(message)
                self.state.log(message, "warn")
            elif not released.foreign:
                self.state.log("port mappings removed")

    def _release_ports(self) -> None:
        """Arrêt (R6) : arrête l'ouverture en cours, puis fait retirer par le registre ce qu'il retient.

        Ni découverte ni description : le registre joint chaque box par la passerelle gardée de
        son ouverture, et décide seul, mapping par mapping, s'il supprime directement (à nous) ou
        s'il vérifie d'abord (peut-être à un tiers). Rien n'est envoyé quand rien n'est retenu
        (AC24). Toutes les requêtes du retrait, toutes box et les deux passes confondues, tiennent
        dans `MAPPING_COUNT` délais de requête (R6, revue A4/I4) : le temps passé à attendre le
        fil d'ouverture n'en est pas décompté. Les échecs ne nomment que port et protocole.

        C1 (P9F2) : l'arrêt ne rend pas la main avant d'avoir retiré ce que l'ouverture en cours a
        posé, ou d'avoir abandonné cette ouverture en nommant ses couples. Une fusion qui arrive
        après le premier retrait dépose ses mappings dans le registre, que la seconde passe vide.
        Une ouverture qui n'a pas fusionné après la seconde attente est abandonnée sous le verrou
        (`_ports_abandoned`) : ce qu'elle rendra ensuite n'est plus fusionné, et ses couples sont
        nommés comme possiblement encore ouverts. Attentes bornées : la découverte et une requête,
        puis une requête encore en vol. Pire cas : `discovery_timeout_s` + (`MAPPING_COUNT` + 2)
        × `request_timeout_s`.
        """
        self._ports_cancel.set()
        with self.lock:
            thread, cfg = self._ports_thread, self._ports_cfg
        if thread is not None and cfg is not None:
            # L'ouverture s'arrête à sa frontière de requête suivante : au plus une requête,
            # ou la découverte, encore en vol. Elle fusionne elle-même son résultat.
            thread.join(cfg["discovery_timeout_s"] + cfg["request_timeout_s"])
        with self.lock:
            self._ports_released = True
            pending = bool(self._ports_pending)
        if cfg is None:
            return
        timeout_s, max_bytes = cfg["request_timeout_s"], cfg["max_response_bytes"]
        deadline = upnp.deadline_in(upnp.MAPPING_COUNT * timeout_s)
        released = self._ports_ledger.release(timeout_s, max_bytes, deadline)
        if pending and thread is not None:
            LOGGER.warning("port opening still running at shutdown")
            # Une requête encore en vol, puis la fusion sous le verrou ; cette attente ne mange
            # pas le budget des suppressions.
            waited = time.monotonic()
            thread.join(timeout_s)
            deadline += time.monotonic() - waited
        with self.lock:
            abandoned = list(self._ports_pending)
            if abandoned:
                self._ports_abandoned = True
                self._ports_pending = []
        released.extend(self._ports_ledger.release(timeout_s, max_bytes, deadline))
        if released.tried:
            self._report_release(released)
        if abandoned:
            named = ", ".join(f"{port}/{protocol}" for port, protocol in abandoned)
            message = (
                f"port opening abandoned at shutdown: {named} may stay open on the router "
                "until their lease ends"
            )
            with self.lock:
                LOGGER.warning(message)
                self.state.log(message, "warn")

    # -- invitation ------------------------------------------------------
    # Hors d'`ACTIONS`, comme le premier lancement (H6). Rien ici ne touche au réseau (AC58).
    def set_invite_address(self, address: object) -> tuple[bool, str]:
        """Retient l'adresse saisie ; un refus ne la cite pas et garde la précédente."""
        mod = self._read_mod() if self.real and is_usable_address(address) else None
        with self.lock:
            if not self.real or self.state.phase != "lobby":
                return False, INVITE_CLOSED
            if not is_usable_address(address):
                return False, ADDRESS_REFUSED
            self._entered_address = address
            self.state.invite = self._build_invite(mod)
            self.state.log("invitation address updated")
        return True, "invitation address updated"

    def guest_launcher(self) -> bytes:
        """Rend le `.bat` de l'invitation, ou lève `LauncherRefused`. Rien n'est écrit sur disque."""
        with self.lock:
            if not self.real or self.state.phase != "lobby":
                raise LauncherRefused(INVITE_CLOSED)
            if self._launcher is None:
                raise LauncherRefused(self._launcher_refusal)
            return self._launcher

    def _read_mod(self) -> tuple[str, ...] | LauncherRefused:
        """Lit et hache le mod, jamais sous `self.lock` : un disque lent figerait `/api/state`."""
        try:
            return read_mod(MOD_SRC)
        except LauncherRefused as refused:
            return refused

    def _open_invite(self, mod: tuple[str, ...] | LauncherRefused | None) -> None:
        """Ouvre l'invitation d'un lobby : seul appel à `local_ip`, jeton neuf, revanche comprise.

        `mod` vient de `_read_mod`, appelé avant le verrou ; `None` hors du vrai mode.
        """
        self._detected_ip = local_ip()
        self._share_token = token_hex(SHARE_TOKEN_BYTES)
        if self.real:
            self._open_share()
        self.state.invite = self._build_invite(mod)

    def _open_share(self) -> None:
        """Un port pris laisse le lobby ouvert : le `.bat` s'enregistre encore depuis l'UI."""
        port = self.runtime["share_port"]
        try:
            poll_s = self.runtime["tick_ms"] / MS_PER_S
            peer_max = self.runtime["share_connections_per_peer"]
            self._share = ShareServer(self, self._share_token, port, poll_s, peer_max=peer_max)
        except OSError:
            self.state.log(f"share link unavailable: TCP port {port} could not be opened", "warn")

    def _close_share(self) -> None:
        """Idempotent, à toute sortie du lobby. `close` attend l'écouteur, hors du verrou."""
        with self.lock:
            share, self._share = self._share, None
            self._share_token = None
            if share is None:
                return
            # Une copie : `snapshot` a pu rendre ce dictionnaire à un lecteur encore en cours.
            closed = {"link": None, "qr_text": None, "share_available": False}
            self.state.invite = {**self.state.invite, **closed}
        share.close()

    def _build_invite(self, mod: tuple[str, ...] | LauncherRefused | None) -> dict:
        """Rend l'invitation et génère son `.bat`, une fois : le code porte sur les octets servis.

        Appelé sous `self.lock` : `mod`, lu et haché avant par `_read_mod`, n'y est qu'assemblé.
        """
        invite = self._invite_fields()
        self._launcher = None
        if not self.real:
            return invite
        try:
            self._launcher = build_launcher(
                invite["address"], invite["port"], invite["password"], mod
            )
        except LauncherRefused as refused:
            self._launcher_refusal = str(refused)
            return invite
        invite["launcher_code"] = launcher_code(self._launcher)
        invite["launcher_code_text"] = LAUNCHER_CODE_TEXT.format(name=LAUNCHER_NAME)
        return invite

    def _invite_fields(self) -> dict:
        """Seule source de l'invitation. `invite` sort par l'API : jamais `host_password`."""
        config = self.state.config
        side = GUEST_SIDE if self.real else HOST_SIDE
        invite = {
            "ip": self._detected_ip,
            "port": self.runtime["ports"][side]["game"],
            "password": config["password"],
            "code": config["code"],
            "address": None,
            "link": None,
            "qr_text": None,
            "share_available": self._share is not None,
            "needs_address": False,
            "scope": SCOPE_MOCK,
            "scope_text": SCOPE_MOCK_TEXT,
        }
        if not self.real:
            return invite
        invite[REMATCH_NOTE] = REMATCH_NOTE_TEXT
        if self._share is None:
            invite[SHARE_TEXT] = SHARE_UNAVAILABLE_TEXT.format(port=self.runtime["share_port"])
        detected = self._detected_ip if is_usable_address(self._detected_ip) else None
        address = self._entered_address or detected
        if address is None:
            invite["needs_address"] = True
            invite["scope"] = SCOPE_NO_ADDRESS
            invite["scope_text"] = SCOPE_NO_ADDRESS_TEXT
            return invite
        invite["ip"] = invite["address"] = address
        if self._entered_address is None:
            invite["scope"] = SCOPE_LAN
            invite["scope_text"] = SCOPE_LAN_TEXT
        else:
            ports = self.runtime["ports"][GUEST_SIDE]
            invite["scope"] = SCOPE_UNVERIFIED
            invite["scope_text"] = SCOPE_UNVERIFIED_TEXT.format(
                game=ports["game"], udp=ports["udp"], share=self.runtime["share_port"]
            )
        if PASSWORD_ALLOWED.fullmatch(config["password"]) is None:
            # Le mot de passe saisi ne peut pas voyager dans un `.bat` (D-H).
            invite["launcher_refused"] = PASSWORD_REFUSED
            return invite
        if self._share is None:
            return invite  # un lien mort ne s'affiche pas
        share_port = self.runtime["share_port"]
        link = f"http://{address}:{share_port}/{self._share_token}/{LAUNCHER_NAME}"
        invite["link"] = invite["qr_text"] = link
        return invite

    # -- moteur ----------------------------------------------------------
    def _begin(
        self, payload: dict, code: str | None = None, host_password: str | None = None
    ) -> None:
        """`code` et `host_password` viennent de la revanche seule, jamais d'un `start`.

        Tout le `payload` se valide ici, avant le premier effet : un refus laisse l'état, les
        mondes et les sauvegardes tels quels.
        """
        preset_id = payload.get("preset", DEFAULT_PRESET)
        if not isinstance(preset_id, str):
            raise ActionRefused("preset refusé : identifiant texte attendu")
        preset = self.preset(preset_id)
        pseudo = _text_option(payload, "pseudo", "pseudo")
        password = _text_option(payload, "password", "mot de passe")
        if self.real:
            for label, value in (("pseudo", pseudo), ("mot de passe", password)):
                if value:
                    _check_ini_value(label, value)
        duration = _duration_of(payload, preset)
        cfg = {
            "pseudo": (pseudo or "hôte")[:20],
            "preset": preset["id"],
            "preset_label": preset["label"],
            "duration_s": duration,
            "day_length": self._day_length_of(payload, preset),
            "code": code or make_code(),
            "summary": preset["summary"],
        }
        # `password` ouvre B et part dans l'invitation ; `host_password` n'ouvre que A.
        cfg["password"] = password or gen_player_password()
        cfg["host_password"] = host_password or gen_player_password()
        cfg["host_address"] = f"{CLIENT_HOST}:{self.runtime['ports'][HOST_SIDE]['game']}"
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

    def _day_length_of(self, payload: Mapping[str, Any], preset: Mapping[str, Any]) -> int:
        """Contrôle la valeur effective : c'est elle que la dérive cherchera dans la table."""
        value = payload.get("day_length")
        value = preset["day_length"] if value is None else value
        known = self.runtime["day_length_minutes"]
        if type(value) is not int or str(value) not in known:
            raise ActionRefused(f"DayLength refusé : valeur attendue parmi {', '.join(known)}")
        return value

    def _guarded(self, run: Callable[[], None]) -> None:
        """Dernier filet : `_redactor` connaît les secrets même si `RealMatch` n'a pas pu naître."""
        try:
            run()
        except Exception as exc:
            self._fail(f"erreur interne: {self._redactor.redact_exception(exc)}")

    def _fail(self, message: str) -> None:
        self._close_share()
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
        with self._setup_lock:
            result = self._measure(table)
        if self._aborted():
            return
        with self.lock:
            self.state.set_step("preflight", "ok" if result.ok else "error", result.detail)
        if not result.ok:
            self._fail("préflight en échec : " + " ; ".join(result.failures))
            return
        match = self._open_match(table, result.values["pz_dir"], result.values["mod_fingerprint"])
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

    def _open_match(
        self, table: Mapping[str, Any], pz_dir: str, mod_print: str | None
    ) -> RealMatch | None:
        """Crée le match sous le verrou : `cancel` voit soit `_match`, soit `_stop` ici relu."""
        with self.lock:
            if self._stop.is_set():
                return None
            secrets = MatchSecrets(gen_secret(), gen_secret())
            self._redactor = Redactor(secrets)
            self._match = RealMatch(
                self.state.config,
                table,
                secrets=secrets,
                runtime=self.runtime,
                effects=self.effects,
                zomboid_dir=self.zomboid_dir,
                pz_dir=pz_dir,
                matches_dir=self.matches_dir,
                clock=self._clock,
                monotonic=self._monotonic,
                proofs=self._proofs,
                mod_print=mod_print,
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
            left_lobby = self.state.phase not in SHARE_PHASES
        if left_lobby:
            self._close_share()

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
            if failure := match.pump(int(self._monotonic() * MS_PER_S)):
                return failure
            if match.result is not None:
                break
        return None

    def _open_lobby(self, match: RealMatch) -> None:
        mod = self._read_mod()  # avant le verrou : le hachage du mod ne fige pas `/api/state`
        with self.lock:
            self.state.set_step("contract", "ok", "contrat vérifié sur A et B")
            self.state.set_step("lobby", "running", "en attente des joueurs")
            self._open_invite(mod)
            self.state.log("lobby ouvert, invitation prête")
        match.announce_table()
        match.enter_phase("lobby")

    def _finish_match(self, match: RealMatch, failure: str | None) -> None:
        """Toute sortie passe ici : arrêt d'abord, puis état final, puis fermeture du journal."""
        self._close_share()
        match.enter_phase("stopping")
        stopped = match.shutdown()
        with self.lock:
            self._match = None
            self._stop_incomplete = self._stop_incomplete or not stopped
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
        self._close_share()
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
            self._open_invite(None)
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
            # Même règle que `RealMatch._enter_countdown` : vu sous `self.lock`, un arrêt
            # engagé (`claim_idle_stop`, `shutdown`) n'ouvre pas de duel.
            stopping = self._stop.is_set()
            if not stopping:
                self.state.set_step("lobby", "ok", "les deux joueurs sont connectés")
                self.state.phase = "countdown"
                self.state.clock = {"remaining_s": self.state.config["duration_s"], "drift_s": 0}
                self.state.log("décompte")
        if stopping:
            return self._cancel_state()
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
