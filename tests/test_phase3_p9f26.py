"""P9F26 : le verrou du départ arrête vraiment de marcher, et se mesure.

`setBlockMovement` n'avait aucun lecteur dans le déplacement du jeu (seules la caméra et les
véhicules lisaient son champ) : le joueur semblait figé tant qu'il ne touchait à rien. Le mod
pose désormais `setIgnoreMovement`, que `IsoPlayer.updateInternal2` consulte avant toute entrée,
le relit par `getIgnoreMovement`, le fait poser aussi par le client du joueur (`DuelHud.lua`,
canal `hold` de P9F22), et mesure la distance parcourue sous tenue (`moved_tiles`). Le service
journalise `hold_leaked` au-delà d'un quart de case. Chaque test cite la mutation qui le fait
échouer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import test_lua_attribution as lua_mod
from doubles.mini_lua import Lua, LuaError, Table
from test_match import (  # noqa: F401
    FreezablePlayer,
    HoldDuel,
    PausedServer,
    engine_rig,
    hold_caps,
)
from test_phase3_p9f22 import HudServer

from service import engine as engine_module
from service.engine import HOLD_CALLS, HOLD_LEAKED, held_player

ROOT = Path(__file__).resolve().parent.parent
MOD_DIR = ROOT / "mods" / "duelcore"
CLIENT = MOD_DIR / "42.21/media/lua/client/DuelHud.lua"
CALLS = ["setInvulnerable", "setZombiesDontAttack", "setInvisible", "setIgnoreMovement"]


def held_server(*players: FreezablePlayer, server: PausedServer | None = None) -> PausedServer:
    server = server or PausedServer()
    server.online.extend(players)
    server.fire("OnServerStarted")
    server.send(1, "hold_time")
    server.paused_pass()
    return server


class Walker(FreezablePlayer):
    """Un joueur dont la position bouge : ce que le jeu ferait si le verrou fuyait."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.x, self.y = 100.5, 200.5

    def getX(self) -> float:
        return self.x

    def getY(self) -> float:
        return self.y


# -- mod, côté serveur : le bon levier, relu ---------------------------------------------------


def test_hold_calls_name_set_ignore_movement_and_never_set_block_movement():
    """La séquence de tenue, au mod comme au service, fige par `setIgnoreMovement` ; plus une
    référence à `setBlockMovement` dans le mod. Un jeu qui n'aurait que l'ancien levier rend
    `no_hold_api` nommant `setIgnoreMovement`. Mutation : remettre `setBlockMovement` dans
    `HOLD_CALLS` (et son setter) fait échouer la liste et la tenue d'un tel joueur passe."""
    mod = (MOD_DIR / "42.21/media/lua/server/DuelCore.lua").read_text(encoding="utf-8")
    [listed] = re.findall(r"^local HOLD_CALLS = \{([^}]*)\}", mod, re.MULTILINE)
    assert re.findall(r'"(\w+)"', listed) == CALLS
    assert list(HOLD_CALLS) == CALLS
    for path in MOD_DIR.rglob("*.lua"):
        assert "setBlockMovement" not in path.read_text(encoding="utf-8"), path

    class OldGame(FreezablePlayer):
        """Le jeu d'avant : un `setBlockMovement` sans effet, pas de `setIgnoreMovement`."""

        setIgnoreMovement = None

        def setBlockMovement(self, blocked: bool) -> None:
            self.calls.append(("setBlockMovement", blocked))

    player = OldGame("ancien")
    server = held_server(player)
    assert server.result(1) == {
        "seq": 1, "status": "refused", "reason": "no_hold_api", "call": "setIgnoreMovement",
    }  # fmt: skip
    assert ("setBlockMovement", True) not in player.calls
    assert not player.invulnerable


