# Mode Duel Project Zomboid — étude de faisabilité (état des lieux)

Date : 04/11/2026 · Cible : Project Zomboid **Build 42 stable (42.20.x)** · Poste de référence : 42.20.4 installé localement

> **Décision (04/11/2026)** : architecture retenue = **deux serveurs dédiés sur la machine d'un joueur**, temps non accéléré par construction. Détail de conception, config, horloge, pause et budget révisé : `02-archi-deux-serveurs.md`. Les sections 4 et 5 ci-dessous (archi A, gating `setGameSpeed`) restent comme référence de l'option écartée.

---

## 1. Verdict court

**Faisable, et plus faisable que prévu.** Aucun des quatre mécanismes nécessaires n'est à inventer :

| Fonction nécessaire | Existe déjà, prouvé par |
|---|---|
| Lire ce qui se passe dans la partie (kills, dégâts, mort, position, météo) | Événements Lua natifs (`OnZombieDead`, `OnHitZombie`, `OnPlayerDeath`…) + mods Twitch/streamers qui les consomment |
| Injecter des événements (pop de zombies, sprinters, objets, hélico, météo, bruit) | `addZombiesInOutfit()`, `addZombiesInOutfitArea()` (utilisés par le tuto vanilla), commandes admin `createhorde`, `chopper`, `gunshot`, `lightning`, `alarm`, `additem`, `addvehicle` |
| Faire parler deux jeux entre eux | **Pas de réseau en Lua**, mais IPC fichier prouvé (SIF lit/écrit `Zomboid/Lua/*.txt`), ou agent Java (ZombieBuddy / pzJBridge) |
| Synchroniser le temps | `getGameTime():getWorldAgeHours()`, `getGameSpeed()`, **`setGameSpeed(0..4)`** (le menu pause et l'UI vitesse vanilla l'utilisent), `isGamePaused()` |

Le vrai risque n'est pas technique, il est **ludique** : l'économie d'événements (1 kill = 2 zombies ?) et l'équité entre deux mondes générés séparément. C'est là que passera l'essentiel des itérations.

Ordre de grandeur : **MVP jouable à deux en 4 à 6 semaines** de travail concentré (≈25-35 jours-homme), dont 3 jours de spikes techniques à faire AVANT de s'engager.

---

## 2. Décomposition fonctionnelle du concept

Un « duel PZ » = 4 fonctions découplées, chacune avec son propre niveau de risque :

1. **Observer** — capter les faits de jeu côté A (kill, dégât, mort, tir, entrée de zone, météo).
2. **Décider** — transformer ces faits en « garbage » pour B selon des règles configurables (taux, caps, anti-neige-boule, combos).
3. **Injecter** — appliquer ces événements dans la partie B (pop, sprinter, objet, bruit, hélico, météo).
4. **Cadencer** — maintenir les deux horloges de jeu synchronisées, gérer pause/mort/crash/reconnexion.

Les fonctions 1 et 3 sont des problèmes résolus par l'écosystème. La 4 est un problème d'ingénierie simple mais plein de cas limites. La 2 est de la conception de jeu, donc itérative.

---

## 3. État de l'écosystème (nov. 2026)

### 3.1 Version du jeu

- **42.20.0 = stable depuis le 29/07/2026**, multi-joueurs inclus ; dernière stable **42.20.4** (26/08/2026).
- **42.21.0 = unstable** (23/09/2026), 42.22 en cours de dev (notes de modding du 27/10/2026).
- Le poste de test local est en **42.20.4** (`version=42.20.4 b0bbce05d5`, console.txt).
- Conséquence : on développe contre 42.20.x et on prévoit une passe de compatibilité à chaque point release. Les mods B41 ne sont pas portables tels quels en B42.

### 3.2 Ce que le Lua de PZ peut faire

- Le Lua de PZ est du **Kahlua** (Lua 5.1 en Java) : accès aux classes Java exposées, **pas de `io.*`/`os.*`**, **pas de socket, pas de HTTP**.
- Le Bac à sable est explicité par la doc de ZombieBuddy : « Unlike Lua mods which run in a sandboxed environment, Java mods are completely unrestricted… Perform network operations. »
- Donc : **toute communication réseau passe par un processus externe ou un agent Java.** Aucun contournement propre en Lua pur.

