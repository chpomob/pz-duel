# Project Zomboid Duel Mode — feasibility study (state of play)

*English — [version française](../fr/01-etat-des-lieux.md)*

Date: 04/11/2026 · Target: Project Zomboid **Build 42 stable (42.20.x)** · Reference machine: 42.20.4 installed locally

> **Decision (04/11/2026)**: retained architecture = **two dedicated servers on one player's machine**, time not accelerated by construction. Design detail, config, clock, pause and revised budget: `02-two-server-architecture.md`. Sections 4 and 5 below (archi A, `setGameSpeed` gating) remain as a reference for the discarded option.

The concept itself — two linked Project Zomboid games, each side's actions becoming threats in the
other's world — comes from **Harumate** ([twitch.tv/harumate](https://www.twitch.tv/harumate)). This
study is an independent implementation of it.

---

## 1. Short verdict

**Feasible, and more feasible than expected.** None of the four required mechanisms has to be invented:

| Required function | Already exists, proven by |
|---|---|
| Read what happens in the game (kills, damage, death, position, weather) | Native Lua events (`OnZombieDead`, `OnHitZombie`, `OnPlayerDeath`…) + Twitch/streamer mods that consume them |
| Inject events (zombie pop, sprinters, items, helicopter, weather, noise) | `addZombiesInOutfit()`, `addZombiesInOutfitArea()` (used by the vanilla tutorial), admin commands `createhorde`, `chopper`, `gunshot`, `lightning`, `alarm`, `additem`, `addvehicle` |
| Make two games talk to each other | **No networking in Lua**, but file-based IPC is proven (SIF reads/writes `Zomboid/Lua/*.txt`), or a Java agent (ZombieBuddy / pzJBridge) |
| Synchronise time | `getGameTime():getWorldAgeHours()`, `getGameSpeed()`, **`setGameSpeed(0..4)`** (used by the pause menu and the vanilla speed UI), `isGamePaused()` |

The real risk is not technical, it is **gameplay**: the event economy (1 kill = 2 zombies?) and fairness between two separately generated worlds. That is where most of the iteration will go.

Order of magnitude: **MVP playable by two in 4 to 6 weeks** of focused work (≈25-35 person-days), including 3 days of technical spikes to be run BEFORE committing.

---

## 2. Functional breakdown of the concept

A “PZ duel” = 4 decoupled functions, each with its own risk level:

1. **Observe** — capture gameplay facts on side A (kill, damage, death, shot, zone entry, weather).
2. **Decide** — turn those facts into “garbage” for B according to configurable rules (rates, caps, anti-snowball, combos).
3. **Inject** — apply those events in game B (pop, sprinter, item, noise, helicopter, weather).
4. **Pace** — keep the two game clocks synchronised, handle pause/death/crash/reconnect.

Functions 1 and 3 are problems the ecosystem has already solved. 4 is a simple engineering problem but full of edge cases. 2 is game design, hence iterative.

---

## 3. State of the ecosystem (Nov. 2026)

### 3.1 Game version

- **42.20.0 = stable since 29/07/2026**, multiplayer included; latest stable **42.20.4** (26/08/2026).
- **42.21.0 = unstable** (23/09/2026), 42.22 in development (modding notes of 27/10/2026).
- The local test machine runs **42.20.4** (`version=42.20.4 b0bbce05d5`, console.txt).
- Consequence: we develop against 42.20.x and plan a compatibility pass at every point release. B41 mods are not portable as-is to B42.

### 3.2 What PZ's Lua can do

- PZ's Lua is **Kahlua** (Lua 5.1 in Java): access to exposed Java classes, **no `io.*`/`os.*`**, **no socket, no HTTP**.
- The sandbox is spelled out by the ZombieBuddy documentation: “Unlike Lua mods which run in a sandboxed environment, Java mods are completely unrestricted… Perform network operations.”
- Therefore: **all network communication goes through an external process or a Java agent.** No clean workaround in pure Lua.

### 3.3 Directly reusable precedents

