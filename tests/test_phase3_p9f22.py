"""P9F22 : les annonces deviennent visibles, en texte flottant au-dessus du personnage.

Le service décide (`MatchAnnouncer`), `HudSender` écrit une commande `announce` dans la file de
commandes du match quand les `caps` du camp portent `functions.hud`, le mod la relaie au client
(`sendServerCommand`), et `DuelHud.lua` l'affiche par `HaloTextHelper.addText`. Sans la capacité,
ou dès que le client répond `no_hud_api`, le camp repasse à `servermsg`, et le lobby le dit.
Chaque test cite la mutation qui le fait échouer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import test_lua_attribution as lua_mod
from doubles.fake_mod import FakeMod
from doubles.mini_lua import Lua, Table, _Parser
from test_announce import Recorder
from test_match import (  # noqa: F401
    PRESET,
    Duel,
    FreezablePlayer,
    PausedServer,
    WorldTime,
    engine_rig,
    in_commands,
    kinds,
    presets,
    wait_command,
)
from test_contract import PLAYERS, SIDES, Lobby

from service import engine as engine_module
from service.announce import (
    ANNOUNCE_KIND,
    DEFAULTS,
    EXPIRE_S,
    MAX_NAME,
    MAX_TEXT,
    RESEND_S,
    TEXTS,
    TONES,
    HudSender,
    MatchAnnouncer,
    Phrases,
    RconSender,
    plain,
    rcon_announcer,
)
from service.protocol import dump_line, parse_command
from service.rules import load_effects, load_runtime

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "mods/duelcore/42.21/media/lua/client/DuelHud.lua"
UI_SCRIPT = ROOT / "ui" / "app.js"
HUD_CAPS = {"functions": {"hud": True}}
NO_HUD_CAPS = {"functions": {"addZombiesInOutfit": True}}
REFUSED = {"status": "refused", "reason": "no_hud_api", "announce": True}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Wired:
    """Un `HudSender` branché comme le fait le match : file de commandes, secours, voix du lobby."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.chat = Recorder()
        self.written: list[tuple[str, dict]] = []
        self.notices: list[tuple[str, str]] = []
        self.sender = HudSender(self.chat, clock=self.clock)
        self.sender.attach(
            lambda side, command: self.written.append((side, dict(command))),
            lambda side, reason: self.notices.append((side, reason)),
        )

    def commands(self, side: str) -> list[dict]:
        return [command for target, command in self.written if target == side]


# -- service : le choix du canal --------------------------------------------------------------


def test_announcement_leaves_as_a_command_with_hud_caps_and_as_servermsg_without():
    """`caps.functions.hud` : commande `announce`, avec `text` et `side`. Sans elle : `servermsg`,
    et le lobby l'apprend une fois. Avant tout `caps`, le secours sert.
    Mutations : dans `HudSender.say`, ôter le test `!= HUD` envoie tout par le mod, même à un mod
    qui ne sait pas relayer ; `on_caps` qui ne lit pas `functions.hud` laisse tout au chat ; ne
    plus appeler `_fall` pour un `caps` sans `hud` tait le lobby."""
    wired = Wired()
    wired.sender.say("A", "trop tot")
    assert wired.chat.said == [("A", "trop tot")] and wired.written == []

    wired.sender.on_caps("A", HUD_CAPS)
    wired.sender.on_caps("B", NO_HUD_CAPS)
    wired.sender.on_caps("B", NO_HUD_CAPS)
    wired.sender.say("A", "Duel against naylo: 25 min.", "start")
    wired.sender.say("B", "Duel against durand: 25 min.", "start")

    [command] = wired.commands("A")
    assert command == {
        "v": 1,
        "seq": 1,
        "kind": "announce",
        "side": "A",
        "text": "Duel against naylo: 25 min.",
        "tone": "start",
        "origin": {"rule": "announce"},
    }
    assert parse_command(command, load_effects()) == command
    assert wired.commands("B") == []
    assert wired.chat.to("B") == ["Duel against durand: 25 min."]
    assert wired.chat.to("A") == ["trop tot"]
    assert wired.notices == [("B", "no_hud_caps")]
    assert wired.sender.channels() == {
        "A": {"channel": "hud", "reason": None},
        "B": {"channel": "chat", "reason": "no_hud_caps"},
    }


def test_no_hud_api_refusal_switches_the_following_announcements_to_servermsg():
    """Le client répond `no_hud_api` : l'annonce refusée repart par `servermsg`, les suivantes
    aussi, le lobby l'apprend une fois, et un `caps` relu n'y change rien — c'est le client qui
    ne sait pas afficher. Un résultat `applied`, lui, ne bascule rien.
    Mutations : `on_result` qui ignore `no_hud_api` laisse les annonces partir vers un écran
    qui ne les montre pas ; ôter `self._refused.add(side)` rend le camp au mod au `caps`
    suivant ; ôter le `self._fallback.say` de `on_result` perd l'annonce refusée."""
    wired = Wired()
    wired.sender.on_caps("A", HUD_CAPS)
    wired.sender.on_caps("B", HUD_CAPS)
    wired.sender.say("A", "premiere", "start")
    wired.sender.say("B", "pour B", "start")

    wired.sender.on_result("B", {"seq": 2, "status": "applied", "announce": True})
    assert wired.sender.delivered is True
    wired.sender.on_result("A", {"seq": 99, **REFUSED})
    assert wired.notices == [] and wired.chat.said == []

    wired.sender.on_result("A", {"seq": 1, **REFUSED})
    wired.sender.on_result("A", {"seq": 1, **REFUSED})
    assert wired.notices == [("A", "no_hud_api")]
    assert wired.chat.said == [("A", "premiere")]

    wired.sender.on_caps("A", HUD_CAPS)
    wired.sender.say("A", "seconde", "wave")
    wired.sender.say("B", "encore B", "wave")
    assert wired.chat.to("A") == ["premiere", "seconde"]
    assert [command["text"] for command in wired.commands("A")] == ["premiere"]
    assert [command["text"] for command in wired.commands("B")] == ["pour B", "encore B"]
    assert wired.sender.channels()["A"] == {"channel": "chat", "reason": "no_hud_api"}


