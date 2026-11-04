"""Orchestration d'un duel — backend simulé (MVP).

Aucun serveur PZ n'est lancé ici : la machine à états, les latences, les
événements et le score sont simulés pour valider l'expérience utilisateur.
Le contrat d'API et les phases sont ceux de la spec (07-spec-mvp.md).
"""

from __future__ import annotations

import json
import os
import random
import socket
import string
import threading
import time

from .state import MatchState

PRESETS_DIR = os.path.join(os.path.dirname(__file__), "presets")

ACTIONS = {"start", "cancel", "rematch", "simulate_join", "open_game"}


def load_presets() -> list[dict]:
    out = []
    for name in sorted(os.listdir(PRESETS_DIR)):
        if name.endswith(".json"):
            with open(os.path.join(PRESETS_DIR, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


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


class Engine:
    def __init__(self, mode: str = "mock") -> None:
        self.lock = threading.RLock()
        self.state = MatchState(mode=mode)
        self.state.reset_steps()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._presets = load_presets()
        self.state.log("service démarré (backend simulé)")

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
                self._begin(payload)
                return True, "démarrage"
            if name == "rematch":
                if phase not in ("over", "error"):
                    return False, "revanche possible seulement après un match"
                cfg = dict(self.state.config)
                self._begin(cfg)
                return True, "revanche"
            if name == "cancel":
                if phase in ("idle", "over"):
                    return False, "rien à annuler"
                self._stop.set()
                self.state.log("annulation demandée", "warn")
                return True, "annulation"
            if name == "simulate_join":
                if phase != "lobby":
                    return False, "personne à faire entrer (phase %s)" % phase
                if self.state.players.get("B"):
                    return False, "l'adversaire est déjà là"
                self.state.players["B"] = "invité (sim)"
                self.state.log("adversaire simulé connecté")
                self.state.event("adversaire connecté", "B")
                return True, "invité connecté"
            if name == "open_game":
                self.state.log("lancement du client PZ demandé (simulé)")
                return True, "client PZ lancé (simulé)"
        return False, "action non traitée"

    # -- moteur ----------------------------------------------------------
    def _begin(self, payload: dict) -> None:
        preset = self.preset(payload.get("preset", "duel_rapide"))
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
        self.state = MatchState(mode="mock", config=cfg)
        self.state.reset_steps()
        self._stop.clear()
        self.state.log(f"préparation du duel « {preset['label']} » ({duration}s)")
        self._thread = threading.Thread(target=self._run, name="duel-engine", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
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
        except Exception as exc:  # pragma: no cover - garde-fou
            with self.lock:
                self.state.phase = "error"
                self.state.error = f"erreur interne: {exc}"
                self.state.log(self.state.error, "error")

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
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set():
                return False
            time.sleep(0.05)
        return True

    def _phase_preflight(self) -> None:
        with self.lock:
            self.state.phase = "preflight"
            self.state.set_step("preflight", "running")
            self.state.log("préflight: installation, ports, mémoire, mods")
        self._sleep(1.2)
        with self.lock:
            self.state.set_step(
                "preflight",
                "ok",
                "PZ 42.21 · ports 17261/17271 libres · 31 Go RAM · mods présents (simulé)",
            )

    def _phase_starting(self) -> None:
        with self.lock:
            self.state.phase = "starting"
            self.state.set_step("serverA", "running")
            self.state.log("démarrage serveur A (17261)")
        if not self._sleep(2.4):
            return self._cancel_state()
        with self.lock:
            self.state.servers["A"] = "up"
            self.state.set_step("serverA", "ok", "démarré en 2,4 s (simulé)")
            self.state.set_step("serverB", "running")
            self.state.log("démarrage serveur B (17271)")
        if not self._sleep(3.6):
            return self._cancel_state()
        with self.lock:
            self.state.servers["B"] = "up"
            self.state.set_step("serverB", "ok", "démarré en 3,6 s (simulé)")
            self.state.log("les deux serveurs sont prêts")

    def _phase_contract(self) -> None:
        with self.lock:
            self.state.phase = "contract"
            self.state.set_step("contract", "running")
        if not self._sleep(0.8):
            return self._cancel_state()
        with self.lock:
            self.state.set_step("contract", "ok", "mods identiques sur A et B")

    def _phase_lobby(self) -> None:
        with self.lock:
            self.state.phase = "lobby"
            self.state.set_step("lobby", "running", "en attente de l'adversaire")
            self.state.invite = {
                "ip": local_ip(),
                "port": 17261,
                "password": self.state.config["password"],
                "code": self.state.config["code"],
            }
            self.state.log("lobby ouvert, invitation prête")
        # l'hôte rejoint son propre serveur
        if not self._sleep(1.2):
            return self._cancel_state()
        with self.lock:
            self.state.players["A"] = self.state.config["pseudo"] + " (toi)"
            self.state.log("hôte connecté au serveur A")
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
            self.state.started_at = time.time()
            self.state.clock = {"remaining_s": cfg["duration_s"], "drift_s": 0}
            self.state.log("match lancé")
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
            self.state.log(f"match terminé — A {a} / B {b} ({verdict})")
            self.state.event(f"fin du match — {verdict}")
