# MVP SPEC — PZ Duel

*English — [version française](../fr/07-spec-mvp.md)*

Status: consolidated specification, decisions locked on 4 November 2026.
Reference documents: [01-state-of-play.md](01-state-of-play.md) (feasibility), [02-two-server-architecture.md](02-two-server-architecture.md) (architecture), [03-mechanisms-and-telemetry.md](03-mechanisms-and-telemetry.md) (rules/telemetry), [04-spike-0-evidence.md](04-spike-0-evidence.md) (evidence), [05-observable-vs-generatable.md](05-observable-vs-generatable.md) (inventory), [06-play-flow-and-relation-table.md](06-play-flow-and-relation-table.md) (flow).

## 1. Locked decisions

| Topic | Decision |
|---|---|
| Architecture | two dedicated servers on a player's machine (VPS later) |
| Pause | none; short matches (20-30 min), `DayLength` 1-2 |
| Host security | admin held by the referee, never by the host player |
| Transport | files in `~/Zomboid/Lua/` + localhost RCON |
| Granularity | batches of 10-15 s (real time in v2) |
| No clock correction | invalidation beyond ±60 s of drift |
| Rules | declarative YAML table, atoms + combos + series |
| Contract | host alone edits, the guest accepts, table hash |
| Mods | list imposed by the preset, verified identical on both sides |
| Threats | WORLD channel (server) + 2 EXPERIENCE effects in v1 (fog, shake) + META |
| Sources | S1 (server) + S3 (derived) only; never `AddXP`/`LevelPerk` |

## 2. Scope

### In v1
- two servers, two worlds, one 25-min match, sudden death or score.
- 6 server atoms: `zombie_kill`, `damage_taken`, `build`, `vehicle_repair`, `explosion`, `idle_check` (derived).
- 8 effects: `spawn_pop`, `spawn_sprinter`, `horde_area`, `noise`, `fog`, `shake`, `power_off`, `supply_drop`.
- YAML table, contract with hash, JSONL journal, replay.
- snapshot/restore of both worlds between matches.
- 3 presets: Quick Duel, Pressure, Hardcore.
- ghost mode (scripted pressure to balance against a single player).

### Out of v1
- real-time event-by-event (Java agent), matchmaking, ranking, anti-cheat, dedicated map, asymmetric modes other than handicaps, NPC/animal effects from the Chaos catalogue, full graphical editor.

## 3. Components

| Component | Language | Responsibility | Never does |
|---|---|---|---|
| `duelcore` (server mod) | Lua | captures the atoms, applies the commands (spawn/effects), writes `out`, reads `in`, heartbeat | no rules, no balancing |
| `duelhud` (client mod) | Lua | HUD (score, timer, alert), applies the 2 EXPERIENCE effects | no decisions |
| `referee` | Python | contract, server lifecycle, transport, table evaluation, journal, refereeing | no hard-coded game logic |
| `presets/` | YAML | shipped tables | — |

## 4. Protocol (v1)

Per-side files in `~/Zomboid/Lua/`: `duel_A_out.txt`, `duel_A_in.txt`, `duel_A_ack.txt` (same for B).
One line = one JSON, terminated by `\n`. The reader only processes complete lines.

**Event (mod → referee)**
```json
{"v":1,"seq":42,"t":312.44,"side":"A","kind":"kill","data":{"z":1234,"w":"Base.Axe","x":10500,"y":9900}}
```

**Command (referee → mod)**
```json
{"v":1,"seq":7,"kind":"spawn_pop","n":2,"distance":[30,60],"warn_s":6}
```

**Heartbeat (mod → referee, every 10 s)**
```json
{"v":1,"kind":"beat","side":"A","age":2.4657,"hp":87,"players":["durand"],"kills":41}
```

Transport rules:
- the referee writes `in` in a single `write` then `fsync` (no partial line);
- the mod remembers the last processed `seq` and writes `ack` every 5 s;
- when `ack == last seq`, the referee truncates `in` (no file deletion, no race);
- on the `out` side, the referee tracks a byte offset; if the file shrinks (server restart), it starts again from 0 and deduplicates by `seq`.

## 5. Data model

- `match.yaml`: duration, `day_length`, `symmetry`, mods (Workshop ids), overridden sandbox, password, presets.
- `relations.yaml`: atoms, rules, modifiers, budget (cf. `06`, §3.2).
- `journal.jsonl`: one record per event, conversion, effect and state change; single source for replay and balancing.

## 6. Acceptance criteria (measurable)

1. 100 kills in game → 100 events, 100% correct attribution, zero loss.
2. p95 latency event → effect < 3 s (measured on the journal).
3. No spawn within 25 tiles of the player; no spawn during sleep or driving.
4. 3 consecutive 25-min matches without a crash; server RSS stable (±10%).
5. Server restart during a match: recovery with no loss of rules; drift > 60 s → match cleanly invalidated, never silently.
6. Invalid table (unknown effect, non-authoritative source) → refused at load, explicit message.
7. Table modified after acceptance → startup refused (hash).
8. Journal replay: same effects at the same timestamps (±250 ms).
9. `Mods=`/`WorkshopItems=` different between the two servers → startup refused.
10. Disconnect > 2 min → forfeit recorded, servers stopped cleanly.

## 7. Build order

| # | Step | Days | Blocking? |
|---|---|---|---|
| 1 | `referee`: rules engine + journal, tested on recorded event streams, without the game | 2-3 | no |
| 2 | `duelcore`: atoms, heartbeat, commands, spawns | 4-5 | yes (spike 1) |
| 3 | Spike 1 in game (5 checks from `05`) | 1 | yes |
| 4 | `duelhud`: HUD + `fog` + `shake` | 3-4 | no |
| 5 | Lifecycle: snapshots, ports, mod verification, contract | 2-3 | no |
| 6 | Table editor/rendering + presets | 2-3 | no |
| 7 | Balancing (ghost mode + real duels) | 4-8 | no |
| 8 | QA, docs, packaging | 3-5 | no |

Total: **21-32 days**, excluding B42 contingencies. Step 1 is immediately feasible and without the game: it yields a testable engine and a journal before even touching PZ.

## 8. Open risks

| Risk | Impact | Treatment |
|---|---|---|
| Craft not observable server-side | `craft` atom absent from v1 | inventory diff (A5 from `05`) if `OnProcessAction` fails |
| `OnWeaponHitCharacter` / `OnEnterVehicle` server-side | `damage_taken`/vehicle atoms uncertain | spike 1, fallback to `OnCharacterDeath` + heartbeat HP |
| Unstable B42 MP mods | match lost | strict versioning, no `reloadlua` during a match, clean restart |
| Table editable by a player | broken or unfair match | strict validation, capped budgets, hashed contract |
| Host = admin | cheating | RCON password held by the referee |
| Clock drift | unfair match | monitoring + invalidation (never correction) |
