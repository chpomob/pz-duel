# Spike 0 — execution evidence (04/11/2026)

*English — [version française](../fr/04-spike-0-preuves.md)*

Objective: prove the "ops" half of architecture B without a player: starting a dedicated server, loading a server-side Lua mod, file IPC in both directions, the real configuration surface.

Machine: Ryzen 7 7700, 31 GB RAM, Linux. Game installed at: `/home/chpo/.steam/steam/steamapps/common/ProjectZomboid/projectzomboid`.

---

## 1. What was executed

```bash
# headless server from the client install (no need for the separate server package)
./start-server.sh -servername duelspike -port 17261
```

Result:

```
LOAD : Loading RakNet64...
LOAD : Loading ZNetNoSteam64...
LOG  : server name is "duelspike"
LOG  : *** SERVER STARTED ****
LOG  : server is listening on port 17261
LOG  : version=42.21.0 4a0e9546ec demo
```

Startup in ~5 s after world generation, ~2048 MB of heap, no window, no Steam (`-Dzomboid.steam=0` already in the script).

**Important correction: the installed version is 42.21.0 (unstable), not 42.20.4.** The profile's `console.txt` dated from September (42.20.4); the server is what counts.

---

## 2. Server-side Lua mod loaded

Location and structure required by B42 (versioned layout, confirmed by comparing with the installed Workshop mods):

```
~/Zomboid/mods/duelspike/mod.info                 # root manifest
~/Zomboid/mods/duelspike/42.20/mod.info           # version variant (versionMin=42.20.0)
~/Zomboid/mods/duelspike/42.20/media/lua/server/DuelSpike.lua
```

and in `~/Zomboid/Server/duelspike.ini`: `Mods=duelspike`.

A mod placed flat (`~/Zomboid/mods/<id>/media/...` with no version folder) is **not** found:

```
WARN : Mod at ZomboidFileSystem.loadModAndRequired> required mod "DuelSpike" not found
```

This is a packaging trap to remember: without the version subfolder, the mod is silently ignored.

---

## 3. File IPC — both directions

The server writes to and reads from **`~/Zomboid/Lua/`** (the same folder as on the client side, resolved relatively).

File written by the mod (outbound path):

```
init age=2.0922 ver=42.21
tick=300 age=2.2253
tick=600 age=2.3586
init age=2.4657 ver=42.21
in lines=2 last=from_referee 2 spawn 3
```

The `in lines=2 last=...` line = the mod re-read a file **written by an external process** (two `from_referee ...` lines dropped by the shell) at the time of its initialisation. Both directions of the transport are therefore proven, with no client, no network, no Java mod.

`OnTick` does fire on the server side, and `getGameTime():getWorldAgeHours()` is readable (2.09 → 2.47 h of world over the session).

---

## 4. Real configuration surface (`duelspike.ini` as generated)

```
PauseEmpty=true          # default: time stops when nobody is connected
UPnP=true
RCONPort=27015           # default 27015, NOT "game port + 1"
RCONPassword=            # empty by default
Mods=
WorkshopItems=
```

Two corrections relative to the initial study:
- **RCON has its own port** (27015 by default). Two servers on the same machine must therefore use two distinct `RCONPort` values (27015 / 27016), otherwise the second one fails.
- `PauseEmpty` is `true` by default: set it to `false` on both servers, otherwise a player who drops freezes their world and not the other one (immediate drift).

---

## 5. Observed behaviour still to be measured with two players

With zero players connected, the server advances but **not at the nominal rate**: ~0.37 h of game time over ~4 min real, whereas `DayLength=4` should give 0.27 h/min. The tick loop seems to throttle when the game is empty (the `tick=300/600` counters then stop).

Practical consequence: do not count on empty servers to stay in sync. The match clock must start **when both players are connected**, and drift monitoring must be active during the match, not before.

To measure at the next spike (with two clients): real ticks/s with two players, drift over 20 min, and the behaviour of `PauseEmpty=false` when a player disconnects.

---

## 6. What remains to be proven (spike 1, with a player)

1. `OnZombieDead` + `getAttackedBy()`: correct attribution 100% of the time over 100 kills.
2. `addZombiesInOutfit(x, y, z, n, nil, nil)` on the server side: spawn visible on the client side, no crash, stable FPS.
3. Craft: `Recipe_OnCreate` / `OnProcessAction` — does the signal exist on the server side in multiplayer?
4. Player disconnect/reconnect: effect on the clock and on events.

---

## 7. Artefacts left on the machine

- Spike mod: `~/Zomboid/mods/duelspike/` (to be deleted or promoted to a test harness).
- Spike server: `~/Zomboid/Server/duelspike.ini`, world `~/Zomboid/Saves/Multiplayer/duelspike` (5.6 MB).
- Logs: `~/.hermes/cache/scratch/pzspike/`.

Cleanup: delete these three paths (server stopped); nothing else was modified.
