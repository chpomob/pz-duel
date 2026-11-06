# Archange “two servers” — detailed design

*English — [version française](../fr/02-archi-deux-serveurs.md)*

Decision: **architecture B**, host = one of the players' machines. Two PZ dedicated servers (one world each) + a local arbiter + two clients. Solo disappears from the setup: each “match” is a dedicated server for one player.

---

## 1. Why this choice really simplifies the concept

| Concept constraint | In solo (arch. A) | With two servers (arch. B) |
|---|---|---|
| Forbid time acceleration | Must be enforced by a mod (`setGameSpeed` in a loop) | **Free**: in multiplayer, the client's speed/pause UI is disabled. Proof in the installed game: `media/lua/client/ISUI/SpeedControlsHandler.lua` line 5 — `if isClient() then return; end` |
| Synchronise the clocks | Master clock + permanent freeze/unfreeze of both games | Both servers start from the same date/time (`StartYear/Month/Day/Time`) and the same `DayLength` → same clock, advancing at the same real-world rate. No more freezing to drive |
| Install the mod on players' machines | Mod + agent on every PC | The server declares its Workshop mods, the client subscribes and joins. Zero installer |
| Prevent save editing | Impossible in solo | The host owns the saves; players do not touch them |
| Mid-match pause | Native (menu) but must be propagated | To build (see §6) |
| Machine powered on during the match | Two player PCs | The host PC must stay on (SPOF accepted among friends) |

What it costs in exchange: two server processes to operate, the known fragility of mods in B42 multiplayer, and two new cases — server-side kill attribution and pause.

---

## 2. Topology

```
        Host PC (Ryzen 7 7700 / 31 GB / 1 Gb)                     Player 2 PC
┌──────────────────────────────────────────────────────┐  ┌──────────────────┐
│  Server A  (servername=duelA, Port=16261)            │  │ PZ client        │
│   world A ── duel mod ── duelA_status.txt            │  │ + duel HUD mod   │
│                              duelA_out.txt ◄─────────┼──┤                  │
│  Server B  (servername=duelB, Port=16271)            │  │                  │
│   world B ── duel mod ── duelB_status.txt            │  │                  │
│                              duelB_out.txt ◄─────────┼──┤                  │
│                                                      │  │                  │
│  Referee (Python, localhost)                         │  │                  │
│   • tails out_A/out_B, writes in_A/in_B              │  │                  │
│   • match clock, score, rules, caps, logs            │  │                  │
│   • RCON localhost (save, quit, createhorde…)        │  │                  │
└──────────────────────────────────────────────────────┘  └──────────────────┘
```

- Both servers run on the same PC, with distinct save folders (`-servername`) and distinct ports.
- The arbiter is the only one talking to both worlds: it applies the rules (1 kill → 2 zombies, caps, combos), keeps the score and the clock, and logs the match.
- Traffic between the two servers never leaves the machine: only the two clients connect from outside.

### Variant without an arbiter (keep in mind)

The two server mods can exchange directly through files in a shared folder (mod A writes `in_B.txt`, mod B reads it). This is the shortest possible MVP (~2 days less), but you lose the logs, the central score, the clock and the possibility of moving the match onto a VPS later. Recommendation: keep the arbiter, it costs ~4 days and it is the future match dashboard.

---

## 3. Concrete configuration of the two servers

Files `~/Zomboid/Server/duelA.ini` and `duelB.ini` (created by `-servername`), with **identical** values except ports and name:

```
# duelA.ini                    # duelB.ini
PublicName=Duel A              PublicName=Duel B
Port=16261                     Port=16271
RCONPort=27015                 RCONPort=27016
RCONPassword=<fort, différent> RCONPassword=<fort, différent>
PauseEmpty=false               PauseEmpty=false
UPnP=false                     UPnP=false
Mods=duelcore;duelhud          Mods=duelcore;duelhud
WorkshopItems=<ids>            WorkshopItems=<ids>
```

`SandboxVars` (the critical point for fairness) — same values on both sides, starting with:

