# Mechanisms and telemetry — the heart of the duel

*English — [version française](../fr/03-mecanismes-et-telemetrie.md)*

Retained concept (clarified on 04/11/2026): **players never cross paths.** Two separate worlds, no spatial interaction. The only link is a stream of threats: each advance by one becomes pressure for the other. There is therefore no arena, no contact, no direct PvP — and the "dense starting point" I had proposed no longer makes sense.

Design consequence: what matters is not the map, it is **the conversion**. One mechanism = (signal captured) → (weight) → (threat delivered). The game holds up or collapses on this conversion table.

---

## 1. The real problem: not everything is observable server-side

The server is the authority (saves, spawns, anti-cheat), but the 42.x event documentation explicitly marks some signals as **(Client)**. This is the structuring constraint of the project, not an implementation detail.

| Mechanism | Signal | Observability | Consequence |
|---|---|---|---|
| Zombie kill | `OnZombieDead` + `IsoZombie:getAttackedBy()` | **Server** | v1 pillar |
| Animal / survivor kill | `OnCharacterDeath` | **Server** | v1 pillar |
| Hit landed / damage taken | `OnHitZombie`, `OnPlayerGetDamage` | **Server** (doc: "fires on the server" for hits) | Combo, health state |
| Noise (gunshot, alarm, world) | `OnWorldSound`, `OnAmbientSound`, `OnWeaponSwing` | **Server** | "Noise" threat |
| Construction / destruction | `OnObjectAdded`, `OnTileRemoved`, `OnWeaponHitThumpable` (Server) | **Server** | "Collapse / noise" threat |
| Vehicle mechanics | `OnMechanicActionDone` | **Server** | "Wreck / noise" threat |
| Explosive / trap | `OnThrowableExplode` | **Server** | "Fire" threat |
| Fire | `OnNewFire`, `OnGridBurnt` | **Server** | Threat propagation |
| Generated containers | `OnFillContainer` (Server) | **Server** | Progression metric |
| **Crafting** | `Recipe_OnCreate`, `CraftRecipe_OnCreate` (umbrella) | **Uncertain** — called in the recipe script, therefore probably client-side in MP; server-side candidate: `OnProcessAction` (Server) | **To be settled by spike** |
| **XP / level-up** | `AddXP`, `LevelPerk` | **Client only** (marked `(Client)`) | Cannot be an authoritative source |
| Loot found | `OnItemFound` | Server (semantics to be verified) | "Wealth" threat |
| Player death | `OnCharacterDeath` (server) / `OnPlayerDeath` (client) | Server via `OnCharacterDeath` | End of match |

Three source levels, to be assumed explicitly in the config:

- **S1 — server**: authoritative, not falsifiable by the player. Core of the game.
- **S2 — client-declared**: the client mod (HUD) reports the event; falsifiable. Tolerable among friends, to be excluded from a ranked mode.
- **S3 — derived**: computed by the arbiter (time survived, distance travelled, health state, score) — authoritative by construction.

Recommendation: **v1 = S1 + S3 only**, with crafting tested in a spike. If `OnProcessAction` allows reliable server-side crafting, crafting enters v1; otherwise it moves to S2 (friendly mode) and v1 settles for kills, noise, constructions, mechanics, fire, death.

---

## 2. Catalogue of deliverable threats (what the opponent can be subjected to)

| Threat | Implementation | Settings |
|---|---|---|
| Zombie pop | `addZombiesInOutfit(x, y, z, n, outfit, chance)` | n, outfit, distance [min,max], spread |
| Sprinter | spawn with walk type "sprinter" (public B42 methods) | n, in-game age before expiry |
| Targeted horde | `createhorde <count> <user>` (RCON) | count, advance warning |
| Noise | `gunshot` / forced `OnWorldSound` / alarm | radius, duration |
| Helicopter | `chopper` (RCON) | window, probability |
| Weather | `startrain` / `stoprain`, fog | duration |
| "Poisoned gift" item | item spawn on the ground near the player | type, decoy |
| Wreck / vehicle | `addvehicle` (RCON) | type, position |
| Fire | controlled ignition (to be validated: dangerous, risk of total loss) | radius, cap |
| Power/water cut | **no reliable runtime API** | out of v1 |