| Project | What it proves | Link |
|---|---|---|
| **Streamer Integration Framework (SIF)** B42.13+ | A SP mod reads a `Zomboid/Lua/SIF.txt` file every minute, reads Lua tables `{type="drunk", level=50}` from it, executes them, empties the file. Modules spawn zombies/items/weather. **This is exactly the duel's injection mechanism, already in production.** | workshop 3387594292 |
| **Twitch Integration / Chaos Mod + Twitch (300+ effects)** | Injection of assorted in-game events (named zombies, items, traits, helicopter) driven by an external bot | workshop 2629375490, Reddit |
| **Zombie Spawn Manager** (B41/B42, dev) | Admin-managed spawns with zones, radii, counts, outfits, walk types, runtime behaviour controller. Also shows **B42's limits**: no reliable public setters left for per-zombie sight/hearing/memory/strength | workshop 3759164356 |
| **ZombieBuddy** (B42+, Java 25) | Java agent framework (ByteBuddy): runtime patching, **exposing Java classes to Lua**, JARs loaded from mods, Windows installer + `-javaagent` on Linux | github zed-0xff/ZombieBuddy |
| **pzJBridge** (42.21, server + client) | Client/server Java bridge packaged with an installer, extension version checking, API for modders — proof that a maintained Java bridge is realistic | github rocky6777/pzJBridge-server |
| **PZHook** (B41) / **ZomboidForge** | Historical Java hooking; ZomboidForge = reflection from Lua. Not retained for new work: fragile and contrary to B42 practice | github Olipro/PZHook |

### 3.4 External control of a dedicated server

- **RCON** operational (port = game port + 1) with a genuine list of world commands: `createhorde <count> <user>`, `chopper`, `gunshot`, `lightning`, `alarm`, `startrain`/`stoprain`, `addvehicle`, `additem`, `addxp`, `teleport`, `save`, `reloadlua`, `changeoption`.
- Linux dedicated server: console via a **FIFO `zomboid.socket`**, used by the Docker images.
- Limit: RCON gives you **coarse** control (a horde around a player), not “2 specific zombies 40 tiles away”. For fine control you need Lua on the server side.

---

## 4. The three possible architectures

### A. Two single-player games + file IPC + bridge agent (recommended for the MVP)

```
   PC player A                        PC player B
┌──────────────────┐              ┌──────────────────┐
│ PZ SP (save A)   │              │ PZ SP (save B)   │
│ duel mod Lua     │              │ duel mod Lua     │
│  outbox.txt ─────┼──┐        ┌──┤───── inbox.txt   │
│  inbox.txt  ◄────┼──┤        ├──┤───── outbox.txt  │
└──────────────────┘  │        │  └──────────────────┘
                  ┌───▼────────▼───────┐
                  │ duel agent (Python)│ ← shared clock,
                  │ rules, caps, log   │   arbitration, scores
                  └──────────┬─────────┘
                             │ outbound WebSocket/TCP (VPS relay)
```

- Lua mod: writes every fact to `outbox.txt` (1 line = 1 event), reads `inbox.txt` at short intervals (0.5-2 s), applies, empties.
- Local agent (Python): tails the file, talks to the relay, applies the rules, writes the inbox, keeps the **match clock** and the score.
- No network mod, no port to open if the agent connects outbound to a relay; LAN possible without a relay.
- **Realistic latency: 0.5 to 3 s** (SIF does 60 s by design choice, not by constraint).
- Drawbacks: two mods to install, no anti-cheat possible (the player owns their save), the agent must be packaged (installer).

### B. Two dedicated servers + server-side Lua mod + RCON

- Time **natively synchronous**: two servers started together, same `SandboxVars` (including `DayLength`), no pause possible in MP → zero drift as long as both are running.
- Injection via batched RCON commands (“17 kills → `createhorde 34` near the opponent”).
- No client mod → simplified installation for the players.
- Drawbacks: need to host/operate 2 servers (RAM/CPU ×2), coarse control, and **B42 MP mods are still temperamental** (servers that fail to load Workshop mods, Lua reload closing the session — 42.20 reports). RCON commands have no fine spatial granularity.
- Relevant if the goal becomes a **service** (matchmaking, ranked, hosted worlds) rather than a tool among friends.

### C. Java agent (ZombieBuddy / pzJBridge) inside the client

- Real socket/HTTP in the game, per-frame events, precise spawns, native HUD.
- This is the “finished product” architecture.
- Drawbacks: heavy installation (agent, JAR approval, `-javaagent` on Linux, Windows installer), huge security surface, **and above all a more complicated Workshop distribution** (a Java mod does not install in one click). +3 to 4 weeks and heavier user support.

### Recommendation

**A for the MVP**, with the agent already isolating the rules and clock logic: if the game is fun, we replace the file transport with C without rewriting either the rules or the protocol. B only comes in if we want a hosted/ranked mode.

---

## 5. Hard point no. 1: time synchronisation

