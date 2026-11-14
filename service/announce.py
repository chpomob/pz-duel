"""Annonces en jeu : ce que l'arbitre sait, dit au joueur dans sa partie.

Le joueur ne regarde pas la page pendant un duel, et l'invité ne la voit jamais. Ce module
lui dit l'essentiel là où il regarde : l'attente de l'adversaire, le départ, les zombies qu'on
lui envoie, le score, le temps restant et la fin. Le canal est la commande `servermsg` du
serveur, par RCON : chaque serveur n'a qu'un joueur (`MaxPlayers=1`), l'annonce est donc privée.

Trois pièces, du plus pur au plus sale :

- `Phrases` écrit les textes, en ASCII : rien ne garantit l'encodage d'un accent jusqu'au jeu ;
- `MatchAnnouncer` décide quoi dire et quand, sans fil ni réseau : l'heure lui est donnée ;
- `RconSender` envoie, sur son propre fil : le match ne l'attend jamais.

Une annonce perdue n'est jamais une panne : rien ici ne touche au journal, au cœur ni au rejeu.
Ce module ne journalise aucun secret.
"""

from __future__ import annotations

import logging
import queue
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from .rcon import RconClient, RconError

LOGGER = logging.getLogger(__name__)

SIDES = ("A", "B")
MS_PER_S = 1000
SECONDS_PER_MINUTE = 60
COMMAND = "command"
SERVERMSG = "servermsg"
MAX_TEXT = 240
# Commandes qui envoient des zombies : leur `n` s'additionne dans l'annonce de vague.
WAVE_KINDS = ("spawn_pop", "spawn_sprinter", "horde_area")
SUPPLY_KIND = "supply_drop"
KILL_ATOM = "atom(zombie_kill)"
# Minutes restantes annoncées aux deux joueurs, une fois chacune.
TIME_MARKS_MIN = (10, 5, 1)
DEFAULTS: Mapping[str, Any] = {
    "enabled": True,
    "lang": "fr",
    "wave_gap_s": 10,
    "waiting_repeat_s": 45,
    "end_notice_s": 8,
    "timeout_s": 2,
    "queue_size": 16,
}
LANGS = ("fr", "en")

TEXTS: Mapping[str, Mapping[str, str]] = {
    "fr": {
        "waiting_frozen": "PZ Duel : en attente de ton adversaire. Ton monde est fige jusqu'a son "
        "arrivee, c'est normal.",
        "waiting": "PZ Duel : en attente de ton adversaire. Le duel partira des son arrivee.",
        "start": "PZ Duel : le duel commence contre {foe} ! Duree : {minutes} min.",
        "start_rule": " Chaque zombie que tu tues en envoie {n} chez {foe}.",
        "wave": "{foe} t'envoie {n} zombie{s} ! Score : toi {mine} - {theirs} {foe}. "
        "Reste {left}.",
        "supply": "Une caisse de soin vient de tomber pres de toi.",
        "mark": "Plus que {left}. Score : toi {mine} - {theirs} {foe}.",
        "won": "PZ Duel : fin du duel, tu as GAGNE contre {foe}. {why} Score : toi {mine} - "
        "{theirs} {foe}.",
        "lost": "PZ Duel : fin du duel, {foe} a gagne. {why} Score : toi {mine} - {theirs} {foe}.",
        "draw": "PZ Duel : fin du duel, egalite. {why} Score : toi {mine} - {theirs} {foe}.",
        "void": "PZ Duel : fin du duel, ce match ne compte pas.",
        "closing": " Le serveur va se fermer.",
        "why_death_won": "{foe} est mort.",
        "why_death_lost": "Tu es mort.",
        "why_forfeit_won": "{foe} est parti trop longtemps.",
        "why_forfeit_lost": "Tu es parti trop longtemps.",
        "why_time": "Temps ecoule.",
        "minutes": "{n} min",
        "seconds": "{n} s",
        "plural": "s",
        "foe": "ton adversaire",
    },
    "en": {
        "waiting_frozen": "PZ Duel: waiting for your opponent. Your world is frozen until they "
        "join, this is expected.",
        "waiting": "PZ Duel: waiting for your opponent. The duel starts as soon as they join.",
        "start": "PZ Duel: the duel against {foe} starts now! Length: {minutes} min.",
        "start_rule": " Each zombie you kill sends {n} to {foe}.",
        "wave": "{foe} sends you {n} zombie{s}! Score: you {mine} - {theirs} {foe}. {left} left.",
        "supply": "A medical crate just dropped near you.",
        "mark": "{left} left. Score: you {mine} - {theirs} {foe}.",
        "won": "PZ Duel: the duel is over, you WON against {foe}. {why} Score: you {mine} - "
        "{theirs} {foe}.",
        "lost": "PZ Duel: the duel is over, {foe} won. {why} Score: you {mine} - {theirs} {foe}.",
        "draw": "PZ Duel: the duel is over, it is a draw. {why} Score: you {mine} - {theirs} {foe}.",
        "void": "PZ Duel: the duel is over, this match does not count.",
        "closing": " The server is about to close.",
        "why_death_won": "{foe} died.",
        "why_death_lost": "You died.",
        "why_forfeit_won": "{foe} was away for too long.",
        "why_forfeit_lost": "You were away for too long.",
        "why_time": "Time is up.",
        "minutes": "{n} min",
        "seconds": "{n} s",
        "plural": "s",
        "foe": "your opponent",
    },
}