def test_ignore_movement_is_read_back_and_its_failure_is_named():
    """`getIgnoreMovement` relu aussitôt posé : un jeu qui ne le confirme pas (getter absent, ou
    qui dit faux) rend `no_hold_api` nommant `getIgnoreMovement`, et l'appel est défait. Un état
    perdu plus tard est reposé à la passe suivante. Mutation : retirer `HOLD_VERIFIED` (ou sa
    vérification dans `freezePlayer`) rend `applied` pour un verrou que rien ne confirme ; ôter
    `setIgnoreMovement = "getIgnoreMovement"` de `HOLD_GETTERS` ne repose pas l'état perdu."""

    class Unreadable(FreezablePlayer):
        getIgnoreMovement = None

    class Denying(FreezablePlayer):
        def getIgnoreMovement(self) -> bool:
            return False

    for kind in (Unreadable, Denying):
        player = kind("muet")
        server = held_server(player)
        assert server.result(1) == {
            "seq": 1, "status": "refused", "reason": "no_hold_api", "call": "getIgnoreMovement",
        }  # fmt: skip
        assert not player.blocked and not player.invulnerable
        assert ("setIgnoreMovement", False) in player.calls

    host = FreezablePlayer("hote")
    server = held_server(host)
    assert server.result(1)["status"] == "applied" and host.blocked
    host.blocked = False  # le jeu l'a perdu : le getter le dit
    server.fire("OnTick")
    assert host.blocked
    assert host.calls.count(("setIgnoreMovement", True)) == 2


def test_client_is_told_held_while_the_hold_lasts_and_free_at_release():
    """Le client du joueur tenu reçoit `hold` `held = true` (canal `duelcore` de P9F22), redit
    toutes les cinq secondes, puis `held = false` au relâchement. Mutations : ôter `tellHold`
    de `freezePlayer` laisse le client libre de marcher ; ne plus le redire laisse le client se
    relâcher après 30 s ; ôter `tellHold(entry.player, false)` de `thawPlayer` laisse le client
    tenu jusqu'à son délai."""
    server = HudServer()
    player = FreezablePlayer("durand")
    held_server(player, server=server)

    def told() -> list[Any]:
        return [args["held"] for who, module, command, args in server.relayed
                if command == "hold" and module == "duelcore" and who is player]  # fmt: skip

    assert server.result(1)["status"] == "applied"
    assert told() == [True]
    for _ in range(4):
        server.paused_pass()
    assert told() == [True]
    server.paused_pass()
    assert told() == [True, True]

    server.send(2, "release_time")
    server.paused_pass()
    assert server.result(2)["status"] == "applied"
    assert told() == [True, True, False]


def test_release_keeps_protection_until_the_client_is_told_free():
    """Un `held = false` qui ne part pas laisserait le client se croire tenu, immobile jusqu'à son
    délai de 30 s, pendant que le match reprend : le joueur reste protégé, le relâchement rend
    `refused` nommant `sendServerCommand`, et le suivant, l'envoi passé, rend `applied`.
    Mutation : ignorer l'échec de `tellHold(entry.player, false)` dans `thawPlayer` rend
    `applied` et le joueur vulnérable."""

    class FlakyRelay(HudServer):
        failing = False

        def _relay(self, player: Any, module: str, command: str, args: Table) -> None:
            if self.failing and command == "hold" and args.items.get("held") is False:
                raise LuaError("envoi perdu")
            super()._relay(player, module, command, args)

    server = FlakyRelay()
    player = FreezablePlayer("durand")
    held_server(player, server=server)
    assert server.result(1)["status"] == "applied"

    server.failing = True
    server.send(2, "release_time")
    server.paused_pass()
    assert server.result(2) == {
        "seq": 2, "status": "refused", "reason": "no_hold_api", "call": "sendServerCommand",
    }  # fmt: skip
    assert player.invulnerable and player.shielded

    server.failing = False
    server.send(3, "release_time")
    server.paused_pass()
    assert server.result(3)["status"] == "applied"
    assert not player.invulnerable and not player.shielded
    told = [args["held"] for who, _, command, args in server.relayed if command == "hold"]
    assert told[-1] is False


