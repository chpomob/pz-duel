"""P9F21 : la tenue s'applique au joueur qui arrive après, et le mod dit ce qu'il a posé.

Le mod pose, à chaque passe et pour chaque joueur en ligne, `setInvulnerable`,
`setZombiesDontAttack`, `setInvisible` puis `setBlockMovement`, sans reposer ce qui l'est déjà,
et rapporte une fois par joueur et par tenue la liste des appels réussis (`hold_applied`). Le
service la journalise et la publie (`hold.players`), le lobby affiche le compte.
Chaque test cite la mutation qui le fait échouer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from doubles.mini_lua import LuaError
from doubles.waiting import wait_until
from test_match import (  # noqa: F401
    WAIT_S,
    FreezablePlayer,
    HoldDuel,
    PausedServer,
    engine_rig,
    hold_caps,
)
from test_modes import call

from service import engine as engine_module
from service.api import make_server
from service.protocol import parse_event
from service.rules import load_effects
from service.state import held_player
from service.transport import BAD_LINE

UI_SCRIPT = Path(__file__).resolve().parent.parent / "ui" / "app.js"
CALLS = ["setInvulnerable", "setZombiesDontAttack", "setInvisible", "setBlockMovement"]
SEQUENCE = [(call_, True) for call_ in CALLS]


class Recorded(FreezablePlayer):
    """`FreezablePlayer` qui note aussi `setZombiesDontAttack` : les quatre appels, dans l'ordre."""

    def setZombiesDontAttack(self, shielded: bool) -> None:
        self.calls.append(("setZombiesDontAttack", shielded))
        self.shielded = shielded


def held_server(*players: FreezablePlayer) -> PausedServer:
    """Serveur dont la tenue (`seq` 1) est posée, les joueurs donnés déjà en ligne."""
    server = PausedServer()
    server.online.extend(players)
    server.fire("OnServerStarted")
    server.send(1, "hold_time")
    server.paused_pass()
    return server


def reports(server: PausedServer) -> list[dict]:
    return [line["data"] for line in server.lines("hold_applied")]


# -- mod : la séquence, pour l'arrivant comme pour le présent ---------------------------------


def test_player_joining_during_the_hold_gets_the_four_calls_in_order_at_the_next_pass():
    """Tenue posée serveur vide ; l'invité arrive : protégé, hors d'atteinte, caché, puis figé,
    à la passe suivante, sans commande. Mutations : ne tenir qu'à la réception de `hold_time`
    (plus de `blockPlayers` dans `holdTick`) ne lui pose rien ; ôter `setInvisible` de
    `HOLD_CALLS` le laisse visible ; permuter deux noms de `HOLD_CALLS` change l'ordre."""
    server = held_server()
    assert server.result(1)["status"] == "applied"
    late = Recorded("invite")
    server.online.append(late)
    assert late.calls == []

    server.fire("OnTick")

    assert late.calls == SEQUENCE
    assert late.invulnerable and late.shielded and late.invisible and late.blocked
    assert [r["data"]["status"] for r in server.lines("cmd_result")] == ["applied"]


def test_state_already_set_is_not_set_again_but_a_lost_one_is():
    """Ce qui est posé n'est pas reposé à chaque passe ; ce que le jeu dit avoir perdu l'est,
    seul. Mutations : `stillSet` qui rend toujours faux repose les quatre appels à chaque
    passe ; `stillSet` qui ne relit pas le jeu laisse le joueur visible."""
    host = Recorded("hote")
    server = held_server(host)
    assert host.calls == SEQUENCE
    for _ in range(3):
        server.paused_pass()
        server.fire("OnTick")
    assert host.calls == SEQUENCE

    host.invisible = False  # le jeu a défait l'invisibilité derrière le mod
    server.fire("OnTick")
    assert host.calls == [*SEQUENCE, ("setInvisible", True)]
    assert host.invisible


def test_player_back_under_a_new_object_gets_the_whole_sequence_again():
    """Même pseudo, nouvel objet (reconnexion pendant la tenue) : rien n'y est posé, la séquence
    repart entière. Mutation : ne pas vider `entry.set` quand l'objet change ne lui pose rien."""
    first = Recorded("invite")
    server = held_server(first)
    server.online.remove(first)
    second = Recorded("invite")
    server.online.append(second)

    server.fire("OnTick")

    assert second.calls == SEQUENCE
    assert first.calls == SEQUENCE


