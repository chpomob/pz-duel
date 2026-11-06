# Play flow and relation table

*English — [version française](../fr/06-play-flow-et-table-de-relations.md)*

Describes the product as it is played: a player defines the rules, the machine launches two servers, the link between the two is deliberately dumb, and everything that matters is a **relation table** action → consequence.

---

## 1. Guiding principle

The game is not hard-coded: it is a **consequence engine**. A duel is one configuration of that engine.
Corollary: the deliverable product is not "a duel mod", it is (a) an engine + (b) presets. A "Quick Duel" preset is a file; a "Hardcore" preset is another; a future cooperative mode would be yet another table.

Three layers, never mixed:

| Layer | Role | Where it lives |
|---|---|---|
| **Match** | who plays, which mods, which world, how long | `match.yaml` |
| **Relations** | what each action/combo/streak produces on the other side | `relations.yaml` |
| **Link** | transport of events and commands, with no game logic | referee (process) |

The link is "dumb" by choice: it knows neither the game nor the balancing. It transports events, applies the table, logs. If one day it has to be replaced (VPS, Java agent), the other two layers do not move.

---

## 2. Play flow

```
 1. HOST              2. CONTRACT          3. LAUNCH             4. MATCH             5. END
┌──────────────┐   ┌─────────────────┐   ┌──────────────────┐  ┌───────────────┐  ┌────────────┐
│ picks a      │   │ guest sees the  │   │ world snapshots  │  │ both players  │  │ result +   │
│ preset or    │──▶│ table in plain  │──▶│ create/restore   │─▶│ play, the two │─▶│ log +      │
│ edits the    │   │ language and    │   │ 2 servers        │  │ servers run   │  │ JSONL      │
│ table        │   │ accepts         │   │ 2 clients        │  │               │  │ replay     │
└──────────────┘   └─────────────────┘   └──────────────────┘  └───────────────┘  └────────────┘
       │                    │                     │                    │
       └── required mods ───┴── mod/server ───────┴── match clock ─────┘
           and versions        verification         (synchronous start)
```

Detail of the steps:

1. **Host** — configuration screen: preset, duration (20/25/30 min), `DayLength`, mod list (inherited from a preset, editable), relation table (editor or imported file), password, symmetric mode or handicaps.
2. **Contract** — the guest receives a readable summary, generated from the table: "Each zombie killed → 2 zombies at your place (max 60/min). 3 builds then a kill within 30 s → a horde." They accept or refuse. No match without acceptance: that is what makes the table "fair" in the eyes of both. The contract is **sealed by a hash** of the table: the referee refuses to start if the effective table no longer matches the one that was accepted.
3. **Launch** — the referee: restores the world snapshots, starts the two servers (distinct ports and RCON), checks that `Mods=` and `WorkshopItems=` match the contract, waits for both players to be connected, then starts the clock.
4. **Match** — no pause. The timer lives in the referee. The servers know only the game.
5. **End** — sudden death or end of timer (score). Result, JSONL log, replay possible (events are timestamped and sequenced).

---

## 3. The relation table

### 3.1 Model: atoms → aggregates → sequences

| Level | The question it answers | Example |
|---|---|---|
| **Atom** | which raw game fact do we capture? | `zombie_kill`, `damage_taken`, `build`, `vehicle_repair`, `explosion` |
| **Aggregate** | how many, over which window? | `3 zombie_kill in 10 s`, `0 damage_taken for 60 s` |
| **Sequence** | in which order? | `build ×3 then zombie_kill within 30 s` |
| **Modifiers** | how does the state of the match change the weight? | combo, score gap, remaining HP |
| **Effects** | what does the other side suffer? | `spawn_pop(2)`, `fog`, `power_off` |

Each level is **declarative**: the table is a file, not code. The referee evaluates the table on the event stream (250 ms tick) and keeps one state per side (counters, windows, combo).

### 3.2 Concrete example