def test_a_silent_client_falls_back_only_when_it_stays_silent():
    """`no_reply` une fois ne dit rien (un paquet perdu) ; deux de suite valent une capacité
    absente. Un refus `no_player` ne bascule jamais : personne n'était là pour lire.
    Mutation : compter `no_reply` comme `no_player` laisse parler à un client muet sans fin."""
    wired = Wired()
    wired.sender.on_caps("A", HUD_CAPS)
    for text in ("un", "deux", "trois", "quatre"):
        wired.sender.say("A", text)
    silent = {"status": "failed", "reason": "no_reply", "announce": True}

    wired.sender.on_result("A", {"seq": 1, "status": "refused", "reason": "no_player"})
    wired.sender.on_result("A", {"seq": 2, **silent})
    assert wired.notices == []
    wired.sender.on_result("A", {"seq": 3, "status": "applied"})
    wired.sender.on_result("A", {"seq": 4, **silent})
    assert wired.notices == [] and wired.chat.said == []

    wired.sender.say("A", "cinq")
    wired.sender.say("A", "six")
    wired.sender.on_result("A", {"seq": 5, **silent})
    assert wired.notices == [("A", "no_reply")]
    wired.sender.on_result("A", {"seq": 6, **silent})
    # Ni l'une ni l'autre n'a été vue : toutes deux repartent par le secours, dans l'ordre.
    assert wired.notices == [("A", "no_reply")]
    assert wired.chat.said == [("A", "cinq"), ("A", "six")]
    wired.sender.say("A", "sept")
    assert len(wired.written) == 6 and wired.chat.to("A")[-1] == "sept"


def test_unanswered_announcement_is_written_again_once_then_forgotten():
    """`in` peut être vidé entre la lecture du mod et son acquittement : sans réponse après
    `RESEND_S`, l'annonce est réécrite une fois, sous un `seq` neuf (le mod écarte un `seq` déjà
    dépassé), puis oubliée. Une annonce répondue n'est jamais réécrite.
    Mutations : `poll` qui ne réécrit rien perd l'annonce avec la troncature ; `resent` jamais
    posé la réécrit à chaque échéance, sans fin."""
    wired = Wired()
    wired.sender.on_caps("A", HUD_CAPS)
    wired.sender.say("A", "repondue", "wave")
    wired.sender.say("A", "perdue", "mark")
    wired.sender.on_result("A", {"seq": 1, "status": "applied"})

    wired.clock.now += RESEND_S - 0.5
    wired.sender.poll()
    assert len(wired.written) == 2

    wired.clock.now += 0.5
    wired.sender.poll()
    wired.sender.poll()
    assert [(c["seq"], c["text"], c["tone"]) for c in wired.commands("A")] == [
        (1, "repondue", "wave"),
        (2, "perdue", "mark"),
        (3, "perdue", "mark"),
    ]

    wired.clock.now += EXPIRE_S
    wired.sender.poll()
    assert len(wired.written) == 3
    # Oubliée : une réponse tardive ne bascule plus rien.
    wired.sender.on_result("A", {"seq": 3, **REFUSED})
    assert wired.notices == []


def test_write_failure_and_missing_queue_fall_back_for_that_line_only():
    """La file de commandes refuse l'écriture : cette annonce part par le secours, le camp reste
    sur le texte flottant. Mutation : laisser monter l'`OSError` casserait la passe d'annonce."""
    chat = Recorder()
    sender = HudSender(chat)
    sender.on_caps("A", HUD_CAPS)
    sender.say("A", "sans file")
    assert chat.said == [("A", "sans file")]

    def full(side: str, command: dict) -> None:
        raise OSError("disque plein")

    sender.attach(full, lambda side, reason: None)
    sender.say("A", "disque plein")
    assert chat.to("A") == ["sans file", "disque plein"]
    assert sender.channels()["A"]["channel"] == "hud"


def test_real_announcer_is_a_hud_sender_over_the_rcon_fallback():
    """Mutation : `rcon_announcer` qui rend `MatchAnnouncer(RconSender(…))` — tout repart au chat."""
    say = rcon_announcer(load_runtime(), "rcon-secret-42")
    try:
        assert isinstance(say._sender, HudSender)
        assert isinstance(say._sender._fallback, RconSender)
    finally:
        say.close()


# -- service : des textes courts --------------------------------------------------------------


