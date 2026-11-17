"""État partagé du service duel, en mode simulé comme en mode réel."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

from .build_info import build_id

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

# Le lobby seul affiche à l'hôte son accès : ailleurs, ce mot de passe ne sort pas de l'état.
HOST_SECRET_PHASE = "lobby"
HOST_SECRET = "host_password"

# P9F21, corrigé en P9F26 : les quatre appels de la tenue, dans l'ordre où le mod les pose pour
# chaque joueur. **Source unique** : `service/engine.py` importe cette constante, il ne la
# redéfinit plus. Deux copies de la même liste ont déjà divergé — le lobby annonçait un échec
# sur un appel que le mod ne posait plus.
HOLD_CALLS = ("setInvulnerable", "setZombiesDontAttack", "setInvisible", "setIgnoreMovement")


def held_player(player: str, calls: list[str]) -> dict:
    """Un joueur tenu tel que `/api/state` le publie dans `hold.players`, phrase du lobby comprise.

    « tenu : <pseudo> (4/4) », ou « tenu : <pseudo> (3/4, setInvisible en échec) » : le compte,
    et le premier appel de la séquence qui manque. La page affiche `label` sans rien calculer.
    """
    missing = next((call for call in HOLD_CALLS if call not in calls), None)
    count = f"{len(calls)}/{len(HOLD_CALLS)}"
    detail = count if missing is None else f"{count}, {missing} en échec"
    return {
        "player": player,
        "calls": list(calls),
        "complete": missing is None,
        "label": f"tenu : {player} ({detail})",
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
    match_id: str | None = None
    capabilities: dict = field(default_factory=dict)
    result: dict | None = None
    # P9F31 : l'identité du binaire, lue une fois ; `dev` depuis les sources.
    build: dict = field(default_factory=build_id)

    # --- helpers ---------------------------------------------------------
    def log(self, msg: str, level: str = "info") -> None:
        self.logs.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        if len(self.logs) > 400:
            del self.logs[: len(self.logs) - 400]

    def event(self, text: str, side: str = "-", what: dict | None = None) -> None:
        """Ajoute une ligne au fil ; `what`, s'il est donné, dit le fait sans phrase."""
        line = {"t": time.strftime("%H:%M:%S"), "side": side, "text": text}
        if what:
            line["what"] = what
        self.events.append(line)
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

    def public_config(self) -> dict:
        """Rend `config` telle qu'elle sort de l'API ; `self.config` garde tout pour la revanche."""
        if self.phase == HOST_SECRET_PHASE:
            return self.config
        return {key: value for key, value in self.config.items() if key != HOST_SECRET}

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "phase": self.phase,
            "phase_label": PHASE_LABELS.get(self.phase, self.phase),
            "mode": self.mode,
            "config": self.public_config(),
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
            "match_id": self.match_id,
            "capabilities": self.capabilities,
            "result": self.result,
            "build": dict(self.build),
        }