class AnnounceError(ValueError):
    """Réglage `announce` invalide : la clé fautive est nommée."""


def settings(runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Rend les réglages d'annonce validés ; une clé absente prend sa valeur par défaut."""
    given = runtime.get("announce", {})
    if not isinstance(given, Mapping):
        raise AnnounceError("announce : un objet est attendu")
    unknown = sorted(set(given) - set(DEFAULTS))
    if unknown:
        raise AnnounceError(f"announce.{unknown[0]} : clé inconnue")
    merged = {**DEFAULTS, **given}
    if not isinstance(merged["enabled"], bool):
        raise AnnounceError("announce.enabled : vrai ou faux attendu")
    if merged["lang"] not in LANGS:
        raise AnnounceError(f"announce.lang : {', '.join(LANGS)} attendu")
    for key in ("wave_gap_s", "waiting_repeat_s", "end_notice_s", "timeout_s", "queue_size"):
        value = merged[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise AnnounceError(f"announce.{key} : nombre positif ou nul attendu")
    if merged["queue_size"] < 1 or merged["timeout_s"] <= 0:
        raise AnnounceError("announce.queue_size et announce.timeout_s : au moins 1 et plus de 0")
    return merged


def plain(text: object) -> str:
    """Rend `text` en ASCII imprimable, sans guillemet : c'est l'argument d'une commande.

    Un pseudo peut porter des accents, un guillemet ou un saut de ligne ; aucun ne doit couper
    ou détourner `servermsg "…"`.
    """
    folded = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    kept = "".join(char if char.isprintable() else " " for char in folded)
    kept = kept.replace('"', "'").replace("\\", "/")
    return " ".join(kept.split())[:MAX_TEXT]


class Phrases:
    """Les textes d'une langue. Tout ce qui vient d'un joueur passe par `plain`."""

    def __init__(self, lang: str) -> None:
        self._texts = TEXTS[lang]

    def say(self, key: str, **values: object) -> str:
        return self._texts[key].format(**values)

    def foe(self, name: str | None) -> str:
        cleaned = plain(name) if name else ""
        return cleaned or self._texts["foe"]

    def left(self, remaining_s: float) -> str:
        seconds = max(0, int(remaining_s))
        if seconds >= 2 * SECONDS_PER_MINUTE:
            return self.say("minutes", n=seconds // SECONDS_PER_MINUTE)
        if seconds >= SECONDS_PER_MINUTE:
            return self.say("minutes", n=1)
        return self.say("seconds", n=seconds)

    def plural(self, count: int) -> str:
        return self._texts["plural"] if count > 1 else ""


class Sender(Protocol):
    """Ce qu'un canal d'annonce doit savoir faire ; `say` ne bloque jamais."""

    def say(self, side: str, text: str) -> None: ...

    def flush(self, timeout_s: float) -> bool: ...

    def close(self) -> None: ...


def kill_reward(table: Mapping[str, Any]) -> int | None:
    """Zombies envoyés par kill selon la table jouée, `None` si aucune règle simple ne le dit."""
    total = 0
    for rule in table.get("rules", ()):
        if rule.get("when") != KILL_ATOM or rule.get("every", 1) != 1:
            continue
        for effect in rule.get("effects", ()):
            if effect.get("kind") == "spawn_pop" and effect.get("target", "opponent") != "self":
                total += int(effect.get("n", 0))
    return total or None


def _opponent(side: str) -> str:
    return SIDES[1] if side == SIDES[0] else SIDES[0]


class MatchAnnouncer:
    """Décide des annonces d'un match. Aucun fil, aucune horloge : `now_ms` vient de l'appelant.

    Les vagues se cumulent par camp et ne s'annoncent qu'une fois par `wave_gap_s` : à dix
    zombies par kill, une annonce par commande noierait le joueur.
    """

    def __init__(self, sender: Sender, options: Mapping[str, Any]) -> None:
        self._sender = sender
        self._phrases = Phrases(options["lang"])
        self._wave_gap_ms = options["wave_gap_s"] * MS_PER_S
        self._waiting_repeat_ms = options["waiting_repeat_s"] * MS_PER_S
        self.end_notice_s: float = options["end_notice_s"]
        self._waiting_said_ms: dict[str, int] = {}
        self._pending: dict[str, int] = dict.fromkeys(SIDES, 0)
        self._wave_said_ms: dict[str, int] = {}
        # Posés par `started` : seuls les repères plus courts que le match s'annoncent.
        self._marks_left: list[int] = []
        self._ended = False

    def waiting(self, now_ms: int, players: Mapping[str, str | None], held: Sequence[str]) -> None:
        """Au lobby : dit au joueur déjà là qu'il attend, et pourquoi il est figé."""
        for side in SIDES:
            if players.get(side) is None or players.get(_opponent(side)) is not None:
                self._waiting_said_ms.pop(side, None)
                continue
            said = self._waiting_said_ms.get(side)
            if said is not None and (
                not self._waiting_repeat_ms or now_ms - said < self._waiting_repeat_ms
            ):
                continue
            self._waiting_said_ms[side] = now_ms
            key = "waiting_frozen" if side in held else "waiting"
            self._sender.say(side, self._phrases.say(key))

    def started(
        self, players: Mapping[str, str | None], duration_s: float, table: Mapping[str, Any]
    ) -> None:
        reward = kill_reward(table)
        self._marks_left = [
            mark for mark in TIME_MARKS_MIN if mark * SECONDS_PER_MINUTE < duration_s
        ]
        for side in SIDES:
            foe = self._phrases.foe(players.get(_opponent(side)))
            minutes = max(1, round(duration_s / SECONDS_PER_MINUTE))
            text = self._phrases.say("start", foe=foe, minutes=minutes)
            if reward:
                text += self._phrases.say("start_rule", n=reward, foe=foe)
            self._sender.say(side, text)

    def tick(
        self,
        now_ms: int,
        records: Sequence[Mapping[str, Any]],
        players: Mapping[str, str | None],
        scores: Mapping[str, int],
        remaining_s: float,
    ) -> None:
        """Une passe du match : cumule les vagues, puis dit ce qui est dû."""
        if self._ended:
            return
        for record in records:
            if record.get("type") != COMMAND or record.get("side") not in SIDES:
                continue
            command = record["data"][COMMAND]
            if command.get("kind") in WAVE_KINDS:
                self._pending[record["side"]] += int(command.get("n", 1))
            elif command.get("kind") == SUPPLY_KIND:
                self._sender.say(record["side"], self._phrases.say("supply"))
        for side in SIDES:
            self._say_wave(side, now_ms, players, scores, remaining_s)
        self._say_mark(players, scores, remaining_s)

    def _score(self, side: str, players: Mapping[str, str | None], scores: Mapping[str, int]) -> dict:
        other = _opponent(side)
        return {
            "foe": self._phrases.foe(players.get(other)),
            "mine": scores.get(side, 0),
            "theirs": scores.get(other, 0),
        }

    def _say_wave(
        self,
        side: str,
        now_ms: int,
        players: Mapping[str, str | None],
        scores: Mapping[str, int],
        remaining_s: float,
    ) -> None:
        count = self._pending[side]
        if not count:
            return
        said = self._wave_said_ms.get(side)
        if said is not None and now_ms - said < self._wave_gap_ms:
            return
        self._pending[side] = 0
        self._wave_said_ms[side] = now_ms
        text = self._phrases.say(
            "wave",
            n=count,
            s=self._phrases.plural(count),
            left=self._phrases.left(remaining_s),
            **self._score(side, players, scores),
        )
        self._sender.say(side, text)

    def _say_mark(
        self, players: Mapping[str, str | None], scores: Mapping[str, int], remaining_s: float
    ) -> None:
        due = [mark for mark in self._marks_left if remaining_s <= mark * SECONDS_PER_MINUTE]
        if not due:
            return
        self._marks_left = [mark for mark in self._marks_left if mark not in due]
        # Plusieurs repères passés d'un coup (reprise, grand saut) : seul le plus proche se dit.
        left = self._phrases.left(min(due) * SECONDS_PER_MINUTE)
        for side in SIDES:
            self._sender.say(
                side, self._phrases.say("mark", left=left, **self._score(side, players, scores))
            )

    def ended(
        self,
        result: Mapping[str, Any],
        players: Mapping[str, str | None],
        scores: Mapping[str, int],
    ) -> None:
        """La fin, dite à chacun de son point de vue, une seule fois."""
        if self._ended:
            return
        self._ended = True
        outcome, winner = result.get("outcome"), result.get("winner")
        for side in SIDES:
            values = self._score(side, players, scores)
            if winner in SIDES:
                won = winner == side
                key = "won" if won else "lost"
                why = self._why(outcome, won, values["foe"])
            elif outcome == "draw":
                key, why = "draw", self._phrases.say("why_time")
            else:
                key, why = "void", ""
            text = self._phrases.say(key, why=why, **values) + self._phrases.say("closing")
            self._sender.say(side, " ".join(text.split()))

    def _why(self, outcome: object, won: bool, foe: str) -> str:
        suffix = "won" if won else "lost"
        if outcome in ("death", "forfeit"):
            return self._phrases.say(f"why_{outcome}_{suffix}", foe=foe)
        return self._phrases.say("why_time")

    def flush(self, timeout_s: float) -> bool:
        return self._sender.flush(timeout_s)

    def close(self) -> None:
        self._sender.close()


class RconSender:
    """Envoie les annonces par `servermsg`, sur son propre fil : `say` ne bloque jamais.

    Chaque annonce ouvre sa connexion : un serveur relancé en cours de match est retrouvé sans
    état à réparer. File bornée : si le serveur ne répond plus, les plus vieilles annonces
    tombent. `connect` est remplaçable pour les tests.
    """

    def __init__(
        self,
        ports: Mapping[str, int],
        password: str,
        timeout_s: float,
        queue_size: int,
        connect: Callable[[int, str, float], Any] = RconClient,
    ) -> None:
        self._ports = dict(ports)
        self._password = password
        self._timeout_s = timeout_s
        self._connect = connect
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=queue_size)
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()
        self._closed = False
        self._warned: set[str] = set()
        # Au moins une annonce est arrivée : un joueur lit, la fin mérite son préavis.
        self.delivered = False
        self._thread = threading.Thread(target=self._run, name="duel-announce", daemon=True)
        self._thread.start()

    def say(self, side: str, text: str) -> None:
        cleaned = plain(text)
        if not cleaned or side not in self._ports:
            return
        with self._lock:
            if self._closed:
                return
            self._idle.clear()
            while True:
                try:
                    self._queue.put_nowait((side, cleaned))
                    return
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass

    def flush(self, timeout_s: float) -> bool:
        """Attend au plus `timeout_s` que la file soit vide ; rend vrai si une annonce est passée."""
        self._idle.wait(timeout_s)
        return self.delivered

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queue.put_nowait(None)
        self._thread.join(self._timeout_s * 2)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._idle.set()
                return
            side, text = item
            try:
                self._send(side, text)
            except RconError as exc:
                if side not in self._warned:
                    self._warned.add(side)
                    LOGGER.warning("annonce en jeu impossible sur le serveur %s : %s", side, exc)
            except Exception:  # un fil d'annonce ne doit jamais emporter le service
                LOGGER.exception("annonce en jeu : panne inattendue")
            with self._lock:
                if self._queue.empty():
                    self._idle.set()

    def _send(self, side: str, text: str) -> None:
        with self._connect(self._ports[side], self._password, self._timeout_s) as client:
            client.command(f'{SERVERMSG} "{text}"')
        self.delivered = True
        self._warned.discard(side)


def rcon_announcer(
    runtime: Mapping[str, Any], password: str
) -> MatchAnnouncer | None:
    """Fabrique des annonces d'un match réel ; `None` si `announce.enabled` est faux."""
    options = settings(runtime)
    if not options["enabled"]:
        return None
    ports = {side: runtime["ports"][side]["rcon"] for side in SIDES}
    sender = RconSender(ports, password, options["timeout_s"], int(options["queue_size"]))
    return MatchAnnouncer(sender, options)
