# PZ Duel

*English — [version française](README.fr.md)*

> ⚠️ **Concept prototype — not a single real duel has been played yet.** The servers, the mod and
> the contract are proven against the real game on Linux; everything that needs a player, and Windows
> beyond its CI runners, is still unverified.

Two separate Project Zomboid solo games, linked by a local referee: the players never meet, but
**every action on one side becomes pressure in the other player's world**. One zombie killed on A
spawns two zombies on B. A successful craft on B sends a horde to A.

Think of it as a Tetris-style duel transposed to survival: each world is its own board, and the only
thing that travels is the consequence of what you do on yours.

## Where the idea comes from

The concept — two separate Project Zomboid games linked so that each side's actions become threats on
the other — was described by **Harumate**, a French Project Zomboid streamer, on his channel:
[twitch.tv/harumate](https://www.twitch.tv/harumate). This repository is an independent
implementation of that idea, not affiliated with him; the idea itself is his.

## How it works

```
        ┌──────────────┐                      ┌──────────────┐
        │   PZ server  │  out (JSONL)         │   PZ server  │
        │      A       │ ─────────┐   ┌────── │      B       │
        │  world A     │          │   │       │  world B     │
        └──────────────┘          ▼   ▼       └──────────────┘
                              ┌──────────────┐
                              │   Referee    │  relation table
                              │   (Python)   │  clock, score, journal
                              └──────────────┘
```

- The `duelcore` mod runs on each server, captures what happens there and **publishes** atoms
  (`zombie_kill`, `damage_taken`, `craft`, `player_death`…).
- The referee reads those atoms, applies a configurable **relation table**, and writes commands into
  the opposing world (`spawn_pop`, `hurt_supply`, `weather_rain`…).
- No connection between the two players, no mirrored world: two separate saves, hence different loot
  and vehicles. Fairness lives in the rules and the starting conditions, not in identical worlds.

## Getting started on Windows

> ⚠️ **Not yet tried with the real game on Windows.** The Windows build passes its tests on CI runners,
> but nothing below has been played on a real Windows desktop or against the real game. This is the
> intended path; the [known limits of phase 2](docs/campaigns/phase2/limites-connues.md) say what is
> proven.

You never need a terminal. Both players need Project Zomboid **42.22 or newer** from Steam, on Windows —
the mod declares `versionMin=42.21.0`, so an older build refuses it. The host runs both servers and needs
about 6 GB of free RAM. Nothing else to install: PZ Duel starts the two servers with the JRE the game
ships (`jre64\bin\java.exe`) and uses `StartServer64.bat` when that script is present.

### Host

1. **Download** `pz-duel.exe` from the [Releases page](https://github.com/chpomob/pz-duel/releases).
   There is nothing to install.
2. **Double-click it.** The file is not signed, so **SmartScreen** may show "Windows protected your
   PC". Click **More info**, then **Run anyway**.
3. **Check the Setup card** in the page that opens. For any line marked ✕, use the button offered:
   `Install mod`, `Choose folder` or `Check again`.
4. **Start a duel**: `Configurer un duel`, pick a preset, adjust the settings if you want (see
   [Settings before the start](#settings-before-the-start)), then `Launch duel`.
5. **Answer the Windows Firewall prompt.** Windows may ask whether PZ Duel or the game's server can use
   the network: click **Allow access**. PZ Duel cannot do this for you. If you dismissed the prompt,
   your guest will not get in: open *Windows Security*, *Firewall & network protection*, *Allow an app
   through firewall*.
6. **Invite your opponent** from the lobby: `Copy link`, or `Save launcher` to get
   `pz-duel-join.bat`. The lobby also shows a **verification code**: **read it aloud** to your guest,
   by voice, **separately from the link**, never in the same message as the link or the file. It is
   the only protection against a file replaced on the way. The lobby shows two different secrets,
   each in its own block: the **launcher code** (the start of the `.bat`'s SHA-256), that the guest
   types when their launcher asks for it, and the **server password**, only for a guest who types
   the address by hand in Project Zomboid. **The launcher code is not the server password**: typed
   into the launcher, the password is refused (`This is not the code of this file`).
   The lobby card reads top to bottom in the order you use it: **one primary button** (what you do
   now), then the **launcher code** in large type (what you read aloud — click it to copy it), then
   the link with `Copy link` and `Save launcher` (what you send). The QR code is folded away behind
   *Show the QR code (phone)*: it only helps a phone. The invitation token is no longer shown.
   The primary button reads `Waiting for your opponent…` (greyed out) until your opponent is
   connected: each world is frozen while nobody plays in it (`PauseEmpty=true`), and joining yours
   earlier would let it run alone. As soon as the guest is identified, it becomes `Open the game` and
   joins your own server; once both of you are in, it reads `Starting the duel…`: the duel starts by
   itself.
   **Whoever arrives first waits frozen.** While a player is missing, PZ Duel holds both worlds
   (`hold_time`): the player already in cannot move and the clock does not advance. As soon as both
   players are identified, both worlds are released together (`release_time`), once, just before the
   countdown, and the drift check starts from that release. If a side's game refuses the hold
   (`no_hold_api`) or its mod cannot do it, the lobby says *Start not frozen* and the duel still
   runs, without that lock.
7. **Leave with `Quit`** in the page. If you just close the browser tab, PZ Duel closes by itself
   once nobody has looked at it for `idle_quit_s` (120 s by default, set in `service/runtime.json`;
   `0` turns it off): closing the tab is enough to start that countdown. It **never** happens during
   a launch or a duel — from the checks (`preflight`) to the end of the lobby, then countdown and
   match — even with no tab open: the first real duel was cut short in `preflight` for want of a tab.
   It only fires at rest, after a duel, or on an error screen; the countdown starts over, whole, when
   a duel or a launch ends. An open page keeps PZ Duel running, even in the background, unless the browser puts the
   tab to sleep: then, in the last 30 s, the page warns that PZ Duel is about to close for want of
   anyone watching, and a click anywhere in the page is enough to stay. It closes the same way as
   `Quit`: servers stopped and, if ports were opened, their mappings removed from the router.

If `Launch duel` ends on an error, use `Try again`. If the screen tells you to, quit and reopen PZ Duel.

**Where the files are.** `pz-duel.exe` is a single file: at every launch it unpacks what it carries,
mod included, into a temporary folder `%LOCALAPPDATA%\Temp\_MEI…\`, deleted when it exits normally (a
forced stop can leave it behind). That copy is not the mod the game uses. `Install mod` copies it to
`%USERPROFILE%\Zomboid\mods\duelcore`: that is the installed mod, the folder the game loads.
`%LOCALAPPDATA%\PZDuel` only holds PZ Duel's log (`pz-duel.log`), the `matches` folder and the
single-instance lock (with the port it listens on).

### Guest

1. **Get the `.bat`** on the Windows PC that has the game: open the host's link there, or save the file
   the host sent. The link only works while the host is in the lobby. Scanning the QR code with a phone
   puts the file on the phone, where it is no use.
2. **Check the file before opening it.** Ask the host for the verification code they read aloud. In
   the folder that holds the file, right-click an empty spot, choose **Open in Terminal** (on Windows 10,
   hold Shift while right-clicking to get the same kind of window), and type
   `certutil -hashfile pz-duel-join.bat SHA256`. **Do not open the file if the result does not start
   with that code**: it was replaced on the way. Delete it and ask the host for a new one.
3. **Only then, double-click `pz-duel-join.bat`.** **SmartScreen** may warn here too: **More info**,
   then **Run anyway**. Only do this for a file from someone you trust, checked at step 2. It asks for
   the code again: type it, then press Enter. A launcher that does not ask for it was replaced.
4. It installs the mod, starts the game connected to the host, and waits a few seconds to check that
   the game keeps running. If it cannot find the game, it asks for the folder. If anything fails, or
   the game closes at once, your existing mod is put back as it was and the window stays open. When
   it succeeds, the `duelcore` mod it replaced is not deleted: it is kept in
   `Zomboid\duelcore.previous`, in your user folder, and the window says how to go back to it.

### Two guests (referee mode)

The PC that runs PZ Duel does not have to play. In `Configurer un duel`, **Who plays** offers
`Me and a guest` (the default, everything above) or `Two guests: this computer only hosts and
referees`. The choice travels in the start request (`"mode": "host"` or `"referee"`) and reads back
in `/api/state` under `config.mode` (the top-level `mode` already says simulated or real). A rematch
keeps it.

In referee mode:

- the lobby shows **two complete invitations**, *Player A* and *Player B*, each with its own launcher
  code, link, `Save launcher` (`GET /api/invite/launcher?side=A` or `?side=B`; without `side`, B) and
  server password. Send each one to its player, and read each code aloud to its own player only;
- there is **no game button**: nobody plays on this computer, and `open_game` answers
  `refused, reason: no_local_player`;
- `Open ports` opens **six** mappings, three per side, all described `PZ Duel`: B's as before
  (49271 and 49272 UDP, 49280 TCP by default) and A's, ten lower (49261 and 49262 UDP, 49270 TCP),
  towards A's game ports and A's own share listener (`share_port` + 1, 17281 by default). A failure
  on A is shown on its own line and leaves B's mappings in place;
- **side A's server password goes into A's invitation.** In the default mode it is the host's own
  access and never leaves the machine; in referee mode it is a player's access, like B's;
- the freeze is unchanged: both worlds stay frozen until **both** players are in, so neither guest
  can play before the other.

### Playing over the Internet

On the same local network, there is nothing more to do. Otherwise the host forwards these ports on
their router to their PC:

- ports **17271** and **17272** in **UDP**, to play;
- the share port **17280** in **TCP**, for the link only. If you send the `.bat` by message, you can
  skip it.

Then enter your public address in the lobby, under *Public address*, and click `Use this address`
before sending anything. PZ Duel cannot test that address, and it provides no relay.

#### Open ports from the lobby (UPnP)

If your router supports UPnP, the lobby of a real duel can open these ports for you. It never does so
on its own: only when the host clicks the button under *Public address*. Before the click, the button
names what it will open and for how long, for example
`Open ports 17271/UDP, 17272/UDP, 17280/TCP for 2 h`. It asks for exactly three port mappings and
nothing else: 17271 and 17272 in UDP, to play, and the share port 17280 in TCP, for the link. No port
range, no DMZ, no other port.

- **The lease and its expiry.** Each mapping is opened for a bounded lease (2 h by default, never
  more). After a successful opening the lobby shows `Ports … are open until` followed by the expiry
  time. A duel lasts at most one hour, so if you stayed long in the lobby since the click, click again
  before launching. Same thing at a rematch: the next lobby does not open anything by itself. Clicking
  again on the same router replaces its three mappings, it does not add new ones. If, between two
  clicks, your PC moved to another network and so to another router, the second click opens three
  mappings on that router too: those of the first router are not replaced, they stay there until
  PZ Duel exits cleanly and removes them, or until their lease runs out.
- **No automatic renewal.** PZ Duel never extends the lease without you. When it runs out, the router
  removes the mappings.
- **Closing.** The mappings are removed when PZ Duel exits cleanly, with `Quit` or when it closes by
  itself after `idle_quit_s` with nobody watching. There is no separate
  button to close them. If PZ Duel is killed or crashes, only the lease bounds them: they stay open on
  the router until it runs out.
- **The public address.** The router also reports its public address. When it is a real public
  address, the lobby shows `Public address read from the router:` and puts it in the *Public address*
  field, unless you already typed something there. It is only a suggestion: click
  `Use this address` yourself, as above. The address stays on your PC: it is not written to the match
  journal or the logs.
- **A non-public address.** If the router reports an address that cannot be reached from the
  Internet (a private address, or one from the `100.64.0.0/10` shared space), the lobby says so and
  proposes nothing. Your router is itself behind another router (double NAT) or your provider shares
  one public address between customers (CGNAT, carrier-grade NAT). The ports may be open on your
  router, but a guest outside your network will likely not get in through it: ask your provider or
  open the ports on the upstream router.
- **When it fails.** If no compatible router answers, or the router refuses, the lobby shows
  `Ports could not be opened automatically` with the reason, then the manual instruction: `Open these
  ports by hand in your router's settings and forward them to this computer`, followed by the three
  ports and protocols. Do that as described above, then enter your public address. A failed opening
  never blocks the duel: you can still launch it.

#### French routers and the high port range

French routers (Freebox, SFR, Bbox; Livebox with a shared IPv4) reserve the ports below 49152: a
Freebox answers `718 ConflictInMappingEntry` to any UPnP request under 49152, even on a free port.
So PZ Duel does not ask the router for 17271, 17272 and 17280: it opens **external** ports in the
high range, by default **49271/UDP, 49272/UDP and 49280/TCP** (`upnp_ports.external_ports` in
`service/runtime.json`), and forwards them to the ports the game keeps listening on (17271, 17272,
17280). If the router still answers `718`, PZ Duel tries once more with three ports from
`upnp_ports.fallback_range` (49152-50175 by default), never more; a port in that range already
used by another device on the router is skipped, never overwritten. Once an opening was tried, the
invitation, the link and the `.bat` carry the external ports really opened. If your router refuses
anyway, the lobby shows the correspondence to type in by hand: `49271 -> 17271/UDP`,
`49272 -> 17272/UDP`, `49280 -> 17280/TCP`. A configured external port under 49152 is accepted but
the lobby warns that most French routers will refuse it. If the address you entered, or the one the
router reports, is private or shared (`10.0.0.0/8`, `100.64.0.0/10`, `192.168.0.0/16`…), the lobby
says so above the button: no port forwarding will make the duel reachable from the Internet.

### Never share the `.bat` publicly

**Do not post or distribute the `.bat`.** It holds your address and the password of the guest's server,
so anyone who has it can join. Send it to your opponent only, in a private message.

### Settings before the start

`Configurer un duel` shows every setting, flat, in two blocks: **Monde** (written to the servers'
`SandboxVars`) and **Règles** (applied to the played preset). No file to edit. The page knows no
setting: it reads the catalogue served by `GET /api/settings` (`?preset=` gives that preset's
defaults), and `Valeurs par défaut` puts them back.

- **Monde**: base population (`Zombies`, 1 Hardcore … 6 None, default 3 *Élevé*; it also writes the
  `ZombieConfig` multipliers, the ones a dedicated server reads), zombie sight, hearing, strength,
  toughness (default *Normaux*), cognition, memory, infection (unchanged: blood and saliva), start
  hour (9 h), start month (July), day length (4 h, 15 min to 4 h), and **Monde neuf** (on by default).
- **Règles**: zombies per kill (10, up to 30), sprinter % (1), pop distance (12–20 tiles), aggressiveness
  (0.6), spacing (4), warning (6 s), healing crate on injury (on), match length (25 min).
- **Refusals**: a value out of bounds, of the wrong type or an unknown key refuses the start before
  anything happens, with a message naming the key, the bound and the value received. Nothing is
  silently clamped.
- **Monde neuf** deletes `Saves/Multiplayer/duelA|duelB`, `db/duelA|duelB.db` and their snapshots
  before the servers start; PZ then creates both worlds again. The deletion is logged (paths, sizes)
  in the page logs and in the match journal (`world_reset`). Off, nothing is touched: start hour,
  start month and starting population only take effect on a new world.
- **Locked**: `ZombieLore.Speed = 4` and `ZombieLore.SprinterPercentage = 0` are written on every
  set start and never offered: sprinters come only from the mod's deterministic count.
- The settings are sealed with the table and written as is in the journal (`settings` and `sandbox`
  in `match_start`); `service/presets/*.json` is never rewritten. `/api/state` gives a one-line
  `config.settings_summary`, shown under the panel.

### Rematch or new duel

- **Rematch** (`Revanche`): a `.bat` you already sent stays valid, as long as your address has not
  changed. The old link no longer works, but your guest can simply run the same `.bat` again.
- **New duel** (`Nouveau duel`): a new duel can make a `.bat` you already sent stale, because a new
  password is drawn. If so, send the new `.bat` again. Typing the previous password in the duel form
  keeps the old `.bat` valid.

## Try it from source (no Project Zomboid needed)

`mock` mode replays the whole flow — preflight, servers, contract, countdown, score — without
launching a single process.

```bash
git clone https://github.com/chpomob/pz-duel.git && cd pz-duel
python3 -m service.main            # opens http://127.0.0.1:8765/
```

Python **≥ 3.12**, no dependencies: the service is standard library only.

Flow: `Configurer un duel` → preset **Démo 90 s** → `Launch duel` → in the lobby,
`Simuler l'adversaire`. Anything simulated is labelled as such.

## Real mode from source

Requires Project Zomboid **42.22 or newer** (the headless server launcher ships with the game; the mod
declares `versionMin=42.21.0`), six free
ports (game 17261/17271, UDP 17262/17272, RCON 27015/27016) and about 6 GB of free RAM — each
server takes 2.

```bash
python3 -m service.main --install-mod     # copies mods/duelcore into ~/Zomboid/mods/
python3 -m service.main --mode real       # automatic Steam detection
python3 -m service.main --mode real --pz-dir /path/to/ProjectZomboid
```

- `--install-mod` only touches `~/Zomboid/mods/duelcore/`. Re-run it after every mod change: the
  preflight compares the installed fingerprint with the repository one and refuses any mismatch.
- The servers run **without Steam** (`-Dzomboid.steam=0`), and so does the client: the lobby can
  launch `projectzomboid.sh -nosteam +connect … +password …`. If the script is missing, or the client
  exits within two seconds, the button returns a failure naming the cause and the command to run by
  hand.
- Each side has its own password; the host's appears only in the lobby, the guest's travels in the
  invitation (QR code or link).
- **Single-user machine only**: the local API has no token, so any local process can read
  `/api/state` and drive the duel. Passwords also show up in `ps`.

What real mode writes into `~/Zomboid`: `Server/duelA.ini` and `duelB.ini` (`0600`, since
`RCONPassword` sits there in clear text), `duel-snapshots/`, `Saves/Multiplayer/duelA|duelB`
(restored on every launch), and the transport files `Lua/duel_A_*.txt`, `duel_B_*.txt`. Redacted
logs and server output go to `matches/<match_id>/`.

## Synchronisation cadence

Values from the code (`service/runtime.json`, `mods/duelcore/…/DuelCore.lua`), not from the design
document.

| Step | Cadence |
|---|---|
| Mod → `out` file | **1 s** buffer, flushed **immediately** on a concluding fact (death, score) |
| Referee reads `out` | every **250 ms** |
| Match core tick | **250 ms** |
| Mod reads `in` | every **1 s** |
| Presence `ack` / `beat` | **5 s** / **10 s** (30 s timeout) |
| Effects | served on every tick, FIFO per side, 30-unit slices, `cap_per_min` caps (max 120/min) |
| Conclusion | **+1.5 s** after the last read (see known limits) |
| Tolerated drift | 60 s, world-age gap × DayLength × 2.5 |

Typical event latency, from a kill in game to the command written on the other side: **~2.3 s**
(1 s flush + 250 ms read + 1 s mod read), excluding any cap wait.

## What is proven, and what is not

Proven on this machine (build **42.22.2**, the stable of 09/11/2026; the same suite passed on 42.21.0
before that update) by `pytest -m pz_integration`:

- two real servers boot, the `duelcore` mod loads, `caps` is published and the contract is fully
  verified (`verify(caps) == []`);
- shutdown through **RCON**, ports released, `.ini` files in `0600`, **no secret** in any match file,
  no leftover process;
- the command reader reads a line at most once, including a cut line or a missing `\n`;
- about 2,000 tests outside the game cover the protocol, the relation table, scoring, the journal, replay,
  the local API and the UI (suite green on Python 3.12 and 3.14).

**Never exercised**, and this is the heart of the product:

- a duel **with two players**: kill attribution in multiplayer (`OnZombieDead` + `getAttackedBy`),
  client-side visibility of a server-initiated spawn, crafting through `OnProcessAction`;
- a `-nosteam` client connecting to a server launched by `start-server.sh`;
- whether a player's health actually changes server-side, the condition for the `damage_taken` atom;
- what a dead player becomes in the real game (stays online, respawns, disconnects).

The 15-minute procedure that settles these points is described [below](#in-game-verdict-procedure);
until it has been run, those boxes stay open — and the documents say so.

## Tests

```bash
python3 -m pytest -q                          # without the game (about 2,000 tests, ~2 min 30)
python3.12 -m pytest -q                       # declared version floor
python3 -m pytest -m pz_integration           # real servers, ~2 min 30
```

`-m pz_integration` **writes into the real `~/Zomboid`** (mod, settings, snapshots, world restore).
Do not run it during a duel: the preflight would refuse the ports.

## In-game verdict procedure

This is the only proof for the points above. It needs two people, the host on A and the guest on B,
and the **Duel rapide** preset (`zombie_kill`, `damage_taken`, `spawn_pop` at 12-20 tiles, where the
first real duel saw them; one zombie in 100 sent to a side is a sprinter, in place of a regular one:
the 100th, the 200th…, set by `sprinter_percent`). A game day lasts 240 min (`day_length` 7) and the
duel starts at 9 am, so 25 min of play are about 2.5 in-game hours, in daylight.

1. Launch in `--mode real`, short duration, connect both players. Note whether the guest joins with
   the mod copied over, and whether a guest without the mod is refused.
2. The countdown starts once both players are in and the spawn probes have answered. One zombie
   appears 30-60 tiles from each player: a first target, and already a spawn observation.
3. On A, kill a zombie. B must receive a `spawn_pop`: **it is player B who checks that the zombies
   appear on screen**; the journal only proves what the API returns.
4. Get hurt by a zombie after 5 s without bleeding: the `damage_taken` event must appear with `hp`,
   `attacker` and `source`. Note what `source` carries, and whether an `OnPlayerGetDamage` `hook_seen`
   follows.
5. Fall asleep or get into a vehicle while the other player kills: the command received meanwhile
   must end `refused` (`asleep`, `in_vehicle`).
6. Let the match run to its end, then check the traces:

```bash
curl -s http://127.0.0.1:8765/api/state > state.json
PZ_LIVE_JOURNAL=matches/<match_id>/journal.jsonl PZ_LIVE_STATE=state.json \
  python3 -m pytest -m pz_integration tests/test_integration_pz.py::test_live_journal_checklist
```

A `skip` is not proof: the point stays open until the procedure has been run.

## Replaying a match

```bash
python3 -m service.replay matches/<match_id>/journal.jsonl            # 0 if the replay yields the same decisions
python3 -m service.replay --summary matches/<match_id>/journal.jsonl  # per-side telemetry
python3 -m service.verdict matches/<match_id>/journal.jsonl           # attribution, spawn, hooks seen
```

The journal is JSONL, timestamped, sequenced and idempotent: replay instantiates the same match core,
so replaying a journal proves the engine.

## Layout

```
service/          referee: preflight, servers, RCON, protocol, file transport,
                  relation table, match core, journal, replay, verdict, local API
mods/duelcore/    server-side mod (Lua): captures facts, publishes capabilities, applies commands
ui/               local interface (HTML/CSS/JS, no dependency beyond a vendored QR encoder)
packaging/        Windows binary: PyInstaller entry point and spec, smoke test
tests/            about 2,000 tests; server, mod and a reduced Lua interpreter as doubles
docs/campaigns/   spec (77 criteria), plan (24 steps), 4 branch reviews, 15 fix steps
docs/design/      ten design notes, English and French (see the index)
```

## Known limits

The dated detail lives in [`docs/campaigns/phase1/limites-connues.md`](docs/campaigns/phase1/limites-connues.md),
and for Windows in [`docs/campaigns/phase2/limites-connues.md`](docs/campaigns/phase2/limites-connues.md).
The important ones:

- **Damage detection relies on a server-side health reading** that has not been observed working for a
  connected player. If it does not, the atom is withdrawn and the wound rule disappears — the lobby
  announces it, the journal records it.
- **The conclusion waits 1.5 s**: the match keeps running for a moment with a death already journaled.
  Deliberate, so an older death still sitting in the mod's buffer can win.
- **No automatic restart of a hung server**, no recovery of orphaned servers if the referee dies
  abruptly, no token on the local API.
- **No anti-cheat**: the player owns their files. A leaderboard only means anything among friends.
- **Opening ports (UPnP) and shutdown**: if you quit while an opening is still running, the exit
  waits at most the discovery window plus one router request timeout (5 s by default) for it to
  finish and record what it placed, then goes on. The whole removal takes at most the discovery
  window plus `(3 mappings + 2)` request timeouts — 17 s with the shipped settings — so a router that
  went silent cannot hold `Quit` open. Every mapping recorded during the session is
  removed, including those placed on a second, different router: the exit no longer needs to
  rediscover a router, it keeps the address of each one it used. A mapping the router refuses to
  delete — or one whose ownership is uncertain, because the lease may have expired — is still
  attempted, and a refusal is accepted: it then stays until its lease runs out. PZ Duel never
  assumes the router honours that lease.
- **When some ports could not be closed**: after a partial failure, the lobby names the couples that
  may still be open (`Ports 17271/UDP may still be open on your router: their removal was not
  confirmed`) and says the rest of the truth: PZ Duel tries again when it stops, otherwise they close
  when their lease ends, *if your router honours it*. Check your router's port forwarding page if
  you are unsure.

## Thanks

**Naylo** was the first person to run this on a Windows desktop with the real game installed — and the
first to break it usefully: his Setup card named a `StartServer64.bat` that a healthy 42.22 install does
not ship. That report is why the service now starts the servers with the JRE the game embeds instead of
requiring that script. He is a contributor to this project, not a footnote to it.

## Licence

WTFPL v2 — see [`LICENSE`](LICENSE). Credits for the mods and authors this project draws on are in
[`CREDITS.md`](CREDITS.md), to be read before reusing any code.