### What the game provides

- `getGameTime():getWorldAgeHours()`: double, world age in hours → **perfect clock metric** (already used by the vanilla Last Stand mod).
- `getGameSpeed()` / `setGameSpeed(int)`: 0 = pause, 1 = normal, 2-4 = speed-ups (used by `SpeedControlsHandler.lua`, `ISWorldMap.lua`, `ISPostDeathUI.lua`).
- `isGamePaused()`.
- `getGameTime():getMinutesPerDay()` / `setMinutesPerDay()`: **fine catch-up lever** (to be validated at runtime).
- `GameTime:setTimeOfDay()`, `setDay/setMonth/setYear`: absolute correction possible, but visible (hour jump) → to be reserved for start/resume.

### Proposed mechanism: master clock + dead band

1. The agent (or the host) keeps the match clock: `T_target = T0 + (elapsed_time_outside_pause) / ratio`, with `ratio` derived from `DayLength` (identical on both sides).
2. Each mod compares local `worldAgeHours` to `T_target` every 0.25-0.5 s:
   - gap within ±5 s of game time → do nothing (dead band, avoids stutter);
   - ahead → `setGameSpeed(0)` until caught up;
   - behind → let it run (or `setGameSpeed(2)` if the rule allows it).
3. **Local pause = shared pause**: in SP, opening the menu pauses the game. The mod signals “pause”; the agent freezes the target clock for both. Otherwise the player who is playing gains ground and gets frozen in turn → incomprehensible behaviour.
4. **Offline / crash**: match paused for both, with a grace delay (e.g. 2 min) then a verdict (abandon = defeat, or unlimited freeze in “friendly mode”).
5. **Death**: `OnPlayerDeath` on side A → end of match (or spectator “ghost mode” with reduced events).
6. **Save resume**: on load, realign the clock on the target; if the gap is huge (save reloaded another day), offer “resume the match” or “invalidate”.

### Synchronisation risks

- `setGameSpeed(0)` in a loop can conflict with vanilla resets (timed actions, UI, vehicle dashboard) → enforcement must be continuous and idempotent, with a short cooldown after legitimate pauses.
- In **MP**, `setGameSpeed` is meaningless: synchronisation goes through simultaneous server start (archi B), not through gating.
- Performance: reading/writing a small file every 0.5 s is negligible; an `OnTick` that writes on every tick is not (ban it, buffer).

---

## 6. Hard point no. 2: designing the duel

### The mechanisms that make the “Tetris-style duel” readable

- **Batched garbage**, not blow by blow: accumulate kills over 10-15 s of game time then send a packet (“17 kills → 34 zombies”). It is more readable, safer for performance, and closer to the Tetris model.
- **Announce the arrival**: siren/text before the pop, 5-10 s of warning, otherwise it is unfair.
- **Safe spawn distance**: 30-60 tiles, never in the immediate field of view, never in a building where the player is sleeping.
- **Caps and diminishing returns**: garbage ceiling per minute, decaying multiplier, otherwise a knife slaughter drowns the opponent in 90 s.
- **Anti-snowball**: the player in trouble receives a positive event (medical crate, weapon, lenient weather); the leading player suffers a penalty (helicopter, alarm).
- **Combos**: chaining kills without being hit increases the ratio; being injured resets the counter to zero → it pushes towards risk, not camped farming.
- **Levers other than zombies**: sprinters (1 per 10 kills), noise (gunshot/alarm near B), weather (fog/rain), item (crate), electricity/water, helicopter. This is what gives variety without heavy extra code — the commands/APIs exist.
- **Victory conditions**: (a) sudden death (the first to die loses), (b) score over N game days, (c) objective (reach X / hold a zone). (a) is the simplest and the most dramatic.

### The structural fairness problem

Two separate saves = two different worlds: loot, vehicles, zombie distribution, buildings. No shared “seed” in PZ. Therefore:

- same build, same mods, **same `SandboxVars`** (including `DayLength`, population, loot rarity, start date/time, difficulty);
- same starting point and same start time → conditions perceived as fair;
- accept loot asymmetry and compensate for it with the score (not with the world);
- in a serious competitive mode: switch to archi B (hosted worlds, players with no control over the files).

### What is impossible / must not be promised

- No “mirror world”: a save cannot be cloned in real time.
- No anti-cheat in SP: the player can edit their save, enable debug, install a cheating mod. A duel among friends, yes; a leaderboard, no.
- No vision of events at a distance: PZ simulation is local around the player. “He killed a zombie at the other end of the map” is not observable — and it is not necessary: everything plays out on aggregated local counters.

