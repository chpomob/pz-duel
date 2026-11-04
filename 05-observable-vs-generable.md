# Face A / Face B — ce qu'on peut observer, ce qu'on peut générer

Inventaire vérifié sur sources primaires, pas sur des affirmations de forums.

**Sources réellement lues pour ce document**
1. Documentation des 260 événements Lua 42.x (`PZEventDoc`, branche develop) — avec les marquages `(Client)` / `(Server)`.
2. Javadoc 42.x de `LuaManager.GlobalObject` — fonctions globales appelables depuis Lua (liste extraite par script : 47 fonctions `add*/spawn*/create*`).
3. Code cloné de **Chaos Mod** (`clixff/ChaosMod-ProjectZomboid`, MIT, 462 fichiers d'effets) — inventaire complet en annexe `05-annexe-chaos-462.md`.
4. Code cloné de **log-extender** (`openzomboid/log-extender`) — loggers B41 et B42, client et serveur.
5. Code cloné de **zomboid-mod-analytics** (`spoutnickgp`) — analytics **côté serveur** (`media/lua/server/`).
6. **SIF** + **SIF - Example Events** (Workshop) — 48 modules d'injection pilotés par fichier.
7. Mon spike 0 : serveur dédié 42.21 headless, mod Lua, IPC fichier (`04-spike-0-preuves.md`).

---

# FACE A — ce qui est observable

## A1. Prouvé côté serveur (donc utilisable comme source autoritaire)

| Signal | API / événement | Preuve | Usage duel |
|---|---|---|---|
| Mort de n'importe quel personnage | `OnCharacterDeath` | `zomboid-mod-analytics` l'enregistre depuis `media/lua/server/` avec un handler qui logge le type de mort | kills, fin de match |
| Coups portés / encaissés | `OnHitZombie`, `OnWeaponHitCharacter(wielder, target, weapon, damage)` | même mod serveur, handler qui lit `damage` et `target` | dégâts, combos, « touché » |
| Kills cumulés du joueur | `IsoPlayer:getZombieKills()` | analytics l'écrit dans son log serveur | score |
| Heures survécues | `IsoPlayer:getHoursSurvived()` | analytics | score, fin de match |
| Horloge monde | `getGameTime():getWorldAgeHours()` | mesuré en spike 0 sur serveur 42.21 | horloge de match |
| Sons du monde | `OnWorldSound` | doc 42.x (non marqué client) | pression « bruit » |
| Conteneurs remplis | `OnFillContainer` | doc, marqué `(Server)` | progression |
| Construction / destruction | `OnWeaponHitThumpable` (`Server`), `OnObjectAdded`, `OnTileRemoved` | doc | pression « bruit / effondrement » |
| Mécanique véhicule | `OnMechanicActionDone` | doc, non marqué client | pression « épave » |
| Explosions | `OnThrowableExplode` | doc | pression « incendie » |
| Feu | `OnNewFire`, `OnGridBurnt` | doc | propagation |
| Météo | `OnWeatherPeriodStart`, `OnWeatherPeriodStage` | doc, marqués `(Server)` | ambiance / pression |
| Apparition de zombie | `OnZombieCreate` | doc | **anti-boucle** : ne pas reconvertir nos propres spawns en menace |

## A2. Prouvé côté client uniquement

| Signal | API / événement | Preuve | Conséquence |
|---|---|---|---|
| XP gagné | `AddXP` | doc `(Client)` + `log-extender` `PlayerLogger` est un logger client | **exclu** des sources autoritaires |
| Montée de niveau | `LevelPerk` | doc `(Client)` + même logger client | idem |
| **Craft terminé** | `ISHandcraftAction` (hook sur `ISBaseTimedAction.perform`) | `log-extender` `CraftLogger.lua` est explicitement un logger **client** ; aucun logger de craft côté serveur dans le projet | **le mécanisme central du duel n'est pas observable serveur par ce chemin** |
| Ramassage / inventaire | loggers d'objets (B41 client) | `log-extender` | à dériver autrement |
| Caméra, UI, sons locaux | événements `(Client)` | doc | par nature client |

## A3. Contradictoire ou non prouvé — à trancher en spike 1

| Cas | Ce que dit la doc | Ce que fait le code | Verdict |
|---|---|---|---|
| `OnWeaponHitCharacter` | `(Client)` : « when a non-zombie character is hit by an attack from a **local player** » | `zomboid-mod-analytics` l'enregistre dans un fichier **serveur** | contradiction franche : soit la doc est incomplète, soit le mod ne reçoit rien côté serveur. **À tester** |
| `OnEnterVehicle` / `OnExitVehicle` | `(Client)` | analytics les enregistre côté serveur | idem, à tester |
| `OnBeingHitByZombie` | **absent de la doc** | analytics l'enregistre avec un handler vide (`print`) | probablement inexistant ou mort : ne pas en dépendre |
| Craft côté serveur | aucun événement serveur documenté | `Recipe_OnCreate` est une fonction « umbrella » appelée dans les scripts de recette, donc côté client | piste restante : `OnProcessAction` (`Server`). Sinon : **diff d'inventaire** |
| Compétences côté serveur | `AddXP` est client | `IsoPlayer:getPerkLevel()` existe dans la javadoc ; en MP les données joueur vivent côté serveur (`players.db`) | probablement lisible directement, à tester |

## A4. Angles morts confirmés (à concevoir autour, pas contre)

1. **Aucun événement de connexion/déconnexion** dans la doc 42.x (`OnPlayerConnect` / `OnPlayerDisconnect` sont absents). La politique « déconnexion = forfait après 2 min » doit se baser sur un **polling de `getPlayers()`** ou sur le log serveur.
2. **Aucun événement « craft terminé » serveur** → le craft doit être reconstruit (voir plus bas).
3. **Aucun événement « objet ramassé » serveur.**
4. La simulation est locale autour du joueur : rien d'observable à distance — sans conséquence ici, les deux joueurs ne se croisent pas.

## A5. Contournement du craft : diff d'inventaire

Puisque le serveur possède l'inventaire du joueur en MP (B42 a introduit la propriété serveur des objets), on peut :

- échantillonner l'inventaire toutes les 2-5 s côté serveur (`player:getInventory()`), compter par type d'objet ;
- détecter l'apparition d'objets **dont la recette est connue** (table `output → recipe`) ;
- émettre un événement `craft` serveur-autoritaire.

Coût : 1-2 jours, faux positifs possibles (loot ramassé, objet donné) → filtrage par « objet fabriqué uniquement » et par delta signé. C'est imparfait mais **non falsifiable par le client**, contrairement à un log client.

---

# FACE B — ce qui est générable

## B1. Serveur, autoritaire (API globale + RCON)

| Menace | Appel | Preuve |
|---|---|---|
| Pop précise | `addZombiesInOutfit(x, y, z, n, outfit, femaleChance, …, health)` | signature complète utilisée par Chaos (`ChaosZombie.SpawnZombieAt`) et par le tuto vanilla |
| Pop en zone | `addZombiesInOutfitArea(x1, y1, x2, y2, z, n, outfit, …)` | javadoc + tuto vanilla |
| **Horde dirigée par coordonnées** | `createHordeFromTo`, `createHordeInAreaTo` | javadoc 42.x — bien plus précis que le `createhorde` RCON (qui ne vise qu'un joueur) |
| Zombies spéciaux | `createZombie`, `addZombieSitting`, `addZombiesEating`, `addZombiesInBuilding` | javadoc 42.x |
| Véhicules | `addVehicle`, `addAllVehicles`, `addCarCrash`, `addAllBurntVehicles`, `addAllSmashedVehicles` | javadoc 42.x |
| Ambiance monde | `addSound`, `addBloodSplat`, `createRandomDeadBody`, `addPhysicsObject` | javadoc 42.x |
| Progression | `addXp`, `addXpMultiplier` | javadoc 42.x |
| Animaux | `addAnimal` | javadoc 42.x |
| Commandes RCON | `createhorde`, `chopper`, `gunshot`, `lightning`, `alarm`, `startrain`/`stoprain`, `additem`, `addvehicle`, `removezombies`, `save`, `quit` | doc serveur + test de démarrage en spike 0 |

## B2. Client — canal « expérience » (prouvé à grande échelle)

- **Chaos Mod : 462 effets**, licence MIT, **single-player uniquement** (README : « Is multiplayer supported? No. »), **411 des 462 fichiers utilisent `getPlayer()`** et **aucun** n'utilise `sendServerCommand`.
- **SIF Example Events : 48 modules** pilotés par fichier, même transport que notre architecture (preuve que le canal fichier suffit à déclencher ce genre d'effets).

Extraits classés par usage duel (inventaire complet en annexe) :

| Famille | Exemples d'identifiants | Effet sur un duel |
|---|---|---|
| Perception / tension | `blurry_vision`, `shake_camera`, `force_zoom_in`, `force_zoom_out`, `disable_sounds`, `invisible_characters`, `look_behind_you` | pression psychologique, aucune létalité |
| Météo | `enable_fog`, `temporary_night`, `toxic_rain`, `hurricane`, `meteor_shower`, `stop_rain` | réduit la visibilité, change la lecture du risque |
| Zombies | `1hp_zombies`, `freeze_zombies`, `all_zombies_are_crawlers`, `zombie_spawner_nearby`, `fill_area_with_zombies`, `zombies_rain`, `electric_zombies` | pression directe |
| Électricité / eau | `turn_power_off`, `turn_water_on` (via `getSandboxOptions():set("ElecShutModifier", …)`) | prive de lumière/frigo → vrai stress |
| Objets | `break_player_weapons`, `food_is_poisoned`, `discharge_batteries`, `drop_item_in_hand`, `duplicate_items` | attaque la logistique |
| Véhicules | `disable_vehicle_engine`, `pop_tires_vehicles_nearby`, `explode_nearby_cars` | attaque la mobilité |
| Joueur | `drunk`, `add_random_wound`, `cant_stop_shouting`, `add_cigarette_addiction` | malus temporaires |
| NPC | `bounty_on_player_head`, `grove_street_gang`, `hire_mercenaries` | nécessite des mods NPC |

**Portage en multi** : chaque effet suppose l'autorité locale et le joueur local. En MP il faut que le **serveur** décide et que le **client de la victime** applique (commande serveur → mod client). Les effets de perception sont les plus faciles (ils ne touchent qu'au rendu/son) ; ceux qui modifient le monde doivent passer par B1.

## B3. Impossible / à ne pas tenter

| Tentative | Pourquoi |
|---|---|
| Tuer le joueur par API | pas de fonction propre, et injuste par design |
| Mettre en pause | notion inexistante en multi |
| Anti-triche | le joueur reste maître de son client ; seul le serveur est autoritaire |
| Régler par zombie `sight`/`hearing`/`memory`/`strength` | B42 n'expose plus de setters publics fiables (documenté par Zombie Spawn Manager) |
| Modifier la sandbox depuis le client en MP | le serveur décide ; il faut le faire côté serveur |
| « Monde miroir » | pas de seed partagé |

---

# Les trois canaux de menace

| Canal | Qui l'applique | Nature | Coût de mise en œuvre | Usage recommandé |
|---|---|---|---|---|
| **MONDE** | serveur | spawns, hordes, véhicules, météo, bruit, feu | faible (API prouvée) | 70 % du duel : c'est la pression lisible et équitable |
| **EXPÉRIENCE** | client de la victime | brouillard, tremblement, zoom, sons, hallucination | moyen (mod client + commande serveur) | 20 % : tension, pas de létalité |
| **META** | serveur | `addXpMultiplier`, objets, électricité/eau, loot | faible | 10 % : retournements, anti-neige-boule |

Un duel qui n'utilise que le canal MONDE devient un concours de farming. C'est le canal EXPÉRIENCE qui crée la « sensation Tetris » (l'adversaire te pourrit l'écran, pas seulement le monde).

---

# Correspondance avec `rules.yaml` (03-mecanismes)

| Mécanisme observé | Canal | Implémentation menaces | Preuve |
|---|---|---|---|
| Kill de zombie | MONDE | `addZombiesInOutfit`, `createHordeInAreaTo`, `createhorde` (RCON) | B1 |
| Dégâts subis | MONDE + EXPÉRIENCE | pop rapprochée + `shake_camera` | A1 + B2 |
| Construction | MONDE | bruit (`addSound`), horde attirée | B1 |
| Véhicule réparé | MONDE | `pop_tires_vehicles_nearby`, `disable_vehicle_engine` | B2 |
| Explosion / feu | MONDE | `createHordeInAreaTo` + `OnNewFire` | B1 |
| Craft (si non observable serveur) | META | objet piégé (`food_is_poisoned`), `break_player_weapons` | B2 + contournement A5 |
| Combo / sang-froid | EXPÉRIENCE | `blurry_vision`, `temporary_night` | B2 |

---

# Conséquences pour le MVP

- **v1 : 6 mécanismes serveur** (kill, dégâts, construction, mécanique, explosion/feu, sons) → 8-10 menaces du canal MONDE, toutes prouvées.
- **2 menaces EXPÉRIENCE en v1** (`enable_fog`, `shake_camera`) : peu de code, effet maximal sur la sensation de duel.
- **Craft** : tenter `OnProcessAction` en spike ; sinon diff d'inventaire (1-2 j) — c'est le seul mécanisme central dont l'observabilité n'est pas garantie.
- **Déconnexion** : polling `getPlayers()`, pas d'événement.

# À vérifier en spike 1 (liste courte)

1. `OnZombieDead` + `getAttackedBy()` : attribution à 100 % sur 100 kills.
2. `addZombiesInOutfit` côté serveur : spawn visible client, pas de crash.
3. `OnWeaponHitCharacter` / `OnEnterVehicle` : côté serveur, oui ou non ?
4. `OnProcessAction` : voit-il un craft ?
5. `getPerkLevel()` côté serveur : valeur fiable ?

# Sources

- Documentation des événements 42.x : https://github.com/demiurgeQuantified/PZEventDoc/blob/develop/docs/Events.md
- Javadoc `LuaManager.GlobalObject` : https://demiurgequantified.github.io/ProjectZomboidJavaDocs/zombie/Lua/LuaManager.GlobalObject.html
- Chaos Mod (MIT, 462 effets, SP only) : https://github.com/clixff/ChaosMod-ProjectZomboid
- log-extender (loggers client/serveur) : https://github.com/openzomboid/log-extender
- zomboid-mod-analytics (analytics serveur) : https://github.com/spoutnickgp/zomboid-mod-analytics
- SIF : https://steamcommunity.com/workshop/filedetails/?id=3387594292
- SIF - Example Events (48 modules) : https://steamcommunity.com/sharedfiles/filedetails/?id=3387596392
- Zombie Spawn Manager (limites B42 par zombie) : https://steamcommunity.com/sharedfiles/filedetails/?id=3759164356
- Spike 0 local : `04-spike-0-preuves.md`