- `DayLength` (4 by default in Apocalypse = 1 in-game day per 1 h 30 of real time)
- `StartYear`, `StartMonth`, `StartDay`, `StartTime` (they set the start date and time, hence the clock)
- population, loot rarity, vehicles, electricity/water, zombie difficulty

Worth knowing: these options are **numbered presets** (`DayLength = 4`), not free values. Any divergence creates an invisible but real asymmetry.

Startup (Linux, SteamCMD dedicated server app 380870):

```
./start-server.sh -servername duelA -port 16261
./start-server.sh -servername duelB -port 16271
```

Both must be started **together, before the match begins**: that is what guarantees the identical clock. Restarting one without the other introduces drift (see §5).

Network: 4 UDP ports to open/forward (16261/16262 and 16271/16272). Behind a CGNAT you need a VPS or a tunnel; plan for that case from the documentation stage.

---

## 4. What the server mod sees and does

### Observation (server side, authoritative)

| Game fact | Source | Note |
|---|---|---|
| Kill | `OnZombieDead` + `IsoZombie:getAttackedBy()` (javadoc 42.x) | `OnZombieDead` only provides the zombie: you have to read the last attacker. `OnHitZombie` provides `attacker` → keep a small “zombie → last attacker” table and purge it |
| Player damage / injury | `OnPlayerGetDamage`, `OnPlayerDeath` | Marks the end of combo, and the end of match |
| Shots / noise | `OnWeaponHitCharacter`, firearm events | Feeds the “noise” events on the opponent's side |
| Position / zone | `getPlayer():getX()/getY()/getZ()` | To place spawns at a safe distance |
| Clock | `getGameTime():getWorldAgeHours()` | Written to the status file every 10-30 s |
| Connection state | `OnPlayerConnect/Disconnect` | Pause/abandon policy |

The mod writes compact lines into `duelX_out.txt` (one per event, buffered and flushed every ~1 s), and re-reads `duelX_in.txt` at the same rate. No networking in Lua: the arbiter does the transport.

### Injection (server side, authoritative)

- `addZombiesInOutfit(x, y, z, n, outfit, chance)` and `addZombiesInOutfitArea(x1,y1,x2,y2,z,n,outfit)` — used by the vanilla tutorial, therefore stable; server-authoritative spawn confirmed by Zombie Spawn Manager on B42 (to be re-tested on 42.20.4 during the spike).
- Server-side “free” effects: weather (`startrain`/`stoprain`), noise (`gunshot`, `alarm`), helicopter (`chopper`), items (`additem`, or ground spawn), danger acceleration (`createhorde`).
- B42 limitation documented by Zombie Spawn Manager: no more reliable public per-zombie setters for sight/hearing/memory/strength. So “sprinter” yes (public walk type), “stronger / more perceptive zombie” no without a runtime controller.

### What the server mod cannot do

- No pause: the server does not pause because a player opens a menu.
- No `setGameSpeed`: speed is a solo notion.
- No networking: everything goes through the arbiter.

---

## 5. Clock: what is guaranteed, what drifts

Guaranteed by construction:
1. same start date/time (identical SandboxVars);
2. same `DayLength`;
3. advance tied to real time, with no client-side acceleration possible (`isClient()` in `SpeedControlsHandler`).

Real sources of drift:
- restart of a single server (crash, update, `quit`);
- `PauseEmpty=true`: if a player disconnects, **that** world stops and the other keeps going → immediate drift. Hence `PauseEmpty=false` and a disconnection policy on the arbiter side;
- host machine asleep / extreme load (tick slowdown).

Handling:
- the arbiter compares `worldAgeHours` on both sides every 10 s and alerts beyond ±30 s of game time;
- fine correction possible via `GameTime:setTimeOfDay()` / `setDay/setMonth/setYear` on the lagging server (visible jump, preferably done at dawn and out of combat) — **to be validated in a spike**: it is not yet known whether a clock setter on a server mid-game is clean (queues, crops, scheduled spawns);
- failing a clean correction, the fallback rule is: match invalidated and restarted from the last common save. That is acceptable among friends, not for a ladder.

---

## 6. Pause, resume, abandonment