---

## 7. Prerequisites

### Technical

- PZ **42.20.x** on both sides, **identical build**, **identical mod list** (exact mirror), same `SandboxVars`.
  Note: the local installation is on **42.21.0 unstable** (verified when the dedicated server started, cf. `04-spike-0-evidence.md`). Decide explicitly stable or unstable, and do not mix the two.
- The duel mod on Workshop (or local for prototyping) + the bridge agent (Python/Go, one binary per OS).
- Transport: direct LAN, or a relay (small VPS) if the players are behind NAT. An agent that connects outbound avoids all port forwarding.
- If the same PC for two players: 2 Steam accounts, 2 instances with separate `-cachedir` (argument supported by the launcher), ~12-16 GB of RAM for two clients + agent. Split-screen exists but it is **a single world**, so out of scope for the duel.
- Reference machine measured here: Ryzen 7 7700, 31 GB RAM, RX 7900 XT, PZ on 42.20.4, JVM at 3 GB → two simultaneous instances are comfortable.

### Human / process

- Two regular testers (the biggest bottleneck: balancing requires dozens of real duels).
- A compatibility pass at every B42 point release (42.21, 42.22…).
- Workshop page + installation documentation (2 components to install = source of support).

---

## 8. Workload estimate

| Phase | Content | Days (range) |
|---|---|---|
| 0. Validation spikes | Test mod: `setGameSpeed` effective in game, file IPC, `addZombiesInOutfit` at the player's coordinates, `OnZombieDead` frequency, polling latency | 2-3 |
| 1. Core mod | Event capture, serialisation, inbox/outbox, spawns, HUD (score, clock, alert) | 6-9 |
| 2. Bridge agent | Protocol, master clock, dead band, caps, reconnection, match logs, config | 5-7 |
| 3. Rules & balancing | Conversion table, combos, anti-snowball, victory conditions, test sessions | 4-8 (iterative) |
| 4. Packaging & distribution | Installer, docs, Workshop page, error messages, Linux/Windows compatibility | 3-5 |
| 5. QA / hardening | Edge cases: death, crash, reload, mod changes, B42 patch, 2 instances | 5-10 |
| **Total MVP** | | **25-42 d** |

Reading: **~5-8 weeks** solo at serious part-time, **4-5 weeks** full-time. Archi C (Java) adds 3-4 weeks and heavier user support. A hosted/ranked mode (industrialised archi B): 3-6 months.

Main uncertainty: the time spent in phase 3 (it is game design, not code) and the regressions at every B42 patch.

---

## 9. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Lua has no networking | Blocking if ignored | Archi A (file IPC) or C (Java) from the start — do not try to “do sockets in Lua” |
| B42 API churn (42.21/42.22) | Breaks at every patch | Isolate game calls in a `compat/` layer, test on stable 42.20.x, watch the modding notes |
| `setGameSpeed` conflicting with the vanilla UI | Stutter, controls that “stick” | Dead band, cooldown after action/pause, idempotent enforcement |
| Unfair spawn (behind the player, in a room, during sleep) | Frustration, absurd deaths | Warning, minimum distance, blacklist of contexts (sleep, driving), cap per minute |
| Incomparable worlds | Feeling of cheating | Identical sandbox + identical start + score rather than comparison of wealth |
| Corrupted save / crash during a spawn | Match loss | Backup before the match, buffered spawn, clean “invalid match” mode |
| No anti-cheat in SP | Uncredible leaderboard | Assume “among friends”; archi B if competitive |
| Two components to install | Abandonment at installation | Single installer, automatic detection of the Zomboid folder, clear diagnostics |

---

## 10. What I would do first: 3-day spike, measurable go/no-go

A single throwaway mod + a Python script, on the local 42.20.4 save:

1. **Day 1 — Clock.** A mod that, every 0.5 s, reads `getWorldAgeHours()` and forces `setGameSpeed(0)` as soon as the lead exceeds the target. Criterion: both instances stay within ±5 s of game time for 20 minutes, with no perceptible stutter and without breaking the pause menu.
2. **Day 2 — Injection.** `addZombiesInOutfit(x, y, z, n, nil, nil)` at 40 tiles from the player, triggered by an external file. Criterion: 20 consecutive spawns, no crash, no zombie behind the player, stable FPS.
3. **Day 3 — Full loop.** A Python script that tails one instance's outbox, applies “1 kill → 2 zombies”, writes the other's inbox. Criterion: 15 minutes of end-to-end duel between two instances, measured latency < 2 s, exact kill counter.