### 3.3 Précédents directement réutilisables

| Projet | Ce qu'il prouve | Lien |
|---|---|---|
| **Streamer Integration Framework (SIF)** B42.13+ | Un mod SP lit un fichier `Zomboid/Lua/SIF.txt` toutes les minutes, y lit des tables Lua `{type="drunk", level=50}`, exécute, vide le fichier. Des modules spawnent zombies/objets/météo. **C'est exactement le mécanisme d'injection du duel, déjà en production.** | workshop 3387594292 |
| **Twitch Integration / Chaos Mod + Twitch (300+ effets)** | Injection d'événements variés en jeu (zombies nommés, objets, traits, hélico) pilotée par un bot externe | workshop 2629375490, Reddit |
| **Zombie Spawn Manager** (B41/B42, dev) | Spawns administrés avec zones, rayons, counts, outfits, types de marche, contrôleur de comportement runtime. Montre aussi les **limites B42** : plus de setters publics fiables pour sight/hearing/memory/strength par zombie | workshop 3759164356 |
| **ZombieBuddy** (B42+, Java 25) | Framework d'agent Java (ByteBuddy) : patch runtime, **exposition de classes Java au Lua**, JAR chargés depuis les mods, installateur Windows + `-javaagent` sur Linux | github zed-0xff/ZombieBuddy |
| **pzJBridge** (42.21, serveur + client) | Pont Java client/serveur packagé avec installateur, vérification de version des extensions, API pour modders — preuve qu'un pont Java maintenu est réaliste | github rocky6777/pzJBridge-server |
| **PZHook** (B41) / **ZomboidForge** | Hooking Java historique ; ZomboidForge = réflexion depuis Lua. Non retenus pour du neuf : fragiles et contraires aux pratiques B42 | github Olipro/PZHook |

### 3.4 Contrôle externe d'un serveur dédié

