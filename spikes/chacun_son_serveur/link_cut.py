"""Spike S4 : coupure du lien en plein match, et horloge de l'invité fausse de 2,5 s.

Vrai `CommandChannel` et vrai `OutReader` côté arbitre, double du mod côté invité, relais TCP
entre les deux. Le lien est coupé pendant que les deux côtés continuent d'écrire, puis rétabli.

    python -m spikes.chacun_son_serveur.link_cut
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from doubles.fake_mod import FakeMod  # noqa: E402

from service.protocol import PROTOCOL_VERSION  # noqa: E402
from service.rules import load_effects, load_runtime  # noqa: E402
from service.transport import CommandChannel, OutReader  # noqa: E402

from spikes.chacun_son_serveur.relay import Bridge  # noqa: E402

SKEW_MS = 2500.0
DELAY_S, JITTER_S = 0.04, 0.02


def wait(check, what: str, timeout_s: float = 10.0) -> float:
    begin = time.monotonic()
    while not check():
        assert time.monotonic() - begin < timeout_s, f"jamais atteint : {what}"
        time.sleep(0.01)
    return time.monotonic() - begin


def command(seq: int, runtime) -> dict:
    return {"v": PROTOCOL_VERSION, "seq": seq, "kind": "probe_spawn", **runtime["probe_spawn"]}


def main() -> None:
    runtime, effects = load_runtime(), load_effects()
    with tempfile.TemporaryDirectory() as root:
        host, guest = Path(root, "hote"), Path(root, "invite")
        host.mkdir()
        guest.mkdir()
        guest_clock = lambda: time.time() * 1000 + SKEW_MS  # noqa: E731
        bridge = Bridge(
            host, guest, "B", delay_s=DELAY_S, jitter_s=JITTER_S,
            rewrite_t_ms=True, guest_clock_ms=guest_clock,
        ).start()
        channel = CommandChannel("B", host)
        channel.reset()
        assert bridge.host.sync_reset()
        channel.write_session("spike", 1, 0)
        mod = FakeMod(guest, "B")
        reader = OutReader(channel.out_path, "B", effects)
        events, anomalies = [], []

        def poll() -> None:
            result = reader.poll()
            events.extend(result.events)
            anomalies.extend(result.anomalies)

        wait(lambda: mod.session_path.exists(), "session chez l'invité")
        wait(lambda: bridge.host.clock.count >= 5, "mesure d'horloge")

        def kill() -> None:
            mod.t_ms = int(guest_clock())
            mod.emit("zombie_kill", {"player": "naylo", "w": "Base.Axe", "x": 1, "y": 2, "origin": "world"})

        for _ in range(50):
            kill()
        wait(lambda: (poll(), len(events) == 50)[1], "50 événements avant la coupure")
        channel.write(command(1, runtime))
        wait(lambda: mod.in_path.read_bytes().count(b"\n") == 1, "commande 1 chez l'invité")

        bridge.cut()
        cut_at = time.monotonic()
        for _ in range(50):
            kill()
        for seq in (2, 3, 4):
            channel.write(command(seq, runtime))
        mod.ack(1)
        time.sleep(2.0)
        poll()
        during = len(events)
        guest_in_during = mod.in_path.read_bytes().count(b"\n")

        bridge.heal(DELAY_S, JITTER_S)
        resumed = wait(lambda: (poll(), len(events) == 100)[1], "100 événements après la reprise")
        wait(lambda: mod.in_path.read_bytes().count(b"\n") == 4, "commandes 2-4 chez l'invité")
        wait(lambda: channel.ack_path.exists() and channel.ack_path.read_bytes() == mod.ack_path.read_bytes(),
             "ack chez l'hôte")
        for _ in range(10):
            kill()
        wait(lambda: (poll(), len(events) == 110)[1], "10 événements de plus")
        time.sleep(0.5)
        poll()

        seqs = [event["seq"] for event in events]
        # Chaque `t_ms`, daté avec 2,5 s d'avance par l'invité, doit revenir à l'heure de l'hôte.
        host_lines = [json.loads(line) for line in channel.out_path.read_bytes().splitlines()]
        guest_lines = [json.loads(line) for line in mod.out_path.read_bytes().splitlines()]
        shifts = [g["t_ms"] - h["t_ms"] for g, h in zip(guest_lines, host_lines)]
        clock = bridge.host.clock
        print(f"coupure de {time.monotonic() - cut_at - resumed:.1f} s, reprise en {resumed:.2f} s")
        print(f"événements lus par l'arbitre pendant la coupure : {during - 50} (attendu 0)")
        print(f"commandes arrivées chez l'invité pendant la coupure : {guest_in_during - 1} (attendu 0)")
        print(f"événements lus au total : {len(events)} ; seq sans trou ni doublon : "
              f"{seqs == list(range(1, 111))}")
        print(f"anomalies de transport : {len(anomalies)}")
        print(f"commandes chez l'invité : {mod.in_path.read_bytes().count(b'\n')} sur 4")
        print(f"écart d'horloge injecté : {SKEW_MS:.0f} ms ; estimé : {clock.offset_ms:.1f} ms "
              f"(borne annoncée ±{clock.bound_ms:.1f} ms)")
        print(f"correction appliquée aux `t_ms` : de {min(shifts)} à {max(shifts)} ms")
        bridge.stop()
        ok = seqs == list(range(1, 111)) and not anomalies and during == 50
        print("RÉSULTAT :", "OK" if ok else "ÉCHEC")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