# -- mod : le rapport -------------------------------------------------------------------------


def test_hold_applied_is_emitted_once_per_player_and_per_hold_with_the_exact_list():
    """Un `hold_applied` par joueur et par tenue : camp, pseudo, appels dans l'ordre. L'arrivant
    a le sien à sa première passe ; une tenue nouvelle rapporte de nouveau. Mutations : retirer
    la garde `heldReported` en émet un par passe ; ne pas la vider dans `dropHold` tait la
    seconde tenue ; retirer `reportHold` de `freezePlayer` n'émet rien."""
    host = Recorded("hote")
    server = held_server(host)
    [line] = server.lines("hold_applied")
    assert (line["src"], line["side"]) == ("A", "A")
    assert line["data"] == {"player": "hote", "calls": CALLS}
    # La ligne passe le contrat de lecture du moteur, et pas celui du match : ce n'est pas un atome.
    raw = next(text for text in "".join(server.out).splitlines() if "hold_applied" in text)
    assert parse_event(raw, engine_module._reader_catalog(load_effects()))["kind"] == "hold_applied"

    late = Recorded("invite")
    server.online.append(late)
    for _ in range(4):
        server.paused_pass()
        server.fire("OnTick")
    assert reports(server) == [
        {"player": "hote", "calls": CALLS},
        {"player": "invite", "calls": CALLS},
    ]

    server.send(2, "release_time")
    server.paused_pass()
    server.send(3, "hold_time")
    server.paused_pass()
    server.paused_pass()
    assert server.result(3)["status"] == "applied"
    assert sorted(report["player"] for report in reports(server)) == [
        "hote", "hote", "invite", "invite",
    ]  # fmt: skip
    assert all(report["calls"] == CALLS for report in reports(server))


def test_missing_call_is_named_once_and_the_report_carries_the_calls_made_before_it():
    """`setInvisible` absent à l'arrivée de l'invité : un seul `no_hold_api`, qui le nomme, et
    `hold_applied` porte les deux appels réussis avant lui. Le joueur n'est pas figé, et ce qui
    lui a été posé est défait. Mutations : rapporter avant la boucle des appels rend une liste
    vide ; ne rien rapporter après un échec (`reportHold` sous le `return`) tait le joueur ;
    continuer après l'échec poserait `setBlockMovement` sur un joueur visible."""

    class Seen(Recorded):
        setInvisible = None

    server = held_server()
    late = Seen("vu")
    server.online.append(late)
    for _ in range(5):
        server.paused_pass()
        server.fire("OnTick")

    refusals = [r["data"] for r in server.lines("cmd_result") if r["data"]["status"] != "applied"]
    assert refusals == [
        {"seq": 1, "status": "refused", "reason": "no_hold_api", "call": "setInvisible"}
    ]
    assert reports(server) == [
        {"player": "vu", "calls": ["setInvulnerable", "setZombiesDontAttack"]}
    ]
    assert late.calls == [
        ("setInvulnerable", True),
        ("setZombiesDontAttack", True),
        ("setZombiesDontAttack", False),
        ("setInvulnerable", False),
    ]
    assert not late.blocked and not late.invulnerable


def test_first_call_missing_reports_an_empty_list():
    """Rien n'a pu être posé : `calls` est une liste vide, pas un objet ni une ligne absente.
    Mutation : encoder `calls` sans `[ARRAY]` écrit `{}`, que le service écarterait."""

    class Mortal(Recorded):
        setInvulnerable = None

    server = held_server(Mortal("mortel"))

    assert server.result(1)["call"] == "setInvulnerable"
    assert reports(server) == [{"player": "mortel", "calls": []}]


# -- mod : le relâchement ---------------------------------------------------------------------