def longest_lines(lang: str) -> list[str]:
    """Chaque annonce, à ses valeurs les plus longues : pseudo plein, scores à quatre chiffres."""
    phrases = Phrases(lang)
    foe = "W" * MAX_NAME
    score = {"foe": foe, "mine": 9999, "theirs": 9999}
    closing = phrases.say("closing")
    lines = [
        phrases.say("waiting_frozen"),
        phrases.say("waiting"),
        phrases.say("start", foe=foe, minutes=999) + phrases.say("start_rule", n=99, s="s"),
        phrases.say("wave", n=999, s="s", left=phrases.left(59 * 60), **score),
        phrases.say("supply"),
        phrases.say("mark", left=phrases.left(600), **score),
        phrases.say("void") + closing,
        phrases.say("draw", why=phrases.say("why_time"), **score) + closing,
    ]
    for key, suffix in (("won", "won"), ("lost", "lost")):
        for why in (f"why_death_{suffix}", f"why_forfeit_{suffix}", "why_time"):
            lines.append(phrases.say(key, why=phrases.say(why, foe=foe), **score) + closing)
    return lines


@pytest.mark.parametrize("lang", sorted(TEXTS))
def test_every_shipped_announcement_fits_the_floating_text(lang):
    """Un texte flottant se lit en une seconde et demie : 80 caractères au plus, ASCII, sans
    guillemet, même avec le pseudo le plus long. Mutation : rendre à `won` sa phrase d'avant
    (« PZ Duel : fin du duel, tu as GAGNE contre … ») dépasse, et `plain` la couperait en route."""
    assert MAX_TEXT == 80
    for line in longest_lines(lang):
        assert len(line) <= MAX_TEXT, line
        assert plain(line) == " ".join(line.split()), line
        assert line.isascii() and '"' not in line


def test_floating_text_is_capped_at_80_ascii_characters_without_quotes():
    """Ce qui part vers l'écran est borné quoi qu'on lui donne : 80 caractères, ASCII, pas de
    guillemet ni de saut de ligne, et un pseudo ramené à 16 caractères.
    Mutations : `MAX_TEXT = 240` laisse passer une phrase illisible d'un coup d'oeil ; ôter
    `plain` de `HudSender.say` laisse l'accent et le guillemet ; ôter `[:MAX_NAME]` de
    `Phrases.foe` laisse un pseudo de 60 caractères pousser le score hors du texte."""
    wired = Wired()
    wired.sender.on_caps("A", HUD_CAPS)
    wired.sender.say("A", 'Zoé dit "bonjour"\n' + "é" * 300, "wave")

    [command] = wired.commands("A")
    text = command["text"]
    assert len(text) == MAX_TEXT == 80
    assert text.startswith("Zoe dit 'bonjour' eee")
    assert text.isascii() and text.isprintable() and '"' not in text and "\\" not in text
    assert len(dump_line(command)) < 400

    say = MatchAnnouncer(wired.sender, {**DEFAULTS, "lang": "fr"})
    say.tick(0, [wave("A", 5)], {"A": "durand", "B": "X" * 60}, {"A": 1, "B": 2}, 500)
    text = wired.commands("A")[-1]["text"]
    assert text == f"{'X' * 16} t'envoie 5 zombies ! Toi 1 - 2. Reste 8 min."
    assert len(text) <= MAX_TEXT


def wave(side: str, count: int, kind: str = "spawn_pop") -> dict:
    return {"type": "command", "side": side, "data": {"command": {"kind": kind, "n": count}}}


def test_each_announcement_carries_its_nature():
    """Attente, départ, vague, lot, repère de temps, fin : la nature suit l'annonce jusqu'à la
    commande, le client en tire la couleur. Mutation : `started` qui ne passe plus `TONE_START`
    donne au départ la couleur de l'attente."""
    wired = Wired()
    wired.sender.on_caps("A", HUD_CAPS)
    say = MatchAnnouncer(wired.sender, {**DEFAULTS, "lang": "en", "wave_gap_s": 0})
    names = {"A": "durand", "B": "naylo"}

    say.waiting(0, {"A": "durand", "B": None}, ["A"])
    say.started(names, 700, {})
    say.tick(1_000, [wave("A", 3)], names, {"A": 0, "B": 1}, 650)
    say.tick(2_000, [wave("A", 1, "supply_drop")], names, {"A": 0, "B": 1}, 640)
    say.tick(3_000, [], names, {"A": 0, "B": 1}, 600)
    say.ended({"outcome": "win", "winner": "A"}, names, {"A": 0, "B": 1})

    assert [command["tone"] for command in wired.commands("A")] == list(TONES)
    assert all(len(command["text"]) <= MAX_TEXT for command in wired.commands("A"))


# -- moteur : la file de commandes du match ---------------------------------------------------


@pytest.fixture
def hud_match(monkeypatch):
    """Les `caps` des camps donnés portent `functions.hud`, et le match annonce par `HudSender`
    sur un secours enregistré : ce qui va au chat se lit dans `chat`, le reste dans `in`."""
    chat = Recorder()

    def install(sides: tuple[str, ...] = SIDES, **options: Any) -> Recorder:
        line = FakeMod.line

        def with_hud(self, kind, data, **envelope):
            if kind == "caps" and self.side in sides:
                data = {**data, "functions": {**data["functions"], "hud": True}}
            return line(self, kind, data, **envelope)

        def open_announcer(self, rcon_password):
            return MatchAnnouncer(HudSender(chat), {**DEFAULTS, "lang": "en", **options})

        monkeypatch.setattr(FakeMod, "line", with_hud)
        monkeypatch.setattr(engine_module.Engine, "_open_announcer", open_announcer)
        return chat

    return install


