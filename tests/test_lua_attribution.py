"""Attribution des kills, sur le vrai `DuelCore.lua` exécuté par `doubles.mini_lua`.

Le texte du mod passe entier par ses hooks et ses lignes par le contrat. Ces tests prouvent
sa logique, pas le jeu : le recyclage y est simulé. Voir `limites-connues.md`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from doubles.mini_lua import Lua, Table
from service.protocol import dump_line, parse_event
from service.rules import load_effects

MOD = Path(__file__).resolve().parent.parent / "mods/duelcore/42.21/media/lua/server/DuelCore.lua"
SIDE = "A"
SESSION = {"v": 1, "match": "m1", "side": SIDE, "boot": 1, "cmd_floor": 0}
START_MS = 1_000_000
# Lus dans le mod : ils doivent rester sous la purge, sinon la purge masquerait le contrôle.
TTL_MS = 10_000
PURGE_MS = 30_000
FLUSH_MS = 1_000


class Hook:
    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.Add = self.handlers.append

    def fire(self, *args: Any) -> None:
        for handler in self.handlers:
            handler(*args)


class Hooks(Table):
    def get(self, key: Any) -> Hook:
        return self.items.setdefault(key, Hook())


class Player:
    def __init__(self, name: str) -> None:
        self.name = name
        self.dead = False

    def getUsername(self) -> str:
        return self.name

    def isDead(self) -> bool:
        return self.dead

    def getX(self) -> float:
        return 10.5

    def getY(self) -> float:
        return 20.5

    def getPrimaryHandItem(self) -> None:
        return None


class Zombie:
    def __init__(self, online_id: int = 7) -> None:
        self.online_id = online_id
        self.attacked_by: Player | None = None
        self.mod_data = Table()

    def getOnlineID(self) -> int:
        return self.online_id

    def getAttackedBy(self) -> Player | None:
        return self.attacked_by

    def getModData(self) -> Table:
        return self.mod_data

    def getX(self) -> float:
        return 1.5

    def getY(self) -> float:
        return 2.5


class Reader:
    def __init__(self, text: bytes) -> None:
        self.codes = list(text)

    def read(self) -> int:
        return self.codes.pop(0) if self.codes else -1

    def close(self) -> None:
        pass


class Writer:
    def __init__(self, sink: list[str]) -> None:
        self.write = sink.append

    def close(self) -> None:
        pass


class Online:
    def __init__(self, players: list[Player]) -> None:
        self.players = players

    def size(self) -> int:
        return len(self.players)

    def get(self, index: int) -> Player:
        return self.players[index]


class Stub:
    """Rend un objet du jeu dont le test ne regarde rien."""

    def __init__(self, **methods: Any) -> None:
        self.__dict__.update(methods)


class Game:
    """Le serveur A, réduit à ce que le mod lui demande."""

    def __init__(self) -> None:
        self.now = START_MS
        self.hooks = Hooks()
        self.out: list[str] = []
        self.commands = b""
        self.online: list[Player] = []
        self.player = self.connect("hote")
        body = Stub(getOverallBodyHealth=lambda: 100)
        self.player.getBodyDamage = lambda: body
        self.player.getZombieKills = lambda: 0
        Lua(
            {
                "Events": self.hooks,
                "getCore": lambda: Stub(getGameAndBuildVersion=lambda: "42.21.0"),
                "getFileReader": self._reader,
                "getFileWriter": self._writer,
                "getGameTime": lambda: Stub(getWorldAgeHours=lambda: 0.0),
                "getOnlinePlayers": lambda: Online(self.online),
                "getServerName": lambda: "duelA",
                "getTimestampMs": lambda: self.now,
                "instanceof": lambda value, name: name == "IsoPlayer" and isinstance(value, Player),
                "isServer": lambda: True,
            }
        ).run(MOD.read_text(encoding="utf-8"))
        self.tick()

    def connect(self, name: str) -> Player:
        player = Player(name)
        self.online.append(player)
        return player

    def _reader(self, name: str, _create: bool) -> Reader | None:
        if name == f"duel_{SIDE}_in.txt":
            return Reader(self.commands)
        return Reader(dump_line(SESSION)) if name == f"duel_{SIDE}_session.txt" else None

    def _writer(self, name: str, _create: bool, _append: bool) -> Writer:
        return Writer(self.out if name == f"duel_{SIDE}_out.txt" else [])

    def tick(self) -> None:
        self.hooks.get("OnTick").fire()

    def after(self, delay_ms: int) -> None:
        """Avance l'heure sans `OnTick` : ni purge ni vidage de `recentDead`."""
        self.now += delay_ms

    def hit(self, zombie: Zombie, player: Player | None = None) -> None:
        self.hooks.get("OnHitZombie").fire(zombie, player or self.player)

    def dies(self, zombie: Zombie) -> None:
        self.hooks.get("OnZombieDead").fire(zombie)

    def flush(self) -> None:
        self.after(FLUSH_MS)
        self.tick()

    def lines(self, kind: str) -> list[dict[str, Any]]:
        """Rend les lignes de `kind`, passées par le contrat comme celles du vrai `out`."""
        self.flush()
        catalog = load_effects()
        events = [parse_event(line, catalog) for line in "".join(self.out).splitlines()]
        return [json.loads(json.dumps(event)) for event in events if event["kind"] == kind]


