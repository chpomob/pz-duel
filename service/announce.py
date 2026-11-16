"""Annonces en jeu : ce que l'arbitre sait, dit au joueur dans sa partie.

Le joueur ne regarde pas la page pendant un duel, et l'invité ne la voit jamais. Ce module
lui dit l'essentiel là où il regarde : l'attente de l'adversaire, le départ, les zombies qu'on
lui envoie, le score, le temps restant et la fin.

Deux canaux (P9F22). Le premier est le texte flottant au-dessus du personnage : une commande
`announce` part dans la file de commandes du match, le mod la relaie au client du joueur, qui
l'affiche. Le second, en secours, est la commande `servermsg` du serveur, par RCON : elle
n'écrit que dans le chat, que personne ne lit pendant un duel. Il ne sert que si le mod
n'annonce pas la capacité `hud`, ou si le client du joueur répond `no_hud_api`. Chaque serveur
n'a qu'un joueur (`MaxPlayers=1`) : dans les deux cas l'annonce est privée.

Quatre pièces, du plus pur au plus sale :

- `Phrases` écrit les textes, courts et en ASCII : rien ne garantit l'encodage d'un accent
  jusqu'au jeu, et un texte flottant se lit en une seconde et demie ;
- `MatchAnnouncer` décide quoi dire et quand, sans fil ni réseau : l'heure lui est donnée ;
- `HudSender` choisit le canal de chaque camp et écrit les commandes `announce` ;
- `RconSender` envoie par `servermsg`, sur son propre fil : le match ne l'attend jamais.

Une annonce perdue n'est jamais une panne : rien ici ne touche au journal, au cœur ni au rejeu.
Ce module ne journalise aucun secret.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple, Protocol

from .protocol import PROTOCOL_VERSION
from .rcon import RconClient, RconError

LOGGER = logging.getLogger(__name__)

SIDES = ("A", "B")
MS_PER_S = 1000
SECONDS_PER_MINUTE = 60
COMMAND = "command"
SERVERMSG = "servermsg"
# `kind` de la commande d'annonce, et marque de son `cmd_result` : ses `seq` ont leur propre
# compteur, hors de celui des commandes du match, que le cœur et le rejeu gardent pour eux.
ANNOUNCE_KIND = "announce"
# Capacité du mod (`caps.functions.hud`) : il sait relayer une annonce au client du joueur.
HUD = "hud"
CHAT = "chat"
APPLIED = "applied"
# Le client du joueur n'a pas `HaloTextHelper` : ce camp repasse à `servermsg`, pour de bon.
NO_HUD_API = "no_hud_api"
# Le client n'a rien répondu : une fois ne dit rien, deux de suite valent une capacité absente.
NO_REPLY = "no_reply"
NO_REPLY_LIMIT = 2
NO_CAPS = "no_hud_caps"
# Un texte flottant se lit d'un coup d'oeil : 80 caractères, pas un de plus (P9F22).
MAX_TEXT = 80
# Un pseudo long ne doit pas pousser le reste de la phrase hors de l'écran.
MAX_NAME = 16
# Nature d'une annonce : le client en tire la couleur du texte flottant.
TONE_WAIT = "wait"
TONE_START = "start"
TONE_WAVE = "wave"
TONE_SUPPLY = "supply"
TONE_MARK = "mark"
TONE_END = "end"
TONES = (TONE_WAIT, TONE_START, TONE_WAVE, TONE_SUPPLY, TONE_MARK, TONE_END)
# Une annonce écrite sans réponse est réécrite une fois après ce délai (`in` a pu être vidé
# entre la lecture du mod et son acquittement), puis oubliée.
RESEND_S = 4.0
EXPIRE_S = 12.0
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

# Chaque phrase tient en `MAX_TEXT` caractères avec un pseudo de `MAX_NAME` et des nombres à
# quatre chiffres : un test le vérifie. Le détail (raison complète, feuille de score) reste
# sur la page et dans le journal du match, pas à l'écran.
TEXTS: Mapping[str, Mapping[str, str]] = {
    "fr": {
        "waiting_frozen": "PZ Duel : attente de ton adversaire. Monde fige d'ici la, c'est normal.",
        "waiting": "PZ Duel : attente de ton adversaire. Depart des son arrivee.",
        "start": "Duel contre {foe} : {minutes} min.",
        "start_rule": " 1 kill = {n} zombie{s} chez lui.",
        "wave": "{foe} t'envoie {n} zombie{s} ! Toi {mine} - {theirs}. Reste {left}.",
        "supply": "Une caisse de soin vient de tomber pres de toi.",
        "mark": "Plus que {left}. Toi {mine} - {theirs} {foe}.",
        "won": "Fin : GAGNE ! {why} Toi {mine} - {theirs}.",
        "lost": "Fin : {foe} a gagne. {why} Toi {mine} - {theirs}.",
        "draw": "Fin : egalite. {why} Toi {mine} - {theirs}.",
        "void": "Fin : ce match ne compte pas.",
        "closing": " Le serveur ferme.",
        "why_death_won": "{foe} est mort.",
        "why_death_lost": "Tu es mort.",
        "why_forfeit_won": "{foe} est parti.",
        "why_forfeit_lost": "Tu es parti.",
        "why_time": "Temps ecoule.",
        "minutes": "{n} min",
        "seconds": "{n} s",
        "plural": "s",
        "foe": "ton adversaire",
    },
    "en": {
        "waiting_frozen": "PZ Duel: waiting for your opponent. World frozen until then, as expected.",
        "waiting": "PZ Duel: waiting for your opponent. The duel starts when they join.",
        "start": "Duel against {foe}: {minutes} min.",
        "start_rule": " 1 kill = {n} zombie{s} on them.",
        "wave": "{foe} sends you {n} zombie{s}! You {mine} - {theirs}. {left} left.",
        "supply": "A medical crate just dropped near you.",
        "mark": "{left} left. You {mine} - {theirs} {foe}.",
        "won": "Over: you WON! {why} You {mine} - {theirs}.",
        "lost": "Over: {foe} won. {why} You {mine} - {theirs}.",
        "draw": "Over: it is a draw. {why} You {mine} - {theirs}.",
        "void": "Over: this match does not count.",
        "closing": " Server closing.",
        "why_death_won": "{foe} died.",
        "why_death_lost": "You died.",
        "why_forfeit_won": "{foe} left.",
        "why_forfeit_lost": "You left.",
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
    """Rend `text` en ASCII imprimable, sans guillemet, en `MAX_TEXT` caractères au plus.

    C'est l'argument d'une commande et un texte flottant. Un pseudo peut porter des accents, un
    guillemet ou un saut de ligne ; aucun ne doit couper ou détourner `servermsg "…"`, ni la
    ligne de `in`. Ce qui dépasse est coupé : une annonce longue ne s'affiche pas en entier.
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
        cleaned = plain(name)[:MAX_NAME].strip() if name else ""
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
    """Ce qu'un canal d'annonce doit savoir faire ; `say` ne bloque jamais.

    `tone` dit la nature de l'annonce (`TONES`) : un canal qui ne colore rien l'ignore.
    """

    def say(self, side: str, text: str, tone: str = TONE_WAIT) -> None: ...

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
            self._sender.say(side, self._phrases.say(key), TONE_WAIT)

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
                text += self._phrases.say("start_rule", n=reward, s=self._phrases.plural(reward))
            self._sender.say(side, text, TONE_START)

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
                self._sender.say(record["side"], self._phrases.say("supply"), TONE_SUPPLY)
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
        self._sender.say(side, text, TONE_WAVE)

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
            text = self._phrases.say("mark", left=left, **self._score(side, players, scores))
            self._sender.say(side, text, TONE_MARK)

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
            self._sender.say(side, " ".join(text.split()), TONE_END)

    def _why(self, outcome: object, won: bool, foe: str) -> str:
        suffix = "won" if won else "lost"
        if outcome in ("death", "forfeit"):
            return self._phrases.say(f"why_{outcome}_{suffix}", foe=foe)
        return self._phrases.say("why_time")

    def flush(self, timeout_s: float) -> bool:
        return self._sender.flush(timeout_s)

    def close(self) -> None:
        self._sender.close()

    # Ce qui suit ne concerne qu'un canal qui passe par le mod (`HudSender`) : un autre canal
    # ne l'a pas, et rien ne se passe.

    def _relay(self, name: str, *args: Any) -> Any:
        method = getattr(self._sender, name, None)
        return None if method is None else method(*args)

    def attach(
        self,
        write: Callable[[str, Mapping[str, Any]], None],
        notify: Callable[[str, str], None],
    ) -> None:
        """Le match donne sa file de commandes (`write`) et sa voix au lobby (`notify`)."""
        self._relay("attach", write, notify)

    def on_caps(self, side: str, caps: Mapping[str, Any]) -> None:
        self._relay("on_caps", side, caps)

    def on_result(self, side: str, result: Mapping[str, Any]) -> None:
        self._relay("on_result", side, result)

    def poll(self) -> None:
        self._relay("poll")

    def pending(self) -> bool:
        """Vrai tant qu'une annonce en texte flottant attend encore la réponse du client."""
        return bool(self._relay("pending"))

    def channels(self) -> dict[str, dict[str, Any]]:
        """Par camp, le canal en service et pourquoi : ce que le lobby affiche."""
        return self._relay("channels") or {}