def announces(mod: FakeMod) -> list[dict]:
    return [command for command in in_commands(mod) if command["kind"] == ANNOUNCE_KIND]


def journal_text(lobby: Lobby) -> str:
    """Le journal, sans l'en-tête : `match_start` recopie les réglages, clé `announce` comprise."""
    records = [r for r in lobby.rig.journal(lobby.match_id) if r["type"] != "match_start"]
    return json.dumps(records)


def test_match_writes_the_announcement_in_the_command_queue_of_a_hud_side(engine_rig, hud_match):
    """A porte `hud`, B non. Le premier arrivé lit qu'il attend : pour A, une commande `announce`
    dans `in`, rien au chat ; pour B, `servermsg`, rien dans `in`, et le lobby le dit.
    Mutations : ne plus appeler `announcer.on_caps` dans `_take_event` laisse A au chat ; ne plus
    appeler `attach` dans `RealMatch.__init__` aussi ; ôter `"announce"` de `_capabilities`
    tait le lobby."""
    chat = hud_match(sides=("A",))
    lobby = Lobby(engine_rig, PRESET)
    state = lobby.wait_for(lambda s: len(s["capabilities"].get("announce", {})) == 2)
    assert state["capabilities"]["announce"] == {
        "A": {"channel": "hud", "reason": None},
        "B": {"channel": "chat", "reason": "no_hud_caps"},
    }
    warned = [line["msg"] for line in state["logs"] if "annonces en jeu dans le chat" in line["msg"]]
    assert len(warned) == 1 and warned[0].startswith("camp B : ") and "no_hud_caps" in warned[0]

    lobby.mods["A"].beat(players=(PLAYERS["A"],))
    command = wait_command(engine_rig, lobby.mods["A"], ANNOUNCE_KIND)
    assert command["side"] == "A" and command["tone"] == "wait"
    assert command["text"] == TEXTS["en"]["waiting"] and len(command["text"]) <= MAX_TEXT
    lobby.sync_pump()
    assert chat.said == []

    lobby.mods["A"].beat(players=())
    lobby.wait_for(lambda s: s["players"]["A"] is None)
    lobby.mods["B"].beat(players=(PLAYERS["B"],))
    lobby.wait_for(lambda _: chat.to("B") == [TEXTS["en"]["waiting"]])
    lobby.sync_pump()
    assert announces(lobby.mods["B"]) == []
    # Rien de tout cela n'est du match : le journal n'en porte aucune trace.
    assert ANNOUNCE_KIND not in journal_text(lobby)


def test_no_hud_api_result_switches_the_match_to_servermsg_and_touches_no_command(
    engine_rig, hud_match
):
    """Le client de A répond `no_hud_api` : l'annonce repart par `servermsg`, les suivantes aussi,
    et le lobby le dit. Ce `cmd_result` porte le `seq` de la sonde de A (les deux compteurs
    partent de 1) sans la toucher : la sonde reste en attente, puis réussit.
    Mutations : ôter la garde `data.get(ANNOUNCE_KIND) is True` de `_take_event` fait échouer la
    sonde sur le refus d'une annonce, et écrit ce refus au journal ; `on_result` jamais appelé
    laisse A sur un texte flottant que son jeu n'affiche pas."""
    chat = hud_match()
    lobby = Lobby(engine_rig, PRESET)
    mod = lobby.mods["A"]
    mod.beat(players=(PLAYERS["A"],))
    command = wait_command(engine_rig, mod, ANNOUNCE_KIND)
    probe = wait_command(engine_rig, mod, "probe_spawn")
    assert command["seq"] == probe["seq"] == 1

    mod.emit("cmd_result", {"seq": command["seq"], **REFUSED})
    state = lobby.wait_for(
        lambda s: s["capabilities"]["announce"]["A"] == {"channel": "chat", "reason": "no_hud_api"}
    )
    assert chat.to("A") == [TEXTS["en"]["waiting"]]
    assert any(
        line["msg"].startswith("camp A : annonces en jeu dans le chat") and "no_hud_api" in line["msg"]
        for line in state["logs"]
    )
    lobby.sync_pump()
    assert lobby.state()["capabilities"]["spawn"]["A"] == "unknown"
    assert "no_hud_api" not in journal_text(lobby)

    mod.cmd_result(probe["seq"], "applied", spawned=1)
    lobby.wait_for(lambda s: s["capabilities"]["spawn"]["A"] != "unknown")
    # L'attente se redit : par `servermsg` cette fois, et plus rien ne part vers le mod.
    lobby.at(DEFAULTS["waiting_repeat_s"] + 1)
    lobby.wait_for(lambda _: len(chat.to("A")) == 2)
    assert len(announces(mod)) == 1