In multiplayer there is no pause. Three mechanisms, to choose from:

- **(a) No pause at all** — the world keeps running, a player who steps away leaves their survivor exposed. Simple, brutal, consistent with a duel. Recommended for v1.
- **(b) Pause = clean shutdown of both servers** — the arbiter sends `save` then `quit` to both (RCON), and restarts both processes on resume. The worlds resume exactly where they were, and so does the clock. Cost: 30-60 s to resume, and mod re-init must be idempotent.
- **(c) Timeout/(dis)connection** — a player drops: match freeze (not the servers) with a 2-5 min grace period, then abandonment = defeat, or match suspended in friendly mode.

Recommendation: (a) + (c) in v1, (b) as a comfort option. Never (b) without testing save resume under mods.

---

## 7. Sequence of one event, end to end

```
t+0.0   player A kills a zombie
t+0.1   mod A: OnZombieDead + getAttackedBy → buffer "kill 7421 1"
t+1.0   mod A flushes the buffer into duelA_out.txt
t+1.1   the referee reads the line, increments A's counter,
        applies the rule (1 kill → 2 zombies), checks caps/cooldowns
t+1.2   the referee writes into duelB_in.txt: "spawn 2 near_player safe=40"
t+1.3   mod B (0.5-1 s poll) reads it, computes a position 40 tiles away,
        addZombiesInOutfit(x,y,z,2,nil,nil) + sound/HUD warning
t+2.2   both zombies exist on server B
```

Realistic latency 1-3 s. “Batched garbage” (accumulate 10-15 s then send one packet) remains preferable for readability and for performance — the pipeline above does not change, only the send rate changes.

---

## 8. Revised budget

| Item | Days |
|---|---|
| Go/no-go spikes (see §9) | 2-3 |
| Industrialising the 2 servers (ini, sandbox, ports, launch script, resume) | 2-3 |
| Server mod `duelcore` (capture, attribution, injection, status, HUD data) | 6-8 |
| Client mod `duelhud` (score, clock, alert, end screen) | 3-4 |
| Python arbiter (protocol, clock, rules, caps, logs, match states) | 4-6 |
| Balancing (conversion table, combos, anti-snowball, victory) | 4-8 |
| Docs + Workshop page + error messages | 2-3 |
| QA (crash, reload, disconnection, B42 patch, 2 instances) | 4-8 |
| **Total** | **27-43 days** |

