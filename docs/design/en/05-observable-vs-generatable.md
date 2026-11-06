# Face A / Face B — what we can observe, what we can generate

*English — [version française](../fr/05-observable-vs-generable.md)*

Inventory verified against primary sources, not on forum claims.

**Sources actually read for this document**
1. Documentation of the 260 Lua events 42.x (`PZEventDoc`, develop branch) — with the `(Client)` / `(Server)` markings.
2. Javadoc 42.x of `LuaManager.GlobalObject` — global functions callable from Lua (list extracted by script: 47 `add*/spawn*/create*` functions).
3. Cloned code of **Chaos Mod** (`clixff/ChaosMod-ProjectZomboid`, MIT, 462 effect files) — full inventory in appendix [`05-appendix-chaos-462.md`](../en/05-appendix-chaos-462.md).
4. Cloned code of **log-extender** (`openzomboid/log-extender`) — B41 and B42 loggers, client and server.
5. Cloned code of **zomboid-mod-analytics** (`spoutnickgp`) — analytics **on the server side** (`media/lua/server/`).
6. **SIF** + **SIF - Example Events** (Workshop) — 48 file-driven injection modules.
7. My spike 0: headless 42.21 dedicated server, Lua mod, file IPC ([`04-spike-0-evidence.md`](04-spike-0-evidence.md)).

---

# FACE A — what is observable

## A1. Proven on the server side (therefore usable as an authoritative source)

| Signal | API / event | Evidence | Duel usage |
|---|---|---|---|
| Death of any character | `OnCharacterDeath` | `zomboid-mod-analytics` records it from `media/lua/server/` with a handler that logs the death type | kills, end of match |
| Hits dealt / taken | `OnHitZombie`, `OnWeaponHitCharacter(wielder, target, weapon, damage)` | same server mod, handler that reads `damage` and `target` | damage, combos, "hit" |
| Player's cumulative kills | `IsoPlayer:getZombieKills()` | analytics writes it to its server log | score |
| Hours survived | `IsoPlayer:getHoursSurvived()` | analytics | score, end of match |
| World clock | `getGameTime():getWorldAgeHours()` | measured in spike 0 on server 42.21 | match clock |
| World sounds | `OnWorldSound` | doc 42.x (not marked client) | "noise" pressure |
| Containers filled | `OnFillContainer` | doc, marked `(Server)` | progression |
| Construction / destruction | `OnWeaponHitThumpable` (`Server`), `OnObjectAdded`, `OnTileRemoved` | doc | "noise / collapse" pressure |
| Vehicle mechanics | `OnMechanicActionDone` | doc, not marked client | "wreck" pressure |
| Explosions | `OnThrowableExplode` | doc | "fire" pressure |
| Fire | `OnNewFire`, `OnGridBurnt` | doc | propagation |
| Weather | `OnWeatherPeriodStart`, `OnWeatherPeriodStage` | doc, marked `(Server)` | atmosphere / pressure |
| Zombie spawn | `OnZombieCreate` | doc | **anti-loop**: do not convert our own spawns into a threat |

## A2. Proven on the client side only

| Signal | API / event | Evidence | Consequence |
|---|---|---|---|
| XP gained | `AddXP` | doc `(Client)` + `log-extender`'s `PlayerLogger` is a client logger | **excluded** from authoritative sources |
| Level up | `LevelPerk` | doc `(Client)` + same client logger | idem |
| **Craft completed** | `ISHandcraftAction` (hook on `ISBaseTimedAction.perform`) | `log-extender`'s `CraftLogger.lua` is explicitly a **client** logger; no server-side craft logger in the project | **the central mechanism of the duel is not observable on the server through this path** |
| Pickup / inventory | object loggers (B41 client) | `log-extender` | to be derived otherwise |
| Camera, UI, local sounds | `(Client)` events | doc | client by nature |

## A3. Contradictory or unproven — to be settled in spike 1

| Case | What the doc says | What the code does | Verdict |
|---|---|---|---|
| `OnWeaponHitCharacter` | `(Client)`: "when a non-zombie character is hit by an attack from a **local player**" | `zomboid-mod-analytics` records it in a **server** file | outright contradiction: either the doc is incomplete, or the mod receives nothing on the server side. **To be tested** |
| `OnEnterVehicle` / `OnExitVehicle` | `(Client)` | analytics records them on the server side | idem, to be tested |
| `OnBeingHitByZombie` | **absent from the doc** | analytics records it with an empty handler (`print`) | probably nonexistent or dead: do not depend on it |
| Craft on the server side | no documented server event | `Recipe_OnCreate` is an "umbrella" function called in the recipe scripts, therefore on the client side | remaining lead: `OnProcessAction` (`Server`). Otherwise: **inventory diff** |
| Skills on the server side | `AddXP` is client | `IsoPlayer:getPerkLevel()` exists in the javadoc; in MP the player data lives on the server side (`players.db`) | probably readable directly, to be tested |