# -- mod : le déplacement mesuré ---------------------------------------------------------------


def test_moved_tiles_is_measured_from_the_anchor_and_reported_when_it_leaks():
    """`hold_applied` porte `anchor` (position à l'entrée en tenue) et `moved_tiles` (distance
    depuis). Immobile : une ligne, à zéro. À 0,8 case : une nouvelle ligne, avec la valeur.
    Mutations : ne plus mesurer (`entry.moved` absent) laisse `moved_tiles` muet ; reprendre
    l'ancre à chaque passe rend toujours zéro ; ôter le second rapport de `reportHold` tait la
    fuite."""
    walker = Walker("harumate")
    server = held_server(walker)
    for _ in range(3):
        server.paused_pass()
    [first] = [line["data"] for line in server.lines("hold_applied")]
    assert first["moved_tiles"] == 0
    assert first["anchor"] == {"x": 100.5, "y": 200.5}

    walker.x += 0.48
    walker.y += 0.64
    server.paused_pass()
    server.paused_pass()
    reports = [line["data"] for line in server.lines("hold_applied")]
    assert len(reports) == 2
    assert reports[1]["moved_tiles"] == pytest.approx(0.8)
    assert reports[1]["anchor"] == {"x": 100.5, "y": 200.5}
    assert reports[1]["calls"] == CALLS


def test_moved_tiles_is_measured_from_the_exact_anchor_not_the_rounded_one():
    """Immobile à (100,504 ; 200,504), le joueur n'a pas bougé : `moved_tiles` vaut zéro, et
    seul `anchor` publié est arrondi au centième. Mutation : arrondir l'ancre avant la mesure
    rend 0,01 case pour un joueur qui n'a pas bougé."""
    walker = Walker("immobile")
    walker.x, walker.y = 100.504, 200.504
    server = held_server(walker)
    server.paused_pass()
    [first] = [line["data"] for line in server.lines("hold_applied")]
    assert first["moved_tiles"] == 0
    assert first["anchor"] == {"x": 100.5, "y": 200.5}


# -- service : `hold_leaked` et le lobby -------------------------------------------------------


def test_service_journals_hold_leaked_past_a_quarter_tile_and_shows_it_in_the_lobby(
    engine_rig, hold_caps
):
    """Un `hold_applied` à 0,8 case sous tenue active : `hold_leaked` au journal, avec la valeur,
    et « déplacement 0,8 case » au lobby. À 0,0 : ni ligne ni mot de plus. Mutations : ôter
    l'écriture de `HOLD_LEAKED` dans `_take_hold_applied` tait la fuite ; comparer à zéro au lieu
    de `HOLD_LEAK_TILES` journalise un tremblement ; ne plus passer `moved` à `held_player`
    laisse le lobby muet."""
    hold_caps()
    duel = HoldDuel(engine_rig)
    holds = duel.wait_holds()
    anchor = {"x": 100.5, "y": 200.5}
    duel.mods["A"].emit(
        "hold_applied", {"player": "hote", "calls": CALLS, "moved_tiles": 0, "anchor": anchor}
    )
    duel.mods["A"].emit(
        "hold_applied", {"player": "tremble", "calls": CALLS, "moved_tiles": 0.25, "anchor": anchor}
    )
    duel.mods["A"].emit(
        "hold_applied", {"player": "harumate", "calls": CALLS, "moved_tiles": 0.8, "anchor": anchor}
    )
    duel.mods["A"].cmd_result(holds["A"]["seq"], "applied")
    state = engine_rig.until(lambda s: s["capabilities"]["hold"]["A"]["status"] == "held")

    labels = [entry["label"] for entry in state["capabilities"]["hold"]["A"]["players"]]
    assert labels == [
        "tenu : hote (4/4)",
        "tenu : tremble (4/4, déplacement 0,25 case)",
        "tenu : harumate (4/4, déplacement 0,8 case)",
    ]
    leaks = [(r["side"], r["data"]) for r in duel.records(HOLD_LEAKED)]
    assert leaks == [("A", {"player": "harumate", "moved_tiles": 0.8, "anchor": anchor})]
    journal = [r["data"] for r in duel.records("hold_applied")]
    assert journal[0] == {"player": "hote", "calls": CALLS, "moved_tiles": 0.0, "anchor": anchor}
    assert all(isinstance(r["moved_tiles"], float) for r in journal)