def test_announcements_never_reach_the_journal_nor_the_replay(engine_rig, presets, hud_match):
    """Un duel entier en texte flottant, dont aucune annonce ne reçoit de réponse : le match se
    joue, se conclut et se rejoue, et son journal ne porte ni commande ni résultat d'annonce.
    Les commandes du match gardent leurs `seq` : l'annonce a son propre compteur.
    Mutation : passer l'annonce par `self._channels[side].write` consomme un `seq` du match —
    la commande suivante du cœur est refusée (`seq` non croissant) et le rejeu diverge."""
    chat = hud_match(wave_gap_s=0)
    path = presets / "demo.json"
    path.write_text(json.dumps({**json.loads(path.read_text("utf-8")), "duration_s": 60}), "utf-8")
    duel = Duel(engine_rig)
    duel.start_match()

    for side in SIDES:
        engine_rig.until(lambda _: any(c["tone"] == "start" for c in announces(duel.mods[side])))
    duel.at_match(1)
    duel.kills("A", 1)
    engine_rig.until(lambda _: any(c["tone"] == "wave" for c in announces(duel.mods["B"])))
    [sent_wave] = [c for c in announces(duel.mods["B"]) if c["tone"] == "wave"]
    assert sent_wave["text"].startswith("durand sends you ")
    match_seqs = [c["seq"] for c in in_commands(duel.mods["B"]) if c["kind"] != ANNOUNCE_KIND]
    assert match_seqs == sorted(set(match_seqs))

    duel.at_match(60)
    state = duel.finish()

    assert state["result"]["winner"] == "A"
    assert chat.said == []
    assert [c["tone"] for c in announces(duel.mods["A"])][-1] == "end"
    assert ANNOUNCE_KIND not in journal_text(duel)
    assert duel.replays()


# -- mod, côté serveur : le relais ------------------------------------------------------------


class HudServer(PausedServer):
    """`PausedServer` dont le jeu a `sendServerCommand` (ou pas) : ce que le mod relaie se lit
    dans `relayed`, et `client_says` joue la réponse du client."""

    def __init__(self, relay: bool = True, client_events: bool = True) -> None:
        self.now = lua_mod.START_MS
        self.hooks = lua_mod.Hooks()
        if not client_events:
            self.hooks.missing = ("OnClientCommand",)
        self.out: list[str] = []
        self.acks: list[str] = []
        self.commands = b""
        self.online: list[Any] = []
        self.time = WorldTime()
        self.paused_message: list[bool] = []
        self.relayed: list[tuple[Any, str, str, dict]] = []
        host = {
            "Events": self.hooks,
            "getCore": lambda: lua_mod.Stub(getGameAndBuildVersion=lambda: "42.21.0"),
            "getFileReader": self._reader,
            "getFileWriter": self._writer,
            "getGameTime": lambda: self.time,
            "getOnlinePlayers": lambda: lua_mod.Online(self.online),
            "getServerName": lambda: "duelA",
            "getTimestampMs": lambda: self.now,
            "instanceof": lambda value, name: isinstance(value, lua_mod.Player),
            "isServer": lambda: True,
            "setShowPausedMessage": self.paused_message.append,
        }
        if relay:
            host["sendServerCommand"] = self._relay
        Lua(host).run(lua_mod.MOD.read_text(encoding="utf-8"))
        self.fire("OnServerStarted")

    def _relay(self, player: Any, module: str, command: str, args: Table) -> None:
        self.relayed.append((player, module, command, dict(args.items)))

    def announce(self, seq: int, text: str = "Duel against naylo: 25 min.", tone: str = "start") -> None:
        command = {
            "v": 1,
            "seq": seq,
            "kind": "announce",
            "side": "A",
            "text": text,
            "tone": tone,
            "origin": {"rule": "announce"},
        }
        self.commands += dump_line(command)

    def client_says(self, seq: Any, shown: Any, module: str = "duelcore") -> None:
        args = Table()
        args.set("seq", seq)
        args.set("shown", shown)
        player = self.online[0] if self.online else None
        self.hooks.get("OnClientCommand").fire(module, "announce_result", player, args)

    def results(self) -> list[dict]:
        return [line["data"] for line in self.lines("cmd_result")]


def test_mod_declares_hud_only_when_it_can_relay():
    """`functions.hud` dit que le mod sait relayer : `sendServerCommand` existe, et la réponse du
    client peut être entendue. Mutations : `functions.hud = true` en dur prétend une capacité
    qu'un jeu sans `sendServerCommand` n'a pas ; ôter la ligne laisse tout au chat."""
    for relay, client_events, expected in (
        (True, True, True),
        (False, True, False),
        (True, False, False),
    ):
        server = HudServer(relay=relay, client_events=client_events)
        [caps] = server.lines("caps")
        assert caps["data"]["functions"]["hud"] is expected
        assert "OnClientCommand" not in caps["data"]["hooks"]
    # Le double d'avant P9F22, sans `sendServerCommand`, dit la même chose.
    old = PausedServer()
    old.fire("OnServerStarted")
    assert old.lines("caps")[0]["data"]["functions"]["hud"] is False


def test_mod_relays_the_announcement_and_answers_once_the_client_has_shown_it():
    """`announce` : relayée au joueur par `sendServerCommand`, texte et nature intacts ; le
    `cmd_result` attend la réponse du client et dit `applied`, marqué `announce`. La même ligne
    relue chaque seconde n'est relayée qu'une fois, et seule une annonce attendue donne un
    résultat. Mutations : rendre `applied` dès le relais prétend un texte que le client n'a pas
    affiché ; ôter la garde `cmd.seq <= lastAnnounceSeq` relaie l'annonce à chaque relecture ;
    ôter `announce = true` du résultat le fait prendre pour celui d'une commande du match."""
    server = HudServer()
    player = FreezablePlayer("durand")
    server.online.append(player)
    server.announce(1)
    server.paused_pass()
    server.paused_pass()

    assert server.relayed == [
        (player, "duelcore", "announce", {"seq": 1, "text": "Duel against naylo: 25 min.", "tone": "start"})
    ]
    assert server.results() == []

    server.client_says(7, True)
    server.client_says(1, True, module="autre")
    assert server.results() == []
    server.client_says(1, True)
    server.client_says(1, True)
    assert server.results() == [{"seq": 1, "status": "applied", "announce": True}]
    # Écrit sans attendre le vidage d'une seconde : le service sait vite si le joueur lit.
    assert '"announce":true' in "".join(server.out)


