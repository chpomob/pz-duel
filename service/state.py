"""État partagé du service duel (MVP, backend simulé)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

PHASE_LABELS = {
    "idle": "au repos",
    "preflight": "vérifications",
    "starting": "démarrage des serveurs",
    "contract": "vérification du contrat",
    "lobby": "lobby",
    "countdown": "décompte",
    "running": "match en cours",
    "over": "terminé",
    "stopping": "arrêt",
    "error": "erreur",
}

LAUNCH_STEPS = [
    ("preflight", "Préflight"),
    ("serverA", "Serveur A"),
    ("serverB", "Serveur B"),
    ("contract", "Contrat"),
    ("lobby", "Lobby"),
]


@dataclass
class Step:
    id: str
    label: str
    state: str = "pending"  # pending | running | ok | error
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MatchState:
    phase: str = "idle"
    mode: str = "mock"
    config: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    servers: dict = field(default_factory=lambda: {"A": "down", "B": "down"})
    players: dict = field(default_factory=lambda: {"A": None, "B": None})
    invite: dict = field(default_factory=dict)
    clock: dict = field(default_factory=lambda: {"remaining_s": 0, "drift_s": None})
    score: dict = field(default_factory=lambda: {"A": 0, "B": 0})
    threats_per_min: float = 0.0
    events: list = field(default_factory=list)
    error: str | None = None
    logs: list = field(default_factory=list)
    started_at: float | None = None
    version: int = 1

    # --- helpers ---------------------------------------------------------
    def log(self, msg: str, level: str = "info") -> None:
        self.logs.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        if len(self.logs) > 400:
            del self.logs[: len(self.logs) - 400]

    def event(self, text: str, side: str = "-") -> None:
        self.events.append({"t": time.strftime("%H:%M:%S"), "side": side, "text": text})
        if len(self.events) > 60:
            del self.events[: len(self.events) - 60]

    def reset_steps(self) -> None:
        self.steps = [Step(i, label).to_dict() for i, label in LAUNCH_STEPS]

    def set_step(self, step_id: str, state: str, detail: str = "") -> None:
        for s in self.steps:
            if s["id"] == step_id:
                s["state"] = state
                if detail:
                    s["detail"] = detail
                return

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "phase": self.phase,
            "phase_label": PHASE_LABELS.get(self.phase, self.phase),
            "mode": self.mode,
            "config": self.config,
            "steps": self.steps,
            "servers": self.servers,
            "players": self.players,
            "invite": self.invite,
            "clock": self.clock,
            "score": self.score,
            "threats_per_min": self.threats_per_min,
            "events": self.events[-12:],
            "error": self.error,
            "logs": self.logs[-80:],
        }
