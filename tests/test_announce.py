"""Annonces en jeu : ce que le joueur lit dans sa partie, et ce qui ne doit jamais gêner le match."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from doubles.fake_server import FakeRcon
from test_match import KILL, Duel, presets  # noqa: F401
from test_snapshots import engine_rig  # noqa: F401
from service import engine as engine_module
from service import main as main_module
from service.announce import (
    DEFAULTS,
    TEXTS,
    AnnounceError,
    MatchAnnouncer,
    RconSender,
    kill_reward,
    plain,
    rcon_announcer,
    settings,
)
from service.rcon import RconError
from service.rules import load_runtime

PLAYERS = {"A": "durand", "B": "naylo"}
PASSWORD = "rcon-secret-42"


class Recorder:
    """Canal d'annonce de test : garde ce qui est dit, par camp, dans l'ordre."""

    def __init__(self, delivered: bool = True) -> None:
        self.said: list[tuple[str, str]] = []
        self.delivered = delivered
        self.flushed: list[float] = []
        self.closed = 0

    def say(self, side: str, text: str) -> None:
        self.said.append((side, text))

    def flush(self, timeout_s: float) -> bool:
        self.flushed.append(timeout_s)
        return self.delivered

    def close(self) -> None:
        self.closed += 1

    def to(self, side: str) -> list[str]:
        return [text for target, text in self.said if target == side]


def announcer(lang: str = "en", **options) -> tuple[MatchAnnouncer, Recorder]:
    recorder = Recorder()
    return MatchAnnouncer(recorder, {**DEFAULTS, "lang": lang, **options}), recorder


def sent(side: str, kind: str = "spawn_pop", **fields) -> dict:
    return {"type": "command", "side": side, "data": {"command": {"kind": kind, **fields}}}


# -- textes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Zoé l'été", "Zoe l'ete"),
        ('pseudo" ; quit "', "pseudo' ; quit '"),
        ("ligne\ncoupée\ttab", "ligne coupee tab"),
        ("a\\b", "a/b"),
        ("   ", ""),
    ],
)
def test_plain_text_cannot_break_out_of_the_command(raw, cleaned):
    """Un pseudo ne ferme pas le guillemet de `servermsg "…"`.

    Mutation : retirer `.replace('"', "'")` de `plain` — `pseudo" ; quit "` sortirait de l'argument.
    """
    assert plain(raw) == cleaned
    assert len(plain("x" * 1000)) == 240


def test_every_text_is_ascii_and_both_languages_say_the_same_things():
    """Rien ne garantit l'encodage d'un accent jusqu'au jeu : les textes livrés n'en portent pas."""
    assert set(TEXTS["fr"]) == set(TEXTS["en"])
    for texts in TEXTS.values():
        for text in texts.values():
            assert text.isascii() and '"' not in text


# -- réglages ----------------------------------------------------------------------------------


def test_shipped_runtime_announces_in_french_and_is_valid():
    options = settings(load_runtime())

    assert options["enabled"] is True and options["lang"] == "fr"
    assert settings({}) == dict(DEFAULTS)


@pytest.mark.parametrize(
    ("given", "named"),
    [
        ({"lang": "de"}, "announce.lang"),
        ({"enabled": "yes"}, "announce.enabled"),
        ({"wave_gap_s": -1}, "announce.wave_gap_s"),
        ({"end_notice_s": True}, "announce.end_notice_s"),
        ({"queue_size": 0}, "announce.queue_size"),
        ({"volume": 3}, "announce.volume"),
        ("on", "announce"),
    ],
)
def test_invalid_setting_names_its_key(given, named):
    with pytest.raises(AnnounceError, match=named):
        settings({"announce": given})


def test_disabled_announcements_build_nothing():
    runtime = {**load_runtime(), "announce": {"enabled": False}}

    assert rcon_announcer(runtime, PASSWORD) is None


# -- ce que le joueur lit ----------------------------------------------------------------------


def test_first_player_in_is_told_why_nothing_moves():
    """Mutation : retirer l'appel `waiting` du lobby — le premier arrivé reste figé sans un mot."""
    say, recorder = announcer(waiting_repeat_s=45)

    say.waiting(0, {"A": None, "B": None}, [])
    assert recorder.said == []

    say.waiting(1_000, {"A": None, "B": "naylo"}, ["B"])
    say.waiting(1_250, {"A": None, "B": "naylo"}, ["B"])
    assert recorder.to("B") == [TEXTS["en"]["waiting_frozen"]]
    assert recorder.to("A") == []

    say.waiting(46_000, {"A": None, "B": "naylo"}, ["B"])
    assert len(recorder.to("B")) == 2

    say.waiting(47_000, {"A": "durand", "B": "naylo"}, ["A", "B"])
    assert len(recorder.said) == 2