The total stays close to architecture A, but the riskiest part (time gating, an agent on every player's machine) disappears, and installation becomes “subscribe to the mod, connect”. The remaining uncertainty is concentrated on **the stability of mods in B42 multiplayer** and on balancing.

---

## 9. Revised go/no-go spike (2-3 days)

The risk is no longer the clock, it is multiplayer.

1. **J1 — Server mod in MP.** A server-side Lua mod on 42.20.4: counts kills with `OnZombieDead` + `getAttackedBy()`, writes to a file. Criterion: 100 kills, 100 % correct attribution, no perceptible lag, file readable by an external process.
2. **J2 — Server spawn.** `addZombiesInOutfit` at 40 tiles from a player, triggered by an external file. Criterion: 20 spawns, no crash, no zombie at the player's back, stable FPS, zombies visible on the client side.
3. **J3 — Two servers + clock.** Both servers on the same machine, same SandboxVars, 30 minutes with two players connected. Criterion: measured clock drift < 10 s of game time, and a `setTimeOfDay` correction test of +2 min to see whether it is clean.

If J1 or J2 fails → we fall back to architecture A (solo + IPC) or C (Java). If J3 fails only on correction, we keep B with “drift accepted + match invalid beyond threshold”.

---

## 10. Risks specific to this architecture

| Risk | Mitigation |
|---|---|
| Unstable B42 multiplayer mods (Workshop loading, Lua reload that kills the session — reports on 42.20) | Version the mods, no `reloadlua` during a match, clean restart, test every point release |
| Host = administrator of both worlds → temptation to cheat | Dedicated admin account held by the arbiter (RCON password not shared), host player without admin rights; otherwise accept “among friends” |
| Host PC off/asleep = match lost | Save policy every 5 min + resume; clear docs; VPS later |
| 4 UDP ports, CGNAT | Port forwarding docs + tunnel/VPS option |
| Non-identical start date/time (worlds created at different moments) | Create both worlds in the same installation session, check `worldAgeHours` is identical before the first match |
| Drift after restarting a single server | The arbiter refuses to start a match if the two clocks differ by more than X; otherwise correction or world replayed |
| Unfair spawn (back, room, sleeping, driving) | Warning, minimum distance, blacklist of contexts, per-minute cap |

---

## 11. Remaining decisions

1. **Pause** — (a) none, the world keeps running *(reco)* · (b) stop/restart both servers · (c) match freeze on the arbiter side only.
2. **Host** — (a) player 1's PC, admin held by the arbiter *(reco)* · (b) rented VPS · (c) both, configurable.
3. **Arbiter ↔ mods transport** — (a) files + RCON *(reco)* · (b) server-side Java agent (pzJBridge-like) from the start.
4. **Event granularity** — (a) 10-15 s batches *(reco)* · (b) real time, event by event.
5. **Clock correction** — (a) none, invalidation beyond ±60 s *(reco v1)* · (b) automatic `setTimeOfDay` if the spike validates it.

---

## 12. Settled decisions (04/11/2026) and “short match” consequences

| # | Decision | Chosen |
|---|---|---|
| 1 | Pause | **1a** — no pause; short matches, the world keeps running |
| 2 | Host | **2a** — a player's PC for the POC; separate server planned in the final architecture |
| 3 | Arbiter ↔ mods transport | **3a** — files + localhost RCON |
| 4 | Granularity | **4a** at MVP (10-15 s batches); **4b** (real time) eventually |
| 5 | Clock drift | **5a** — no correction, invalidation beyond ±60 s |

### 12.1 The short format changes three settings, not the architecture

`DayLength` is a numbered preset (verified in `media/lua/shared/Translate/EN/Sandbox.json`):

| Value | One in-game day |
|---|---|
| 1 | 15 min of real time |
| 2 | 30 min |
| 3 | 1 h |
| 4 (Apocalypse default) | 1 h 30 |
| 5 / 6 / 7 | 2 h / 3 h / 4 h |

Consequence: a **20-30 min match = 1 to 2 in-game days** with `DayLength=1 or 2`. The match clock becomes a simple real-time timer displayed on the HUD, and time drift over 30 min will be on the order of a second: rule 5a will practically never trigger. The clock monitor remains useful as a diagnostic, not as a game mechanism.

### 12.2 What must be settled with short matches

1. **World, not arena.** Players never cross paths: there is neither contact nor PvP. The world only has to be survivable and a **generator of actions** (zombies, loot, materials); the map is a density-of-opportunities setting, not a meeting setting. See `03-mechanisms-and-telemetry.md`.
2. **Reset between matches.** Two persistent worlds degrade (loot consumed, zombies killed, buildings wrecked). To chain duels, you need a **snapshot of both save folders taken at creation**, restored with the servers stopped between two matches (~1-2 min). That is a real work item (+2-3 d) and an operational step, not a detail.
3. **End of match.** With 1a (no pause) and a short format: timer + score, and **death = immediate defeat** (sudden death). The timer must be visible on both sides and identical (it lives in the arbiter, not in the game).
4. **Disconnection.** 1a implies that a player who drops cannot freeze the match: 2 min grace then forfeit, match saved and result recorded. To be written in black and white in the displayed rules.
5. **4b later.** The protocol must be transport-agnostic: timestamped, idempotent event lines, with a sequence number. Switching from “10-15 s batch” to “event by event” then becomes a rate change + a server-side Java agent, without touching the rules.
6. **2a → separate server.** The arbiter and the mods must only know `localhost`. The day the servers move to a VPS, only the file paths and the ports change.

### 12.3 Adjusted budget

Add 2-3 d for the world snapshot/reset, remove ~1 d of clock monitoring that has become cosmetic: **total 28-45 d** for the MVP, excluding a dedicated map and excluding real-time mode.