@pytest.fixture
def game() -> Game:
    return Game()


def anonymous(line: dict[str, Any]) -> bool:
    return line["side"] is None and "player" not in line["data"]


def test_mod_constants_keep_the_window_under_the_purge():
    source = MOD.read_text(encoding="utf-8")
    assert f"local ATTACKER_TTL_MS = {TTL_MS}\n" in source
    assert f"local ATTACKER_PURGE_MS = {PURGE_MS}\n" in source
    assert f"local FLUSH_MS = {FLUSH_MS}\n" in source


def test_recent_hit_names_the_author(game):
    zombie = Zombie()
    game.hit(zombie)
    game.after(TTL_MS - 1)
    game.dies(zombie)

    [kill] = game.lines("zombie_kill")
    assert kill["side"] == SIDE
    assert kill["data"]["player"] == "hote"


def test_expired_hit_names_nobody(game):
    """Péremption : sans `OnTick`, la purge n'a pas tourné, seule la fenêtre peut refuser."""
    zombie = Zombie()
    game.hit(zombie)
    game.after(TTL_MS)
    game.dies(zombie)

    [kill] = game.lines("zombie_kill")
    assert anonymous(kill)


def test_recycled_zombie_inherits_no_author(game):
    """Même objet Lua, autre zombie : le coup de son ancienne vie ne compte pas."""
    zombie = Zombie(online_id=7)
    game.hit(zombie)
    game.after(1)
    zombie.online_id = 8
    game.dies(zombie)

    [kill] = game.lines("zombie_kill")
    assert anonymous(kill)


@pytest.mark.parametrize("change", ["dead", "gone"])
def test_stale_wielder_names_nobody(game, change):
    zombie = Zombie()
    other = game.connect("autre")
    game.hit(zombie, other)
    if change == "dead":
        other.dead = True
    else:
        game.online.remove(other)
    game.dies(zombie)

    [kill] = game.lines("zombie_kill")
    assert anonymous(kill)


def test_attacker_entry_dies_with_the_zombie_even_on_a_repeated_hook(game):
    """Le second tir de `OnZombieDead` purge aussi : l'objet recyclé repart sans coup."""
    zombie = Zombie()
    game.dies(zombie)
    game.hit(zombie)
    game.dies(zombie)
    game.flush()
    game.dies(zombie)

    kills = game.lines("zombie_kill")
    assert len(kills) == 2
    assert all(anonymous(kill) for kill in kills)


def test_game_author_needs_no_recorded_hit(game):
    zombie = Zombie()
    zombie.attacked_by = game.player
    game.dies(zombie)

    [kill] = game.lines("zombie_kill")
    assert (kill["side"], kill["data"]["player"]) == (SIDE, "hote")


def test_camp_atoms_name_their_player(game):
    """Le service refuse un atome de camp sans `player` : chacun doit le porter."""
    game.hooks.get("OnProcessAction").fire("build", game.player)
    game.hooks.get("OnWeaponHitCharacter").fire(None, game.player, None, 3)
    game.hooks.get("OnThrowableExplode").fire(Stub(getAttacker=lambda: game.player), None)

    for kind in ("build", "damage_taken", "explosion"):
        [line] = game.lines(kind)
        assert (line["side"], line["data"]["player"]) == (SIDE, "hote"), kind


def test_due_command_leaves_the_middle_of_pending_and_answers_once(game):
    """`table.remove` du vrai mod : la commande prête sort de `pending`, l'autre y reste."""
    waiting = {"v": 1, "seq": 1, "kind": "inconnu", "warn_s": 5}
    due = {"v": 1, "seq": 2, "kind": "inconnu"}
    game.commands = dump_line(waiting) + dump_line(due)

    game.flush()
    assert [line["data"]["seq"] for line in game.lines("cmd_result")] == [2]

    game.after(5_000)
    results = game.lines("cmd_result")
    assert [line["data"]["seq"] for line in results] == [2, 1]
    assert {line["data"]["reason"] for line in results} == {"unknown_kind"}