def test_lobby_sentence_says_the_measured_move_only_when_it_is_not_zero():
    """« (4/4) » immobile ou non mesuré ; « déplacement 0,8 case » sinon, « cases » dès deux.
    Mutation : afficher le déplacement à zéro alourdit chaque ligne du lobby."""
    assert held_player("harumate", CALLS, 0.0)["label"] == "tenu : harumate (4/4)"
    assert held_player("harumate", CALLS)["label"] == "tenu : harumate (4/4)"
    assert held_player("harumate", CALLS, 0.8)["label"] == "tenu : harumate (4/4, déplacement 0,8 case)"
    assert held_player("vu", CALLS[:2], 2.5)["label"] == (
        "tenu : vu (2/4, setInvisible en échec, déplacement 2,5 cases)"
    )
    assert held_player("harumate", CALLS, 0.8)["moved_tiles"] == 0.8


def test_hold_leaked_waits_for_an_active_hold():
    """Un rapport tardif, tenue relâchée : pas de `hold_leaked`, le joueur est libre de bouger.
    Mutation : ôter `_hold_active` de la condition journalise une fuite après le départ."""
    written: list[tuple[str, dict]] = []

    class Probe:
        _held_sides = ["A"]
        _released_ms = 1
        _hold_view = {"A": {"status": "released", "reason": None}}
        _hold_players = {"A": []}
        redactor = engine_module.Redactor(())

        def _write_safely(self, type_, data, side, mt):
            written.append((type_, dict(data)))
            return True

        _hold_active = engine_module.RealMatch._hold_active

    probe = Probe()
    report = {"player": "harumate", "calls": CALLS, "moved_tiles": 3.0}
    engine_module.RealMatch._take_hold_applied(probe, "A", report)
    assert [type_ for type_, _ in written] == ["hold_applied"]

    probe._released_ms = None
    probe._hold_view = {"A": {"status": "held", "reason": None}}
    engine_module.RealMatch._take_hold_applied(probe, "A", report)
    assert [type_ for type_, _ in written] == ["hold_applied", "hold_applied", HOLD_LEAKED]


# -- client : le verrou là où les entrées sont lues ---------------------------------------------


class Pad(lua_mod.Player):
    """Le joueur local du client : `setIgnoreMovement` et `stopMoving` notés dans l'ordre."""

    def __init__(self, name: str = "durand") -> None:
        super().__init__(name)
        self.calls: list[tuple[str, Any]] = []
        self.ignoring = False

    def setIgnoreMovement(self, value: bool) -> None:
        self.calls.append(("setIgnoreMovement", value))
        self.ignoring = value

    def stopMoving(self) -> None:
        self.calls.append(("stopMoving", None))


class Client:
    """`DuelHud.lua` chargé avec une horloge, un joueur local, et les messages du serveur."""

    def __init__(self, player: Any = None, clock: bool = True) -> None:
        self.hooks = lua_mod.Hooks()
        self.player = player if player is not None else Pad()
        self.now = 5_000_000
        host: dict[str, Any] = {
            "Events": self.hooks,
            "getPlayer": lambda: self.player,
            "sendClientCommand": lambda *args: None,
        }
        if clock:
            host["getTimestampMs"] = lambda: self.now
        Lua(host).run(CLIENT.read_text(encoding="utf-8"))

    def hold(self, held: Any) -> None:
        args = Table()
        args.set("held", held)
        self.hooks.get("OnServerCommand").fire("duelcore", "hold", args)

    def update(self) -> None:
        self.hooks.get("OnPlayerUpdate").fire(self.player)