def test_mod_answers_no_hud_api_when_the_client_or_the_game_cannot_show():
    """Le client répond qu'il n'a rien affiché : `refused`, `no_hud_api`. Un jeu sans
    `sendServerCommand` le dit sans relayer. Mutations : traiter toute réponse comme `applied`
    cache au service un écran vide ; rendre `failed` plutôt que `refused`, `no_hud_api` quand le
    relais est impossible laisse le service écrire vers un jeu qui ne relaiera jamais."""
    server = HudServer()
    server.online.append(FreezablePlayer("durand"))
    server.announce(1)
    server.paused_pass()
    server.client_says(1, False)
    assert server.results() == [
        {"seq": 1, "status": "refused", "reason": "no_hud_api", "announce": True}
    ]

    bare = HudServer(relay=False)
    bare.online.append(FreezablePlayer("durand"))
    bare.announce(1)
    bare.paused_pass()
    assert bare.results() == [
        {"seq": 1, "status": "refused", "reason": "no_hud_api", "announce": True}
    ]


def test_mod_gives_up_on_a_silent_client_and_on_an_empty_server():
    """Sans réponse du client en trois secondes : `failed`, `no_reply`, une fois. Sans joueur :
    `refused`, `no_player`. Mutation : ôter `expireAnnounces` de `step` laisse l'annonce sans
    résultat pour toujours, et le service sans moyen de voir un client muet."""
    server = HudServer()
    server.announce(1)
    server.paused_pass()
    assert server.results() == [
        {"seq": 1, "status": "refused", "reason": "no_player", "announce": True}
    ]

    server.online.append(FreezablePlayer("durand"))
    server.announce(2)
    server.paused_pass()
    server.paused_pass()
    assert len(server.results()) == 1
    for _ in range(3):
        server.paused_pass()
    silent = {"seq": 2, "status": "failed", "reason": "no_reply", "announce": True}
    assert server.results()[1:] == [silent]
    server.client_says(2, True)
    server.paused_pass()
    assert server.results()[1:] == [silent]


def test_announcement_seq_is_apart_from_the_command_seq():
    """Une annonce et une commande du match portent le même `seq` : les deux passent, et
    l'acquittement ne suit que la commande. Une annonce de `seq` élevé ne ferme pas la porte aux
    commandes qui suivent. Mutation : faire avancer `lastCmdSeq` par l'annonce écarte sans un
    mot la commande du match de même `seq` ou de `seq` moindre."""
    server = HudServer()
    server.online.append(FreezablePlayer("durand"))
    server.announce(50)
    server.paused_pass()
    assert len(server.relayed) == 1
    assert json.loads(server.acks[-1])["seq"] == 0

    server.send(1, "hold_time")
    server.announce(51, "second", "wave")
    server.paused_pass()

    assert server.result(1)["status"] == "applied"
    assert "announce" not in server.result(1)
    assert [args["seq"] for *_, args in server.relayed] == [50, 51]
    assert json.loads(server.acks[-1])["seq"] == 1


# -- mod, côté client : l'affichage, et rien d'autre ------------------------------------------


class Screen:
    """Le jeu vu du client : `HaloTextHelper` (ou pas), le joueur local, et ce qui repart."""

    def __init__(self, halo: str = "ok") -> None:
        self.hooks = lua_mod.Hooks()
        self.player = lua_mod.Player("durand")
        self.shown: list[tuple] = []
        self.replies: list[tuple[Any, str, str, dict]] = []
        self.calls: list[str] = []
        host: dict[str, Any] = {
            "Events": self.hooks,
            "getPlayer": self._player,
            "sendClientCommand": self._reply,
        }
        if halo == "ok":
            host["HaloTextHelper"] = lua_mod.Stub(addText=self._add_text)
        elif halo == "other_build":
            host["HaloTextHelper"] = lua_mod.Stub(addText=self._refuse_text)
        elif halo == "no_method":
            host["HaloTextHelper"] = lua_mod.Stub()
        Lua(host).run(CLIENT.read_text(encoding="utf-8"))

    def _player(self) -> lua_mod.Player:
        self.calls.append("getPlayer")
        return self.player

    def _add_text(self, *args: Any) -> None:
        self.calls.append("addText")
        self.shown.append(args)

    def _refuse_text(self, *args: Any) -> None:
        self.calls.append("addText")
        raise TypeError("no such overload")

    def _reply(self, player: Any, module: str, command: str, args: Table) -> None:
        self.calls.append("sendClientCommand")
        self.replies.append((player, module, command, dict(args.items)))

    def receive(self, text: Any = "Duel against naylo: 25 min.", tone: Any = "start", seq: int = 4,
                module: str = "duelcore", command: str = "announce") -> None:
        args = Table()
        args.set("seq", seq)
        args.set("text", text)
        args.set("tone", tone)
        self.hooks.get("OnServerCommand").fire(module, command, args)