def test_waiting_does_not_claim_a_freeze_the_game_refused():
    say, recorder = announcer()

    say.waiting(0, {"A": "durand", "B": None}, [])

    assert recorder.to("A") == [TEXTS["en"]["waiting"]]
    assert "frozen" not in recorder.to("A")[0]


def test_start_names_the_opponent_the_length_and_the_rule():
    table = {
        "rules": [
            {"when": "atom(zombie_kill)", "every": 1, "effects": [{"kind": "spawn_pop", "n": 10}]},
            {"when": "atom(damage_taken)", "effects": [{"kind": "supply_drop", "target": "self"}]},
        ]
    }
    say, recorder = announcer()

    say.started(PLAYERS, 1500, table)

    assert recorder.to("A") == [
        "PZ Duel: the duel against naylo starts now! Length: 25 min."
        " Each zombie you kill sends 10 to naylo."
    ]
    assert "against durand" in recorder.to("B")[0]


def test_kill_reward_only_speaks_for_a_simple_rule():
    simple = {"when": "atom(zombie_kill)", "effects": [{"kind": "spawn_pop", "n": 2}]}
    every_three = {**simple, "every": 3}
    to_self = {"when": "atom(zombie_kill)", "effects": [{"kind": "spawn_pop", "n": 2, "target": "self"}]}

    assert kill_reward({"rules": [simple]}) == 2
    assert kill_reward({"rules": [every_three, to_self]}) is None
    assert kill_reward({}) is None


def test_waves_add_up_and_are_said_once_per_gap():
    """À dix zombies par kill, une annonce par commande noierait le joueur.

    Mutation : annoncer chaque commande dans `tick` — trois lignes au lieu d'une.
    """
    say, recorder = announcer(wave_gap_s=10)
    score = {"A": 3, "B": 1}

    say.tick(1_000, [sent("B", n=10)], PLAYERS, score, 1400)
    assert recorder.to("B") == ["durand sends you 10 zombies! Score: you 1 - 3 durand. 23 min left."]
    assert recorder.to("A") == []

    say.tick(2_000, [sent("B", n=10), sent("B", "spawn_sprinter", n=1)], PLAYERS, score, 1399)
    say.tick(5_000, [sent("B", n=10)], PLAYERS, score, 1396)
    assert len(recorder.to("B")) == 1

    say.tick(11_000, [], PLAYERS, {"A": 6, "B": 1}, 1390)
    assert recorder.to("B")[1] == "durand sends you 21 zombies! Score: you 1 - 6 durand. 23 min left."

    say.tick(30_000, [sent("A", n=1)], PLAYERS, {"A": 6, "B": 2}, 45)
    assert recorder.to("A") == ["naylo sends you 1 zombie! Score: you 6 - 2 naylo. 45 s left."]


def test_supply_drop_is_said_to_the_player_who_gets_it():
    say, recorder = announcer()

    say.tick(0, [sent("A", "supply_drop")], PLAYERS, {"A": 0, "B": 0}, 600)

    assert recorder.said == [("A", TEXTS["en"]["supply"])]


def test_other_records_and_world_commands_say_nothing():
    say, recorder = announcer()
    records = [
        {"type": "event", "side": "A", "data": {"kind": "zombie_kill"}},
        sent("A", "weather_rain"),
        {"type": "command", "side": None, "data": {"command": {"kind": "spawn_pop", "n": 5}}},
    ]

    say.tick(0, records, PLAYERS, {"A": 1, "B": 0}, 600)

    assert recorder.said == []


