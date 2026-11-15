"""Spike S2 : deux PC, deux horloges. Ce que l'écart fait au verdict, et ce que vaut la correction.

Le mod date chaque événement à l'heure murale de sa machine (`getTimestampMs`). Le cœur de match
ne s'en sert que pour trancher : laquelle de deux morts est la plus ancienne, et si un fait tombe
avant le terme du minuteur. Avec deux machines, l'écart entre leurs horloges entre dans ce jugement.

Trois mesures, sur le vrai `MatchCore`, sans réseau ni jeu :

1. morts presque simultanées : pour quel écart d'horloge le vainqueur s'inverse ;
2. kill juste avant le terme : pour quel écart il cesse de compter ;
3. précision de `ClockSync` (aller-retour, plus petit RTT) sur des liens simulés.

    python -m spikes.chacun_son_serveur.clock_skew
"""

from __future__ import annotations

import random
import statistics

from service.engine import load_presets
from service.rules import MatchCore, load_effects, load_runtime

from spikes.chacun_son_serveur.relay import ClockSync

T0_MS = 1_800_000_000_000
RUNTIME, EFFECTS = load_runtime(), load_effects()
TABLE = next(p for p in load_presets(RUNTIME, EFFECTS) if p["id"] == "demo")
DURATION_S = TABLE["duration_s"]


def event(side: str, seq: int, kind: str, true_te_ms: int, skew_ms: float) -> dict:
    """Un événement survenu à `true_te_ms` du match, daté par l'horloge de sa machine."""
    data = {"player": side} if kind != "player_death" else {}
    return {
        "v": 1, "boot": 1, "seq": seq, "src": side, "side": side, "kind": kind, "data": data,
        "t_ms": int(T0_MS + true_te_ms + skew_ms),
    }  # fmt: skip


def play(events: list[dict], until_ms: int) -> MatchCore:
    core = MatchCore(TABLE, RUNTIME, EFFECTS, T0_MS, DURATION_S)
    for item in events:
        core.submit_event(item)
    core.advance(T0_MS + until_ms)
    return core


def deaths(gap_ms: int, skew_b_ms: float) -> str | None:
    """A meurt à 30 s, B `gap_ms` plus tard : B doit gagner. L'horloge de B a `skew_b_ms` d'écart."""
    core = play(
        [event("A", 1, "player_death", 30_000, 0), event("B", 1, "player_death", 30_000 + gap_ms, skew_b_ms)],
        31_000 + gap_ms,
    )
    return core.result["winner"] if core.result else None


def last_kill(before_end_ms: int, skew_b_ms: float) -> int:
    """B tue un zombie `before_end_ms` avant le terme : rend le score de B à la fin.

    Le kill n'est LU qu'au tick du minuteur, comme un fait resté dans le tampon du mod (1 s)
    ou en vol : c'est le seul tick où le cœur compare la date d'un fait au terme.
    """
    end_ms = DURATION_S * 1000
    core = MatchCore(TABLE, RUNTIME, EFFECTS, T0_MS, DURATION_S)
    core.advance(T0_MS + end_ms - 1)
    core.submit_event(event("B", 1, "zombie_kill", end_ms - before_end_ms, skew_b_ms))
    core.advance(T0_MS + end_ms + 1000)
    return core.scores["B"]


def flip_threshold(gap_ms: int) -> float:
    """Plus petit retard de l'horloge de B (ms) qui donne la victoire au mauvais camp."""
    for skew in range(0, -5001, -10):
        if deaths(gap_ms, skew) != "B":
            return skew
    return float("-inf")


def sync_error(true_offset_ms: float, out_ms: float, back_ms: float, jitter_ms: float,
               pings: int, rng: random.Random) -> tuple[float, float]:
    """Erreur de `ClockSync` (ms) après `pings` allers-retours, et la borne qu'il annonce."""
    now = [0.0]
    sync = ClockSync(lambda: now[0])
    for _ in range(pings):
        sent = now[0]
        there = out_ms + rng.expovariate(1 / jitter_ms) if jitter_ms else out_ms
        back = back_ms + rng.expovariate(1 / jitter_ms) if jitter_ms else back_ms
        guest_clock = sent + there + true_offset_ms
        now[0] = sent + there + back
        sync.sample(sent, guest_clock)
        now[0] += 500
    return sync.offset_ms - true_offset_ms, sync.bound_ms


def main() -> None:
    print("1. Morts presque simultanées — A meurt d'abord, B doit gagner")
    print("   écart réel entre les morts | retard d'horloge de B qui inverse le vainqueur")
    for gap in (100, 250, 500, 1000, 2000):
        print(f"   {gap:>5} ms                   | {flip_threshold(gap):>7.0f} ms")
    wrong = sum(deaths(400, skew) != "B" for skew in range(-3000, 3001, 100))
    print(f"   morts à 400 ms d'écart, écart d'horloge balayé de -3 s à +3 s : "
          f"{wrong} verdicts faux sur 61")

    print("\n2. Kill de B lu au tick du minuteur — fait avant le terme il compte, après non")
    print("   écart d'horloge de B | kill 300 ms AVANT le terme | kill 300 ms APRÈS le terme")
    for skew in (-1000, -500, -200, 0, 200, 500, 1000):
        before, after = last_kill(300, skew), last_kill(-300, skew)
        marks = ("" if before == 1 else "  ← perdu à tort", "" if after == 0 else "  ← compté à tort")
        print(f"   {skew:>+6} ms            | score {before}{marks[0]:<18}   | score {after}{marks[1]}")

    print("\n3. Précision de la mesure d'écart (20 allers-retours, 2 000 essais par ligne)")
    print("   lien (aller / retour / gigue)        | erreur médiane | erreur p99 | borne annoncée p50")
    rng = random.Random(7)
    links = [
        ("réseau local      1 /  1 / 0.5 ms", 1, 1, 0.5),
        ("fibre            15 / 15 / 5 ms  ", 15, 15, 5),
        ("ADSL/4G          40 / 40 / 20 ms ", 40, 40, 20),
        ("via relais       80 / 80 / 30 ms ", 80, 80, 30),
        ("asymétrique      20 / 90 / 20 ms ", 20, 90, 20),
    ]
    for label, out_ms, back_ms, jitter in links:
        errors, bounds = [], []
        for _ in range(2000):
            error, bound = sync_error(rng.uniform(-5000, 5000), out_ms, back_ms, jitter, 20, rng)
            errors.append(abs(error))
            bounds.append(bound)
        errors.sort()
        print(f"   {label}   | {statistics.median(errors):>8.1f} ms    | {errors[1979]:>6.1f} ms  "
              f"| {statistics.median(bounds):>6.1f} ms")

    print("\n4. Verdict après correction — morts à 400 ms d'écart, horloge de B fausse de -3 s à +3 s")
    rng = random.Random(11)
    for label, out_ms, back_ms, jitter in links:
        wrong = 0
        trials = 0
        for skew in range(-3000, 3001, 100):
            for _ in range(20):
                error, _ = sync_error(skew, out_ms, back_ms, jitter, 20, rng)
                # Le relais retire l'écart estimé : il ne reste que l'erreur d'estimation.
                wrong += deaths(400, error) != "B"
                trials += 1
        print(f"   {label}   | {wrong} verdicts faux sur {trials}")


if __name__ == "__main__":
    main()