def test_release_undoes_every_call_and_a_player_who_left_blocks_nothing():
    """Au relâchement, les quatre appels de chacun sont défaits, dans l'ordre inverse du risque ;
    l'invité parti entre-temps, dont l'objet ne répond plus, n'empêche ni `applied` ni le rendu
    de l'hôte. Mutations : ignorer `gone` dans `undo` rend `refused` (`setInvisible`) ; ne pas
    défaire un des appels dans `thawPlayer` le laisse posé sur l'hôte."""

    class Leaving(Recorded):
        gone = False

        def setInvisible(self, invisible: bool) -> None:
            if self.gone:
                raise LuaError("objet d'un joueur parti")
            super().setInvisible(invisible)

        def setBlockMovement(self, blocked: bool) -> None:
            if self.gone:
                raise LuaError("objet d'un joueur parti")
            super().setBlockMovement(blocked)

    host, guest = Recorded("hote"), Leaving("invite")
    server = held_server(host, guest)
    assert guest.calls == SEQUENCE
    server.online.remove(guest)
    guest.gone = True
    host.calls.clear()

    server.send(2, "release_time")
    server.paused_pass()

    assert server.result(2) == {"seq": 2, "status": "applied"}
    assert host.calls == [
        ("setInvisible", False),
        ("setBlockMovement", False),
        ("setZombiesDontAttack", False),
        ("setInvulnerable", False),
    ]
    assert not (host.invisible or host.blocked or host.shielded or host.invulnerable)
    # Ce que son objet acceptait encore est rendu aussi, et rien ne reste à réessayer.
    assert not guest.shielded and not guest.invulnerable
    server.send(3, "release_time")
    server.paused_pass()
    assert server.result(3)["status"] == "applied"
    assert guest.calls[-2:] == [("setZombiesDontAttack", False), ("setInvulnerable", False)]


def test_release_still_refuses_for_an_online_player_who_cannot_be_freed():
    """La tolérance ne vaut que pour un joueur parti : en ligne et impossible à rendre mobile, il
    garde sa protection et le relâchement rend `refused`. Mutation : traiter tout joueur comme
    parti (`gone` toujours vrai) rendrait `applied` en laissant un joueur figé."""

    class Stuck(Recorded):
        stuck = False

        def setBlockMovement(self, blocked: bool) -> None:
            if self.stuck:
                raise LuaError("refusé par le jeu")
            super().setBlockMovement(blocked)

    player = Stuck("fige")
    server = held_server(player)
    player.stuck = True

    server.send(2, "release_time")
    server.paused_pass()

    assert server.result(2) == {
        "seq": 2,
        "status": "refused",
        "reason": "no_hold_api",
        "call": "setBlockMovement",
    }
    assert player.blocked and player.invulnerable and player.shielded


# -- service ----------------------------------------------------------------------------------


