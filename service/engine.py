"""Orchestration d'un duel, en mode simulé ou réel.

En mock, aucun serveur PZ n'est lancé : la machine à états, les latences, les événements
et le score sont simulés pour valider l'expérience utilisateur, et tout ce qui ressemble
à une vérification s'annonce « simulé ». En réel, le mode demandé est tenu de bout en
bout : un échec finit en `error`, jamais dans le parcours simulé.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import socket
import string
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .journal import Journal
from .preflight import run_preflight
from .protocol import PROTOCOL_VERSION, SIDES
from .rules import MS_PER_S, load_effects, load_runtime, load_table, rules_hash
from .servers import (
    LINE_BREAK,
    STATUS_ERROR,
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
from .transport import CommandChannel, OutReader

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


class RealMatch:
    """Un lancement réel, de la préparation à l'arrêt, dans l'ordre de D23.

    Le constructeur tire `match_id` et les secrets et arme le `Redactor` : la passe de
    création écrit déjà `server_X.log`. `on_status` suit `ServerPair` ; `stop` est l'événement
    d'annulation du moteur. `shutdown` est le seul chemin d'arrêt : `cancel`, erreur et fin.
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
    ) -> None:
        self.config = config
        self.table = table
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
        self._channels = {side: CommandChannel(side, lua_dir) for side in SIDES}
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
                "rules_hash": rules_hash(self.table),
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
        caps: dict[str, Mapping[str, Any] | None] = dict.fromkeys(SIDES)
        deadline = time.monotonic() + self.runtime["contract_timeout_s"]
        while True:
            self._poll_out(caps)
            if all(caps.values()) or time.monotonic() >= deadline or self._any_exited():
                return caps
            if self._stop.wait(self.poll_s):
                return caps

    def _poll_out(self, caps: dict[str, Mapping[str, Any] | None]) -> None:
        for side, reader in self._readers.items():
            events, anomalies = reader.poll()
            for anomaly in anomalies:
                self._journal.write(anomaly.type, anomaly.data, anomaly.side)
            for event in events:
                self._journal.write("event", event, event["side"])
                if event["kind"] == "caps":
                    caps[side] = event["data"]

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
                reason = f"serveur {side} : sortie inattendue, code {server.exit_code}"
                detail = "\n".join([reason, *server.log_tail()])
                self._on_status(side, STATUS_ERROR, detail)
                return detail
        return None

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
            journal.write("state", data, side)
        except (OSError, ValueError) as exc:
            LOGGER.error("journal : %s", self.redactor.redact_exception(exc))

    def shutdown(self) -> bool:
        """Arrête les serveurs une seule fois, depuis n'importe quel fil, verrou du moteur libre.

        Un appel concurrent attend la fin de l'arrêt et en rend le résultat.
        """
        with self._shutdown_lock:
            if self._stopped is None:
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
                    self._begin(cfg)
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
    def _begin(self, payload: dict) -> None:
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
            "code": make_code(),
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
            )
            self.state.match_id = self._match.match_id
            self.state.config["rules_hash"] = rules_hash(table)
            self.state.phase = "starting"
            self.state.log("préparation des mondes et des réglages")
            return self._match

    def _on_server_status(self, side: str, status: str, detail: str) -> None:
        with self.lock:
            apply_status(self.state, side, status, detail)

    def _enter_phase(self, match: RealMatch, phase: str) -> None:
        """Le journal s'écrit hors du verrou : un `fsync` ne doit pas figer `/api/state`."""
        with self.lock:
            self.state.phase = phase
        match.record_phase(phase)

    def _drive_match(self, match: RealMatch) -> str | None:
        """Mène le match jusqu'au lobby et l'y tient. Rend le motif d'échec, `None` si annulé."""
        match.prepare()
        match.record_phase("starting")
        if self._stop.is_set():
            return None
        if not match.launch():
            return match.pair.failure
        self._enter_phase(match, "contract")
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
            if lost := match.lost_server():
                return lost
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
        self._enter_phase(match, "lobby")

    def _finish_match(self, match: RealMatch, failure: str | None) -> None:
        """Toute sortie passe ici : arrêt d'abord, puis état final, puis fermeture du journal."""
        self._enter_phase(match, "stopping")
        stopped = match.shutdown()
        with self.lock:
            self._match = None
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
