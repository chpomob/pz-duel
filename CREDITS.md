# Credits and inspirations

*English — [version française](CREDITS.fr.md)*

This project would not exist without the work of the PZ community. House rule: **always credit, never
copy without saying so**, and never reuse third-party mod code without a compatible licence.

## Origin of the idea

The concept of linking two Project Zomboid games so that each player's actions become threats in the
other's world comes from **Harumate** ([twitch.tv/harumate](https://www.twitch.tv/harumate)), a French
Project Zomboid streamer. He is credited for the idea; this repository is an independent
implementation, not affiliated with him.

## What the project takes, and from whom

| Project | Author(s) | Licence / status | What we take | What we do not take |
|---|---|---|---|---|
| **Streamer Integration Framework (SIF)** | YebNFlo | Workshop 3387594292, all rights reserved | The **idea** of a file bridge inside `Zomboid/Lua/` (periodic read, registered modules, file cleared after processing) | No code, no file |
| **SIF — Example Events** | YebNFlo, ShavedTube | Workshop 3387596392 | The **catalogue** of 48 triggerable modules (spawns, weather, items, vehicles, player effects) as a feasibility proof | No code |
| **Chaos Mod + Twitch/YouTube** | zoidxf (`clixff` repository) | MIT | The **inventory** of 462 effects (`docs/design/en/05-appendix-chaos-462.md`), used to map what can be generated | No effect implementation copied: our subset is written separately |
| **log-extender** | outdead (`openzomboid` repository) | MIT | The **client/server mapping** of signals (craft through `ISHandcraftAction`, `PlayerLogger`, `CraftLogger`) | No code |
| **zomboid-mod-analytics** | spoutnickgp | public repository | The **proof** that `OnCharacterDeath`, `OnHitZombie`, `OnWeaponHitCharacter` and `OnEnterVehicle` are usable server-side | No code |
| **Zombie Spawn Manager** | El1oN (Elyon) | author's rights reserved (modification forbidden) | The **documentation of B42 limits** (no public per-zombie setters) | No code, no resource |
| **ZombieBuddy** | Andrey "Zed" Zaikin (`zed-0xff`) | permissive licence | Assessment of the "Java agent" path for a future version | Not used in the MVP |
| **pzJBridge** | `rocky6777` | public repository | Assessment of the Java bridge path (42.21) | Not used |
| **PZHook** | Olipro | public repository | Historical reference on Java hooking | Not used |
| **PZEventDoc** | `demiurgeQuantified` | public documentation | The list of 260 events and their `(Client)` / `(Server)` tags | — |
| **PZwiki** | community | CC / wiki | Versions, launch parameters (`+connect`, `-adminpassword`), server options | — |
| **qrcode-generator** | Kazuhiko Arase | **MIT** | **Embedded code**: `ui/vendor/qrcode.js` (the only third-party code dependency of the project) | — (see `ui/vendor/qrcode.LICENSE.txt`) |

## Principles

1. **No third-party mod code is copied.** What we take are architecture ideas, capability lists and
   the names of the game's public APIs.
2. **Every reference names its author**: mod name, author, link. If an author asks for a mention to be
   removed, it is removed immediately.
3. **Licences are respected**: MIT for the QR encoder (text kept next to the file); "all rights
   reserved" mods are cited for documentation only.
4. **Many of these projects are recent and sometimes built with AI assistance.** All the more reason
   to be explicit: credit the authors, do not absorb their work, and state clearly what comes from
   them and what comes from us.

## Thanks

To the PZ modding community (The Indie Stone forum, PZ Modding Discord), to the authors above, and to
The Indie Stone for a Lua API open enough to make this kind of project possible.
