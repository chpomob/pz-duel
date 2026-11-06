---
name: "duel-phase1-mock-to-real"
version: "1.0"
author: "adversarial-spec"
status: "draft"
tags: [adversarial, spec, pz-duel, phase1]
# targets : fichiers du dépôt que le plan crée ou modifie. Les chemins d'exécution n'y figurent pas (§ Portée de targets).
targets:
  - file: service/protocol.py
    description: "Nouveau : validation des lignes événement, commande, heartbeat, ack et session (contrat v1)"
  - file: service/transport.py
    description: "Nouveau : suivi par offset des fichiers out, écriture et troncature de in, lecture de ack, déduplication (boot, seq)"
  - file: service/rules.py
    description: "Nouveau : chargement strict d'une table (dont revalidation sur les atomes actifs) et évaluation déterministe atomes, agrégats, séries, modificateurs, budgets, horloge à ticks, files de lots par cible"
  - file: service/journal.py
    description: "Nouveau : écriture du journal JSONL de match, séquencé et horodaté"
  - file: service/replay.py
    description: "Nouveau : CLI de rejeu d'un journal, sur la même horloge à ticks que le moteur, et résumé de télémétrie"
  - file: service/verdict.py
    description: "Nouveau : calcul du statut attribution/spawn et de la branche de repli, CLI sur un journal"
  - file: service/preflight.py
    description: "Nouveau : vérifications réelles (install, .ini, mods, ports, RAM)"
  - file: service/servers.py
    description: "Nouveau : écriture des réglages gérés, lancement, attente, arrêt gradué des deux serveurs"
  - file: service/rcon.py
    description: "Nouveau : client RCON localhost (save, quit) en stdlib"
  - file: service/snapshots.py
    description: "Nouveau : création et restauration des snapshots de mondes, serveurs arrêtés"
  - file: service/runtime.json
    description: "Nouveau : ports, délais, seuils, tick_ms, hook_window_s et plafonds durs dont limits (aucune valeur dans le code)"
  - file: service/effects.json
    description: "Nouveau : catalogue des atomes (source, hook_proof) et effets déclarés en phase 1, avec paramètres, et catalogue supply_items des objets autorisés"
  - file: service/engine.py
    description: "Sépare le chemin simulé du chemin réel, ajoute le déroulé réel du match, la fenêtre de contrat et la cadence des ticks, honore le mode demandé"
  - file: service/state.py
    description: "Ajoute au dict d'état les champs match_id, capabilities (dont active_atoms, withdrawn), result ; retire tout secret de l'état"
  - file: service/main.py
    description: "Ajoute --install-mod et --pz-dir, arrêt propre des serveurs sur SIGINT/SIGTERM"
  - file: service/presets/duel_rapide.json
    description: "Passe au schéma strict (ids, atomes, budget, score, cible self et items de supply_drop)"
  - file: service/presets/pression.json
    description: "Passe au schéma strict ; remplace fog par weather_rain et retire shake (effets client hors phase 1) ; items de supply_drop"
  - file: service/presets/hardcore.json
    description: "Passe au schéma strict"
  - file: service/presets/demo.json
    description: "Passe au schéma strict"
  - file: mods/duelcore/mod.info
    description: "Nouveau : manifeste racine du mod serveur"
  - file: mods/duelcore/42.21/mod.info
    description: "Nouveau : variante de version (versionMin=42.21.0)"
  - file: mods/duelcore/42.21/media/lua/server/DuelCore.lua
    description: "Nouveau : capture, sondes et hook_seen, heartbeat, lecture des commandes, application des effets (objets de supply_drop reçus par commande)"
  - file: tests/
    description: "Nouveau : suite pytest (fichiers nommés dans les critères, dont test_clock.py), doubles de processus et du mod (tests/doubles/), fixtures de flux d'événements (tests/fixtures/)"
  - file: pytest.ini
    description: "Nouveau : enregistre le marqueur pz_integration"
  - file: README.md
    description: "Documente --mode real, --install-mod, la copie du mod chez l'invité et la procédure de verdict en jeu"
  - file: .gitignore
    description: "Ignore le dossier matches/ (journaux et logs serveurs)"
---

# Phase 1 — du mock au duel réel

Les sections obligatoires portent leur nom anglais ; le contenu est en français, conformément au brief.

## Problem

`pz-duel` a une interface validée mais aucun backend : `service/engine.py` simule le préflight, les serveurs, les joueurs, le score et la dérive. L'option `--mode real` existe, mais `Engine._begin` recrée toujours un état `mode="mock"`. Le préflight affiche des valeurs inventées (« 31 Go RAM ») et l'étape contrat annonce « mods identiques sur A et B » sans aucune mention de simulation. Aucun mod n'existe, les règles des presets ne sont jamais évaluées, aucun journal n'est écrit.

La phase 1 rend le duel réel de bout en bout sans changer le parcours utilisateur : le service pilote deux serveurs dédiés PZ, le mod `duelcore` observe et applique, le service évalue la table et journalise. Le mock reste disponible pour la démonstration, sans jamais présenter une vérification fictive comme réelle.

Deux faits restent non prouvés et décident de l'architecture : l'attribution des kills en multi et le spawn côté serveur. Le produit doit les mesurer et le dire. Il ne doit pas les supposer.

Hors périmètre : publication Steam, matchmaking, classement, éditeur graphique de table, HUD in-game et mod `duelhud`, effets client (`fog`, `shake`), portage du directeur en Lua, agent Java, mode ghost, atome dérivé `idle_check`, Windows (`start-server.bat`), reprise d'un match après la mort du service lui-même.

## Requirements

Les détails normatifs sont dans les sous-sections qui suivent la liste ; ils font partie de l'exigence qui les cite.