def test_service_publishes_and_journals_the_players_held(engine_rig, hold_caps):
    """`hold_applied` lu dans `out` : journalisé sous son type, publié par camp dans
    `hold.players` (sous `capabilities`, où le lobby lit la tenue), servi par `/api/state`, sans
    ligne refusée. Un second rapport du même joueur remplace le premier ; un appel inconnu ou
    doublé est écarté. Mutations : donner aux lecteurs le catalogue du match (`effects` au lieu
    de `_reader_catalog`) écarte la ligne en `bad_line` ; ne pas brancher `_take_hold_applied`
    dans `_take_event` laisse `players` vide ; ne pas publier `players` dans `_capabilities`.

    P9F24 : `hold_time` est dans `in` avant que la passe qui l'a écrit ne publie `sent` — lu
    aussitôt, l'état dit encore `off`. Mutation : remplacer l'attente par `duel.state()` rend
    le test instable (`off` au lieu de `sent`)."""
    hold_caps()
    duel = HoldDuel(engine_rig)
    holds = duel.wait_holds()

    def hold_a() -> dict:
        return duel.state()["capabilities"]["hold"]["A"]

    wait_until(
        lambda: hold_a()["status"] == "sent" and "players" in hold_a(),
        what="tenue de A publiée `sent`, avec `players`",
        observe=hold_a,
    )
    assert hold_a() == {"status": "sent", "reason": None, "players": []}

    duel.mods["A"].emit("hold_applied", {"player": "invite", "calls": CALLS[:2]})
    duel.mods["A"].emit(
        "hold_applied", {"player": "invite", "calls": [*CALLS, "setInvisible", "autre"]}
    )
    duel.mods["A"].emit("hold_applied", {"player": "hote", "calls": CALLS[:2]})
    duel.mods["A"].emit("hold_applied", {"player": "", "calls": CALLS})
    duel.mods["A"].cmd_result(holds["A"]["seq"], "applied")
    state = engine_rig.until(lambda s: s["capabilities"]["hold"]["A"]["status"] == "held")

    players = [
        {"player": "invite", "calls": CALLS, "complete": True, "label": "tenu : invite (4/4)"},
        {
            "player": "hote",
            "calls": CALLS[:2],
            "complete": False,
            "label": "tenu : hote (2/4, setInvisible en échec)",
        },
    ]
    assert state["capabilities"]["hold"]["A"] == {
        "status": "held", "reason": None, "players": players,
    }  # fmt: skip
    assert state["capabilities"]["hold"]["B"]["players"] == []
    journal = [(r["side"], r["mt"], r["data"]) for r in duel.records("hold_applied")]
    assert journal == [
        ("A", None, {"player": "invite", "calls": CALLS[:2]}),
        ("A", None, {"player": "invite", "calls": CALLS}),
        ("A", None, {"player": "hote", "calls": CALLS[:2]}),
    ]
    assert duel.records(BAD_LINE) == []
    assert not [r for r in duel.records("event") if r["data"].get("kind") == "hold_applied"]

    server = make_server(duel.engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, served = call(f"http://127.0.0.1:{server.server_address[1]}/api/state")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(WAIT_S)
    assert status == 200
    assert served["capabilities"]["hold"]["A"]["players"] == players


def test_held_player_sentence_gives_the_count_and_the_first_missing_call():
    """La phrase du lobby : « (4/4) », ou le compte et le premier appel manquant de la séquence.
    Mutations : compter sans nommer l'appel ; nommer le dernier appel manquant au lieu du
    premier ; tenir pour complète une liste de trois appels."""
    assert held_player("invite", CALLS) == {
        "player": "invite", "calls": CALLS, "complete": True, "label": "tenu : invite (4/4)",
    }  # fmt: skip
    three = [call_ for call_ in CALLS if call_ != "setInvisible"]
    partial = held_player("invite", three)
    assert partial["label"] == "tenu : invite (3/4, setInvisible en échec)"
    assert partial["complete"] is False
    assert held_player("vu", CALLS[:2])["label"] == "tenu : vu (2/4, setInvisible en échec)"
    assert held_player("mortel", [])["label"] == "tenu : mortel (0/4, setInvulnerable en échec)"


# -- page -------------------------------------------------------------------------------------


def notice_script() -> str:
    """Le bloc de la page qui écrit les lignes « tenu : … », tel qu'il est servi."""
    script = UI_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = script.index("const HOLD_FAILED = ")
    return script[start : script.index("function holdNotice() {", start)]


def test_lobby_shows_the_sentence_of_each_held_player():
    """Sous l'état de tenue, une ligne par joueur rapporté, phrase du service échappée ; sur un
    camp dont la tenue est tombée, seul le joueur incomplet reste. Le bloc de la page est exécuté
    tel quel quand `node` est là ; son branchement dans `holdNotice` est lu dans le source.
    Mutations : ne plus appeler `heldPlayersNotice` dans `holdNotice` ; afficher autre chose que
    `label` ; ne pas l'échapper (`esc` retiré) ; lister les joueurs complets d'un camp refusé."""
    script = UI_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = script[script.index("function holdNotice() {") :]
    body = body[: body.index("\n}\n")]
    assert "const hold = (S.capabilities && S.capabilities.hold) || {};" in body
    assert body.rstrip().splitlines()[-2:] == [
        "  parts.push(heldPlayersNotice(hold));",
        "  return parts.join('');",
    ]
    block = notice_script()
    assert "${esc(entry.label)}" in block

    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        return
    three = [call_ for call_ in CALLS if call_ != "setInvisible"]
    hold = {
        "A": {
            "status": "held",
            "players": [held_player("hote", CALLS), held_player("<invite>", three)],
        },
        "B": {
            "status": "refused",
            "players": [held_player("ancien", CALLS), held_player("vu", CALLS[:2])],
        },
    }
    harness = (
        "const esc = (s) => String(s).replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
        + block
        + f"\nconsole.log(JSON.stringify(heldPlayersNotice({json.dumps(hold)})"
        + ".split('</p>').filter(Boolean)));\n"
    )
    # Node écrit en UTF-8 ; `text=True` seul décoderait avec l'encodage local (cp1252 sous Windows).
    done = subprocess.run(
        [node, "-e", harness], capture_output=True, encoding="utf-8", timeout=30, check=True
    )
    assert json.loads(done.stdout) == [
        '<p class="notice">tenu : hote (4/4)',
        '<p class="err">tenu : &lt;invite&gt; (3/4, setInvisible en échec)',
        '<p class="err">tenu : vu (2/4, setInvisible en échec)',
    ]


def test_p9f23_held_players_notice_survives_a_windows_locale(monkeypatch):
    """Sous un encodage local cp1252 (runner Windows), la phrase rendue par Node garde son « é ».

    Mutation : `encoding="utf-8"` retiré de l'appel à Node (retour à `text=True`) fait décoder la
    sortie en cp1252, « en échec » devient « en Ã©chec » et l'égalité tombe — l'erreur de la CI
    Windows sur P9F21.
    """
    if (shutil.which("node") or shutil.which("nodejs")) is None:
        pytest.skip("node absent")
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")

    test_lobby_shows_the_sentence_of_each_held_player()


# -- P9F24 : l'attente bornée -----------------------------------------------------------------


def test_p9f24_wait_until_returns_the_value_once_a_thread_has_produced_the_state():
    """L'état vient d'un fil, après l'appel : l'attente le rend dès qu'il tient, bien avant son
    délai. Mutation : un seul relevé sans boucle (`return condition()` d'emblée) rend `None`."""
    produced: list[str] = []
    timer = threading.Timer(0.05, lambda: produced.append("sent"))
    timer.start()
    try:
        started = time.monotonic()
        value = wait_until(lambda: produced and produced[0], timeout_s=5.0, what="état produit")
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
        timer.join()

    assert value == "sent"
    assert elapsed < 4.0


def test_p9f24_wait_until_names_the_condition_and_the_state_seen_when_it_expires():
    """Un état jamais atteint n'est pas masqué : l'échec cite `what`, le délai et l'état vu, et
    ne vient qu'après le délai. Mutations : ôter `what` du message ; citer la valeur de la
    condition (`False`) au lieu de `observe()` ; rendre la dernière valeur au lieu d'échouer."""
    state = {"status": "off", "reason": None}
    started = time.monotonic()

    with pytest.raises(AssertionError) as raised:
        wait_until(
            lambda: state["status"] == "sent", timeout_s=0.05, what="tenue `sent`",
            observe=lambda: state,
        )  # fmt: skip

    assert time.monotonic() - started >= 0.05
    assert str(raised.value) == (
        "attendu en 0.05 s : tenue `sent` ; observé : {'status': 'off', 'reason': None}"
    )


def test_p9f24_wait_until_without_observer_shows_the_last_value_of_the_condition():
    """Sans `observe`, l'état cité est ce que la condition a rendu en dernier : une condition
    qui rend la valeur lue (`None`, liste vide) se diagnostique seule.
    Mutation : ne rien citer après « observé »."""
    with pytest.raises(AssertionError, match=r"lien de A ; observé : \[\]$"):
        wait_until(lambda: [], timeout_s=0.02, what="lien de A")


def test_p9f24_wait_until_reads_once_more_at_the_deadline_and_never_swallows_a_failure():
    """Délai nul : la condition est tout de même lue une fois — un état déjà là n'échoue pas.
    Une exception de la condition remonte telle quelle, et une attente sans nom est refusée.
    Mutations : sortir de la boucle avant le relevé quand le délai est passé ; envelopper la
    condition d'un `except Exception` ; accepter `what=""`."""
    assert wait_until(lambda: "là", timeout_s=0, what="état déjà là") == "là"

    def broken() -> bool:
        raise KeyError("capabilities")

    with pytest.raises(KeyError, match="capabilities"):
        wait_until(broken, timeout_s=0.5, what="état lisible")
    with pytest.raises(ValueError, match="what"):
        wait_until(lambda: True, what=" ")