def calls_in(node: Any) -> list[tuple]:
    """Les appels d'un arbre, corps de fonctions exclus : ce qui s'exécute au chargement."""
    if not isinstance(node, (tuple, list)):
        return []
    if isinstance(node, tuple) and node and node[0] == "function":
        return []
    found = [node] if isinstance(node, tuple) and node and node[0] in ("invoke", "method") else []
    for child in node:
        found += calls_in(child)
    return found


def test_client_file_calls_nothing_until_a_message_arrives():
    """Hors duel, le mod reste inerte. Statique : au chargement, le fichier ne fait que déclarer
    des locales et s'inscrire à `OnServerCommand` — aucun autre appel hors d'un corps de
    fonction. Dynamique : chargé, il n'a touché ni au joueur, ni à l'écran, ni au réseau.
    Mutation : un `getPlayer()` ou un `HaloTextHelper.addText(…)` posé en tête de fichier (un
    « bonjour » au chargement) fait échouer les deux moitiés."""
    source = CLIENT.read_text(encoding="utf-8")
    statements = _Parser(source).block()
    assert {statement[0] for statement in statements} <= {"local", "localfunction", "call"}
    top_calls = calls_in(statements)
    subscribe = ("index", ("index", ("name", "Events"), ("const", "OnServerCommand")), ("const", "Add"))
    assert [call[1] for call in top_calls] == [subscribe]
    # La garde ne se trompe pas de cible : un appel au chargement serait bien vu.
    assert len(calls_in(_Parser("local p = getPlayer()\n" + source).block())) == 2
    for word in ("OnTick", "OnGameStart", "OnCreatePlayer", "require", "getFileWriter"):
        assert word not in source

    screen = Screen()
    assert screen.calls == []
    assert len(screen.hooks.get("OnServerCommand").handlers) == 1
    assert list(screen.hooks.items) == ["OnServerCommand"]

    screen.receive(module="autre_mod")
    screen.receive(command="autre_action")
    screen.receive(text=None)
    assert screen.calls == []


def test_client_shows_the_text_above_the_player_with_the_colour_of_its_nature():
    """`announce` : `HaloTextHelper.addText(joueur, texte, séparateur, r, g, b)` — la forme à
    couleur lue dans le jar du jeu, dont le troisième argument est un séparateur — puis la réponse
    `shown = true` au serveur. Une couleur par nature, toutes distinctes.
    Mutations : ne plus appeler `addText` répond `shown` sans rien afficher ; la même couleur
    pour toutes les natures ; ne plus répondre laisse le serveur en `no_reply`."""
    screen = Screen()
    colours = {}
    for seq, tone in enumerate(TONES, start=1):
        screen.receive(text=f"texte {tone}", tone=tone, seq=seq)
        player, text, separator, *colour = screen.shown[-1]
        assert (player, text, separator) == (screen.player, f"texte {tone}", "[br/]")
        assert len(colour) == 3 and all(isinstance(part, int) and 0 <= part <= 255 for part in colour)
        colours[tone] = tuple(colour)
        assert screen.replies[-1] == (
            screen.player, "duelcore", "announce_result", {"seq": seq, "shown": True}
        )
    assert len(set(colours.values())) == len(TONES)

    screen.receive(tone="inconnue", seq=9)
    assert screen.shown[-1][3:] == (255, 255, 255)
    assert screen.replies[-1][3] == {"seq": 9, "shown": True}


@pytest.mark.parametrize("halo", ["absent", "no_method", "other_build"])
def test_client_without_the_halo_api_breaks_nothing_and_claims_nothing(halo):
    """`HaloTextHelper` absent, sans `addText`, ou d'une autre signature : aucune erreur ne
    sort du gestionnaire, et la réponse dit `shown = false` — le serveur rendra `no_hud_api`.
    Mutations : ôter le `pcall` de `show` laisse l'erreur casser la réception ; répondre
    `shown = true` sans regarder le résultat prétend un texte que personne ne voit."""
    screen = Screen(halo)

    screen.receive(seq=3)

    assert screen.shown == []
    assert screen.replies == [(screen.player, "duelcore", "announce_result", {"seq": 3, "shown": False})]


# -- du service à l'écran ---------------------------------------------------------------------


@pytest.mark.parametrize(("halo", "expected"), [("ok", "hud"), ("absent", "chat")])
def test_from_the_service_to_the_screen_and_back(halo, expected):
    """La chaîne entière sur le vrai Lua : l'annonce décidée par le service traverse le mod et le
    client, et sa réponse revient régler le canal. Avec `HaloTextHelper`, le texte est à l'écran
    et le camp reste en texte flottant ; sans lui, le service repasse à `servermsg`."""
    wired = Wired()
    server = HudServer()
    player = FreezablePlayer("durand")
    server.online.append(player)
    screen = Screen(halo)
    screen.player = player
    wired.sender.on_caps("A", server.lines("caps")[0]["data"])

    wired.sender.say("A", 'Duel contre "naylo" : 25 min.', "start")
    server.commands += dump_line(wired.commands("A")[0])
    server.paused_pass()
    _, module, command, args = server.relayed[0]
    screen.receive(text=args["text"], tone=args["tone"], seq=args["seq"], module=module, command=command)
    _, module, command, reply = screen.replies[0]
    assert (module, command) == ("duelcore", "announce_result")
    server.client_says(reply["seq"], reply["shown"])
    [result] = server.results()
    wired.sender.on_result("A", result)

    assert wired.sender.channels()["A"]["channel"] == expected
    if halo == "ok":
        assert screen.shown[0][1] == "Duel contre 'naylo' : 25 min."
        assert wired.sender.delivered and wired.chat.said == []
    else:
        assert wired.chat.said == [("A", "Duel contre 'naylo' : 25 min.")]
        assert wired.notices == [("A", "no_hud_api")]