def test_time_marks_are_said_once_to_both_players():
    say, recorder = announcer()
    say.started(PLAYERS, 420, {})
    recorder.said.clear()
    score = {"A": 4, "B": 9}

    say.tick(0, [], PLAYERS, score, 400)
    assert recorder.said == []

    say.tick(0, [], PLAYERS, score, 300)
    say.tick(0, [], PLAYERS, score, 299)
    assert recorder.said == [
        ("A", "5 min left. Score: you 4 - 9 naylo."),
        ("B", "5 min left. Score: you 9 - 4 durand."),
    ]

    say.tick(0, [], PLAYERS, score, 59)
    assert recorder.to("A")[-1] == "1 min left. Score: you 4 - 9 naylo."
    assert len(recorder.said) == 4


def test_a_match_shorter_than_a_mark_never_says_it():
    """Un duel de 90 s n'annonce pas « 5 min left » à sa première seconde."""
    say, recorder = announcer()
    say.started(PLAYERS, 90, {})
    recorder.said.clear()

    say.tick(0, [], PLAYERS, {"A": 0, "B": 0}, 89)
    assert recorder.said == []

    say.tick(0, [], PLAYERS, {"A": 0, "B": 0}, 60)
    assert [text for _, text in recorder.said] == ["1 min left. Score: you 0 - 0 naylo."] + [
        "1 min left. Score: you 0 - 0 durand."
    ]


@pytest.mark.parametrize(
    ("result", "to_a", "to_b"),
    [
        (
            {"outcome": "death", "winner": "B"},
            "naylo won. You died.",
            "you WON against durand. durand died.",
        ),
        (
            {"outcome": "forfeit", "winner": "A"},
            "you WON against naylo. naylo was away for too long.",
            "durand won. You were away for too long.",
        ),
        (
            {"outcome": "win", "winner": "A"},
            "you WON against naylo. Time is up.",
            "durand won. Time is up.",
        ),
        ({"outcome": "draw", "winner": None}, "it is a draw. Time is up.", "it is a draw. Time is up."),
        (
            {"outcome": "invalidated", "winner": None},
            "this match does not count.",
            "this match does not count.",
        ),
    ],
)
def test_end_is_said_to_each_player_from_their_side(result, to_a, to_b):
    """Mutation : dire `won` aux deux camps — le perdant lirait qu'il a gagné."""
    say, recorder = announcer()

    say.ended(result, PLAYERS, {"A": 7, "B": 5})
    say.ended(result, PLAYERS, {"A": 7, "B": 5})

    (text_a,), (text_b,) = recorder.to("A"), recorder.to("B")
    assert to_a in text_a and to_b in text_b
    assert text_a.endswith("The server is about to close.")
    if result["outcome"] != "invalidated":
        assert "Score: you 7 - 5 naylo." in text_a and "Score: you 5 - 7 durand." in text_b


def test_nothing_is_said_after_the_end():
    say, recorder = announcer()
    say.ended({"outcome": "draw", "winner": None}, PLAYERS, {"A": 0, "B": 0})
    recorder.said.clear()

    say.tick(99_000, [sent("A", n=10)], PLAYERS, {"A": 0, "B": 0}, 0)

    assert recorder.said == []


def test_a_missing_or_odd_pseudo_still_gives_a_readable_line():
    say, recorder = announcer(lang="fr")

    say.tick(0, [sent("A", n=2)], {"A": "durand", "B": None}, {"A": 0, "B": 1}, 130)
    say.tick(0, [sent("B", n=1)], {"A": 'Zoé "Z"', "B": "naylo"}, {"A": 0, "B": 1}, 130)

    assert recorder.to("A") == [
        "ton adversaire t'envoie 2 zombies ! Score : toi 0 - 1 ton adversaire. Reste 2 min."
    ]
    assert recorder.to("B") == ["Zoe 'Z' t'envoie 1 zombie ! Score : toi 1 - 0 Zoe 'Z'. Reste 2 min."]


# -- envoi par RCON ----------------------------------------------------------------------------


