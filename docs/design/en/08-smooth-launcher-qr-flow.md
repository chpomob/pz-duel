# Smooth flow: launcher, settings window, QR invite

*English — [version française](../fr/08-flow-fluide-lanceur-qr.md)*

Goal: "I start my VS game, it brings up two servers, it shows me my settings, it gives me a QR code to show my friend." Everything that can go through the Steam Workshop goes through the Steam Workshop.

---

## 1. What the test just proved (04/11/2026)

Two dedicated servers started simultaneously on the same machine:

```
*** SERVER STARTED ****  server is listening on port 17261
*** SERVER STARTED ****  server is listening on port 17271
```

And both mods write into the **same shared folder** `~/Zomboid/Lua/`, into distinct files:

```
~/Zomboid/Lua/duel_A_out.txt : init side=A age=2.0000 ver=42.21
~/Zomboid/Lua/duel_B_out.txt : init side=B age=2.0000 ver=42.21
```

Three major consequences:

1. **The link requires no external process**: both servers, started under the same system account, share `~/Zomboid/Lua/`. Server A writes, server B reads. The transport is inside the game.
2. **The clocks start identical** (2.0000 h on both sides) when the worlds are created in the same session — temporal synchronisation is a fact of configuration, not a mechanism to be driven.
3. The "director" (rules, score, timer) can live **in server A's mod**, which reads B's outbox and writes its inbox. No more Python arbiter for the transport or for the rules.

What remains impossible for a Lua mod: `os.*`, sockets, process launching. So **starting two servers, restoring saves and opening a window remain the responsibility of an external launcher.**

---

## 2. What the Workshop can carry — and what it cannot

| Element | Workshop? | Detail |
|---|---|---|
| Server mod (`duelcore`) | **yes** | published as a mod, required by the server |
| Client mod (`duelhud`) | **yes** | the server pushes `WorkshopItems`, the client subscribes automatically |
| Presets and tables (`relations.yaml`) | **yes** | files embedded in the mod; the host can load others |
| Documentation, changelog | **yes** | Workshop page |
| Starting two servers | **no** | no mod can create a process |
| Restoring worlds between matches | **no** | disk access outside the Lua folder |
| Settings window / QR | **no** | no UI outside the game |

Conclusion: **the Workshop carries the whole game; a single launcher carries the mechanics.** It is the only piece outside the Workshop, and it can fit in an executable of a few MB distributed via GitHub (or copied into the mod folder for advanced users).

---

## 3. The flow

```
[1] The player double-clicks "PZ Duel"
        │
[2] The launcher: checks the PZ install, the existence of the Workshop mods,
    starts server A (17261) + server B (17271), waits for "SERVER STARTED" ×2
        │
[3] Settings window (local): nickname, preset, duration, DayLength,
    mod list (read-only, from the preset), preview of the table in French,
    "Start the duel" button
        │
[4] Lobby screen: QR code + 5-character match code,
    readable contract, status "waiting for the opponent…"
        │
[5] The guest scans the QR
        │
[6] The guest's client joins; PZ offers them the required Workshop mods,
    they subscribe (1 Steam click), connect
        │
[7] Both players connected → 10 s countdown → match
```

Going back is possible at every step: if a server does not start, the launcher shows the reason (port taken, missing mod, not enough RAM) instead of a black screen.

---

## 4. The invite: QR and link

QR content (everything is local, no third-party service, no account):

```
pzduel://join?host=82.64.x.x&port=17261&pw=K7QX2&match=8f3a
```

- If the launcher is installed on the guest's machine, it registers the `pzduel://` protocol → the scan opens the launcher, which starts PZ with the right arguments.
- Otherwise, the QR points to a local page served by the launcher (`http://<ip>:8787/join/8f3a`) which displays IP, port, password and a "copy" button, plus the 3 clicks for the direct connection.

**Automatic connection verified in the official documentation of launch parameters**: `+connect {ip}:{port}` and `+password {password}` (equivalents of `-Dargs.server.connect`). The launcher can therefore start PZ directly in the right game, with no manual steps.

