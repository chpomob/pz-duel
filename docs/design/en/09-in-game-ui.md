# Launcher UI — everything visible, no console

*English — [version française](../fr/09-ui-full-ingame.md)*

Decision of 04/11/2026: **no console mode**. The launcher is an application with a graphical interface from the MVP on: configuration, launch, status, invitation, score — everything must be visible and clickable.

Starting constraint: a Lua mod cannot launch processes, so the service stays external. But it is **drivable and observable through a UI**, never through a terminal. And the same state will later feed an in-game menu (see §5).

---

## 1. Interface choice

| Option | Cost | Advantages | Drawbacks |
|---|---|---|---|
| **Local web page served by the service** *(MVP rec.)* | 1-2 d | zero dependency, native QR in HTML, same rendering reusable for the guest, instant iteration | opens in a tab |
| Native window (pywebview) | +2 d | app feel, no tab | webview dependencies per OS |
| In-game menu only (mod) | +1 week | total immersion | the service still has to be started before the game |
| Console / TUI | — | — | **rejected** |

Recommendation: **local web page** at the MVP (`http://127.0.0.1:8765`, opened automatically), native window when packaging (v1.2), in-game mirror in v1.1. All three consume the same state: nothing is rewritten.

Bonus: the QR code, hard to display in-game, is trivial in HTML. It therefore lives in the launcher UI, not in the mod.

---

## 2. Screens

### 2.1 Home / status
```
┌────────────────────────────────────────────────────────┐
│  PZ DUEL                                   service: OK │
│                                                        │
│  Server A    ● ready    17261   player: durand         │
│  Server B    ● ready    17271   player: —              │
│  Clock       —           Drift: —                      │
│                                                        │
│  [ Configure a duel ]          [ Resume the last one ] │
│  [ View logs ]                 [ Quit ]                │
└────────────────────────────────────────────────────────┘
```

### 2.2 Configuration
```
┌─────────────────────────────────────────────────────────────┐
│  New duel                                                   │
│                                                             │
│  Preset        [ Quick duel ▾ ]  (Quick duel, Pressure,     │
│                                   Hardcore, Custom)         │
│  Duration      [ 25 min ▾ ]                                 │
│  Game day      [ 15 min ▾ ]  (= DayLength 1)                │
│  Nickname      [ durand            ]                        │
│  Password      [ random  ⟳ ]                                │
│                                                             │
│  Required mods duelcore ✓   duelhud ✓   (read-only)         │
│                ⚠ 1 mod missing → [ open the Workshop ]      │
│                                                             │
│  Rules (preview)                                            │
│   • 1 zombie killed → 2 zombies at the opponent (max 60/min)│
│   • 3 builds then 1 kill within 30 s → a horde              │
│   • 10 kills in 60 s without being hit → fog                │
│                                      [ Edit the table ]     │
│                                                             │
│  [ Cancel ]                        [ Launch the duel ]      │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Launch — honest progress, step by step
```
  ✓ Preflight        PZ install, free ports, 2 GB RAM, mods
  ✓ Server A         started in 34 s
  ⟳ Server B         generating the world…              12 s
  · Contract         mod verification
  · Lobby
```
Each step has a state (waiting / in progress / ok / failed) and an actionable message ("port 17261 busy: close the previous instance"). Never a frozen screen: if something blocks, we see where.

### 2.4 Lobby
```
┌────────────────────────────────────────────────────────┐
│  Match code   K7QX2                                    │
│  82.64.x.x:17261        password  K7QX2                │
│  ┌─────────┐   [ Copy the link ]   [ Open PZ ]         │
│  │  QR     │                                           │
│  └─────────┘                                           │
│  Opponent: waiting…                                    │
│  [ Cancel the duel ]                                   │
└────────────────────────────────────────────────────────┘
```

### 2.5 Match — observation panel
Score of both sides, timer, threats/minute, latest events, clock drift. Used for refereeing and balancing; it is also the source of the in-game HUD in v1.1.

### 2.6 Logs
Scrollable panel (not a system console): service events, logs of both servers, errors highlighted, "copy the diagnostics" button.

---

## 3. The service behind the UI

`duel-service` (Python), a single process, three responsibilities:

1. **Lifecycle** — preflight, start/stop of both servers (`-servername`, `-port`, `-adminpassword`), wait for `SERVER STARTED` ×2, restore of snapshots.
2. **Director** — evaluation of the table (atoms → aggregates → series → effects), score, clock, budgets, JSONL journal.
3. **UI server** — `127.0.0.1:8765`: page + `/api/state` (1 s poll) + `/api/action` (start/stop/cancel/rematch). Also writes `duel_state.json` into `~/Zomboid/Lua/` for the future in-game menu.

No native window at the MVP: `webbrowser.open()` on startup. Bind to `127.0.0.1` only.

---

## 4. Breakdown

| Step | UI | What works |
|---|---|---|
| **MVP** | local page: status, basic config, progress, lobby + QR, logs | full duel between two players |
| **v1.1** | + match panel, + table editor, + one-click rematch; in-game mirror | comfort |
| **v1.2** | packaged native window (no tab) | app feel |
| **v2** | full in-game menu, invisible service | immersion |

---

## 5. Heading for full in-game (v1.1+)

The client mod can write to and read from `~/Zomboid/Lua/` (proven). So the same service can be driven by the game:

- `duel_ctl.txt` (mod → service): `{"cmd":"start","preset":"duel_rapide"}` · `stop` · `reset` · `rematch`
- `duel_state.json` (service → mod): phase, servers, players, invitation, clock, score, threats/min, error

The in-game menu then becomes a mirror of the web page. We keep both: the page for the host (config, logs, QR), the menu for immersion. No protocol change between the two.

---

## 6. Points of vigilance

| Risk | Mitigation |
|---|---|
| The tab is closed during a match | the service keeps running; reopen `127.0.0.1:8765` |
| Port 8765 taken | free port chosen automatically, URL shown in a small fallback window |
| Service not started | "PZ Duel" shortcut; clear message in the in-game mod if it runs without a service |
| Invalid config | validation before launch, button disabled + reason displayed |
| Missing mods | red badge + "open the Workshop" button, launch refused |
| "Who is hosting" confusion | the UI says explicitly "you are hosting; send this QR" |

---

## 7. Open decisions

1. **MVP UI tech** — (a) local web page served by the service *(rec.)* · (b) native pywebview window · (c) direct in-game menu.
2. **Table editor** — (a) read-only preview at the MVP, editor in v1.1 *(rec.)* · (b) editor from the MVP.
3. **Tab vs window** — (a) tab at the MVP, packaged window in v1.2 *(rec.)* · (b) native window from the MVP.