def wait_for(check, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "condition jamais atteinte"
        time.sleep(0.01)


def test_sender_passes_a_quoted_servermsg_to_the_right_server():
    with FakeRcon(PASSWORD) as rcon_a, FakeRcon(PASSWORD) as rcon_b:
        sender = RconSender({"A": rcon_a.port, "B": rcon_b.port}, PASSWORD, 2, 16)
        try:
            sender.say("B", 'Zoé dit "bonjour"\n')
            assert sender.flush(5) is True
        finally:
            sender.close()

        assert rcon_b.commands == ["servermsg \"Zoe dit 'bonjour'\""]
        assert rcon_a.commands == []
        assert sender.delivered is True


def test_sender_never_blocks_nor_raises_when_the_server_is_gone(caplog):
    """Mutation : envoyer dans `say` au lieu du fil — le match attendrait le délai RCON."""
    with FakeRcon(PASSWORD) as gone:
        port = gone.port
    sender = RconSender({"A": port, "B": port}, PASSWORD, 0.5, 16)
    begin = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="service.announce"):
        try:
            for _ in range(5):
                sender.say("A", "hello")
            assert time.monotonic() - begin < 0.4
            assert sender.flush(10) is False
        finally:
            sender.close()

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1 and "serveur A" in warnings[0]
    assert PASSWORD not in caplog.text


def test_sender_with_a_wrong_password_warns_without_citing_it(caplog):
    with FakeRcon("other-password") as rcon:
        sender = RconSender({"A": rcon.port}, PASSWORD, 1, 16)
        with caplog.at_level(logging.WARNING, logger="service.announce"):
            try:
                sender.say("A", "hello")
                assert sender.flush(5) is False
            finally:
                sender.close()

        assert rcon.commands == []
    assert "annonce en jeu impossible" in caplog.text and PASSWORD not in caplog.text


def test_full_queue_drops_the_oldest_lines_not_the_caller():
    release = threading.Event()
    seen: list[str] = []

    class SlowClient:
        def __init__(self, port, password, timeout_s):
            pass

        def __enter__(self):
            release.wait(5)
            return self

        def __exit__(self, *exc_info):
            return None

        def command(self, text):
            seen.append(text)

    sender = RconSender({"A": 1}, PASSWORD, 1, 2, connect=SlowClient)
    try:
        sender.say("A", "first")
        wait_for(lambda: sender._queue.empty())
        for index in range(6):
            sender.say("A", f"line {index}")
        release.set()
        assert sender.flush(5) is True
    finally:
        sender.close()

    assert seen == ['servermsg "first"', 'servermsg "line 4"', 'servermsg "line 5"']


def test_closed_sender_says_nothing_more():
    calls: list[str] = []

    class Client:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def command(self, text):
            calls.append(text)

    sender = RconSender({"A": 1}, PASSWORD, 1, 4, connect=Client)
    sender.close()
    sender.close()
    sender.say("A", "too late")

    assert calls == [] and not sender._thread.is_alive()


def test_unexpected_failure_of_one_line_does_not_stop_the_next():
    calls: list[str] = []

    class Client:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def command(self, text):
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("boom")
            if len(calls) == 2:
                raise RconError("muet")

    sender = RconSender({"A": 1}, PASSWORD, 1, 8, connect=Client)
    try:
        for index in range(3):
            sender.say("A", f"line {index}")
        assert sender.flush(5) is True
    finally:
        sender.close()

    assert len(calls) == 3


# -- dans un vrai match, sur le double ---------------------------------------------------------

MATCH_S = 60


def recorded_match(monkeypatch, recorder: Recorder, **options) -> None:
    def open_announcer(self, rcon_password):
        return MatchAnnouncer(recorder, {**DEFAULTS, "lang": "en", **options})

    monkeypatch.setattr(engine_module.Engine, "_open_announcer", open_announcer)