Safety rule: every delivery goes through an **advance warning** (sound + HUD text) and a **minimal radius** (30-60 tiles), never right behind the player's back, never during sleep or driving (blacklist of contexts).

---

## 3. Rules schema — everything is data, nothing is hardcoded

Explicit objective: "we put the mechanisms in place, balancing is the delicate part". So the code contains no balancing value; it reads a reloadable rules file.

```yaml
# rules.yaml — versioned, one file per mode (duel_rapide, duel_long, test)
meta:
  name: duel_rapide
  match_seconds: 1500          # 25 min real time
  day_length: 1                # 1 in-game day = 15 min real time
  pressure_floor: 0.2          # minimum ambient pressure (avoids an empty match)
  pressure_ceiling: 6.0        # global ceiling, in "threat units" per minute

mechanisms:
  zombie_kill:
    source: { kind: server_event, event: OnZombieDead, actor: getAttackedBy }
    weight: 1.0
    batch: 15                  # aggregation in seconds
    caps: { per_minute: 60, per_match: 2000 }
    converts:
      - { threat: zombie_pop, per: 1, amount: 2, distance: [30, 60], warn_s: 6 }
      - { threat: sprinter,   per: 25, amount: 1, warn_s: 10 }

  construction:
    source: { kind: server_event, event: OnObjectAdded, filter: player_built }
    weight: 2.0
    batch: 20
    caps: { per_minute: 10 }
    converts:
      - { threat: noise, per: 1, amount: 1, radius: 40 }

  damage_taken:
    source: { kind: server_event, event: OnPlayerGetDamage }
    weight: 0.0                 # does not generate a threat, breaks the combo
    effects: { reset_combo: true }

modifiers:
  combo:
    step: 0.15                  # +15% per kill without being hit
    max: 3.0
    reset_on: damage_taken
  anti_snowball:
    when_score_gap_gt: 30
    attacker_multiplier: 0.6    # whoever is ahead generates less
    defender_bonus: supply_drop # whoever is behind receives a crate
  decay:
    window_s: 90
    factor: 0.9
```

Each `threat` references a **capability** declared by the mod (name, parameters, prerequisites). A mode that requests an absent capability fails at startup with a clear message — never silently.

---

## 4. Telemetry: balancing with measurements, not impressions

Each match writes a JSONL file (one line per event and per conversion):

```json
{"t":312.4,"side":"A","kind":"zombie_kill","raw":3,"weight":3.0,"combo":1.45,
 "emitted":[{"threat":"zombie_pop","amount":9}],"caps_hit":false,
 "state":{"hp":87,"score":41,"threats_pending":12}}
```

Usable for:

- pressure curve per minute and per side (is the match flat or explosive?);
- actual vs theoretical conversion rate (do the caps bite?);
- correlation pressure suffered ↔ death (is the threat lethal or decorative?);
- dead time (periods with no event at all) → adjust `pressure_floor`.

Without this file, balancing will be done blind and the project will have no proof of progress. It is the first brick to write, even before the threats.

---

## 5. "Ghost" mode: balancing with a single player

The arbiter can generate a scripted threat stream (parameterisable pressure curve) without a second player. A single human is then enough to:

- feel the difficulty curve of a 25 min match;
- check the readability of the advance warnings;
- measure the lethality of a pack of N zombies at distance D;
- calibrate `pressure_floor` / `ceiling`.

It is the cheapest way to do the first 20-30 balancing iterations, before real duels. To be planned from the MVP (≈1 day), not after.

---

## 6. What this changes in the plan

- The "dense starting point / arena" disappears: players never cross paths. The map must only be **survivable and action-generating** (zombies, loot, materials). The choice of map becomes a setting of "density of opportunities", not of contact.
- The reset between matches remains necessary (worlds that degrade).
- Crafting becomes uncertainty point #1: it is the most interesting mechanism (crafting = arming the other) and the only one whose server observability is not guaranteed. To be handled first in the spike.
- Balancing becomes data work: `rules.yaml` + JSONL + ghost mode. Allow 4-8 days, part of which can be done solo.
