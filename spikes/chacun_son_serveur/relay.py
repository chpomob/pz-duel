"""Spike « chacun son serveur » : le transport par fichiers du contrat v1, porté par un lien TCP.

Aujourd'hui l'arbitre et les deux mods partagent un disque : `duel_X_out/in/ack/session.txt` dans
`~/Zomboid/Lua`. Ici le camp distant garde ses quatre fichiers chez lui, et deux bouts de relais
les tiennent en miroir avec ceux que l'arbitre de l'hôte lit et écrit :

    hôte                                   invité
    arbitre ── Lua/duel_B_*  ⇄ HostEnd ══ TCP ══ GuestEnd ⇄ Lua/duel_B_* ── mod B

Le relais rejoue des *opérations*, pas des contenus : ajout, troncature, remplacement atomique.
Le mod voit donc chez lui les mêmes écritures que celles de l'arbitre local, déjà prouvées.

Sens hôte → invité : `in` (ajouts et troncatures), `session` (remplacement), remise à zéro de
`out` et `ack` avant un lancement. Sens invité → hôte : octets ajoutés à `out`, contenu de `ack`.

Le lien porte aussi une mesure d'écart d'horloge (`ClockSync`), à la Cristian : le mod date ses
événements à l'heure murale de SA machine, et l'arbitre les juge à la sienne.

Code de spike : pas de reprise sur coupure, pas d'authentification, pas de chiffrement.
"""

from __future__ import annotations

import heapq
import json
import os
import random
import socket
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

FILES = ("out", "in", "ack", "session")
POLL_S = 0.02
LENGTH = struct.Struct(">I")
MAX_FRAME = 4 << 20