CGNAT case: the launcher detects that the public IP does not answer on the port and offers either a tunnel or a remote server. To be handled in v2, but the screen must say so clearly rather than letting the guest fail.

---

## 5. Smoothness: the details that make the difference

1. **No manual mod installation**: both mods are on the Workshop; the server imposes the list; the client subscribes automatically when joining.
2. **No IP entry**: QR → launcher → `+connect` → `+password`.
3. **No port configuration**: 17261/17271 and RCON 27015/27016 are chosen by the launcher, with conflict detection.
4. **Contract visible before accepting**: the guest sees the table in French in their lobby (or on the QR page) before saying yes.
5. **One-click rematch**: the servers stay up, the snapshots are restored, the QR remains valid. The match code does not change.
6. **Non-admin host**: the admin password is generated and kept by the launcher, never shown to the player.
7. **A single component to install on the guest side** (the launcher, optional but recommended); zero on the host side beyond Steam.

---

## 6. What the launcher does exactly (and nothing else)

| Step | Action | Failure → message |
|---|---|---|
| Preflight | finds the PZ install, Java, free RAM, free ports, present mods | "port 17261 busy", "2 GB free required" |
| Start | 2 × `start-server.sh`/`bat` with `-servername`, `-port` and **`-adminpassword`** | "server A did not start: see A.log" |
| Contract | reads the two `.ini` files, compares `Mods=`/`WorkshopItems=` with the preset | "different mods between the two servers" |
| Window | local UI (embedded web page or native window) | — |
| Invite | QR + link + match code | "public IP unreachable: CGNAT likely" |
| Match | timer, score, log, clean shutdown | "clock drift > 60 s: match invalidated" |
| Reset | restore of the stopped server snapshots | "snapshot missing: new world" |

No game rule inside it: the table is evaluated by server A's `duelcore` mod.

---

## 7. Two modes, one single binary

The launcher does not need to be complete to be useful. Two modes, chosen by argument:

| Mode | Usage | What it does | What it does not have |
|---|---|---|---|
| `duel admin` | **MVP**, host only | starts the 2 servers (`-adminpassword`), waits for `SERVER STARTED` ×2, applies the table, tails the outboxes, writes the inboxes, exposes a **local interface** (status, config, progress, lobby + QR, logs), clean shutdown on Ctrl-C | nothing: the UI is described in `09-in-game-ui.md` |
| `duel play` | everyday | starts PZ with `+connect IP:PORT +password pw` (+ the mod if needed) | nothing else |

Important point: **the launcher never installs a mod.** The server declares its `WorkshopItems`, the client subscribes when joining. "Start PZ with the right mod" is therefore automatic; the only exception is an offline test.

The admin mode is **not** a console: it opens a local interface (`09-in-game-ui.md`).

## 8. Where the director lives for the MVP

Two possible implementations of the same protocol:

| | Director in Python (launcher) | Director in Lua (server A's mod) |
|---|---|---|
| Iteration speed | high (edit, relaunch, log) | low (server restart on every change) |
| Log / debug | native | to be written |
| Zero installation | no (the launcher already exists) | yes (everything Workshop) |
| Risk | none on the game | depends on the Lua APIs in MP |

Recommendation: **director in Python for the MVP**, protocol identical to the one in the spec, and Lua porting only if the "zero installation" need becomes a priority. The launcher exists anyway to start the servers, so the rules logic comes for free there.

---

## 9. Open decisions

1. **Launcher tech** — (a) packaged Python (fast, cross-platform, ~15 MB) *(reco)* · (b) .NET/Avalonia app · (c) Electron (heavy).
2. **Settings UI** — (a) local web page served by the launcher *(reco: same rendering as the QR and the contract)* · (b) native window.
3. **Launcher distribution** — (a) GitHub Releases + the Workshop for the mods *(reco)* · (b) everything in the Workshop mod folder (Steam downloads the files but does not run the exe).
4. **`pzduel://` protocol** — (a) registered at installation, one-click QR *(reco)* · (b) web page with copy/paste.
5. **Guest without launcher** — (a) supported (web page + 3 clicks) *(reco)* · (b) launcher mandatory for both.