class _Sent(NamedTuple):
    text: str
    tone: str
    at: float
    resent: bool


class HudSender:
    """Envoie les annonces en texte flottant, par la commande `announce` ; `servermsg` en secours.

    Rien ici n'ouvre de connexion : `write`, donné par le match, ajoute une ligne à sa file de
    commandes, et le mod relaie au client du joueur. Tout tourne sur le fil du match.

    Un camp passe par le mod quand ses `caps` portent `functions.hud`. Sans elle, ou dès que le
    client répond `no_hud_api`, ses annonces partent par `fallback`, et `notify` le dit une fois
    au lobby : une annonce dans le chat vaut mieux que rien, à condition de ne pas la prétendre
    visible. Un refus `no_hud_api` tient jusqu'à la fin du match, même si le mod, relancé,
    republie `hud` : c'est le client qui ne sait pas afficher.

    Les `seq` d'annonce ont leur propre compteur. Une annonce sans réponse est réécrite une fois,
    sous un `seq` neuf : `in` a pu être vidé avant que le mod ne la lise. Perdue, elle le reste :
    le match, son journal et son rejeu n'en savent rien.
    """

    def __init__(
        self,
        fallback: Sender,
        clock: Callable[[], float] = time.monotonic,
        resend_s: float = RESEND_S,
        expire_s: float = EXPIRE_S,
    ) -> None:
        self._fallback = fallback
        self._clock = clock
        self._resend_s = resend_s
        self._expire_s = expire_s
        self._write: Callable[[str, Mapping[str, Any]], None] | None = None
        self._notify: Callable[[str, str], None] | None = None
        # Camp → canal. Absent : `caps` pas encore lues, le secours sert en attendant.
        self._channel: dict[str, str] = {}
        self._reason: dict[str, str] = {}
        # Camps dont le client a dit ne pas savoir afficher : plus aucun `caps` n'y change rien.
        self._refused: set[str] = set()
        self._silent: dict[str, int] = {}
        self._seq = 0
        self._pending: dict[tuple[str, int], _Sent] = {}
        # Au moins une annonce a été affichée : un joueur lit, la fin mérite son préavis.
        self.delivered = False

    def attach(
        self,
        write: Callable[[str, Mapping[str, Any]], None],
        notify: Callable[[str, str], None],
    ) -> None:
        self._write = write
        self._notify = notify

    def on_caps(self, side: str, caps: Mapping[str, Any]) -> None:
        """Lit la capacité `hud` du mod ; un `caps` relu (serveur relancé) est rejugé."""
        functions = caps.get("functions")
        able = isinstance(functions, Mapping) and functions.get(HUD) is True
        if side in self._refused:
            return
        if able:
            self._channel[side] = HUD
            self._reason.pop(side, None)
        else:
            self._fall(side, NO_CAPS)

    def _fall(self, side: str, reason: str) -> None:
        known = self._channel.get(side) == CHAT and self._reason.get(side) == reason
        self._channel[side] = CHAT
        self._reason[side] = reason
        if not known and self._notify is not None:
            self._notify(side, reason)

    def channels(self) -> dict[str, dict[str, Any]]:
        return {
            side: {"channel": channel, "reason": self._reason.get(side)}
            for side, channel in self._channel.items()
        }

    def say(self, side: str, text: str, tone: str = TONE_WAIT) -> None:
        cleaned = plain(text)
        if not cleaned:
            return
        if self._channel.get(side) != HUD or self._write is None:
            self._fallback.say(side, cleaned, tone)
            return
        if not self._send(side, cleaned, tone, resent=False):
            self._fallback.say(side, cleaned, tone)

    def _send(self, side: str, text: str, tone: str, resent: bool) -> bool:
        self._seq += 1
        command = {
            "v": PROTOCOL_VERSION,
            "seq": self._seq,
            "kind": ANNOUNCE_KIND,
            "side": side,
            "text": text,
            "tone": tone if tone in TONES else TONE_WAIT,
            "origin": {"rule": ANNOUNCE_KIND},
        }
        try:
            self._write(side, command)
        except (OSError, ValueError) as exc:
            LOGGER.warning("annonce en jeu non écrite pour le camp %s : %s", side, type(exc).__name__)
            return False
        self._pending[side, self._seq] = _Sent(text, tone, self._clock(), resent)
        return True

    def on_result(self, side: str, result: Mapping[str, Any]) -> None:
        """Lit le `cmd_result` d'une annonce : `no_hud_api` bascule le camp sur le secours.

        L'annonce refusée n'a pas été vue : elle repart par le secours, comme les suivantes.
        """
        sent = self._pending.pop((side, result.get("seq")), None)
        if sent is None:
            return
        status, reason = result.get("status"), result.get("reason")
        if status == APPLIED:
            self.delivered = True
            self._silent.pop(side, None)
            return
        if reason == NO_REPLY:
            self._silent[side] = self._silent.get(side, 0) + 1
            if self._silent[side] < NO_REPLY_LIMIT:
                return
        elif reason != NO_HUD_API:
            # `no_player` et les autres : personne pour lire, rien à en conclure.
            return
        self._refused.add(side)
        self._fall(side, reason)
        self._fallback.say(side, sent.text, sent.tone)

    def poll(self) -> None:
        """Réécrit une fois ce qui reste sans réponse, puis l'oublie : appelé à chaque passe."""
        now = self._clock()
        for key, sent in list(self._pending.items()):
            age = now - sent.at
            if age >= self._expire_s or (sent.resent and age >= self._resend_s):
                del self._pending[key]
            elif age >= self._resend_s:
                del self._pending[key]
                if self._channel.get(key[0]) == HUD:
                    self._send(key[0], sent.text, sent.tone, resent=True)

    def pending(self) -> bool:
        """Vrai tant qu'une annonce écrite attend sa réponse : `poll` peut encore la réécrire."""
        return bool(self._pending)

    def flush(self, timeout_s: float) -> bool:
        """Vrai si un joueur a pu lire une annonce, par l'un ou l'autre canal.

        N'attend que le secours : une annonce en texte flottant encore sans réponse se suit
        par `pending`, `on_result` et `poll`, que le match continue d'appeler pendant le préavis.
        """
        return self._fallback.flush(timeout_s) or self.delivered

    def close(self) -> None:
        self._pending.clear()
        self._fallback.close()


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

    def say(self, side: str, text: str, tone: str = TONE_WAIT) -> None:
        # `tone` : le chat n'a pas de couleur.
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
    """Fabrique des annonces d'un match réel ; `None` si `announce.enabled` est faux.

    Texte flottant par le mod, `servermsg` par RCON en secours.
    """
    options = settings(runtime)
    if not options["enabled"]:
        return None
    ports = {side: runtime["ports"][side]["rcon"] for side in SIDES}
    fallback = RconSender(ports, password, options["timeout_s"], int(options["queue_size"]))
    return MatchAnnouncer(HudSender(fallback), options)