def _path(lua_dir: Path, side: str, name: str) -> Path:
    return Path(lua_dir) / f"duel_{side}_{name}.txt"


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def _append(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _truncate(path: Path) -> None:
    with open(path, "wb"):
        pass


def _replace(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".relay")
    temporary.write_bytes(data)
    os.replace(temporary, path)


class Link:
    """Messages JSON encadrés sur un socket. `delay_s`/`jitter_s` retardent l'ÉMISSION, dans
    l'ordre : c'est la latence d'un lien Internet, que TCP ne réordonne pas."""

    def __init__(
        self,
        sock: socket.socket,
        delay_s: float = 0.0,
        jitter_s: float = 0.0,
        seed: int = 0,
    ) -> None:
        self._sock = sock
        self._delay_s = delay_s
        self._jitter_s = jitter_s
        self._random = random.Random(seed)
        self._queue: list[tuple[float, int, bytes]] = []
        self._count = 0
        self._last_release = 0.0
        self._wake = threading.Condition()
        self._closed = False
        self.sent_bytes = 0
        self.sent_messages = 0
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()

    def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        frame = LENGTH.pack(len(payload)) + payload
        release = time.monotonic() + self._delay_s + self._random.uniform(0, self._jitter_s)
        with self._wake:
            release = max(release, self._last_release)
            self._last_release = release
            self._count += 1
            heapq.heappush(self._queue, (release, self._count, frame))
            self._wake.notify()

    def _send_loop(self) -> None:
        while True:
            with self._wake:
                while not self._closed and (
                    not self._queue or self._queue[0][0] > time.monotonic()
                ):
                    timeout = self._queue[0][0] - time.monotonic() if self._queue else None
                    self._wake.wait(timeout)
                if self._closed:
                    return
                _, _, frame = heapq.heappop(self._queue)
            try:
                self._sock.sendall(frame)
            except OSError:
                return
            self.sent_bytes += len(frame)
            self.sent_messages += 1

    def receive(self) -> dict[str, Any] | None:
        """Bloque jusqu'au message suivant ; `None` quand le lien est fermé."""
        header = self._read_exact(LENGTH.size)
        if header is None:
            return None
        (size,) = LENGTH.unpack(header)
        if size > MAX_FRAME:
            return None
        payload = self._read_exact(size)
        return None if payload is None else json.loads(payload)

    def _read_exact(self, count: int) -> bytes | None:
        chunks = bytearray()
        while len(chunks) < count:
            try:
                chunk = self._sock.recv(count - len(chunks))
            except OSError:
                return None
            if not chunk:
                return None
            chunks += chunk
        return bytes(chunks)

    def close(self) -> None:
        with self._wake:
            self._closed = True
            self._wake.notify()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


def _text(data: bytes) -> str:
    return data.decode("latin-1")


def _bytes(text: str) -> bytes:
    return text.encode("latin-1")


class _End:
    def __init__(self, lua_dir: str | os.PathLike[str], side: str, link: Link) -> None:
        self.lua_dir = Path(lua_dir)
        self.side = side
        self.link = link
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._watch_loop, daemon=True),
            threading.Thread(target=self._receive_loop, daemon=True),
        ]

    def path(self, name: str) -> Path:
        return _path(self.lua_dir, self.side, name)

    def start(self) -> "_End":
        self.lua_dir.mkdir(parents=True, exist_ok=True)
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.link.close()
        for thread in self._threads:
            thread.join(2)

    def _watch_loop(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                self._watch()
            except OSError:
                continue

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            link = self.link
            message = link.receive()
            if message is None:
                # Lien coupé : on attend qu'un nouveau soit posé par `heal`, ou l'arrêt.
                while not self._stop.is_set() and self.link is link:
                    time.sleep(POLL_S)
                continue
            try:
                self._take(message)
            except OSError:
                continue

    def heal(self, link: Link) -> None:
        """Pose un nouveau lien après une coupure ; `_resume` remet les deux bouts d'accord."""
        self.link = link
        self._resume()

    def _resume(self) -> None:
        raise NotImplementedError

    def _watch(self) -> None:
        raise NotImplementedError

    def _take(self, message: dict[str, Any]) -> None:
        raise NotImplementedError


class HostEnd(_End):
    """Côté arbitre. Suit `in` et `session`, reçoit `out` et `ack`, mesure l'écart d'horloge.

    `rewrite_t_ms` : si vrai, chaque ligne complète de `out` reçue voit son `t_ms` ramené à
    l'heure de l'hôte (`t_ms - offset_ms`) avant d'être posée. Les octets d'une ligne sans `\\n`
    attendent alors leur fin ici.
    """

    def __init__(
        self,
        lua_dir: str | os.PathLike[str],
        side: str,
        link: Link,
        *,
        clock_ms: Callable[[], float] = lambda: time.time() * 1000,
        rewrite_t_ms: bool = False,
        ping_s: float = 0.5,
    ) -> None:
        super().__init__(lua_dir, side, link)
        self._in_sent = b""
        self._in_stamp: tuple[int, int] | None = None
        self._session_stamp: tuple[int, int] | None = None
        self._out_written = 0
        self._out_lock = threading.Lock()
        self._partial = b""
        self.clock = ClockSync(clock_ms)
        self._rewrite = rewrite_t_ms
        self._ping_s = ping_s
        self._next_ping = 0.0
        self._ping_id = 0
        self.resets = 0
        # Époque de `out` : elle avance à chaque remise à zéro, et ce qui arrive d'une époque
        # passée est jeté. Sans elle, des octets encore en vol traversent la remise à zéro.
        self._epoch = 0
        # Octets de `out` reçus de l'invité dans l'époque courante, réécriture comprise ou non :
        # c'est le repère de reprise après une coupure.
        self._out_received = 0
        self._reset_done = threading.Condition()
        self._reset_acked = 0
        self.stale_dropped = 0

    def sync_reset(self, timeout_s: float = 5.0) -> bool:
        """Remise à zéro de `out` et `ack` chez l'invité, ACQUITTÉE avant de rendre la main.

        Sur un disque partagé, vider `out` est instantané. Sur un lien, c'est un ordre : ce que
        l'invité avait déjà émis peut arriver après, et doit être reconnu comme périmé.
        """
        with self._out_lock:
            self._epoch += 1
            epoch = self._epoch
            self._out_received = 0
            self._partial = b""
            self.resets += 1
            # L'arbitre vient aussi de vider `in` : l'invité repart du même état.
            self._in_sent = b""
            self.link.send({"t": "reset", "e": epoch})
        with self._reset_done:
            acked = self._reset_done.wait_for(lambda: self._reset_acked >= epoch, timeout_s)
        with self._out_lock:
            for name in ("out", "ack"):
                _truncate(self.path(name))
            self._out_written = 0
        return acked

    def _resume(self) -> None:
        """Dit à l'invité où l'hôte en est : époque et octets de `out` déjà posés. `in` et
        `session` repartent d'un contenu entier : le mod ignore un `seq` déjà pris."""
        with self._out_lock:
            self.link.send({"t": "hello", "e": self._epoch, "out": self._out_received})
        content = _read(self.path("in")) or b""
        self.link.send({"t": "in=", "d": _text(content)})
        self._in_sent = content
        self._session_stamp = None

    def _watch(self) -> None:
        self._watch_in()
        self._watch_session()
        now = time.monotonic()
        if now >= self._next_ping:
            self._next_ping = now + self._ping_s
            self._ping_id += 1
            self.link.send({"t": "ping", "id": self._ping_id, "t0": self.clock.now_ms()})

    def _watch_in(self) -> None:
        stamp = _stamp(self.path("in"))
        if stamp == self._in_stamp:
            return
        content = _read(self.path("in"))
        self._in_stamp = stamp
        if content is None or content == self._in_sent:
            return
        if content.startswith(self._in_sent):
            self.link.send({"t": "in+", "d": _text(content[len(self._in_sent) :])})
        else:
            # Troncature (ack couvrant tout, relance), puis ce qui a été réécrit depuis.
            self.link.send({"t": "in=", "d": _text(content)})
        self._in_sent = content

    def _watch_session(self) -> None:
        stamp = _stamp(self.path("session"))
        if stamp == self._session_stamp:
            return
        content = _read(self.path("session"))
        self._session_stamp = stamp
        if content:
            self.link.send({"t": "session", "d": _text(content)})

    def _take(self, message: dict[str, Any]) -> None:
        kind = message["t"]
        if kind in ("out+", "ack") and message.get("e", 0) != self._epoch:
            self.stale_dropped += 1
            return
        if kind == "out+":
            self._take_out(_bytes(message["d"]), message.get("e", 0))
        elif kind == "ack":
            _replace(self.path("ack"), _bytes(message["d"]))
        elif kind == "reset_done":
            with self._reset_done:
                self._reset_acked = max(self._reset_acked, message["e"])
                self._reset_done.notify_all()
        elif kind == "pong":
            self.clock.sample(message["t0"], message["tg"])

    def _take_out(self, data: bytes, epoch: int) -> None:
        with self._out_lock:
            if epoch != self._epoch:
                self.stale_dropped += 1
                return
            self._out_received += len(data)
            if self._rewrite:
                data = self._rewritten(data)
                if not data:
                    return
            _append(self.path("out"), data)
            self._out_written += len(data)

    def _rewritten(self, data: bytes) -> bytes:
        buffered = self._partial + data
        end = buffered.rfind(b"\n")
        if end < 0:
            self._partial = buffered
            return b""
        self._partial = buffered[end + 1 :]
        offset = self.clock.offset_ms
        lines = []
        for line in buffered[: end + 1].split(b"\n")[:-1]:
            lines.append(_shift_t_ms(line, offset))
        return b"\n".join(lines) + b"\n"


def _shift_t_ms(line: bytes, offset_ms: float | None) -> bytes:
    """Ramène `t_ms` à l'heure de l'hôte. Une ligne illisible passe telle quelle : l'arbitre
    la jugera lui-même (`bad_line`)."""
    if offset_ms is None:
        return line
    try:
        record = json.loads(line)
        record["t_ms"] = int(round(record["t_ms"] - offset_ms))
    except (ValueError, KeyError, TypeError):
        return line
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class GuestEnd(_End):
    """Côté mod distant. Suit `out` et `ack`, applique `in`, `session` et les remises à zéro."""

    def __init__(
        self,
        lua_dir: str | os.PathLike[str],
        side: str,
        link: Link,
        *,
        clock_ms: Callable[[], float] = lambda: time.time() * 1000,
    ) -> None:
        super().__init__(lua_dir, side, link)
        self._out_offset = 0
        self._ack_stamp: tuple[int, int] | None = None
        self._ack_sent: bytes | None = None
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._epoch = 0

    def _watch(self) -> None:
        with self._lock:
            self._watch_out()
            self._watch_ack()

    def _watch_out(self) -> None:
        try:
            with open(self.path("out"), "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size < self._out_offset:
                    self._out_offset = 0
                if size == self._out_offset:
                    return
                handle.seek(self._out_offset)
                data = handle.read(size - self._out_offset)
        except FileNotFoundError:
            self._out_offset = 0
            return
        self._out_offset += len(data)
        self.link.send({"t": "out+", "e": self._epoch, "d": _text(data)})

    def _watch_ack(self) -> None:
        stamp = _stamp(self.path("ack"))
        if stamp == self._ack_stamp:
            return
        content = _read(self.path("ack"))
        self._ack_stamp = stamp
        # Le mod réécrit `ack` sans atomicité : vide ou coupé, la passe suivante le reverra.
        if not content or not content.endswith(b"\n") or content == self._ack_sent:
            return
        self._ack_sent = content
        self.link.send({"t": "ack", "e": self._epoch, "d": _text(content)})

    def _resume(self) -> None:
        pass  # l'invité attend le `hello` de l'hôte

    def _take(self, message: dict[str, Any]) -> None:
        kind = message["t"]
        if kind == "hello":
            with self._lock:
                if message["e"] == self._epoch:
                    # Reprise exacte : on renvoie `out` depuis ce que l'hôte a déjà.
                    self._out_offset = message["out"]
                self._ack_sent = None
                self._ack_stamp = None
            return
        if kind == "in+":
            _append(self.path("in"), _bytes(message["d"]))
        elif kind == "in=":
            _truncate(self.path("in"))
            if message["d"]:
                _append(self.path("in"), _bytes(message["d"]))
        elif kind == "session":
            _replace(self.path("session"), _bytes(message["d"]))
        elif kind == "reset":
            with self._lock:
                for name in ("out", "ack", "in"):
                    _truncate(self.path(name))
                self._out_offset = 0
                self._ack_sent = None
                self._ack_stamp = None
                self._epoch = message["e"]
            self.link.send({"t": "reset_done", "e": message["e"]})
        elif kind == "ping":
            self.link.send({"t": "pong", "id": message["id"], "t0": message["t0"], "tg": self._clock_ms()})


class ClockSync:
    """Écart d'horloge invité − hôte, estimé par aller-retour (Cristian).

    Un échantillon suppose l'aller et le retour égaux : son erreur est bornée par RTT/2. On
    garde donc celui du plus petit RTT d'une fenêtre glissante. `bound_ms` est cette borne.
    """

    def __init__(self, clock_ms: Callable[[], float], window: int = 40) -> None:
        self._clock_ms = clock_ms
        self._window = window
        self._samples: list[tuple[float, float]] = []
        self.count = 0

    def now_ms(self) -> float:
        return self._clock_ms()

    def sample(self, sent_ms: float, guest_ms: float) -> None:
        received_ms = self._clock_ms()
        rtt = received_ms - sent_ms
        if rtt < 0:
            return
        offset = guest_ms - (sent_ms + received_ms) / 2
        self._samples.append((rtt, offset))
        del self._samples[: -self._window]
        self.count += 1

    @property
    def best(self) -> tuple[float, float] | None:
        return min(self._samples) if self._samples else None

    @property
    def offset_ms(self) -> float | None:
        best = self.best
        return None if best is None else best[1]

    @property
    def bound_ms(self) -> float | None:
        best = self.best
        return None if best is None else best[0] / 2


def connect_pair(
    delay_s: float = 0.0, jitter_s: float = 0.0, seed: int = 0
) -> tuple[Link, Link]:
    """Deux `Link` reliés par un vrai socket TCP local, chacun retardant ses émissions."""
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        client = socket.create_connection(listener.getsockname())
        server, _ = listener.accept()
    finally:
        listener.close()
    for sock in (client, server):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return Link(server, delay_s, jitter_s, seed), Link(client, delay_s, jitter_s, seed + 1)


class Bridge:
    """Un relais complet pour un camp, entre deux dossiers `Lua`, sur un lien local retardé."""

    def __init__(
        self,
        host_lua: str | os.PathLike[str],
        guest_lua: str | os.PathLike[str],
        side: str = "B",
        *,
        delay_s: float = 0.0,
        jitter_s: float = 0.0,
        links: tuple[Link, Link] | None = None,
        **host_options: Any,
    ) -> None:
        guest_clock = host_options.pop("guest_clock_ms", None)
        host_link, guest_link = links or connect_pair(delay_s, jitter_s)
        self.host = HostEnd(host_lua, side, host_link, **host_options)
        guest_options = {} if guest_clock is None else {"clock_ms": guest_clock}
        self.guest = GuestEnd(guest_lua, side, guest_link, **guest_options)

    def start(self) -> "Bridge":
        self.guest.start()
        self.host.start()
        return self

    def cut(self) -> None:
        """Coupe le lien, comme une box qui redémarre : rien ne passe, les fichiers continuent."""
        self.host.link.close()
        self.guest.link.close()

    def heal(self, delay_s: float = 0.0, jitter_s: float = 0.0) -> None:
        host_link, guest_link = connect_pair(delay_s, jitter_s)
        self.guest.heal(guest_link)
        self.host.heal(host_link)

    def stop(self) -> None:
        self.host.stop()
        self.guest.stop()