## A4. Confirmed blind spots (to design around, not against)

1. **No connection/disconnection event** in the 42.x doc (`OnPlayerConnect` / `OnPlayerDisconnect` are absent). The "disconnection = forfeit after 2 min" policy must be based on **polling `getPlayers()`** or on the server log.
2. **No server-side "craft completed" event** → crafting must be reconstructed (see below).
3. **No server-side "object picked up" event.**
4. The simulation is local around the player: nothing observable at a distance — no consequence here, the two players do not cross paths.

## A5. Working around crafting: inventory diff

Since the server owns the player's inventory in MP (B42 introduced server ownership of objects), we can:

- sample the inventory every 2-5 s on the server side (`player:getInventory()`), count by object type;
- detect the appearance of objects **whose recipe is known** (table `output → recipe`);
- emit a server-authoritative `craft` event.

Cost: 1-2 days, possible false positives (loot picked up, object given) → filtering by "crafted object only" and by signed delta. It is imperfect but **not falsifiable by the client**, unlike a client log.

---

# FACE B — what is generatable

## B1. Server, authoritative (global API + RCON)

| Threat | Call | Evidence |
|---|---|---|
| Precise pop | `addZombiesInOutfit(x, y, z, n, outfit, femaleChance, …, health)` | full signature used by Chaos (`ChaosZombie.SpawnZombieAt`) and by the vanilla tutorial |
| Area pop | `addZombiesInOutfitArea(x1, y1, x2, y2, z, n, outfit, …)` | javadoc + vanilla tutorial |
| **Horde directed by coordinates** | `createHordeFromTo`, `createHordeInAreaTo` | javadoc 42.x — far more precise than the RCON `createhorde` (which only targets a player) |
| Special zombies | `createZombie`, `addZombieSitting`, `addZombiesEating`, `addZombiesInBuilding` | javadoc 42.x |
| Vehicles | `addVehicle`, `addAllVehicles`, `addCarCrash`, `addAllBurntVehicles`, `addAllSmashedVehicles` | javadoc 42.x |
| World atmosphere | `addSound`, `addBloodSplat`, `createRandomDeadBody`, `addPhysicsObject` | javadoc 42.x |
| Progression | `addXp`, `addXpMultiplier` | javadoc 42.x |
| Animals | `addAnimal` | javadoc 42.x |
| RCON commands | `createhorde`, `chopper`, `gunshot`, `lightning`, `alarm`, `startrain`/`stoprain`, `additem`, `addvehicle`, `removezombies`, `save`, `quit` | server doc + startup test in spike 0 |

## B2. Client — the "experience" channel (proven at scale)

- **Chaos Mod: 462 effects**, MIT licence, **single-player only** (README: "Is multiplayer supported? No."), **411 of the 462 files use `getPlayer()`** and **none** uses `sendServerCommand`.
- **SIF Example Events: 48 modules** driven by file, same transport as our architecture (proof that the file channel is enough to trigger this kind of effect).

Excerpts classified by duel usage (full inventory in the appendix):

| Family | Example identifiers | Effect on a duel |
|---|---|---|
| Perception / tension | `blurry_vision`, `shake_camera`, `force_zoom_in`, `force_zoom_out`, `disable_sounds`, `invisible_characters`, `look_behind_you` | psychological pressure, no lethality |
| Weather | `enable_fog`, `temporary_night`, `toxic_rain`, `hurricane`, `meteor_shower`, `stop_rain` | reduces visibility, changes how risk is read |
| Zombies | `1hp_zombies`, `freeze_zombies`, `all_zombies_are_crawlers`, `zombie_spawner_nearby`, `fill_area_with_zombies`, `zombies_rain`, `electric_zombies` | direct pressure |
| Electricity / water | `turn_power_off`, `turn_water_on` (via `getSandboxOptions():set("ElecShutModifier", …)`) | deprives of light/fridge → real stress |
| Objects | `break_player_weapons`, `food_is_poisoned`, `discharge_batteries`, `drop_item_in_hand`, `duplicate_items` | attacks logistics |
| Vehicles | `disable_vehicle_engine`, `pop_tires_vehicles_nearby`, `explode_nearby_cars` | attacks mobility |
| Player | `drunk`, `add_random_wound`, `cant_stop_shouting`, `add_cigarette_addiction` | temporary penalties |
| NPC | `bounty_on_player_head`, `grove_street_gang`, `hire_mercenaries` | requires NPC mods |