# -- lobby ------------------------------------------------------------------------------------


def test_lobby_says_when_announcements_only_reach_the_chat():
    """Un camp au chat : la page le dit, avec la raison, sans prétendre que c'est visible. Deux
    camps en texte flottant : rien à dire. Le bloc de la page est exécuté tel quel quand `node`
    est là ; son branchement dans le lobby est lu dans le source.
    Mutations : ne plus appeler `announceNotice` dans la carte du lobby ; ne pas échapper la
    raison ; afficher l'avertissement pour un camp `hud`."""
    script = UI_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert (
        "${holdNotice()}${announceNotice((S.capabilities && S.capabilities.announce) || {})}"
        in script
    )
    start = script.index("const ANNOUNCE_WHY = ")
    block = script[start : script.index("\n}\n", script.index("function announceNotice(", start)) + 3]
    assert "esc(chat.map(why).join(', '))" in block

    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        return
    views = [
        {"A": {"channel": "hud", "reason": None}, "B": {"channel": "hud", "reason": None}},
        {},
        {"A": {"channel": "hud", "reason": None}, "B": {"channel": "chat", "reason": "no_hud_api"}},
        {"A": {"channel": "chat", "reason": "<b>"}, "B": {"channel": "chat", "reason": "no_hud_caps"}},
    ]
    harness = (
        "const esc = (s) => String(s).replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
        + block
        + f"\nconsole.log(JSON.stringify({json.dumps(views)}.map(announceNotice)"
        + ".map((html) => html.replace(/\\s+/g, ' '))));\n"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30, check=True)
    quiet_a, quiet_b, one, two = json.loads(done.stdout)
    assert quiet_a == quiet_b == ""
    assert one.startswith('<p class="err" id="announce-chat">In-game announcements on side B (its game')
    assert "only go to the chat: nothing shows on screen" in one and "side A" not in one
    assert "side A (&lt;b&gt;), B (its mod cannot relay them)" in two


# -- préavis de fin : le suivi des annonces continue ------------------------------------------


class NoticeMatch:
    """Ce que `end_notice` touche d'un match jugé : `pump` ne tourne plus, `out` se lit encore."""

    result = {"outcome": "draw", "winner": None}
    poll_s = 0.5
    _announce = engine_module.RealMatch._announce
    _follow_announces = engine_module.RealMatch._follow_announces
    end_notice = engine_module.RealMatch.end_notice

    class redactor:
        redact = staticmethod(lambda text: text)
        redact_exception = staticmethod(str)

    class Out:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def drain(self):
            events, self.events = self.events, []
            yield events, []

    def __init__(self) -> None:
        self.wired = Wired()
        self.wired.sender.on_caps("A", HUD_CAPS)
        self._announcer = MatchAnnouncer(self.wired.sender, dict(DEFAULTS))
        self._readers = {"A": self.Out()}
        self.waits: list[float] = []
        self.on_wait = lambda: None

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.wired.clock.now += seconds
        self.on_wait()
        return False


def test_end_notice_rewrites_a_final_announcement_erased_from_the_queue():
    """La fin, effacée de `in` par la troncature de la passe qui conclut, n'a pas de réponse :
    le préavis la réécrit à `RESEND_S`, lit le `applied` du client, puis attend son reste d'un
    bloc. Le préavis dure `end_notice_s`, pas plus.
    Mutation : un `end_notice` qui n'appelle que `flush` puis `wait` ne réécrit jamais la fin."""
    match = NoticeMatch()
    match.wired.sender.say("A", "Over: it is a draw.", "end")

    def client() -> None:
        if len(match.wired.written) == 2 and not match._readers["A"].events:
            match._readers["A"].events.append(
                {"kind": "cmd_result", "data": {"seq": 2, "status": "applied", "announce": True}}
            )

    match.on_wait = client
    match.end_notice(match.wait)

    assert [(c["seq"], c["tone"]) for c in match.wired.commands("A")] == [(1, "end"), (2, "end")]
    assert match.wired.sender.delivered and not match.wired.sender.pending()
    assert sum(match.waits) == DEFAULTS["end_notice_s"]
    assert match.waits[-1] > match.poll_s


def test_end_notice_sends_a_refused_final_announcement_to_the_chat():
    """Le client répond `no_hud_api` à la fin, pendant le préavis : elle repart par `servermsg`,
    que le préavis attend une seconde fois.
    Mutation : ne pas lire `out` pendant le préavis laisse la fin sur un écran qui ne l'affiche pas."""
    match = NoticeMatch()
    match.wired.sender.say("A", "Over: it is a draw.", "end")
    match._readers["A"].events.append({"kind": "cmd_result", "data": {"seq": 1, **REFUSED}})
    match._readers["A"].events.append({"kind": "beat", "data": {"players": []}})

    match.end_notice(match.wait)

    assert match.wired.chat.to("A") == ["Over: it is a draw."]
    assert match.wired.notices == [("A", "no_hud_api")]
    assert match.wired.chat.flushed == [engine_module.END_FLUSH_S] * 2
    assert sum(match.waits) == DEFAULTS["end_notice_s"]