If all three pass: the concept is technically validated, we move to the real mod. If Day 1 fails (the most likely of the three failures), we switch to archi B or C before writing any gameplay.

---

## 11. Decisions to settle

1. **MVP architecture** — (a) SP + file IPC + agent *(rec.)* · (b) two dedicated servers + RCON · (c) Java agent from the start · (d) hybrid A now, C later.
2. **Network transport** — (a) LAN only for the MVP · (b) VPS relay (no port to open) *(rec.)* · (c) port forwarding at the host's place.
3. **Sync rigour** — (a) strict lockstep (±2 s, micro-pauses) · (b) ±5-10 s dead band *(rec.)* · (c) warning only, no freeze.
4. **First milestone scope** — (a) only “kill → zombies”, 1 map, 1 victory condition *(rec.)* · (b) 3 event types · (c) the full catalogue from the start.
5. **Victory condition** — (a) sudden death *(rec.)* · (b) score over N days · (c) survival/zone objective.
6. **Distribution** — (a) private among friends (local folder, no Workshop) *(rec. for the MVP)* · (b) public Workshop · (c) Workshop + signed installer.

---

## Sources

- PZwiki, *Build 42* — stable/unstable versions, dates: https://pzwiki.net/wiki/Build_42
- PZwiki, *Game time* — `getGameTime()`, `getMinutesPerDay`: https://pzwiki.net/wiki/Game_time
- PZwiki, *Multiplayer* — online MP since 42.13: https://pzwiki.net/wiki/Multiplayer
- Javadoc 42.x, `LuaManager.GlobalObject` — `setGameSpeed`, `getGameSpeed`, `isGamePaused`, `addZombiesInOutfit`, `getFileWriter`: https://demiurgequantified.github.io/ProjectZomboidJavaDocs/zombie/Lua/LuaManager.GlobalObject.html
- Javadoc 42.x, `GameTime` — `getWorldAgeHours`, `setTimeOfDay`, `setMinutesPerDay`: https://demiurgequantified.github.io/ProjectZomboidJavaDocs/zombie/GameTime.html
- PZEventDoc, *Events* — `OnZombieDead`, `OnHitZombie`, `OnPlayerDeath`, `OnTick`, `EveryTenMinutes`: https://github.com/demiurgeQuantified/PZEventDoc/blob/develop/docs/Events.md
- Local checks in the installed game (42.20.4): `media/lua/client/ISUI/SpeedControlsHandler.lua` (`setGameSpeed(0..4)`), `media/lua/client/Tutorial/Steps.lua` (`addZombiesInOutfit(x,y,z,n,outfit,…)`), `media/lua/client/LastStand/WinterIsComing.lua` (`getWorldAgeHours()`)
- Steam Workshop, *[B42.20.4+] Streamer Integration Framework* — file IPC `Zomboid/Lua/SIF.txt`, 60 s polling, event modules: https://steamcommunity.com/workshop/filedetails/?id=3387594292
- Steam Workshop, *Zombie Spawn Manager* — admin spawns, zones, B42 per-zombie limits: https://steamcommunity.com/sharedfiles/filedetails/?id=3759164356
- GitHub, *ZombieBuddy* — Java agent, Java 25, network access for Java mods, JAR approval: https://github.com/zed-0xff/ZombieBuddy
- GitHub, *pzJBridge-server* — 42.21 client/server bridge, installer, Workshop extensions: https://github.com/rocky6777/pzJBridge-server
- GitHub, *Zomboid-Modding-Guide* — Kahlua, no `io.*`/`os.*`, no reflection: https://github.com/FWolfe/Zomboid-Modding-Guide/blob/master/api/README.md
- PingPlayers, *RCON commands* — `createhorde`, `chopper`, `gunshot`, `lightning`, `alarm`, `startrain`, `additem`: https://pingplayers.com/knowledgebase/project-zomboid/rcon-commands-and-how-to-use-them
- The Indie Stone forums, *getFileWriter — relative / direct path* — resolution to `Zomboid/Lua`: https://theindiestone.com/forums/topic/14698-getfilewriter-relative-direct-path-b323/
- Shockbyte / RespawnHost, state of B42 stable with MP (secondary sources, consistent with PZwiki): https://shockbyte.com/blog/project-zomboid-build-42-multiplayer