**Porting to MP**: every effect assumes local authority and the local player. In MP the **server** must decide and the **victim's client** must apply (server command → client mod). Perception effects are the easiest (they only touch rendering/sound); those that modify the world must go through B1.

## B3. Impossible / not to be attempted

| Attempt | Why |
|---|---|
| Killing the player through the API | no clean function, and unfair by design |
| Pausing | a notion that does not exist in MP |
| Anti-cheat | the player remains master of their client; only the server is authoritative |
| Setting per-zombie `sight`/`hearing`/`memory`/`strength` | B42 no longer exposes reliable public setters (documented by Zombie Spawn Manager) |
| Modifying the sandbox from the client in MP | the server decides; it must be done on the server side |
| "Mirror world" | no shared seed |

---

# The three threat channels

| Channel | Who applies it | Nature | Implementation cost | Recommended usage |
|---|---|---|---|---|
| **WORLD** | server | spawns, hordes, vehicles, weather, noise, fire | low (proven API) | 70 % of the duel: it is the readable and fair pressure |
| **EXPERIENCE** | victim's client | fog, shaking, zoom, sounds, hallucination | medium (client mod + server command) | 20 %: tension, no lethality |
| **META** | server | `addXpMultiplier`, objects, electricity/water, loot | low | 10 %: comebacks, anti-snowball |

A duel that only uses the WORLD channel becomes a farming contest. It is the EXPERIENCE channel that creates the "Tetris feeling" (the opponent wrecks your screen, not just the world).

---

# Mapping to `rules.yaml` ([03-mechanisms-and-telemetry.md](03-mechanisms-and-telemetry.md))

| Observed mechanism | Channel | Threat implementation | Evidence |
|---|---|---|---|
| Zombie kill | WORLD | `addZombiesInOutfit`, `createHordeInAreaTo`, `createhorde` (RCON) | B1 |
| Damage taken | WORLD + EXPERIENCE | close pop + `shake_camera` | A1 + B2 |
| Construction | WORLD | noise (`addSound`), attracted horde | B1 |
| Vehicle repaired | WORLD | `pop_tires_vehicles_nearby`, `disable_vehicle_engine` | B2 |
| Explosion / fire | WORLD | `createHordeInAreaTo` + `OnNewFire` | B1 |
| Craft (if not observable on the server) | META | rigged object (`food_is_poisoned`), `break_player_weapons` | B2 + workaround A5 |
| Combo / composure | EXPERIENCE | `blurry_vision`, `temporary_night` | B2 |

---

# Consequences for the MVP

- **v1: 6 server mechanisms** (kill, damage, construction, mechanics, explosion/fire, sounds) → 8-10 threats from the WORLD channel, all proven.
- **2 EXPERIENCE threats in v1** (`enable_fog`, `shake_camera`): little code, maximum effect on the duel feeling.
- **Craft**: try `OnProcessAction` in a spike; otherwise inventory diff (1-2 d) — it is the only central mechanism whose observability is not guaranteed.
- **Disconnection**: polling `getPlayers()`, no event.

# To verify in spike 1 (short list)

1. `OnZombieDead` + `getAttackedBy()`: attribution at 100 % over 100 kills.
2. `addZombiesInOutfit` on the server side: spawn visible to the client, no crash.
3. `OnWeaponHitCharacter` / `OnEnterVehicle`: server side, yes or no?
4. `OnProcessAction`: does it see a craft?
5. `getPerkLevel()` on the server side: reliable value?

# Sources

- 42.x event documentation: https://github.com/demiurgeQuantified/PZEventDoc/blob/develop/docs/Events.md
- Javadoc `LuaManager.GlobalObject`: https://demiurgequantified.github.io/ProjectZomboidJavaDocs/zombie/Lua/LuaManager.GlobalObject.html
- Chaos Mod (MIT, 462 effects, SP only): https://github.com/clixff/ChaosMod-ProjectZomboid
- log-extender (client/server loggers): https://github.com/openzomboid/log-extender
- zomboid-mod-analytics (server analytics): https://github.com/spoutnickgp/zomboid-mod-analytics
- SIF: https://steamcommunity.com/workshop/filedetails/?id=3387594292
- SIF - Example Events (48 modules): https://steamcommunity.com/sharedfiles/filedetails/?id=3387596392
- Zombie Spawn Manager (B42 per-zombie limits): https://steamcommunity.com/sharedfiles/filedetails/?id=3759164356
- Local spike 0: [`04-spike-0-evidence.md`](04-spike-0-evidence.md)