def test_match_tells_both_players_the_start_the_waves_and_the_end(
    engine_rig, presets, monkeypatch  # noqa: F811
):
    """Le parcours entier : départ, vague reçue, fin, puis fermeture du canal.

    Mutation : retirer `self._announce(...)` de `_conclude` — la fin n'est plus dite et les
    joueurs sont coupés sans un mot.
    """
    recorder = Recorder(delivered=False)
    recorded_match(monkeypatch, recorder, wave_gap_s=0)
    path = presets / "demo.json"
    path.write_text(
        json.dumps({**json.loads(path.read_text("utf-8")), "duration_s": MATCH_S}), "utf-8"
    )
    duel = Duel(engine_rig)
    duel.start_match()

    # Le premier arrivé a d'abord lu qu'il attendait ; puis chacun lit le départ.
    assert any(text.startswith("PZ Duel: waiting for your opponent") for _, text in recorder.said)
    (start_a,) = [text for text in recorder.to("A") if "starts now" in text]
    (start_b,) = [text for text in recorder.to("B") if "starts now" in text]
    assert "the duel against dupont starts now" in start_a
    assert "the duel against durand starts now" in start_b

    duel.at_match(1)
    duel.kills("A", 1)
    engine_rig.until(lambda _: any("sends you" in text for text in recorder.to("B")))
    wave = next(text for text in recorder.to("B") if "sends you" in text)
    assert wave.startswith("durand sends you ") and "Score: you 0 - " in wave
    assert not any("sends you" in text for text in recorder.to("A"))

    duel.at_match(MATCH_S)
    state = duel.finish()

    assert state["result"]["winner"] == "A"
    assert "you WON against dupont" in recorder.to("A")[-1]
    assert "durand won" in recorder.to("B")[-1]
    assert recorder.flushed == [engine_module.END_FLUSH_S]
    assert recorder.closed == 1
    assert duel.replays()


def test_end_notice_waits_only_when_a_player_could_read_it():
    """Le préavis retarde l'arrêt : sans annonce arrivée, personne ne lit, rien n'attend.

    Mutation : attendre sans regarder `flush` — chaque fin de duel coûterait 8 s, même sans RCON.
    """

    class Judged:
        result = {"outcome": "draw", "winner": None}
        redactor = None
        _announce = engine_module.RealMatch._announce
        end_notice = engine_module.RealMatch.end_notice

    waits: list[float] = []
    for delivered, expected in ((False, []), (True, [8])):
        match = Judged()
        match._announcer = MatchAnnouncer(Recorder(delivered), dict(DEFAULTS))
        waits.clear()
        match.end_notice(waits.append)
        assert waits == expected

    unjudged = Judged()
    unjudged.result = None
    unjudged._announcer = MatchAnnouncer(Recorder(True), dict(DEFAULTS))
    unjudged.end_notice(waits.append)
    assert waits == [8]

    silent = Judged()
    silent._announcer = None
    silent.end_notice(waits.append)
    assert waits == [8]


def test_broken_announcer_never_breaks_the_match(engine_rig, presets, monkeypatch, caplog):  # noqa: F811
    class Broken(Recorder):
        def say(self, side, text):
            raise RuntimeError("canal cassé")

    recorded_match(monkeypatch, Broken())
    duel = Duel(engine_rig)

    with caplog.at_level(logging.WARNING, logger="service.engine"):
        duel.start_match()
        duel.at_match(1)
        duel.kills("A", 1)
        state = engine_rig.until(lambda s: s["score"]["A"] == 1)

    assert state["phase"] == "running"
    assert "annonce en jeu" in caplog.text


def test_engine_without_a_factory_opens_no_announcer(engine_rig):  # noqa: F811
    """Seul `service.main` branche les annonces : aucun test n'ouvre de RCON d'annonce par mégarde."""
    engine = engine_rig.build()

    assert engine._open_announcer(PASSWORD) is None


def test_invalid_announce_setting_is_logged_and_does_not_refuse_the_duel(engine_rig):  # noqa: F811
    engine = engine_rig.build()
    engine.runtime["announce"] = {"lang": "de"}
    engine._announcer_factory = rcon_announcer

    assert engine._open_announcer(PASSWORD) is None
    assert any("annonces en jeu coupées" in line["msg"] for line in engine.state.logs)


def test_main_gives_the_real_engine_its_announcements(monkeypatch):
    """Mutation : `Engine(mode=…, pz_dir=…)` sans fabrique dans `main` — plus aucune annonce."""
    built: list[dict] = []

    class Stop(Exception):
        pass

    def spy(**kwargs):
        built.append(kwargs)
        raise Stop

    monkeypatch.setattr(main_module, "Engine", spy)
    with pytest.raises(Stop):
        main_module.main(["--mode", "mock", "--no-browser"])

    assert built[0]["announcer_factory"] is rcon_announcer
