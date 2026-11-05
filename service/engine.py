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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .preflight import run_preflight
from .rules import load_effects, load_runtime, load_table
from .servers import LINE_BREAK, ServerProcess
from .state import MatchState

PRESETS_DIR = os.path.join(os.path.dirname(__file__), "presets")
MATCHES_DIR = Path(__file__).resolve().parent.parent / "matches"

ACTIONS = {"start", "cancel", "rematch", "simulate_join", "open_game"}
MODE_MOCK = "mock"
MODE_REAL = "real"
MODES = (MODE_MOCK, MODE_REAL)
STEAM_BINARY = "steam"
# L'hôte joue sur la machine du serveur A.
CLIENT_HOST = "127.0.0.1"
# Transitoire : P18 retire cet arrêt, P24 vérifie qu'il n'en reste rien.
REAL_LAUNCH_PENDING = (
    "préflight réussi, mais le lancement réel des serveurs n'est pas encore disponible : "
    "aucun serveur n'a été démarré"
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
        if result.ok:
            self._fail(REAL_LAUNCH_PENDING)
        else:
            self._fail("préflight en échec : " + " ; ".join(result.failures))

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