- **RCON** opérationnel (port = port de jeu + 1) avec une vraie liste de commandes monde : `createhorde <count> <user>`, `chopper`, `gunshot`, `lightning`, `alarm`, `startrain`/`stoprain`, `addvehicle`, `additem`, `addxp`, `teleport`, `save`, `reloadlua`, `changeoption`.
- Serveur dédié Linux : console via **FIFO `zomboid.socket`**, utilisé par les images Docker.
- Limite : RCON donne du **grossier** (un horde autour d'un joueur), pas « 2 zombies précis à 40 cases ». Pour du fin, il faut du Lua côté serveur.

---

## 4. Les trois architectures possibles

### A. Deux jeux solo + IPC fichier + agent de pont (recommandée pour le MVP)

```
   PC joueur A                          PC joueur B
┌──────────────────┐                ┌──────────────────┐
│ PZ SP (save A)   │                │ PZ SP (save B)   │
│ mod duel Lua     │                │ mod duel Lua     │
│  outbox.txt ─────┼──┐          ┌──┼───── inbox.txt   │
│  inbox.txt  ◄────┼──┤          │  ├───── outbox.txt  │
└──────────────────┘  │          │  └──────────────────┘
                  ┌───▼──────────▼───┐
                  │ agent duel (Python)│  ← horloge partagée,
                  │  règles, caps, log │    arbitrage, scores
                  └────────┬──────────┘
                           │ WebSocket/TCP sortant (relais VPS)
```

- Mod Lua : écrit chaque fait dans `outbox.txt` (1 ligne = 1 événement), lit `inbox.txt` à intervalle court (0,5-2 s), applique, vide.
- Agent local (Python) : tail le fichier, parle au relais, applique les règles, écrit l'inbox, tient **l'horloge de match** et le score.
- Aucun mod réseau, aucun port à ouvrir si l'agent se connecte en sortie à un relais ; LAN possible sans relais.
- **Latence réaliste : 0,5 à 3 s** (SIF fait 60 s par choix de conception, pas par contrainte).
- Inconvénients : deux mods à installer, impossibilité d'anti-triche (le joueur possède sa save), l'agent doit être empaqueté (installeur).

### B. Deux serveurs dédiés + mod Lua serveur + RCON

- Temps **nativement synchrone** : deux serveurs lancés ensemble, mêmes `SandboxVars` (dont `DayLength`), pas de pause possible en MP → dérive nulle tant que les deux tournent.
- Injection par commandes RCON par lots (« 17 kills → `createhorde 34` près de l'adversaire »).
- Aucun mod client → installation simplifiée pour les joueurs.
- Inconvénients : besoin d'héberger/opérer 2 serveurs (RAM/CPU ×2), contrôle grossier, et **les mods en MP B42 sont encore capricieux** (serveurs qui ne chargent pas les mods Workshop, reload Lua qui ferme la session — rapports 42.20). Les commandes RCON n'ont pas de granularité spatiale fine.
- Pertinent si l'objectif devient un **service** (matchmaking, ranked, mondes hébergés) plutôt qu'un outil entre amis.

### C. Agent Java (ZombieBuddy / pzJBridge) dans le client

- Vrai socket/HTTP dans le jeu, événements à la frame, spawns précis, HUD natif.
- C'est l'architecture « produit fini ».
- Inconvénients : installation lourde (agent, approbation de JAR, `-javaagent` sur Linux, installateur Windows), surface de sécurité énorme, **et surtout distribution Workshop plus compliquée** (un mod Java ne s'installe pas en un clic). +3 à 4 semaines et un support utilisateur plus lourd.

### Recommandation

**A pour le MVP**, avec l'agent qui isole déjà la logique de règles et d'horloge : si le jeu plaît, on remplace le transport fichier par C sans réécrire ni les règles ni le protocole. B arrive seulement si on veut un mode hébergé/ranked.

---

## 5. Le point dur n°1 : la synchronisation temporelle

### Ce que le jeu donne

- `getGameTime():getWorldAgeHours()` : double, âge du monde en heures → **métrique d'horloge parfaite** (déjà utilisée par le mod vanilla Last Stand).
- `getGameSpeed()` / `setGameSpeed(int)` : 0 = pause, 1 = normal, 2-4 = accélérations (utilisés par `SpeedControlsHandler.lua`, `ISWorldMap.lua`, `ISPostDeathUI.lua`).
- `isGamePaused()`.
- `getGameTime():getMinutesPerDay()` / `setMinutesPerDay()` : **levier de rattrapage fin** (à valider à l'exécution).
- `GameTime:setTimeOfDay()`, `setDay/setMonth/setYear` : correction absolue possible, mais visible (saut d'heure) → à réserver au démarrage/reprise.

### Mécanisme proposé : horloge maître + bande morte

1. L'agent (ou l'hôte) tient l'horloge de match : `T_cible = T0 + (temps_écoulé_hors_pause) / ratio`, avec `ratio` déduit de `DayLength` (identique des deux côtés).
2. Chaque mod compare `worldAgeHours` local à `T_cible` toutes les 0,25-0,5 s :
   - écart dans ±5 s de jeu → ne rien faire (bande morte, évite le stutter) ;
   - en avance → `setGameSpeed(0)` jusqu'au rattrapage ;
   - en retard → laisser tourner (ou `setGameSpeed(2)` si la règle l'autorise).
3. **Pause locale = pause partagée** : en SP, ouvrir le menu met le jeu en pause. Le mod signale « pause » ; l'agent gèle l'horloge cible pour les deux. Sinon le joueur qui joue prend de l'avance et se fait geler à son tour → comportement incompréhensible.
4. **Hors-ligne / crash** : match en pause pour les deux, avec un délai de grâce (ex. 2 min) puis verdict (abandon = défaite, ou gel illimité si « mode amical »).
5. **Mort** : `OnPlayerDeath` côté A → fin de match (ou « ghost mode » spectateur avec événements réduits).
6. **Reprise de save** : au chargement, recaler l'horloge sur la cible ; si l'écart est énorme (partie rechargée un autre jour), proposer « reprendre le match » ou « invalider ».

### Risques de synchronisation

- `setGameSpeed(0)` en boucle peut entrer en conflit avec les resets vanilla (actions chronométrées, UI, dashboard véhicule) → l'enforcement doit être continu et idempotent, avec un petit cooldown après les pauses légitimes.
- En **MP**, `setGameSpeed` n'a pas de sens : la synchro passe par le démarrage simultané des serveurs (archi B), pas par le gating.
- Performance : lire/écrire un petit fichier toutes les 0,5 s est négligeable ; un `OnTick` qui écrit à chaque tick ne l'est pas (à bannir, bufferiser).

---

## 6. Le point dur n°2 : la conception du duel

### Les mécanismes qui rendent le « duel à la Tetris » lisible

- **Garbage par lots**, pas au coup par coup : accumuler les kills sur 10-15 s de jeu puis envoyer un paquet (« 17 kills → 34 zombies »). C'est plus lisible, plus sûr pour les perfs, et plus proche du modèle Tetris.
- **Annonce de l'arrivée** : sirène/texte avant le pop, 5-10 s de préavis, sinon c'est injuste.
- **Distance de spawn sûre** : 30-60 cases, jamais dans le champ visuel immédiat, jamais dans un bâtiment où le joueur dort.
- **Caps et rendements décroissants** : plafond de garbage par minute, multiplicateur qui décroît, sinon une boucherie au couteau noie l'adversaire en 90 s.
- **Anti-neige-boule** : le joueur en difficulté reçoit un événement positif (caisse de soin, arme, météo clémente) ; le joueur en tête subit un malus (hélico, alarme).
- **Combos** : enchaîner des kills sans être touché augmente le ratio ; être blessé remet le compteur à zéro → ça pousse au risque, pas au farm planqué.
- **Autres leviers que les zombies** : sprinters (1 pour 10 kills), bruit (gunshot/alarme près de B), météo (brouillard/pluie), objet (caisse), électricité/eau, hélico. C'est ce qui donne de la variété sans code supplémentaire lourd — les commandes/API existent.
- **Conditions de victoire** : (a) mort subite (le premier qui meurt perd), (b) score sur N jours de jeu, (c) objectif (atteindre X / tenir une zone). La (a) est la plus simple et la plus dramatique.

### Le problème d'équité structurel

Deux saves séparées = deux mondes différents : loot, véhicules, distribution de zombies, bâtiments. Aucun « seed » partagé dans PZ. Donc :

- même build, mêmes mods, **mêmes `SandboxVars`** (dont `DayLength`, population, loot rareté, start date/heure, difficulté) ;
- même point de départ et même heure de départ → conditions perçues comme équitables ;
- accepter l'asymétrie de loot et la compenser par le score (pas par le monde) ;
- en mode compétitif sérieux : passer en archi B (mondes hébergés, joueurs sans contrôle des fichiers).

### Ce qui est impossible / à ne pas promettre

- Pas de « monde miroir » : on ne clone pas une save en temps réel.
- Pas d'anti-triche en SP : le joueur peut éditer sa save, activer le debug, installer un mod qui triche. Un duel entre amis, oui ; un classement, non.
- Pas de vision des événements à distance : la simulation PZ est locale autour du joueur. « Il a tué un zombie à l'autre bout de la map » n'est pas observable — et ce n'est pas nécessaire : tout se joue sur des compteurs locaux agrégés.

---

## 7. Prérequis

### Techniques

- PZ **42.20.x** des deux côtés, **build identique**, **liste de mods identique** (miroir exact), mêmes `SandboxVars`.
  Note : l'installation locale est en **42.21.0 unstable** (vérifié au démarrage du serveur dédié, cf. `04-spike-0-preuves.md`). Décider explicitement stable ou unstable, et ne pas mélanger les deux.
- Le mod duel en Workshop (ou en local pour prototyper) + l'agent de pont (Python/Go, un binaire par OS).
- Transport : LAN direct, ou relais (petit VPS) si les joueurs sont derrière NAT. Un agent qui se connecte en sortie évite tout port forwarding.
- Si même PC pour deux joueurs : 2 comptes Steam, 2 instances avec `-cachedir` séparés (argument supporté par le lanceur), ~12-16 Go de RAM pour deux clients + agent. Le split-screen existe mais c'est **un seul monde**, donc hors sujet pour le duel.
- Machine de référence mesurée ici : Ryzen 7 7700, 31 Go RAM, RX 7900 XT, PZ en 42.20.4, JVM à 3 Go → deux instances simultanées sont confortables.

### Humains / process

- Deux testeurs réguliers (le plus gros goulot : équilibrer demande des dizaines de duels réels).
- Une passe de compatibilité à chaque point release B42 (42.21, 42.22…).
- Page Workshop + documentation d'installation (2 composants à installer = source de support).

---

## 8. Estimation de charge

| Phase | Contenu | Jours (fourchette) |
|---|---|---|
| 0. Spikes de validation | Mod-test : `setGameSpeed` effectif en jeu, IPC fichier, `addZombiesInOutfit` aux coordonnées du joueur, fréquence `OnZombieDead`, latence de polling | 2-3 |
| 1. Mod cœur | Capture d'événements, sérialisation, inbox/outbox, spawns, HUD (score, horloge, alerte) | 6-9 |
| 2. Agent de pont | Protocole, horloge maître, bande morte, caps, reconnexion, logs de match, config | 5-7 |
| 3. Règles & équilibrage | Table de conversion, combos, anti-neige-boule, conditions de victoire, sessions de test | 4-8 (itératif) |
| 4. Empaquetage & distribution | Installeur, doc, page Workshop, messages d'erreur, compat Linux/Windows | 3-5 |
| 5. QA / durcissement | Cas limites : mort, crash, reload, changement de mods, patch B42, 2 instances | 5-10 |
| **Total MVP** | | **25-42 j** |

Lecture : **~5-8 semaines** en solo à temps partiel sérieux, **4-5 semaines** à plein temps. L'archi C (Java) ajoute 3-4 semaines et un support utilisateur plus lourd. Un mode hébergé/ranked (archi B industrialisée) : 3-6 mois.

Incertitude principale : le temps passé en phase 3 (c'est du game design, pas du code) et les régressions à chaque patch B42.

---

## 9. Risques et parades

| Risque | Impact | Parade |
|---|---|---|
| Le Lua n'a pas de réseau | Bloquant si ignoré | Archi A (IPC fichier) ou C (Java) dès le départ — ne pas tenter de « faire du socket en Lua » |
| Churn de l'API B42 (42.21/42.22) | Casse à chaque patch | Isoler les appels jeu dans une couche `compat/`, tester sur 42.20.x stable, surveiller les notes de modding |
| `setGameSpeed` en conflit avec l'UI vanilla | Stutter, contrôles qui « collent » | Bande morte, cooldown après action/pause, enforcement idempotent |
| Spawn injuste (dans le dos, dans une pièce, pendant le sommeil) | Frustration, morts absurdes | Préavis, distance minimale, liste noire de contextes (sommeil, conduite), cap par minute |
| Mondes non comparables | Sentiment de triche | Sandbox identique + départ identique + score plutôt que comparaison de richesses |
| Save corrompue / crash pendant un spawn | Perte de match | Sauvegarde avant match, spawn bufferisé, mode « match invalide » propre |
| Pas d'anti-triche en SP | Classement non crédible | Assumer « entre amis » ; archi B si compétition |
| Deux composants à installer | Abandon à l'installation | Installeur unique, détection automatique du dossier Zomboid, diagnostic clair |

---

## 10. Ce que je ferais en premier : spike de 3 jours, go/no-go mesurable

Un seul mod jetable + un script Python, sur la save 42.20.4 locale :

1. **J1 — Horloge.** Mod qui, toutes les 0,5 s, lit `getWorldAgeHours()` et force `setGameSpeed(0)` dès que l'avance dépasse la cible. Critère : les deux instances restent à ±5 s de jeu pendant 20 minutes, sans stutter perceptible et sans casser le menu pause.
2. **J2 — Injection.** `addZombiesInOutfit(x, y, z, n, nil, nil)` à 40 cases du joueur, déclenché par un fichier externe. Critère : 20 spawns consécutifs, aucun crash, aucun zombie dans le dos, FPS stable.
3. **J3 — Boucle complète.** Script Python qui tail l'outbox d'une instance, applique « 1 kill → 2 zombies », écrit l'inbox de l'autre. Critère : 15 minutes de duel bout-en-bout entre deux instances, latence mesurée < 2 s, compteur de kills exact.

Si les trois passent : le concept est validé techniquement, on passe au vrai mod. Si J1 échoue (le plus probable des trois échecs), on bascule sur l'archi B ou C avant d'écrire du gameplay.

---

## 11. Décisions à trancher

1. **Architecture du MVP** — (a) SP + IPC fichier + agent *(reco)* · (b) deux serveurs dédiés + RCON · (c) agent Java dès le départ · (d) hybride A maintenant, C ensuite.
2. **Transport réseau** — (a) LAN uniquement pour le MVP · (b) relais VPS (aucun port à ouvrir) *(reco)* · (c) port forwarding chez l'hôte.
3. **Rigueur de synchro** — (a) lockstep strict (±2 s, micro-pauses) · (b) bande morte ±5-10 s *(reco)* · (c) avertissement seulement, sans gel.
4. **Périmètre du premier milestone** — (a) uniquement « kill → zombies », 1 map, 1 condition de victoire *(reco)* · (b) 3 types d'événements · (c) le catalogue complet dès le départ.
5. **Condition de victoire** — (a) mort subite *(reco)* · (b) score sur N jours · (c) objectif de survie/zone.
6. **Distribution** — (a) privé entre amis (dossier local, pas de Workshop) *(reco pour le MVP)* · (b) Workshop public · (c) Workshop + installeur signé.

---

## Sources

- PZwiki, *Build 42* — versions stable/unstable, dates : https://pzwiki.net/wiki/Build_42
- PZwiki, *Game time* — `getGameTime()`, `getMinutesPerDay` : https://pzwiki.net/wiki/Game_time
- PZwiki, *Multiplayer* — MP en ligne depuis 42.13 : https://pzwiki.net/wiki/Multiplayer
- Javadoc 42.x, `LuaManager.GlobalObject` — `setGameSpeed`, `getGameSpeed`, `isGamePaused`, `addZombiesInOutfit`, `getFileWriter` : https://demiurgequantified.github.io/ProjectZomboidJavaDocs/zombie/Lua/LuaManager.GlobalObject.html
- Javadoc 42.x, `GameTime` — `getWorldAgeHours`, `setTimeOfDay`, `setMinutesPerDay` : https://demiurgequantified.github.io/ProjectZomboidJavaDocs/zombie/GameTime.html
- PZEventDoc, *Events* — `OnZombieDead`, `OnHitZombie`, `OnPlayerDeath`, `OnTick`, `EveryTenMinutes` : https://github.com/demiurgeQuantified/PZEventDoc/blob/develop/docs/Events.md
- Vérifications locales dans le jeu installé (42.20.4) : `media/lua/client/ISUI/SpeedControlsHandler.lua` (`setGameSpeed(0..4)`), `media/lua/client/Tutorial/Steps.lua` (`addZombiesInOutfit(x,y,z,n,outfit,…)`), `media/lua/client/LastStand/WinterIsComing.lua` (`getWorldAgeHours()`)
- Steam Workshop, *[B42.20.4+] Streamer Integration Framework* — IPC fichier `Zomboid/Lua/SIF.txt`, polling 60 s, modules d'événements : https://steamcommunity.com/workshop/filedetails/?id=3387594292
- Steam Workshop, *Zombie Spawn Manager* — spawns admin, zones, limites B42 par zombie : https://steamcommunity.com/sharedfiles/filedetails/?id=3759164356
- GitHub, *ZombieBuddy* — agent Java, Java 25, accès réseau des mods Java, approbation de JAR : https://github.com/zed-0xff/ZombieBuddy
- GitHub, *pzJBridge-server* — pont client/serveur 42.21, installateur, extensions Workshop : https://github.com/rocky6777/pzJBridge-server
- GitHub, *Zomboid-Modding-Guide* — Kahlua, pas d'`io.*`/`os.*`, pas de réflexion : https://github.com/FWolfe/Zomboid-Modding-Guide/blob/master/api/README.md
- PingPlayers, *RCON commands* — `createhorde`, `chopper`, `gunshot`, `lightning`, `alarm`, `startrain`, `additem` : https://pingplayers.com/knowledgebase/project-zomboid/rcon-commands-and-how-to-use-them
- The Indie Stone forums, *getFileWriter — relative / direct path* — résolution vers `Zomboid/Lua` : https://theindiestone.com/forums/topic/14698-getfilewriter-relative-direct-path-b323/
- Shockbyte / RespawnHost, état de B42 stable avec MP (sources secondaires, cohérentes avec PZwiki) : https://shockbyte.com/blog/project-zomboid-build-42-multiplayer