- R1: Le service et le mod échangent par fichiers dans `~/Zomboid/Lua/` selon le contrat v1 (§ Contrat de transport), sans perte silencieuse, sans double traitement et sans double application, y compris au redémarrage d'un serveur.
- R2: En mode réel, le service exécute le cycle de vie des deux serveurs (§ Cycle de vie) : préflight réel, écriture des réglages, démarrage, attente de disponibilité, vérification du contrat, arrêt gradué sans processus résiduel.
- R3: Le mod `duelcore` capte les faits de jeu côté serveur, sonde et publie ses capacités réelles, applique les commandes et rend compte de chaque résultat, sans contenir aucune règle ni valeur d'équilibrage (§ Mod duelcore).
- R4: Le moteur de règles charge strictement une table et l'évalue de façon déterministe : atomes, agrégats, séries, modificateurs, budgets, refus de tout ce qui est interdit (§ Moteur de règles).
- R5: Le service conduit le match réel du lobby à la fin : décompte, minuteur, score, mort subite, forfait, dérive, redémarrage d'un serveur, snapshots et revanche (§ Déroulé du match).
- R6: Chaque match produit un journal JSONL horodaté et séquencé, autosuffisant pour le rejeu et exploitable comme télémétrie d'équilibrage (§ Journal).
- R7: L'état exposé dit la vérité sur le mode et ne contient aucun secret : le mock s'annonce simulé à chaque vérification, le mode réel ne retombe jamais sur le mock, le mot de passe admin et le mot de passe RCON n'apparaissent dans aucun log, journal ou état (§ Honnêteté et secrets).
- R8: Le produit mesure l'attribution des kills et le spawn serveur, tolère l'absence d'attribution sans inventer de camp, et signale explicitement la branche de repli quand un critère d'échec est atteint (§ Critères d'échec).
- R9: Le moteur et les briques système se testent sans le jeu : flux synthétiques rejoués, doubles de processus, test d'intégration marqué et ignoré si PZ est absent (§ Stratégie de test).

Classement : R1 à R8 sont non triviales (8, soit le plafond). R9 ne décrit que de l'outillage de test, sans comportement à l'exécution. Le plafond est atteint : toute exigence non triviale nouvelle impose de scinder la spec.

### Arbitrages et contradictions résolues

Ces points tranchent des écarts entre les documents de conception, le brief et la machine. Ils ne modifient pas les documents `01` à `09`.

| Sujet | Écart constaté | Décision |
|---|---|---|
| Format des tables | `06`/`07` : YAML ; contrainte : stdlib seule, sans parseur YAML | JSON, dans les presets existants, en gardant la syntaxe `when` déjà écrite |
| Directeur | `08` hésite entre Python et Lua | Python (brief : portage Lua hors objectif) |
| Clé de port | `02` écrit `Port=` ; le `.ini` réel écrit `DefaultPort=` | `DefaultPort` |
| Second port UDP | `duelA.ini` et `duelB.ini` ont tous deux `UDPPort=16262` | Ports UDP distincts, passés par `-udpport` (argument présent dans `GameServer.class`) et écrits dans le `.ini` |
| Port de l'invitation | le mock invite sur 17261, le serveur de l'hôte | En réel, l'hôte joue sur A et l'invité reçoit le port de B |
| Unité de la dérive | `02` : « secondes de jeu » ; `07` : « 60 s » | Secondes réelles. Avec `DayLength=1`, 60 s de jeu valent 0,625 s réelle : ce seuil invaliderait tout match |
| Base de la dérive | spike 0 : un serveur vide n'avance pas au rythme nominal | Avance d'horloge depuis le début du match, pas valeur absolue |
| Granularité | `07` : lots de 10-15 s, mais p95 < 3 s | Transport à 1 s. Les lots naissent du cooldown (§ Budgets) : le premier effet part aussitôt, les suivants se cumulent |
| Cooldown et plafond | `per_effect_cooldown_s: 20` rendrait faux « 1 kill → 2 zombies (max 60/min) » si l'effet était jeté | Le cooldown diffère et cumule ; seul `cap_per_min` retire des unités |
| Dédup par `seq` | le fichier `out` est en ajout et un mod relancé perd son compteur : la dédup par `seq` seul jetterait de vrais événements | Clé `(boot, seq)`, `boot` fourni par le service à chaque lancement |
| Revanche | `08` : serveurs maintenus ; `02` : restauration serveurs arrêtés | Serveurs arrêtés, restauration, relance. Le code de match et le mot de passe sont conservés |
| Effets du preset Pression | `fog`, `shake` sont des effets client, hors objectif | `fog` devient `weather_rain` de même durée, `shake` est retiré, le résumé est corrigé |
| Distance de `supply_drop` | `06` : `[10, 25]` ; `07` et le mod : plancher de 25 | Un seul plancher de 25 pour tout effet positionné : `supply_drop` passe à `[25, 40]`. Le plancher reste vérifiable sans exception par effet |
| `defender_bonus` | `06` : un nom d'effet ; or `supply_drop` exige des paramètres | Objet effet complet, de même schéma qu'un effet de règle |
| Acceptation du contrat | `06` : l'invité accepte ; aucun écran invité n'existe | En phase 1 le contrat est scellé par hash côté service ; la connexion de l'invité vaut acceptation |
| Repli de `damage_taken` | `07` : repli sur `OnCharacterDeath` + PV du heartbeat ; ce repli donnerait un atome de sémantique différente, non observé | Aucune source de substitution : sans `hook_seen`, l'atome est retiré des atomes actifs du match, et la table est jouée sans ce qui en dépend (§ Cycle de vie) |
| Fin de la fenêtre de contrat | à l'étape `contract` aucun joueur n'est connecté (H3) : un hook qui exige un coup ne peut pas y être vu, tout preset qui en dépend serait toujours refusé | La fenêtre de contrat s'étend jusqu'à l'entrée en décompte (§ Cycle de vie) |
| Contenu de `supply_drop` | R3 interdit tout équilibrage dans le mod ; aucun champ ne portait les objets | Le contenu est dans la table (`items`), borné par `limits` et restreint au catalogue `supply_items` |
| Temps du moteur | `mt` « tiré de `t_ms` » excluait toute émission en silence | Grille de ticks de 250 ms, cadencée par l'instant courant ; `t_ms` date les événements, jamais les ticks (§ Horloge du moteur) |

Vérifiés sur la machine pour cette spec : `getServerName()`, `getOnlinePlayers()`, `getTimestampMs()`, `getClimateManager()` et `addSound` existent dans `LuaManager$GlobalObject` ; `getAttackedBy()`, `getZombieKills()`, `isAsleep()`, `getVehicle()` dans `IsoGameCharacter` ; `setWalkType` dans `IsoZombie`. Leur présence ne prouve pas leur effet côté serveur en multi. Vérifiés aussi : `getTimestampMs()` renvoie `System.currentTimeMillis()`, donc la même base que l'horloge du service sur le même hôte ; `instanceItem(String)` existe dans `LuaManager$GlobalObject` ; `Base.Bandage` et `Base.Disinfectant` existent dans `media/scripts/` ; `OnWeaponHitCharacter` est déclenché dans `IsoGameCharacter.Hit(HandWeapon, wielder, …)`, y compris pour une cible zombie.

Pièges constatés : `~/Zomboid/version.txt` est périmé (42.20.4 pour un build 42.21) ; `~/Zomboid/server-console.txt` est commun aux deux serveurs ; `start-server.sh` lance `java` comme enfant, sans `exec`. La version vient donc de la sortie du serveur, la disponibilité de sa sortie standard, et l'arrêt vise le groupe de processus.

Hypothèses non vérifiées, à confirmer au premier lancement réel :

- H1 : un `.ini` partiel écrit avant la première création d'un `servername` est conservé et complété par PZ. La relecture du contrat détecte l'échec.
- H2 : un client doit posséder le mod `duelcore` localement pour rejoindre, même sans code client. Sans Workshop, l'invité copie donc le dossier à la main (README).
- H3 : le serveur arrête de tiquer sans joueur (spike 0). Aucun heartbeat n'est donc exigé avant la première connexion.

### Contrat de transport

Fichiers, avec `X` valant `A` ou `B` : `duel_X_out.txt` (mod → service, ajout), `duel_X_in.txt` (service → mod), `duel_X_ack.txt` (mod → service, réécrit), `duel_X_session.txt` (service → mod, écrit avant chaque lancement). Une ligne est un objet JSON UTF-8 terminé par `\n`. Tout champ inconnu est ignoré ; un `v` différent de 1 rejette la ligne.

Le mod déduit son camp de `getServerName()` : `duelA` donne `A`, `duelB` donne `B`. Tout autre nom laisse le mod inerte.

**Session** :
```json
{"v":1,"match":"8f3a2c","side":"A","boot":2,"cmd_floor":17}
```

**Événement** (toute ligne de `out`) :
```json
{"v":1,"boot":2,"seq":42,"t_ms":1793830000123,"src":"A","side":"A","kind":"zombie_kill","data":{"w":"Base.Axe","x":10500,"y":9900,"origin":"world"}}
```
`src` est le serveur émetteur. `side` est le camp de l'auteur, ou `null` s'il est inconnu. `seq` repart de 1 à chaque `boot`. `kind` vaut un atome du catalogue, ou `player_death`, `caps`, `hook_seen`, `cmd_result`, `beat`. `data.origin` vaut `duel` pour un zombie créé par une commande du duel, sinon `world`. Pour `hook_seen`, `data` vaut `{"hook":"OnPlayerGetDamage"}` et `side` vaut `src`.

**Heartbeat**, tous les 10 s de temps réel quand le serveur tique, dans `out` :
```json
{"v":1,"boot":2,"seq":43,"t_ms":1793830010123,"src":"A","side":"A","kind":"beat","data":{"age":2.4657,"players":["durand"],"hp":87,"kills":41}}
```
Sans joueur : `players` vaut `[]`, `hp` et `kills` valent `null`.

**Capacités**, première ligne de chaque `boot` :
```json
{"v":1,"boot":2,"seq":1,"t_ms":1793830000000,"src":"A","side":"A","kind":"caps","data":{"build":"42.21.0","mod":"0.1.0","protocol":1,"hooks":{"OnZombieDead":true,"OnPlayerGetDamage":false},"functions":{"addZombiesInOutfit":true},"atoms":["zombie_kill"],"effects":["spawn_pop"]}}
```

**Commande** (ligne de `in`) :
```json
{"v":1,"seq":7,"kind":"spawn_pop","n":2,"distance":[30,60],"warn_s":6,"origin":{"rule":"kill_baseline","boot":2,"event_seq":42}}
```
`seq` est strictement croissant sur tout le match, à travers les `boot`. Une commande `supply_drop` porte ses objets :
```json
{"v":1,"seq":8,"kind":"supply_drop","distance":[25,40],"warn_s":3,"items":[{"item":"Base.Bandage","count":2},{"item":"Base.Disinfectant","count":1}],"origin":{"rule":"hurt_supply","boot":2,"event_seq":51}}
```

**Résultat** (événement `cmd_result`) : `data` vaut `{"seq":7,"status":"applied|refused|failed","reason":"…","spawned":2}`. `spawned` n'existe que pour les effets de spawn.

**Ack** (contenu unique de `ack`, réécrit à au plus 5 s d'intervalle) :
```json
{"v":1,"boot":2,"seq":7}
```

Sémantique côté lecture de `out` :

- Le service suit un offset en octets et ne traite que les lignes complètes ; le reste attend la lecture suivante.
- Si la taille du fichier devient inférieure à l'offset, l'offset repart de 0.
- Une ligne de `(boot, seq)` inférieur ou égal au dernier couple accepté pour ce camp, en ordre lexicographique, est un doublon : elle est comptée et jamais retraitée.
- Un trou de `seq` dans un même `boot` produit un enregistrement `seq_gap` : la perte est visible, rien n'est reconstitué.
- Une ligne invalide produit `bad_line` et ne bloque pas les suivantes.
- Une ligne dont `src` n'est pas le camp du fichier, ou dont `side` n'est ni `src` ni `null`, est rejetée en `side_mismatch` : aucun serveur ne crédite l'autre camp.

Sémantique de `in` et `ack` :

- Le service écrit `in` en un seul `write` suivi d'un `fsync`, et il est le seul écrivain.
- Le mod relit tout `in` à chaque passage (au plus 1 s), ignore toute ligne incomplète ou de `seq` inférieur ou égal à son dernier `seq` traité ou à `cmd_floor`.
- `ack` signifie « commande prise en charge » ; le résultat arrive par `cmd_result`.
- Le service tronque `in` à 0 octet, sans supprimer le fichier, seulement si `ack.boot` est le `boot` courant et `ack.seq` égale le dernier `seq` écrit, sans écriture entre la lecture de `ack` et la troncature.
- Un `ack.seq` supérieur au dernier `seq` écrit ou d'un autre `boot` est journalisé et ne tronque rien.
- Sans ack après `ack_timeout_s`, le service journalise `ack_timeout` et ne réémet rien.

Redémarrage d'un serveur, garantie « au plus une fois » pour les commandes :

1. Avant de relancer, le service tronque `in`, écrit la session avec `boot` + 1 et `cmd_floor` égal au dernier `seq` écrit.
2. Toute commande écrite sans `cmd_result` reçu est journalisée `lost_on_restart` avec son `seq` et libère sa place `concurrent_effects`. Elle reste comptée dans `threats_per_minute` et dans le cooldown. Elle n'est jamais rejouée.
3. Le service conserve offset, couple `(boot, seq)` et état des règles.

### Cycle de vie

Ports, délais et seuils viennent de `service/runtime.json` : jeu 17261 / 17271, UDP 17262 / 17272, RCON 27015 / 27016, `server_start_timeout_s`, `stop_timeout_s`, `min_free_ram_mb`. Le fichier porte aussi tous les autres délais, seuils et plafonds nommés dans cette spec, dont `max_units_per_command`, `cmd_result_timeout_s`, `tick_ms` (250), `hook_window_s` (120) et l'objet `limits` (§ Moteur de règles).

**Préflight**, avant tout lancement ; chaque échec donne un message français actionnable et l'étape `preflight` en `error` :

- installation : `--pz-dir`, sinon `PZ_INSTALL_DIR`, sinon les chemins Steam connus, avec `start-server.sh` exécutable ;
- ports : six ports libres, testés par liaison réelle, UDP pour le jeu et TCP pour RCON ;
- RAM : `MemAvailable` de `/proc/meminfo` au moins égal à `min_free_ram_mb` ;
- mod : `~/Zomboid/mods/duelcore/<version>/mod.info` et `media/lua/server/` présents, empreinte identique à celle du dépôt, sinon message citant `--install-mod` ;
- `.ini` : les fichiers présents sont lisibles ;
- preset : table valide.

Le détail de l'étape rapporte les valeurs mesurées. `--install-mod` est le seul chemin qui écrit dans `~/Zomboid/mods/`, et seulement dans `duelcore/`.

**Réglages gérés**, écrits avant lancement sans toucher au reste du fichier (commentaires compris) : dans `duelX.ini`, `DefaultPort`, `UDPPort`, `RCONPort`, `RCONPassword`, `Password`, `PauseEmpty=false`, `Mods=duelcore`, `WorkshopItems=`, `UPnP=false`, `Public=false`, `MaxPlayers=1`, `SaveWorldEveryMinutes=5` ; dans `duelX_SandboxVars.lua`, `DayLength` et les `Start*`, identiques sur A et B. Le `.ini` a les droits `0600`. Le mot de passe admin et le mot de passe RCON sont tirés au hasard à chaque lancement et gardés en mémoire.

**Démarrage** : les deux serveurs partent ensemble, chacun dans son groupe de processus, avec toujours `-servername`, `-port`, `-udpport`, `-adminpassword`. Un serveur est prêt quand sa sortie standard contient `*** SERVER STARTED ****` puis `server is listening on port <son port>`. Une sortie anticipée ou le dépassement de `server_start_timeout_s` met l'étape du serveur en `error`, avec la fin du log expurgée, et arrête l'autre serveur. Les sorties sont écrites expurgées dans `matches/<match_id>/server_X.log`.

**Contrat**, après disponibilité, sur les fichiers relus et non sur ce qui a été écrit :

- `Mods` et `WorkshopItems` égaux à ceux du preset sur A et B ;
- `PauseEmpty=false` sur A et B ;
- `DefaultPort`, `UDPPort`, `RCONPort` distincts entre A et B ;
- `DayLength` et `Start*` égaux à la configuration du match ;
- `caps` reçu des deux côtés dans `contract_timeout_s`, avec `protocol` et `mod` identiques, `build` égal à celui du log serveur ;
- chaque atome et effet de la table présent dans `caps.atoms` et `caps.effects` des deux camps.

Le moindre écart refuse le match et nomme la clé et les valeurs en cause.

**Fenêtre de contrat et preuve des hooks.** Un atome du catalogue peut porter `hook_proof`, le nom du hook dont il dépend ; en phase 1, seul `damage_taken` en porte un (`OnPlayerGetDamage`). Un tel atome n'est actif que si ce hook a produit un `hook_seen` sur A et sur B. La liste des atomes de `caps` ne le prouve pas.

- La fenêtre s'ouvre au premier `caps` du match. Le contrôle de l'étape `contract` ci-dessus ne juge donc aucun `hook_seen` : sans joueur, il n'y a rien à voir.
- Sa clôture suit l'instant `L`, où les deux joueurs sont connectés et les deux sondes de spawn résolues. Si la table ne contient aucun atome à `hook_proof`, elle se ferme à `L`. Sinon, elle se ferme dès que les hooks requis sont vus sur les deux camps, et au plus tard à `L` + `hook_window_s`.
- À la clôture, les atomes actifs sont ceux présents dans `caps.atoms` des deux camps, moins tout atome à `hook_proof` dont le hook n'a pas été vu sur les deux camps. Chaque atome retiré donne un enregistrement `atom_withdrawn` (atome, hook, camps en défaut) et figure dans `capabilities.withdrawn`. `capabilities.active_atoms` publie la liste retenue.
- La table jouée perd alors les atomes retirés, les règles qui les citent, absence comprise, leurs points, et le combo s'il en dépend. Elle est revalidée par le chargeur strict, le catalogue étant restreint aux atomes actifs. La table scellée et son hash ne changent pas, et le rejeu refait ce retrait depuis les `atom_withdrawn`.
- Le match se poursuit avec les atomes survivants, le retrait restant journalisé et publié. Seule une table privée de tout atome refuse le match : phase `error`, serveurs arrêtés, `error` en français nommant l'atome, le hook et le camp. `fallback` n'est pas modifié : ce refus ne conclut rien sur l'architecture.
- La décision vaut pour tout le match : un redémarrage de serveur ne rouvre pas la fenêtre, et un `hook_seen` tardif ne réactive rien.
- Pendant la fenêtre, le détail de l'étape `contract` nomme le hook attendu, les camps en défaut et le temps restant. `hook_seen` prouve que le hook a été reçu, pas qu'une blessure par zombie le déclenche : ce point relève de la vérification en jeu (AC19).

**Arrêt** gradué : RCON `save` puis `quit` ; à défaut `quit` sur l'entrée standard ; après `stop_timeout_s`, SIGTERM au groupe ; après 10 s, SIGKILL au groupe. `cancel`, une erreur, la fin du match et SIGINT/SIGTERM du service empruntent tous ce chemin.

### Mod duelcore

- Layout `mods/duelcore/mod.info` et `mods/duelcore/42.21/{mod.info,media/lua/server/DuelCore.lua}`. Lua PZ uniquement, sans `os.*`, `io.*` ni dépendance.
- Atomes de phase 1 : `zombie_kill`, `damage_taken`, `build`, `vehicle_repair`, `explosion`. Événement de contrôle : `player_death`.
- `damage_taken` : émis sur `OnPlayerGetDamage` pour une blessure du joueur du serveur (`BLEEDING`, `WEAPONHIT`), et sur `OnBeingHitByZombie`, déclaré par le jeu mais jamais tiré en 42.21. `side` est ce camp, `data` porte `hp`, `damage` et `attacker` s'il est connu. Une blessure reste un seul atome tant que 5 s sans signal ne se sont pas écoulées. `OnWeaponHitCharacter` ne suit qu'un coup d'arme de joueur et n'en est plus la source.
- `zombie_kill` : l'auteur vient de `getAttackedBy()`, sinon du dernier attaquant retenu via `OnHitZombie`. Sans auteur joueur, `side` vaut `null`. Le mod n'infère jamais l'auteur du seul fait que le serveur a un joueur.
- Sondes : à l'initialisation, `caps` rapporte pour chaque hook si son enregistrement a réussi, et pour chaque fonction d'effet si elle existe. Pour `OnPlayerGetDamage`, `OnEnterVehicle` et les autres hooks incertains, un enregistrement réussi ne vaut pas preuve : le mod émet `hook_seen` à la première réception réelle de chaque hook.
- Les lignes `out` sont regroupées et vidées au plus toutes les 1 s.
- Effets déclarés : `spawn_pop`, `spawn_sprinter`, `horde_area`, `noise`, `weather_rain`, `supply_drop`, et la commande de service `probe_spawn`. `combo_reset` reste interne au moteur.
- Application : le mod attend `warn_s`, choisit une position à une distance comprise dans `distance` si l'effet en porte une, puis applique. Il refuse (`refused`) une commande sur un joueur endormi ou en véhicule, sans joueur connecté, ou dont `distance[0]` est inférieur à 25. Une exception Lua donne `failed`, sans interrompre la boucle. Un `kind` inconnu donne `failed` avec `unknown_kind`.
- `supply_drop` : le mod crée d'abord tous les objets de `items` par `instanceItem` (sondée dans `caps.functions`), `count` fois chacun, puis les dépose au sol à la position choisie. Un objet que le jeu ne connaît pas donne `failed` avec `unknown_item`, et rien n'est déposé.
- Le mod ne contient ni règle, ni score, ni budget, ni nom d'objet : les quantités et le contenu viennent des commandes.

### Moteur de règles

**Table** (preset JSON) : `id`, `label`, `tagline`, `duration_s`, `day_length`, `mods`, `atoms`, `rules`, `modifiers`, `budget`, `score`, `disconnect_grace_s`, `summary`, toutes obligatoires.

**Schéma strict**, à tous les niveaux de la table. Une clé absente des listes ci-dessous est refusée. Une clé obligatoire manquante, une valeur de mauvais type ou hors bornes est refusée. « Entier » exclut les booléens et les flottants.

| Clé de table | Valeur |
|---|---|
| `id` | identifiant (`[a-z0-9_]+`), égal au nom du fichier sans extension |
| `label`, `tagline` | chaîne non vide |
| `duration_s`, `disconnect_grace_s` | entier ≥ 1 |
| `day_length` | entier, clé de la table `DayLength` de `runtime.json` (1 à 7) |
| `mods` | exactement `["duelcore"]` en phase 1 |
| `atoms` | liste non vide, sans doublon, d'atomes du catalogue ; contient tout atome cité par un `when`, par `score.points` et par `modifiers.combo` |
| `rules` | liste non vide d'objets règle |
| `modifiers` | objet aux clés facultatives `combo` et `score_gap` ; `{}` est permis |
| `budget` | objet, voir ci-dessous |
| `score` | objet, voir ci-dessous |
| `summary` | liste non vide de chaînes |

| Objet | Clés obligatoires | Clés facultatives |
|---|---|---|
| règle | `id` (identifiant unique dans la table), `when`, `effects` (liste non vide) | `every` (entier ≥ 1, permis seulement si `when` est un atome seul), `sides` (liste non vide, sans doublon, de `"A"` et `"B"`) |
| `budget` | `threats_per_minute`, `concurrent_effects` (entiers ≥ 1), `per_effect_cooldown_s` (entier ≥ 0) | aucune |
| `score` | `points` : objet non vide, atome de `atoms` vers entier ≥ 0 | aucune |
| `modifiers.combo` | `per` (nombre > 0), `max` (nombre ≥ 1), `on`, `reset_on` (atomes de `atoms`) | aucune |
| `modifiers.score_gap` | `when_gt` (entier ≥ 0), `attacker` (nombre > 0), `defender_bonus` (objet effet sans `target`, la cible étant le camp mené) | aucune |

Chaque effet porte `kind`, plus les paramètres de son type. `service/effects.json` reproduit ce tableau ; le chargeur n'accepte aucun autre paramètre.

| `kind` | Paramètres obligatoires | Paramètres facultatifs |
|---|---|---|
| `spawn_pop`, `spawn_sprinter` | `n`, `distance`, `warn_s` | `cap_per_min`, `target` |
| `horde_area` | `n`, `radius`, `distance`, `warn_s` | `cap_per_min`, `target` |
| `noise` | `radius`, `distance`, `warn_s` | `target` |
| `weather_rain` | `duration_s`, `warn_s` | `target` |
| `supply_drop` | `distance`, `warn_s`, `items` | `target` |

`items` est une liste non vide d'objets aux seules clés obligatoires `item` et `count`. Chaque `item` figure dans le catalogue `supply_items` de `service/effects.json`, et il apparaît au plus une fois dans la liste. Ce schéma s'applique aussi à `defender_bonus`.

`supply_items` livré : `Base.Bandage`, `Base.Bandaid`, `Base.Disinfectant`, `Base.AlcoholWipes`, `Base.Antibiotics`, `Base.Pills`. Les presets `duel_rapide` et `pression` livrent `items` = 2 `Base.Bandage` et 1 `Base.Disinfectant` (« caisse de soin »). `hardcore` et `demo` n'ont pas de `supply_drop`.

`probe_spawn` figure au catalogue comme commande de service, et une table ne peut pas la citer. `combo_reset` n'est pas un effet de table : `modifiers.combo.reset_on` en tient lieu.

Bornes, dans l'objet `limits` de `runtime.json`, avec leurs valeurs livrées :

| Paramètre | Borne |
|---|---|
| `n` | entier de 1 à `max_n` (30) |
| `distance` | `[min, max]` entiers, `min_distance` (25) ≤ min ≤ max ≤ `max_distance` (100) |
| `warn_s` | entier de 0 à `max_warn_s` (30) |
| `radius` | entier de 1 à `max_radius` (50) |
| `duration_s` | entier de 1 à `max_effect_duration_s` (300) |
| `cap_per_min` | entier de 1 à `max_cap_per_min` (120) |
| `items` | de 1 à `max_supply_entries` (5) entrées |
| `items[].count` | entier de 1 à `max_item_count` (10) |
| `target` | `"self"` ou `"opponent"` (défaut `"opponent"`) |
| `budget.threats_per_minute` | au plus `max_threats_per_minute` (12) |
| `budget.concurrent_effects` | au plus `max_concurrent_effects` (8) |

Une commande reprend les paramètres de l'effet, sans `cap_per_min` ni `target`, avec le `n` modifié.

**Score** : chaque événement accepté, de `side` non nul et postérieur au début du match, ajoute `score.points[kind]` au camp `side`. Un atome absent de `points` vaut 0.

**Grammaire de `when`** :
```
when  := base [ ".and(not " atom "." within ")" ] [ ".then(" atom ", within=" dur ")" ]
base  := atom [ ".count(" entier ")." within ]
atom  := "atom(" identifiant ")"
within:= "within(" dur ")"
dur   := entier "s"
```
Seuls les événements avec `side` non nul et postérieurs au début du match alimentent les règles. Les dates sont définies au § Horloge du moteur.

- **Atome** : `atom(x)` avec `every: N` se déclenche à chaque N-ième occurrence (`every` vaut 1 par défaut).
- **Agrégat** : `count(N).within(Ds)` se déclenche quand N occurrences tiennent dans les D dernières secondes. Le déclenchement consomme ces N occurrences. Six kills à t = 0,1,2,3,4,5 avec `count(3).within(10s)` déclenchent à t = 2 et t = 5, pas quatre fois.
- **Absence** : `.and(not atom(y).within(Ds))` exige zéro `y` dans les D secondes précédentes, à l'instant du déclenchement. Si elle échoue, rien n'est consommé et la condition est réévaluée au prochain événement.
- **Série** : `.then(atom(b), within=Ds)` arme la règle quand la base se réalise. Le premier `b` dans l'intervalle ouvert à gauche `]t, t+D]` déclenche et désarme. Une nouvelle réalisation de la base repousse l'échéance. L'échéance dépassée désarme sans effet. Elle se juge sur le `te` du `b` traité, jamais sur `mt` : un `b` en retard mais dans l'intervalle déclenche.
- **Filtre anti-boucle** : un `zombie_kill` d'`origin` `duel` compte au score mais n'alimente aucune règle, pour éviter la conversion en boucle de nos propres spawns.
- **Camps** : une règle s'applique aux deux camps, sauf `sides` explicite. Un effet vise l'adversaire de l'auteur, sauf `target: "self"`.

**Horloge du moteur**. Deux temps coexistent, et chacun a son usage :

- `t0_ms` est l'instant d'entrée en `running`, journalisé dans l'enregistrement `state` de cette transition. Ce relevé est le seul à donner une valeur issue de l'horloge du service.
- `te` = (`t_ms` − `t0_ms`) / 1000 est la date source d'un événement. « Postérieur au début du match » signifie `te` ≥ 0. Le score et les fenêtres de règles (`count`, `within`, `then`, absence) se calculent sur `te`.
- `mt` est le tick courant, en secondes : le tick `k` vaut `k` × `tick_ms`, à partir de 0. Cooldown, fenêtres de 60 s, `concurrent_effects`, `cmd_result_timeout_s` et fin de match se calculent sur `mt`.

Règles d'avancement :

1. **Pas** : entre le dernier `t_ms` connu et l'instant courant, `mt` avance par pas de 250 ms. À chaque passage, le service exécute dans l'ordre tous les ticks non clos jusqu'au dernier multiple de 250 ms inférieur ou égal à l'instant courant. Aucun tick n'est sauté, en silence comme en rattrapage.
2. **Instant courant** : l'horloge du service en direct, le temps fourni par le test en test, le `mt` du journal au rejeu. Cet instant cadence les ticks et n'entre dans aucun calcul de règle. Un `t_ms` en avance sur l'instant courant ne fait jamais avancer `mt`.
3. **Rattachement** : tout intrant reçu (événement accepté, dont `cmd_result` ; `state` ; `lost_on_restart` ; `match_end`) est traité au prochain tick à exécuter, jamais entre deux ticks.
4. **Retard** : un événement dont `te` est inférieur ou égal au `mt` du dernier tick clos est traité au tick suivant, avec un enregistrement `late_event` (`boot`, `seq`, `te`, `mt`). Il garde son `te`, n'est ni rejeté ni redaté. Les lignes `out` étant vidées au plus toutes les 1 s, ce cas est courant en réel : `late_event` mesure le délai de transport et ne signale pas une panne.
5. **Ordre dans un tick** : d'abord les événements, triés par `(t_ms, src, boot, seq)`, retards compris ; puis les intrants du service, dans leur ordre de création, `match_end` en dernier ; puis les échéances (`cmd_timeout`, fin de `duration_s`) ; enfin l'émission des lots. À `mt` égal, tout événement est donc traité avant toute émission. Un tick qui traite `match_end` n'émet rien.

Avant `running`, `mt` est `null` et aucun tick n'existe. L'enregistrement `state` d'entrée en `running` ouvre le tick 0 : il porte `mt`=0 et précède tout ce que ce tick traite.

**Modificateurs**, sur le `n` des effets qui en ont un, dans cet ordre, l'état étant mis à jour avant l'évaluation de l'événement :

1. `combo` (`per`, `max`, `on`, `reset_on`) : multiplicateur `min(max, 1 + per × série)`.
2. `score_gap` (`when_gt`, `attacker`, `defender_bonus`) : si score auteur − score adversaire > `when_gt`, multiplicateur `attacker`, et l'adversaire reçoit l'effet `defender_bonus`.

Le résultat est `max(1, floor(n × produit))`. Exemple : n=2, combo 1,6, écart 0,6 donne 1.

**Budgets**, garde-fous appliqués à chaque effet produit, dans cet ordre :

1. `cap_per_min` : les unités admises sur l'intervalle `]mt − 60, mt]`, par règle et effet, sont plafonnées ; l'excédent est retiré et journalisé `effect_capped`.
2. Les unités admises rejoignent un lot en attente de clé (cible, type, paramètres). `n` ne fait pas partie de la clé ; `items` et tous les autres paramètres en font partie. Un effet sans `n` déjà en attente est fusionné (`effect_coalesced`).
3. Chaque camp cible a sa propre file de lots, FIFO dans l'ordre de création des lots. Un lot bloqué ne bloque jamais l'autre file : un lot vers un serveur `restarting` ne retient aucun lot vers l'autre camp, même plus récent. À chaque tick, après les intrants, la file de A puis celle de B sont servies : le lot de tête part en **une** commande si les quatre conditions tiennent, et ainsi de suite tant que la tête part. La première tête bloquée arrête sa file jusqu'au tick suivant, car aucun lot n'en dépasse un autre dans une même file. Les conditions : `per_effect_cooldown_s` écoulé depuis la dernière émission de ce type à cette cible ; moins de `threats_per_minute` commandes vers cette cible sur l'intervalle `]mt − 60, mt]` ; moins de `concurrent_effects` effets en cours sur cette cible ; serveur de la cible hors de l'état `restarting`.
4. Un effet est en cours de l'écriture jusqu'à son `cmd_result`, plus `duration_s` s'il en a une. Au premier tick où `mt` ≥ `mt` d'écriture + `cmd_result_timeout_s`, s'il n'a toujours pas de `cmd_result`, il libère sa place et est journalisé `cmd_timeout`. `lost_on_restart` libère aussi la place. Une place ne se libère qu'une fois.
5. Un lot dépassant `max_units_per_command` part par tranches.
6. À la fin du match, les lots restants sont journalisés `effect_dropped`.

**Interdits**, refusés au chargement avec un message français nommant la règle, ou la section hors règle, et le champ :

- effet absent de `service/effects.json` ;
- atome absent du catalogue, ou de `source` autre que `server` (`craft`, `AddXP`, `LevelPerk` n'y figurent pas) ;
- clé ou paramètre hors du schéma strict, paramètre obligatoire manquant, ou valeur hors de `limits` (`distance[0]` inférieur à 25, budget supérieur au plafond) ;
- `probe_spawn` ou `combo_reset` cités comme effet ;
- objet de `items` absent de `supply_items`, ou cité deux fois ;
- `when` hors grammaire ;
- `budget` absent ;
- toute tentative d'agir sur la pause, l'horloge ou la vie du joueur, qui ne peut être exprimée que par un effet non déclaré.

Le repli craft par diff d'inventaire n'est pas livré en phase 1 : l'atome n'existe pas au catalogue.

**Hash** : SHA-256 du preset en JSON canonique (clés triées, sans espace, UTF-8). Il est calculé au lancement, écrit dans `config.rules_hash` et dans le journal, et recontrôlé sur le fichier à l'entrée du décompte.

### Déroulé du match

Phases et étapes de l'UI inchangées (`preflight`, `starting`, `contract`, `lobby`, `countdown`, `running`, `stopping`, `over`, `error`).

1. **Préparation** : restauration des snapshots, écriture des réglages et des sessions (`boot`=1), vidage de `out`, `in`, `ack`, lancement.
2. **Lobby** : `invite` donne l'IP locale, le port de **B**, le mot de passe joueur et le code. `players` se remplit depuis les heartbeats. L'absence de heartbeat avant la première connexion n'est pas une panne. Chaque camp reçoit une `probe_spawn` à sa première connexion.
3. **Décompte** : 3-2-1 quand les deux joueurs sont connectés, les deux sondes résolues et la fenêtre de contrat close sans refus (§ Cycle de vie).
4. **Running** : le minuteur vit dans le service et ne s'arrête jamais. Le service relève l'`age` de référence au premier heartbeat de chaque camp, et les `kills` de référence au premier heartbeat de chaque camp dont `kills` n'est pas nul.
5. **Fin** : voir ci-dessous. Les serveurs sont arrêtés, `result` est rempli (`outcome`, `winner`, `reason`).

| Cas | Condition | `outcome` |
|---|---|---|
| Minuteur | `remaining_s` à 0 | `win` au meilleur score, `draw` à égalité |
| Mort subite | `player_death` d'un camp | `death`, l'autre camp gagne immédiatement |
| Déconnexion | joueur absent des heartbeats pendant `disconnect_grace_s` (120 s) | `forfeit` |
| Double déconnexion | les deux camps atteignent le délai | `invalidated` |
| Dérive | \|dérive\| strictement supérieure à `max_drift_s` (60 s) | `invalidated` |
| Serveur perdu | deuxième sortie inattendue d'un même serveur (`max_server_restarts` = 1) | `invalidated` |

**Dérive**, en secondes réelles : `((ageA − ageA0) − (ageB − ageB0)) × minutes_par_jour × 2,5`, avec `minutes_par_jour` lu dans la table `DayLength` de `runtime.json` (1 → 15, 2 → 30, 3 → 60, 4 → 90, 5 → 120, 6 → 180, 7 → 240). La valeur est recalculée à chaque heartbeat et publiée dans `clock.drift_s`. Les heartbeats n'étant pas simultanés, l'erreur de mesure peut atteindre 10 s ; elle est acceptée face au seuil de 60 s.

**Heartbeat manquant** : après `beat_timeout_s` (30 s) en `running`, le serveur passe `suspect` et le joueur compte comme absent.

**Redémarrage en match** : à une sortie inattendue d'un serveur, le service journalise, applique la procédure du § Contrat de transport et relance le même monde. `servers[X]` vaut `restarting`, le minuteur continue, les règles et le score sont conservés. Aucune commande ne part vers X dans cet état : ses lots attendent dans la file de X, sous les mêmes budgets, et la file de l'autre camp continue d'être servie. Le délai de grâce du joueur est suspendu jusqu'à la disponibilité du serveur, puis repart de zéro. La dérive tranche ensuite.

**Snapshots** : un snapshot couvre `~/Zomboid/Saves/Multiplayer/duelX/` et `~/Zomboid/db/duelX.db`, sous `~/Zomboid/duel-snapshots/`.

- Si aucun snapshot n'existe et qu'un monde existe, le service le capture serveurs arrêtés avant de lancer.
- Si aucun monde n'existe, le service fait une passe de création (démarrage, disponibilité, arrêt), capture, puis relance. Les deux mondes sont créés dans la même session.
- Une restauration n'a lieu qu'avec les deux serveurs arrêtés, sans port de jeu occupé. Elle copie d'abord dans un dossier temporaire et ne remplace le monde qu'une fois la copie complète.
- Un snapshot présent d'un seul côté ou une restauration en échec arrête le lancement avec un message. Le service ne démarre jamais de match sur deux mondes d'origine différente.

**Revanche** : mêmes presets, même code et même mot de passe, `match_id` neuf, snapshots restaurés.

### Journal

`matches/<match_id>/journal.jsonl`. Une ligne :
```json
{"v":1,"n":123,"ts_ms":1793830000456,"mt":312.5,"type":"event","side":"A","data":{}}
```
`n` croît sans trou. Chaque ligne est écrite et vidée à la suite ; les changements de phase sont suivis d'un `fsync`.

`mt` sépare deux classes de lignes, dont la frontière est le `state` d'entrée en `running`. Dans ce document, « `mt` nul » signifie `null`, jamais 0.

- **Avant-match**, `mt` nul : toute ligne écrite avant cette transition, soit `match_start`, les `state` des phases antérieures, `atom_withdrawn`, les anomalies de transport constatées avant `running`, ainsi que les `event` de ces phases (`caps`, `hook_seen`, `beat`), les `command` de `probe_spawn` et leurs `cmd_result`.
- **Match**, `mt` sur la grille de 250 ms : cette transition, à `mt`=0, et toute ligne suivante. `mt` y vaut le tick qui traite l'enregistrement. Une ligne écrite hors du traitement d'un tick, comme une anomalie de transport constatée à la lecture ou un `state` postérieur à `match_end`, prend le dernier tick clos, ou 0 si aucun ne l'est.

| `type` | Contenu |
|---|---|
| `match_start` | `match_id`, preset complet, `rules_hash`, ports, version du protocole, `runtime` (copie complète de `runtime.json` en vigueur), `effects` (copie complète de `effects.json` en vigueur) |
| `state` | changement de phase, ou d'état de serveur avec le camp et le nouvel état ; l'entrée en `running` porte `t0_ms` |
| `atom_withdrawn` | atome, hook, camps en défaut, à la clôture de la fenêtre de contrat |
| `event` | ligne d'événement acceptée complète, dont `boot` et `seq`, y compris `side` nul, `caps`, `hook_seen`, `beat` |
| `dup`, `bad_line`, `seq_gap`, `side_mismatch` | anomalies de transport |
| `late_event` | `boot`, `seq`, `te` et `mt` d'un événement traité après sa date source |
| `rule_fired` | règle, événement source, multiplicateurs |
| `effect_capped`, `effect_coalesced`, `effect_dropped` | budget |
| `command` | commande écrite, avec `latency_ms` depuis l'événement source |
| `cmd_result`, `ack_timeout`, `cmd_timeout`, `lost_on_restart` | suivi d'exécution, avec le `seq` de la commande |
| `match_end` | `result`, score, statut de `verdict` |

Le rejeu (`python -m service.replay <journal>`) relit `match_start` et construit un moteur neuf à partir du preset, de `runtime` et de `effects` de cet enregistrement. Il ne lit ni `service/runtime.json` ni `service/effects.json`. Un `match_start` privé de `runtime` ou d'`effects` donne le code 1 et nomme le champ.

Le rejeu applique dans l'ordre de `n`, chacun à son `mt`, tout enregistrement qui change l'ordonnancement : `event`, `cmd_result`, `state`, `lost_on_restart`, `match_end`. Un enregistrement à `mt` nul est appliqué avant le tick 0. Les `command` de `probe_spawn`, émises hors moteur, sont réinjectées aussi : elles consomment leur `seq` et n'entrent pas dans la comparaison. `rule_fired`, `effect_*`, `late_event` et `cmd_timeout` sont recalculés, jamais réinjectés. `atom_withdrawn` précède le match et n'y change rien.

Le rejeu applique exactement les règles du § Horloge du moteur : il parcourt tous les ticks de 0 au `mt` de `match_end`, ou au dernier `mt` lu si le journal est tronqué, traite les enregistrements de chaque tick, puis les échéances et l'émission. Il ne lit jamais l'horloge système. Un journal qui contient un `mt` non nul sans `state` portant `t0_ms` donne le code 1 et nomme le champ, et un `mt` hors grille donne le code 1 et cite son `n`.

Le rejeu compare ensuite les `command` et les `late_event` produits à ceux du journal : mêmes enregistrements, dans le même ordre, au même tick. Le tick étant reproduit, l'écart attendu est nul, et la borne de ±250 ms d'AC45 est tenue a fortiori. Une dernière ligne incomplète est ignorée et signalée. Code de sortie 0 si identique, 1 sinon, avec le premier écart.

`python -m service.replay --summary <journal>` produit en JSON, par camp : commandes et unités par minute, unités retirées par `cap_per_min`, événements à `side` nul, minutes sans aucun événement, et p95 de `latency_ms`.

### Honnêteté et secrets

- `--mode mock` reste le défaut. `Engine` conserve le mode demandé dans tout `MatchState`.
- En mock, le détail des étapes `preflight`, `serverA`, `serverB` et `contract` contient « simulé » et aucune valeur mesurée fictive. Aucune mention de PZ, de RAM ou de port présentée comme constatée.
- En réel, `simulate_join` est refusé, aucun texte d'état ne contient « simulé », aucune donnée de score ou de dérive n'est aléatoire. Un échec réel ne bascule jamais vers le mock.
- En réel, un `preset` inconnu est refusé, alors que le code actuel prend silencieusement le premier.
- `open_game` en réel lance le client par Steam avec `+connect 127.0.0.1:<port de A>` et `+password`, ou répond `ok=false` avec un message si Steam est introuvable.
- L'API `/api/state` et `/api/action` reste compatible : mêmes actions, champs existants conservés, ajouts seulement.
- Secrets : le mot de passe admin et le mot de passe RCON n'apparaissent ni dans `/api/state`, ni dans les `logs`, ni dans `matches/`, ni dans les lignes de commande journalisées. Toute sortie de serveur passe par une expurgation avant écriture ou affichage.
- Reste à risque, assumé : `-adminpassword` est visible dans `ps` pendant la vie du processus (le brief impose l'argument) et le `.ini` contient `RCONPassword` en clair (PZ le lit là), d'où `0600`.
- Le mot de passe joueur n'est pas un secret d'administration : il reste dans `invite` et `config`, mais pas dans le journal.

### Critères d'échec

**Attribution des kills**, calculée sur le match et par `python -m service.verdict <journal>`. Les deux compteurs couvrent le même intervalle, propre à chaque camp :

- Les heartbeats retenus d'un camp sont ceux reçus entre le début et la fin du match avec `kills` non nul. Le premier, H0, est le heartbeat de référence du § Déroulé du match. Le dernier est Hk.
- `référence` est la somme, sur les deux camps, des hausses de `kills` entre heartbeats retenus consécutifs. Une baisse compte 0.
- `attribués` est le nombre, sur les deux camps, de `zombie_kill` de `side` égal à ce camp dont le couple `(boot, seq)` est strictement compris entre ceux de H0 et de Hk.
- Un `zombie_kill` antérieur à H0 ou postérieur à Hk n'entre dans aucun des deux compteurs. Un camp avec moins de 2 heartbeats retenus apporte 0 aux deux.

Statut :

- `insufficient` si `référence` < `attribution_min_kills` (100) ;
- `ok` si `attribués` = `référence` ;
- `ko` sinon.

**Spawn serveur**, par camp :

- `ok` si la `probe_spawn` revient `applied` avec `spawned` ≥ 1 ;
- `ko` si elle revient autrement, ou sans résultat après `probe_timeout_s` (30 s) une fois le joueur connecté ;
- `unknown` tant qu'elle n'a pas abouti.

Un `refused`, ou un `failed` à `no_square`, ne dit rien de la fonction de spawn : le service réessaie toutes les `probe_retry_s` (5 s), dans le délai du premier essai, et ne conclut `ko` qu'à son terme.

`spawned` compte les zombies renvoyés par l'API : leur visibilité côté client se vérifie en jeu (README).

**Branches de repli**, jamais appliquées automatiquement, car elles changent l'architecture :

| Spawn | Attribution | `fallback` | Motif |
|---|---|---|---|
| `ok` ou `unknown` | `ok` ou `insufficient` | `null` | rien à conclure |
| `ok` ou `unknown` | `ko` | `java_agent` | le monde serveur reste valable, seule l'observation manque |
| `ko` | toute valeur | `solo_ipc` | la génération serveur échoue, seule l'autorité locale est prouvée (Chaos Mod, solo) |

**Signalement** : `capabilities` dans `/api/state` porte `A`, `B` (hooks et fonctions constatés, hooks vus), `active_atoms`, `withdrawn`, `attribution`, `spawn` et `fallback`.

- Sonde `ko` : le lancement s'arrête en lobby, phase `error`, serveurs arrêtés, `error` en français nommant le camp, la fonction et la branche.
- Attribution `ko` : mesurée à la fin du match, elle n'annule pas le résultat. `result.outcome` et `result.winner` ne changent pas. `result.reason` reçoit en plus une mention citant `attribution` et la branche de repli. Le statut figure aussi dans `match_end` et dans les logs en `warn`.
- Un événement à `side` nul est journalisé, ne donne ni points ni effet, et n'est jamais crédité à un camp.

### Stratégie de test

- Tests avec `.venv/bin/pytest`, sans réseau externe. Syntaxe limitée à Python 3.11.
- Moteur : flux synthétiques dans `tests/fixtures/*.jsonl`, rejoués avec un temps fourni par le test, qui tient lieu d'instant courant.
- Système : le dossier `~/Zomboid/` est remplacé par un dossier temporaire ; `tests/doubles/fake_server.py` imite `start-server.sh` (sortie standard scriptée, enfant lancé, refus de quitter, crash, RCON factice).
- Le double du mod, `tests/doubles/fake_mod.py`, écrit les fichiers du contrat et émet `hook_seen` sur demande ; il ne prouve pas le comportement du vrai Lua.
- Mod sans jeu : contrôles statiques du paquet seulement.
- Intégration : `tests/test_integration_pz.py`, marqueur `pz_integration`, `skip` explicite si PZ est absent. Il prouve le démarrage et le contrat réels, sans joueur.
- Un `skip` n'est pas une preuve. Ce qui exige un joueur (kills, spawn visible, distance, déconnexion) relève de la procédure du README et du verdict sur journal.

### Caller enumeration

Recherche : `grep -rn` sur `service/` et `ui/` pour les symboles ci-dessous. Angle mort : le JS de l'UI n'est couvert que par ses chemins d'API littéraux. Aucun test n'existe aujourd'hui.

| File | Function/Method | Migration Note |
|------|----------------|----------------|
| `service/main.py:37` | `Engine(mode=args.mode)` | Signature conservée ; dépendances système en arguments optionnels, valeurs réelles par défaut |
| `service/main.py:39` | `make_server(engine, port=port)` | Inchangé |
| `service/api.py:62`, `service/api.py:86` | `Engine.snapshot()` | Inchangé ; dict enrichi de `match_id`, `capabilities`, `result`, `config.rules_hash` |
| `service/api.py:65` | `Engine.presets()` | Inchangé ; `summary`, `label`, `duration_s`, `day_length` conservés |
| `service/api.py:85` | `Engine.action(name, payload)` | Inchangé |
| `service/engine.py:57` | `load_presets()` | Devient strict : un preset invalide est écarté et son erreur journalisée |
| `service/engine.py:114` | `Engine.preset(id)` | Id inconnu : refus en réel, comportement actuel en mock |
| `service/engine.py:61` | `MatchState.to_dict()` | Ajouts seulement ; aucun champ existant retiré |
| `ui/app.js:24`, `ui/app.js:36`, `ui/app.js:394` | `/api/state`, `/api/action`, `/api/presets` | Aucun changement requis ; l'UI ignore les nouveaux champs |

### Portée de targets

`targets` liste les fichiers du dépôt que le plan crée ou modifie, et rien d'autre. Un chemin d'exécution n'est pas une cible : il naît sur la machine pendant un lancement, et les tests le remplacent par un dossier temporaire. `.gitignore` n'en déclare aucun : il figure dans `targets` pour la règle qu'il ajoute. Tout fichier nommé par un critère est donc soit couvert par `targets`, soit listé ici avec son producteur et le test qui l'observe. `.venv/` est un outil local, ni cible ni produit du plan.

| Chemin d'exécution, avec `X` valant `A` ou `B` | Fichier du dépôt qui le produit | Test qui l'observe |
|---|---|---|
| `~/Zomboid/Lua/duel_X_out.txt`, `~/Zomboid/Lua/duel_X_ack.txt` | `mods/duelcore/42.21/media/lua/server/DuelCore.lua` ; `tests/doubles/fake_mod.py` en test | `tests/test_transport.py::test_foreign_side_rejected`, `tests/test_integration_pz.py::test_caps_from_real_mod` |
| `~/Zomboid/Lua/duel_X_in.txt`, `~/Zomboid/Lua/duel_X_session.txt` | `service/transport.py` | `tests/test_transport.py::test_truncate_only_on_full_ack`, `tests/test_transport.py::test_restart_is_at_most_once` |
| `~/Zomboid/Server/duelX.ini`, `~/Zomboid/Server/duelX_SandboxVars.lua` | `service/servers.py` | `tests/test_servers.py::test_managed_keys_only`, `tests/test_contract.py::test_each_mismatch_refused` |
| `~/Zomboid/mods/duelcore/` | `service/main.py` (`--install-mod`), copie de `mods/duelcore/` | `tests/test_preflight.py::test_each_failure_is_named` |
| `~/Zomboid/Saves/Multiplayer/duelX/`, `~/Zomboid/db/duelX.db` | le serveur PZ les crée ; `service/snapshots.py` les restaure | `tests/test_snapshots.py::test_restore_exact` |
| `~/Zomboid/duel-snapshots/` | `service/snapshots.py` | `tests/test_snapshots.py::test_restore_exact`, `tests/test_snapshots.py::test_first_run_creation_pass` |
| `matches/<match_id>/journal.jsonl` | `service/journal.py` | `tests/test_journal.py::test_sequenced_records`, `tests/test_secrets.py::test_no_secret_leak` |
| `matches/<match_id>/server_X.log` | `service/servers.py` | `tests/test_secrets.py::test_no_secret_leak` |

## Acceptance criteria

Chaque critère nomme son test. Sauf mention, les tests tournent sans le jeu.

- AC1 (R1): Un événement, une commande, un heartbeat, un ack et une session conformes aux exemples sont acceptés ; le même objet avec `v`=2 est rejeté — `tests/test_protocol.py::test_contract_examples_and_version`.
- AC2 (R1): Une ligne de 40 octets écrite en deux fois (25 puis 15 octets) produit 1 événement, après la seconde lecture, et l'offset final vaut 40 — `tests/test_transport.py::test_partial_line_waits_for_newline`.
- AC3 (R1): Un fichier `out` qui rétrécit de 5 à 2 lignes, ces 2 lignes étant déjà vues, produit 0 nouvel événement et 2 `dup` — `tests/test_transport.py::test_shrink_resets_offset_and_dedups`.
- AC4 (R1): Après un passage de `boot` 1 (dernier `seq` 50) à `boot` 2 (`seq` 1 à 3), les 3 événements sont acceptés ; `boot` 1 `seq` 50 relu est un `dup` — `tests/test_transport.py::test_new_boot_restarts_seq_without_loss`.
- AC5 (R1): Les `seq` 1, 2, 5 d'un même `boot` donnent 3 événements et 1 `seq_gap` de 2 manquants ; une ligne JSON invalide entre deux lignes valides donne 2 événements et 1 `bad_line` — `tests/test_transport.py::test_gap_and_bad_line_are_visible`.
- AC6 (R1): Une ligne de `duel_A_out.txt` avec `src`=`B` ou `side`=`B` est rejetée en `side_mismatch` et n'atteint pas le moteur — `tests/test_transport.py::test_foreign_side_rejected`.
- AC7 (R1): `in` n'est tronqué à 0 octet qu'avec `ack.seq` égal au dernier `seq` écrit : ack 6 pour 7 écrits laisse le fichier intact, ack 7 le vide sans le supprimer, ack 9 et un ack d'un autre `boot` le laissent intact et sont journalisés — `tests/test_transport.py::test_truncate_only_on_full_ack`.
- AC8 (R1): Avec 3 commandes écrites, dont 1 avec `cmd_result`, un redémarrage donne 2 `lost_on_restart`, `in` à 0 octet, une session avec `boot`+1 et `cmd_floor`=3, et 0 commande réémise — `tests/test_transport.py::test_restart_is_at_most_once`.
- AC9 (R2): Le préflight sur un dossier sans `start-server.sh`, avec un port déjà lié, avec `MemAvailable` sous le seuil, ou sans sous-dossier de version du mod échoue chaque fois avec un message français nommant la cause (4 cas) ; sans défaut il réussit avec les valeurs mesurées dans le détail — `tests/test_preflight.py::test_each_failure_is_named`.
- AC10 (R2): L'écriture des réglages sur un `.ini` de 200 lignes modifie uniquement les clés gérées, conserve les commentaires et laisse le fichier en `0600` — `tests/test_servers.py::test_managed_keys_only`.
- AC11 (R2): La commande de lancement contient toujours `-servername`, `-port`, `-udpport`, `-adminpassword`, avec les ports 17261/17262 pour A et 17271/17272 pour B — `tests/test_servers.py::test_launch_arguments`.
- AC12 (R2): Le double qui écrit `*** SERVER STARTED ****` puis le port attendu passe `up` ; une ligne citant un autre port ne suffit pas ; le double qui quitte avec le code 1 met l'étape en `error` et arrête l'autre serveur ; le double muet échoue à `server_start_timeout_s` — `tests/test_servers.py::test_readiness_paths`.
- AC13 (R2): Chaque écart du contrat (`Mods`, `PauseEmpty=true`, `RCONPort` égaux, `DayLength` différent, `mod` différent dans `caps`, effet de la table absent de `caps`) refuse le match et nomme la clé (6 cas) ; le cas conforme passe — `tests/test_contract.py::test_each_mismatch_refused`.
- AC14 (R2): Un double qui ignore RCON et l'entrée standard et ignore SIGTERM est tué par SIGKILL, et 0 processus de son groupe, enfant compris, ne survit — `tests/test_servers.py::test_graded_stop_kills_group`.
- AC15 (R2): `cancel` pendant `starting`, une erreur de contrat et SIGTERM du service laissent 0 processus de serveur (3 cas) — `tests/test_servers.py::test_every_exit_stops_servers`.
- AC16 (R3): Le paquet contient les 3 fichiers du layout, `versionMin=42.21.0`, aucun usage de `os.` ou `io.`, et aucun `require` externe — `tests/test_mod_package.py::test_layout_and_forbidden_calls`.
- AC17 (R3): Le Lua ne contient aucun nom de règle, de budget ou de score des presets, et chaque effet de `service/effects.json` a son traitement dans le Lua — `tests/test_mod_package.py::test_no_balance_and_effects_covered`.
- AC18 (R3): Sur PZ réel, chaque serveur écrit `caps` comme première ligne de `out`, avec `build` 42.21.0 et le bon `src` — `tests/test_integration_pz.py::test_caps_from_real_mod` (`pz_integration`).
- AC19 (R3): En jeu, selon la procédure du README, un zombie tué et une commande `spawn_pop` à 30-60 cases apparaissent toujours au journal, le spawn étant visible par le client, et une commande reçue pendant le sommeil ou la conduite donne toujours `refused`. La blessure dépend de `damage_taken`, seul atome soumis à cette condition : si l'atome figure dans `capabilities.active_atoms`, sans `atom_withdrawn` à son nom, au moins un `event` `damage_taken` apparaît au journal ; s'il a été retiré à la clôture de la fenêtre de contrat (AC67), `capabilities.withdrawn` le cite, le journal porte son `atom_withdrawn`, et aucune blessure n'est exigée — `tests/test_integration_pz.py::test_live_journal_checklist` contrôle le journal désigné par `PZ_LIVE_JOURNAL` et la copie de `/api/state` prise en phase `over`, désignée par `PZ_LIVE_STATE` (`skip` sans l'une de ces variables), et échoue si ces deux sources se contredisent sur le retrait ; la visibilité côté client reste constatée par le joueur.
- AC20 (R4): `count(3).within(10s)` sur 6 kills à t = 0..5 déclenche exactement à t = 2 et t = 5 ; 3 kills à t = 0, 6, 11 ne déclenchent rien — `tests/test_rules.py::test_count_within_consumes`.
- AC21 (R4): `every: 2` sur 5 kills déclenche 2 fois — `tests/test_rules.py::test_every`.
- AC22 (R4): `count(2).within(60s).and(not atom(damage_taken).within(60s))` avec des kills à t = 10, 11, 70 et une blessure à t = 5, ne déclenche pas à t = 11 et déclenche à t = 70 — `tests/test_rules.py::test_absence_condition`.
- AC23 (R4): `atom(build).count(3).within(120s).then(atom(zombie_kill), within=30s)` déclenche avec un kill à +30 s, pas à +31 s, et un seul kill ne déclenche qu'une fois — `tests/test_rules.py::test_series_window`.
- AC24 (R4): Combo `per` 0,15, `max` 3,0 : n=2 donne 3 à la 4e occurrence (1,6) et 6 à la 20e (3,0), et revient à 2 au premier kill après `damage_taken` — `tests/test_rules.py::test_combo`.
- AC25 (R4): Avec un écart de 31 pour `when_gt` 30, n=2 et combo 1,6 donnent 1 et l'adversaire reçoit `supply_drop` ; avec un écart de 30, aucun effet de l'écart — `tests/test_rules.py::test_score_gap`.
- AC26 (R4): Cooldown 20 s, n=2, kills à t = 0,1,2 : commande n=2 à t = 0, commande n=4 à t = 20, total 6 unités — `tests/test_budget.py::test_cooldown_merges_not_drops`.
- AC27 (R4): `cap_per_min` 60, n=2, 40 kills à 1 par seconde sans autre limite : 60 unités admises, 20 journalisées `effect_capped` — `tests/test_budget.py::test_cap_per_min`.
- AC28 (R4): `per_effect_cooldown_s` 0, `threats_per_minute` 6, `concurrent_effects` 8, 8 lots distincts vers une même cible à t = 0, soit un lot de chacun des 6 types de table, plus un second `spawn_pop` de `distance` différente et un second `noise` de `radius` différent : 6 commandes à t = 0, 2 à t = 60 ; `per_effect_cooldown_s` 0, `threats_per_minute` 6, `concurrent_effects` 4, `cmd_result_timeout_s` 1 et 5 lots distincts sans `duration_s` créés à t = 0 : sans `cmd_result`, sur la fenêtre bornée des ticks `mt`=0 à `mt`=0,75, 4 commandes partent à `mt`=0, la 5e reste en file et aucun `cmd_timeout` n'est journalisé ; dans une variante de cette fixture, un premier `cmd_result` traité au tick `mt`=0,5 libère sa place et la 5e commande part à `mt`=0,5, dans la même passe, jamais à `mt`=0,75. La phase d'expiration relève d'AC77 — `tests/test_budget.py::test_threats_and_concurrency`.
- AC29 (R4): Les lots en attente à la fin du match sont journalisés `effect_dropped` et aucune commande n'est écrite après `match_end` — `tests/test_budget.py::test_pending_dropped_at_end`.
- AC30 (R4): Une table avec effet `fog`, atome `craft`, atome de `source` `client`, clé inconnue à la racine, `distance[0]`=24, `when` hors grammaire, `budget` absent, clé inconnue dans une règle, `radius` sur `spawn_pop`, clé inconnue dans `budget`, clé inconnue dans `score`, clé inconnue dans `modifiers.combo`, `warn_s` manquant, `n`=31, `every` sur un `when` agrégat, effet `probe_spawn` ou `threats_per_minute` manquant est refusée, le message nommant la règle ou la section, et le champ (17 cas) — `tests/test_rules_load.py::test_forbidden_tables`.
- AC31 (R4): Les 4 presets livrés se chargent sans erreur, et passer `n` de 2 à 5 dans une copie de table fait passer la commande produite de n=2 à n=5 sans changement de code — `tests/test_rules_load.py::test_shipped_presets_and_data_driven`.
- AC32 (R4): Un `zombie_kill` d'`origin` `duel` ajoute au score et produit 0 `rule_fired` — `tests/test_rules.py::test_duel_spawns_do_not_loop`.
- AC33 (R4): Le preset modifié d'un octet après le calcul du hash empêche l'entrée en décompte, avec une erreur citant le hash — `tests/test_match.py::test_rules_hash_sealed`.
- AC34 (R5): Avec les doubles, un parcours `idle` → `preflight` → `starting` → `contract` → `lobby` → `countdown` → `running` → `stopping` → `over` passe par ces 9 phases dans l'ordre ; `invite.port` vaut 17271 — `tests/test_match.py::test_full_real_path`.
- AC35 (R5): Le minuteur à 0 avec les scores 40 et 30 donne `win` pour A ; 30 et 30 donne `draw` ; un `player_death` de A à t = 100 donne `death`, B vainqueur, sans attendre le minuteur (3 cas) — `tests/test_match.py::test_outcomes`.
- AC36 (R5): Un joueur absent 119 s ne perd pas ; absent 120 s il déclare forfait ; les deux absents 120 s donnent `invalidated` — `tests/test_match.py::test_disconnect_grace`.
- AC37 (R5): `DayLength`=1 : un écart d'avance de 1,6 h de jeu donne `drift_s` 60,0 et le match continue ; 1,7 h donne 63,75 et `invalidated`. `DayLength`=2 : 1 h donne 75,0. Deux `age` initiaux différents de 3 h et une même avance donnent 0 — `tests/test_match.py::test_drift_real_seconds`.
- AC38 (R5): La sortie inattendue de A à t = 300 donne `restarting`, un relancement avec `boot`=2, un score et un état de règles inchangés, et un délai de grâce repartant à zéro après disponibilité ; une deuxième sortie donne `invalidated` — `tests/test_match.py::test_restart_in_match`.
- AC39 (R5): En lobby, l'absence de heartbeat pendant 300 s n'entraîne ni erreur ni `suspect` ; en `running`, 30 s sans heartbeat donne `suspect` — `tests/test_match.py::test_beat_silence_by_phase`.
- AC40 (R5): Sur un monde modifié après snapshot, la restauration rend un contenu identique octet pour octet au snapshot, pour le dossier et pour `duelA.db` — `tests/test_snapshots.py::test_restore_exact`.
- AC41 (R5): La restauration est refusée, avec le monde intact, quand un serveur tourne, quand un port de jeu est occupé, ou quand la copie échoue à mi-chemin (3 cas) ; un snapshot présent d'un seul côté arrête le lancement — `tests/test_snapshots.py::test_restore_guards`.
- AC42 (R5): Sans monde ni snapshot, le service fait une passe de création, puis capture A et B avant le lobby : 2 lancements par serveur, 2 snapshots — `tests/test_snapshots.py::test_first_run_creation_pass`.
- AC43 (R5): `rematch` conserve `code` et `password`, change `match_id`, restaure les 2 snapshots et remet `boot` à 1 — `tests/test_match.py::test_rematch`.
- AC44 (R6): Un match synthétique donne un journal où `n` croît de 1 en 1 sans trou, chaque ligne porte `v`, `ts_ms` et `type`, et les types `match_start`, `event`, `rule_fired`, `command`, `match_end` sont tous présents — `tests/test_journal.py::test_sequenced_records`.
- AC45 (R6): Le rejeu d'une fixture de 1000 événements sur 2 camps redonne le même nombre de commandes, dans le même ordre, chacune à ±250 ms, avec le code de sortie 0 ; un journal à commande altérée donne le code 1 et cite le `n` de l'écart — `tests/test_replay.py::test_replay_matches_and_detects_tamper`.
- AC46 (R6): Un journal dont la dernière ligne est coupée se rejoue sans exception et signale 1 ligne ignorée — `tests/test_replay.py::test_truncated_tail`.
- AC47 (R6): `--summary` sur une fixture à valeurs connues (12 commandes en 2 minutes sur A, 20 unités plafonnées, 3 événements à `side` nul) rend exactement ces nombres — `tests/test_replay.py::test_summary_values`.
- AC48 (R7): En mock, les étapes `preflight`, `serverA`, `serverB`, `contract` contiennent toutes « simulé » (4 sur 4), et le détail de `preflight` ne contient ni « Go » ni numéro de port — `tests/test_modes.py::test_mock_never_claims_real`.
- AC49 (R7): `Engine(mode="real")` garde `mode`=`real` après `start` ; `simulate_join` en réel répond faux ; un preflight en échec finit en `error` et jamais en parcours simulé ; aucun texte de l'état réel ne contient « simulé » — `tests/test_modes.py::test_real_never_falls_back`.
- AC50 (R7): `start` avec le preset `inconnu` en réel est refusé avec un message français — `tests/test_modes.py::test_unknown_preset_refused`.
- AC51 (R7): `open_game` en réel appelle le lanceur double avec `+connect 127.0.0.1:17261` et `+password` ; sans Steam il renvoie `ok=false` — `tests/test_modes.py::test_open_game_real`.
- AC52 (R7): Sur un parcours complet avec des mots de passe marqueurs, et un double qui les imprime sur sa sortie standard, aucune occurrence des deux marqueurs n'apparaît dans `/api/state`, `logs`, `journal.jsonl` et `server_A.log` — `tests/test_secrets.py::test_no_secret_leak`.
- AC53 (R7): `/api/state` conserve toutes les clés actuelles de `MatchState.to_dict()` (15 clés) en mock et en réel, et les 5 actions restent acceptées — `tests/test_modes.py::test_api_backward_compatible`.
- AC54 (R8): Référence 100 et attribués 100 donnent `ok` ; 99 donnent `ko` ; référence 60 et attribués 50 donnent `insufficient` — `tests/test_verdict.py::test_attribution_thresholds`.
- AC55 (R8): Un `zombie_kill` à `side` nul est journalisé, ajoute 0 point et produit 0 `rule_fired` — `tests/test_rules.py::test_null_side_never_credited`.
- AC56 (R8): Les 3 lignes de la table donnent `fallback` `null`, `java_agent`, `solo_ipc` — `tests/test_verdict.py::test_fallback_table`.
- AC57 (R8): Une `probe_spawn` à `failed` hors `no_square`, refusée sur tout le délai, ou sans résultat à 30 s, met la phase en `error`, arrête les 2 serveurs, renseigne `capabilities.fallback`=`solo_ipc` et un message nommant le camp — `tests/test_match.py::test_spawn_probe_failure_is_signalled`.
- AC58 (R8): Un `caps` à `OnPlayerGetDamage`=true sans `hook_seen` est publié comme enregistré mais non vu ; après `hook_seen` il est publié comme vu — `tests/test_verdict.py::test_hook_seen_distinct_from_registered`.
- AC59 (R8): Sur un même flux, une attribution `ko` donne les mêmes `result.outcome` et `result.winner` qu'une attribution `ok` ; `result.reason` contient en plus `attribution` et `java_agent` ; `match_end` porte `attribution`=`ko` et `fallback`=`java_agent` — `tests/test_match.py::test_attribution_ko_reported_at_end`.
- AC60 (R9): Sur un dossier d'installation vide, la détection de PZ répond « absent », ce qui place les tests `pz_integration` en `skip` avec ce motif, et `.venv/bin/pytest` finit avec le code 0 — `tests/test_integration_pz.py::test_detection_reports_absent`.
- AC61 (R9): Sur une machine avec PZ, `pytest -m pz_integration` démarre deux vrais serveurs, passe le contrat, et les arrête sans processus résiduel — `tests/test_integration_pz.py::test_real_servers_boot_contract_stop`.
- AC62 (R9): Aucun module de `service/` n'importe de paquet hors stdlib — `tests/test_stdlib_only.py::test_imports`.
- AC63 (R6): Un journal produit avec `max_units_per_command`=30 et `cmd_result_timeout_s`=20, où un lot de 40 unités part en 30 puis 10, se rejoue avec le code 0 alors que `service/runtime.json` vaut 5 et 1 sur le disque ; `match_start` porte ces 2 valeurs ; le même journal privé de `runtime` donne le code 1 et nomme le champ — `tests/test_replay.py::test_replay_uses_journaled_runtime`.
- AC64 (R6): Avec `concurrent_effects`=1, `cmd_result_timeout_s`=60, 1 commande sans résultat à `mt`=290, 1 lot en attente et `lost_on_restart` à `mt`=300, le lot part à `mt`=300, dans la passe qui traite `lost_on_restart`, et le rejeu redonne ces 2 commandes avec le code 0 ; la même fixture sans la ligne `lost_on_restart` donne le code 1 et cite le `n` de la seconde commande — `tests/test_replay.py::test_replay_applies_restart_records`.
- AC65 (R5): Pendant `restarting` de A, 3 kills de B à n=2 produisent 0 commande vers A, et les commandes vers B continuent, même si le plus ancien lot en attente vise A ; au retour de A, une commande n=6 part au tick suivant. Une commande `lost_on_restart` libère 1 place `concurrent_effects` et reste comptée dans `threats_per_minute` — `tests/test_match.py::test_no_command_while_restarting`.
- AC66 (R8): 10 kills attribués avant H0, puis 100 entre H0 et Hk avec une hausse de `kills` de 100, puis 5 après Hk donnent `référence` 100, `attribués` 100 et `ok` ; un camp A à H0 tardif et un camp B à H0 précoce sont bornés chacun par son propre intervalle ; un `zombie_kill` entre H0 et Hk à `side` nul donne `ko` avec 100 et 99 — `tests/test_verdict.py::test_attribution_interval`.
- AC67 (R3): Avec les doubles et `hook_window_s`=120, une table contenant `damage_taken` et un `hook_seen` sur A seulement entre en décompte à `L` + 120 s, avec un `atom_withdrawn` citant `damage_taken`, `OnPlayerGetDamage` et B au journal, `damage_taken` dans `capabilities.withdrawn` et les règles qui en dépendent désactivées ; une table sans aucun atome survivant donne la phase `error`, 0 processus de serveur et une `error` citant l'atome, le hook et les camps ; le même flux avec une table sans `damage_taken` entre en décompte à `L`, avec le même retrait publié et `damage_taken` absent de `capabilities.active_atoms` ; `hook_seen` sur A et B à `L` + 5 s fait entrer en décompte à cet instant avec l'atome actif (3 cas) — `tests/test_contract.py::test_unseen_hook_withdraws_atom`.
- AC68 (R2): Une table contenant `damage_taken`, sans aucun joueur ni `hook_seen`, passe l'étape `contract` et atteint `lobby` sans `atom_withdrawn` ; après une clôture avec l'atome actif, un redémarrage de A ne rouvre pas la fenêtre, et `capabilities.active_atoms` ne change pas — `tests/test_contract.py::test_hook_window_outlives_contract_step`.
- AC69 (R3): Le Lua ne contient aucune entrée de `supply_items` ni aucun nom d'objet `Base.*`, et le traitement de `supply_drop` y lit le champ `items` de la commande — `tests/test_mod_package.py::test_supply_content_not_in_mod`.
- AC70 (R4): Une table dont un `supply_drop` a `items` absent, `items` vide, 6 entrées, `count`=0, `count`=11, `count`=`true`, un `item` hors de `supply_items`, un `item` cité deux fois ou une clé inconnue dans une entrée est refusée, le message nommant la règle et le champ ; le cas `item` hors catalogue placé dans `defender_bonus` est refusé en nommant `modifiers.score_gap` (10 cas) — `tests/test_rules_load.py::test_supply_items_schema`.
- AC71 (R4): La commande `supply_drop` issue d'une règle de `duel_rapide` porte exactement 2 `Base.Bandage` et 1 `Base.Disinfectant` ; changer `items` dans une copie de table change la commande sans changement de code ; deux `supply_drop` de mêmes `items` en attente donnent 1 lot et 1 `effect_coalesced`, et de `items` différents 2 lots — `tests/test_budget.py::test_supply_items_reach_command`.
- AC72 (R4): A étant `restarting`, un lot vers A créé à t = 10 et un lot vers B créé à t = 11 : le lot vers B part à t = 11 et celui vers A attend ; au retour de A, deux lots vers A partent dans leur ordre de création ; avec un lot de tête retenu par le cooldown, le lot suivant de la même file ne le dépasse pas, et la file de l'autre camp est servie — `tests/test_budget.py::test_queues_are_per_target`.
- AC73 (R4): Après un dernier événement à t = 0 et un silence jusqu'à l'instant courant t = 20, le moteur exécute 81 ticks de 0 à 20 sans en sauter un, et le lot en cooldown part à `mt`=20 ; un instant courant passant de 5,1 à 6,6 d'un coup exécute les 6 ticks de 5,25 à 6,5 dans l'ordre ; un événement de `te`=500 reçu à l'instant courant 7 laisse `mt` à 7 — `tests/test_clock.py::test_ticks_advance_in_silence`.
- AC74 (R4): Un événement de `te`=10,1 reçu avant la clôture du tick 10,25 y est traité sans `late_event` ; reçu après la clôture du tick 10,5, il est traité au tick 10,75 avec un `late_event` portant `te`=10,1 et `mt`=10,75 ; de `te`=10,5 reçu après la clôture du tick 10,5, il donne aussi `late_event`. Une série `then(…, within=30s)` armée à t = 0 déclenche sur un `b` de `te`=30 traité en retard au tick 31 — `tests/test_clock.py::test_late_event_next_tick`.
- AC75 (R4): Deux événements du même tick, de A et B à même `t_ms`, reçus dans les deux ordres possibles, donnent le même journal, A avant B ; un `cmd_result` et un kill du même tick sont tous deux traités avant l'émission, de sorte que le lot part dans ce tick sur la place libérée ; un tick traitant `match_end` n'émet aucune commande et journalise `effect_dropped` — `tests/test_clock.py::test_tick_order`.
- AC76 (R6): Le rejeu d'une fixture complète, de `match_start` à `match_end`, mêlant silence de 60 s, événements en retard et événements de même tick, redonne les mêmes `command` et `late_event`, dans le même ordre et au même `mt` exact, code 0, l'horloge système étant remplacée par une fonction qui lève une exception ; dans cette fixture, toute ligne antérieure au `state` d'entrée en `running` a `mt` à `null`, dont `match_start`, les `state` des phases antérieures, un `atom_withdrawn` et une anomalie de transport constatée avant `running`, et ce `state`, à `mt`=0, comme toute ligne suivante, a un `mt` multiple de 0,25 ; déplacer d'un tick le `mt` d'un `event` dans le journal donne le code 1 et cite le `n` du premier écart ; retirer `t0_ms` donne le code 1 et nomme le champ — `tests/test_replay.py::test_replay_same_clock_rule`.
- AC77 (R4): Avec la fixture d'AC28 à `concurrent_effects` 4 et `cmd_result_timeout_s` 1, 4 commandes parties à `mt`=0, la 5e en file et aucun `cmd_result` : au tick `mt`=1, les 4 commandes expirent ensemble, 4 `cmd_timeout` sont journalisés, un par `seq`, et la 5e commande part à `mt`=1, dans la même passe, après ces `cmd_timeout` dans le journal, jamais à `mt`=1,25 ; c'est la seule commande émise après la libération, soit 5 commandes au total — `tests/test_budget.py::test_threats_and_concurrency`.