```yaml
version: 1
match: { duration_s: 1500, day_length: 1, symmetry: true }

# ---- atoms: only S1 sources (server, non-falsifiable)
atoms:
  zombie_kill:   { source: server, event: OnZombieDead }
  damage_taken:  { source: server, event: OnPlayerGetDamage }
  build:         { source: server, event: OnObjectAdded, filter: player_built }
  vehicle_repair:{ source: server, event: OnMechanicActionDone }
  explosion:     { source: server, event: OnThrowableExplode }

# ---- rules: the relation table
rules:
  - id: kill_baseline
    when: atom(zombie_kill)
    every: 1                     # per 1 kill increment
    effects:
      - { kind: spawn_pop, n: 2, distance: [30, 60], warn_s: 6, cap_per_min: 60 }

  - id: kill_streak
    when: atom(zombie_kill).count(3).within(10s)     # combo
    effects:
      - { kind: spawn_sprinter, n: 1, distance: [40, 70], warn_s: 10 }

  - id: untouched_minute
    when: atom(zombie_kill).count(10).within(60s).and(not atom(damage_taken).within(60s))
    effects:
      - { kind: fog, duration_s: 90 }

  - id: builder_payoff
    when: atom(build).count(3).within(120s).then(atom(zombie_kill), within: 30s)   # streak
    effects:
      - { kind: horde_area, n: 25, radius: 20, distance: [50, 80], warn_s: 12 }

  - id: pain_relief
    when: atom(damage_taken).count(1).within(1s)
    effects:
      - { kind: combo_reset }                       # effect on oneself, not on the other
      - { kind: supply_drop, distance: [10, 25] }    # anti-snowball

modifiers:
  combo:        { per: 0.15, max: 3.0, reset_on: damage_taken }
  score_gap:    { when_gt: 30, attacker: 0.6, defender_bonus: supply_drop }

budget:                          # global safeguards, not negotiable by the table
  threats_per_minute: 6
  concurrent_effects: 4
  per_effect_cooldown_s: 20
```

### 3.3 What the table cannot do

- Reference an effect not declared by the mod (load-time failure, explicit message).
- Exceed the global budget (the `cap_per_min` values are capped by the engine).
- Use a non-authoritative source (a rule based on `AddXP` would be marked `source: client` and refused in ranked mode).
- Touch pause, the clock, or the player's life through an API.

That is what makes a table "editable by a player" without letting them break the match.

---

## 4. Link: what the referee does, exactly

| Responsibility | Detail |
|---|---|
| Check the contract | reads `Mods=`/`WorkshopItems=` from both `.ini` files, compares to the contract, refuses to start on any discrepancy |
| Restore the worlds | snapshot of the two save folders, restored with the servers stopped |
| Start/stop | two server processes, distinct ports and RCON (`RCONPort` 27015/27016) |
| Transport | tail of the mods' `outbox`, writes to the `inbox`, RCON for stock commands |
| Evaluate | applies the table to the stream, keeps counters/windows/combo per side |
| Log | one JSONL line per event, conversion, effect and state change |
| Arbitrate | timer, sudden death, disconnection (2 min grace then forfeit), invalidation on clock drift |
| Make it readable | generates the contract summary and the end-of-match summary from the table and the log |

Nothing else. No game logic in the referee: if a rule demands an unforeseen behaviour, we add an **effect type** on the mod side, not a special case in the referee.

---

## 5. Technical consequences

1. **Table editor**: a form that produces the YAML + a rendering in natural language. The same rendering serves the contract and the end-of-match summary. It is the UX piece that decides whether the product is usable by anyone other than its author.
2. **Presets**: at least three — "Quick Duel" (kill → pop, few rules), "Pressure" (combos + weather + items), "Hardcore" (sprinters, hordes, electricity). These are versioned files.
3. **Log = replay**: every match can be replayed from its JSONL. It is also the balancing tool (see `03-mechanisms-and-telemetry.md`).
4. **Strict validation at load time**: an invalid table does not start. No silent failure — that is the difference between a tool and a demo.
5. **Symmetry by default, handicaps as an option**: `symmetry: true` duplicates the rules for both sides; otherwise `sides: [A]` or `[B]` allows an explicit handicap.

---

## 6. Settled decisions (04/11/2026)

| # | Topic | Retained |
|---|---|---|
| 1 | Who edits | host alone, the guest accepts the contract (hash) |
| 2 | Format | **commented YAML** (settled by default, "idgaf") |
| 3 | Granularity | atoms + combos + streaks |
| 4 | Symmetry | symmetric by default, optional handicaps |
| 5 | Mods | list imposed by the preset, verified identical on both sides |
| 6 | EXPERIENCE effects | in v1 (fog, shake) |

The rest of this document describes the state before the decisions; the consolidated specification is in `07-mvp-spec.md`.