def test_client_ignores_movement_on_hold_and_gives_it_back_on_release():
    """`hold` reçu : `setIgnoreMovement(true)` aussitôt, puis à chaque `OnPlayerUpdate` avec
    `stopMoving()`. `held = false` : `setIgnoreMovement(false)`, et rien d'autre ; les passes
    suivantes ne touchent plus au joueur. Mutations : ne pas poser le verrou à la réception, ou
    ne pas s'inscrire à `OnPlayerUpdate`, laisse le joueur marcher ; ne pas rendre `false` à la
    libération le laisse paralysé."""
    client = Client()
    pad = client.player
    assert "OnPlayerUpdate" not in client.hooks.items

    client.hold(True)
    assert pad.calls == [("setIgnoreMovement", True), ("stopMoving", None)]
    for _ in range(3):
        client.update()
    assert pad.calls[2:] == [("setIgnoreMovement", True), ("stopMoving", None)] * 3
    client.hold(True)
    assert len(client.hooks.get("OnPlayerUpdate").handlers) == 1

    pad.calls.clear()
    client.hold(False)
    assert pad.calls == [("setIgnoreMovement", False)]
    assert not pad.ignoring
    client.update()
    client.hold(False)
    assert pad.calls == [("setIgnoreMovement", False)]


def test_client_lets_go_by_itself_after_thirty_seconds_without_news():
    """Sans nouvelle depuis plus de 30 s, la tenue se relâche côté client ; une nouvelle la
    prolonge. Sans horloge lisible, le client se rabat sur « libre ». Mutation : ôter le délai
    de `stillHeld` laisse un joueur paralysé par une session coupée."""
    client = Client()
    pad = client.player
    client.hold(True)
    client.now += 29_000
    client.update()
    assert pad.ignoring
    client.hold(True)
    client.now += 30_000
    client.update()
    assert pad.ignoring
    client.now += 1
    pad.calls.clear()
    client.update()
    assert pad.calls == [("setIgnoreMovement", False)]
    client.update()
    assert pad.calls == [("setIgnoreMovement", False)]

    blind = Client(clock=False)
    blind.hold(True)
    blind.update()
    assert not blind.player.ignoring


def test_client_without_the_movement_api_breaks_nothing():
    """Un joueur sans `setIgnoreMovement` ni `stopMoving` : rien ne sort du gestionnaire, et les
    annonces passent toujours. Mutation : ôter les `pcall` de `lockMovement` casse le HUD."""

    class Bare(lua_mod.Player):
        def stopMoving(self) -> None:
            raise LuaError("absent")

    client = Client(Bare("nu"))
    client.hold(True)
    client.update()
    client.hold(False)
    client.update()


def test_the_hold_call_list_is_defined_once_and_the_lobby_sentence_agrees():
    """La liste des quatre appels n'a qu'une définition, et le lobby la compte juste.

    P9F26 a renommé le quatrième appel dans `service/engine.py` seulement : `service/state.py`
    gardait `setBlockMovement`, et la page annonçait « (4/4, setBlockMovement en échec) » — un
    compte juste collé à un échec qui n'existait pas. Une seule source, importée des deux côtés.
    """
    from service import engine, state

    assert engine.HOLD_CALLS is state.HOLD_CALLS, "la liste est redéfinie au lieu d'être importée"
    assert "setBlockMovement" not in state.HOLD_CALLS
    reported = state.held_player("harumate", list(state.HOLD_CALLS))
    assert reported["complete"] is True
    assert reported["label"] == "tenu : harumate (4/4)"
    assert "en échec" not in reported["label"]
