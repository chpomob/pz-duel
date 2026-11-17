"""Double du mod `duelcore` : écrit `out` et `ack`, et lit `session`.

Il produit des lignes du contrat sur demande. Il ne prouve rien du vrai Lua. Les
paramètres `src`, `side`, `boot` et `seq` de `emit` servent à forger des lignes
fausses ; sans eux, le double se comporte comme un mod conforme : un atome de camp
porte `player`, le premier nom de son dernier heartbeat, sauf `data` qui le fixe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from service.protocol import PROTOCOL_VERSION, dump_line, parse_session

OWN_SIDE = object()
SERVER_KINDS = ("beat", "caps", "cmd_result", "hook_seen")
# Relevé sur le vrai jeu (README, premier contact), pour `DayLength`=1 et `PauseEmpty=false`.
EMPTY_BEAT_S = 10
EMPTY_AGE_PER_BEAT = 0.27


class FakeMod:
    def __init__(self, lua_dir: str | Path, side: str, boot: int = 1, t_ms: int = 0) -> None:
        lua_dir = Path(lua_dir)
        self.side = side
        self.boot = boot
        self.seq = 0
        self.t_ms = t_ms
        self.player: str | None = None
        self.out_path = lua_dir / f"duel_{side}_out.txt"
        self.in_path = lua_dir / f"duel_{side}_in.txt"
        self.ack_path = lua_dir / f"duel_{side}_ack.txt"
        self.session_path = lua_dir / f"duel_{side}_session.txt"

    @classmethod
    def resuming(cls, lua_dir: str | Path, side: str) -> FakeMod:
        """Le mod déjà en route sur ce camp : il reprend au `(boot, seq)` de la dernière ligne de
        `out`, celle que le serveur lancé y a écrite. Ses lignes suivantes sont donc les suivantes
        du flux, ni doublon ni trou, comme celles du vrai mod au fil du match."""
        mod = cls(lua_dir, side)
        lines = mod.out_path.read_bytes().splitlines() if mod.out_path.exists() else []
        last = next((json.loads(line) for line in reversed(lines) if line.strip()), None)
        if last is not None:
            mod.boot, mod.seq, mod.t_ms = last["boot"], last["seq"], last["t_ms"]
        return mod

    def restart(self, boot: int | None = None) -> None:
        """Relance le mod : `seq` repart de 1 au `boot` donné, ou au suivant."""
        self.boot = self.boot + 1 if boot is None else boot
        self.seq = 0

    def read_session(self) -> dict[str, Any]:
        """Lit `session` et relance le mod au `boot` qu'elle fixe."""
        session = dict(parse_session(self.session_path.read_bytes()))
        self.restart(session["boot"])
        return session

    def write_raw(self, data: bytes) -> None:
        """Ajoute des octets bruts à `out`, pour une ligne partielle ou invalide."""
        with open(self.out_path, "ab") as handle:
            handle.write(data)

    def line(
        self,
        kind: str,
        data: dict[str, Any],
        *,
        src: str | None = None,
        side: Any = OWN_SIDE,
        boot: int | None = None,
        seq: int | None = None,
        t_ms: int | None = None,
    ) -> bytes:
        """Construit la ligne suivante sans l'écrire ; `seq` avance comme à l'émission."""
        self.seq = self.seq + 1 if seq is None else seq
        if self.player is not None and kind not in SERVER_KINDS and side is not None:
            data = {"player": self.player, **data}
        return dump_line(
            {
                "v": PROTOCOL_VERSION,
                "boot": self.boot if boot is None else boot,
                "seq": self.seq,
                "t_ms": self.t_ms if t_ms is None else t_ms,
                "src": self.side if src is None else src,
                "side": self.side if side is OWN_SIDE else side,
                "kind": kind,
                "data": data,
            }
        )

    def emit(self, kind: str, data: dict[str, Any], **envelope: Any) -> bytes:
        """Écrit une ligne dans `out`, en un seul `write`, et la rend."""
        line = self.line(kind, data, **envelope)
        self.write_raw(line)
        return line

    def caps(self, **overrides: Any) -> bytes:
        data = {
            "build": "42.21.0",
            "mod": "0.1.0",
            "protocol": PROTOCOL_VERSION,
            "hooks": {"OnZombieDead": True, "OnPlayerGetDamage": True},
            "functions": {"addZombiesInOutfit": True},
            "atoms": ["zombie_kill"],
            "effects": ["spawn_pop"],
        }
        return self.emit("caps", data | overrides)

    def beat(
        self,
        age: float = 0.0,
        players: tuple[str, ...] = (),
        hp: float | None = None,
        kills: int | None = None,
    ) -> bytes:
        if players:
            self.player = players[0]
        return self.emit("beat", {"age": age, "players": list(players), "hp": hp, "kills": kills})

    def join(self, player: str) -> bytes:
        """Le joueur entre sur ce serveur : le heartbeat suivant l'annonce, comme le vrai mod
        quand `getOnlinePlayers` le compte. C'est la seule entrée par laquelle le moteur apprend
        qu'un joueur est là ; un test qui veut un invité passe par ici, pas par
        `state.players`."""
        return self.beat(players=(player,))

    def leave(self) -> bytes:
        """Le serveur ne compte plus personne : le heartbeat suivant n'annonce aucun joueur."""
        return self.beat(players=())

    def hook_seen(self, hook: str = "OnPlayerGetDamage") -> bytes:
        return self.emit("hook_seen", {"hook": hook})

    def cmd_result(
        self,
        cmd_seq: int,
        status: str = "applied",
        reason: str | None = None,
        spawned: int | None = None,
    ) -> bytes:
        data: dict[str, Any] = {"seq": cmd_seq, "status": status}
        if reason is not None:
            data["reason"] = reason
        if spawned is not None:
            data["spawned"] = spawned
        return self.emit("cmd_result", data)

    def ack(self, seq: int, boot: int | None = None) -> None:
        """Réécrit `ack`, qui ne contient jamais qu'une ligne."""
        ack = {"v": PROTOCOL_VERSION, "boot": self.boot if boot is None else boot, "seq": seq}
        self.ack_path.write_bytes(dump_line(ack))


class EmptyServer:
    """Serveur sans joueur : il tique, bat toutes les 10 s, et son âge avance au rythme nominal."""

    def __init__(self, mod: FakeMod, age: float = 0.0, start_s: float = 0.0) -> None:
        self.mod = mod
        self.age = age
        self.next_beat_s = start_s + EMPTY_BEAT_S

    def run_until(self, now_s: float) -> None:
        """Émet les `beat` échus à l'instant `now_s`, en secondes de match."""
        while self.next_beat_s <= now_s:
            self.age += EMPTY_AGE_PER_BEAT
            self.mod.beat(age=self.age, players=())
            self.next_beat_s += EMPTY_BEAT_S
