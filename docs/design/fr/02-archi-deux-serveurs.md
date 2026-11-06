# Archange « deux serveurs » — conception détaillée

Décision : **architecture B**, hôte = machine d'un des joueurs. Deux serveurs dédiés PZ (un monde chacun) + un arbitre local + deux clients. Le solo disparaît du montage : chaque « partie » est un serveur dédié à un joueur.

---

## 1. Pourquoi ce choix simplifie réellement le concept

| Contrainte du concept | En solo (archi A) | En deux serveurs (archi B) |
|---|---|---|
| Interdire l'accélération du temps | À imposer par mod (`setGameSpeed` en boucle) | **Gratuit** : en multi, l'UI vitesse/pause du client est désactivée. Preuve dans le jeu installé : `media/lua/client/ISUI/SpeedControlsHandler.lua` ligne 5 — `if isClient() then return; end` |
| Synchroniser les horloges | Horloge maître + gel/dégel permanent des deux jeux | Les deux serveurs partent de la même date/heure (`StartYear/Month/Day/Time`) et du même `DayLength` → même horloge, avance au même rythme réel. Plus de gel à piloter |
| Installer le mod chez les joueurs | Mod + agent sur chaque PC | Le serveur déclare ses mods Workshop, le client souscrit et rejoint. Zéro installeur |
| Empêcher l'édition des saves | Impossible en solo | L'hôte possède les saves ; les joueurs n'y touchent pas |
| Pause en cours de match | Native (menu) mais à propager | À construire (voir §6) |
| Machine allumée pendant le match | Deux PC de joueurs | Le PC hôte doit rester allumé (SPOF assumé entre amis) |

Ce que ça coûte en échange : deux processus serveur à opérer, la fragilité connue des mods en multi B42, et deux cas nouveaux — l'attribution des kills côté serveur et la pause.

---

## 2. Topologie

```
        PC hôte (Ryzen 7 7700 / 31 Go / 1 Gb)                    PC joueur 2
┌────────────────────────────────────────────────────┐      ┌──────────────────┐
│  Serveur A  (servername=duelA, Port=16261)         │      │  Client PZ       │
│   monde A ── mod duel ── duelA_status.txt          │      │  + mod HUD       │
│                                   duelA_out.txt ◄──┼──┐   └────────┬─────────┘
│  Serveur B  (servername=duelB, Port=16271)         │  │            │
│   monde B ── mod duel ── duelB_status.txt          │  │            │
│                                   duelB_out.txt ◄──┼──┤            │
│                                                    │  │            │
│  Arbitre (Python, localhost)                       │  │            │
│   • tail out_A/out_B, écrit in_A/in_B              │  │            │
│   • horloge de match, score, règles, caps, logs    │  │            │
│   • RCON localhost (save, quit, createhorde…)      │  │            │
└────────────────────────────────────────────────────┘  │            │
        ▲ Client PZ de l'hôte ───────────────────────────┘            │
        └───────────────────── UDP 16261/16262, 16271/16272 ──────────┘
```

- Les deux serveurs tournent sur le même PC, dossiers de save distincts (`-servername`), ports distincts.
- L'arbitre est le seul à parler aux deux mondes : il applique les règles (1 kill → 2 zombies, caps, combos), tient le score et l'horloge, et journalise le match.
- Le trafic entre les deux serveurs ne sort jamais de la machine : seuls les deux clients se connectent de l'extérieur.

### Variante sans arbitre (à garder en tête)

Les deux mods serveur peuvent s'échanger directement par fichiers dans un dossier partagé (mod A écrit `in_B.txt`, mod B le lit). C'est le MVP le plus court possible (~2 jours de moins), mais on perd les logs, le score central, l'horloge et la possibilité de déplacer le match sur un VPS plus tard. Recommandation : garder l'arbitre, il coûte ~4 jours et il est le futur tableau de bord du match.

---

## 3. Configuration concrète des deux serveurs

Fichiers `~/Zomboid/Server/duelA.ini` et `duelB.ini` (créés par `-servername`), avec des valeurs **identiques** sauf ports et nom :

```
# duelA.ini                    # duelB.ini
PublicName=Duel A              PublicName=Duel B
Port=16261                     Port=16271
RCONPort=27015                 RCONPort=27016
RCONPassword=<fort, différent> RCONPassword=<fort, différent>
PauseEmpty=false               PauseEmpty=false
UPnP=false                     UPnP=false
Mods=duelcore;duelhud          Mods=duelcore;duelhud
WorkshopItems=<ids>            WorkshopItems=<ids>
```

`SandboxVars` (le point critique pour l'équité) — mêmes valeurs des deux côtés, à commencer par :

- `DayLength` (4 par défaut dans Apocalypse = 1 jour de jeu par 1 h 30 réelles)
- `StartYear`, `StartMonth`, `StartDay`, `StartTime` (fixent la date et l'heure de départ, donc l'horloge)
- population, rareté du loot, véhicules, électricité/eau, difficulté des zombies

À savoir : ces options sont des **préréglages numérotés** (`DayLength = 4`), pas des valeurs libres. Toute divergence crée une asymétrie invisible mais réelle.

Démarrage (Linux, serveur dédié SteamCMD app 380870) :

```
./start-server.sh -servername duelA -port 16261
./start-server.sh -servername duelB -port 16271
```

Les deux doivent être lancés **ensemble, avant le début du match** : c'est ce qui garantit l'horloge identique. Un redémarrage de l'un sans l'autre introduit une dérive (voir §5).

Réseau : 4 ports UDP à ouvrir/rediriger (16261/16262 et 16271/16272). Derrière un CGNAT, il faut un VPS ou un tunnel ; prévoir ce cas dès la doc.

---

## 4. Ce que voit et fait le mod serveur

### Observation (côté serveur, autorité)

| Fait de jeu | Source | Remarque |
|---|---|---|
| Kill | `OnZombieDead` + `IsoZombie:getAttackedBy()` (javadoc 42.x) | `OnZombieDead` ne fournit que le zombie : il faut lire le dernier attaquant. `OnHitZombie` fournit `attacker` → tenir une petite table « zombie → dernier attaquant » et la purger |
| Dégâts / blessure du joueur | `OnPlayerGetDamage`, `OnPlayerDeath` | Marque la fin de combo, et la fin de match |
| Tirs / bruit | `OnWeaponHitCharacter`, événements d'arme à feu | Alimente les events « bruit » chez l'adversaire |
| Position / zone | `getPlayer():getX()/getY()/getZ()` | Pour placer les spawns à distance sûre |
| Horloge | `getGameTime():getWorldAgeHours()` | Écrite dans le fichier de statut toutes les 10-30 s |
| État de connexion | `OnPlayerConnect/Disconnect` | Politique de pause/abandon |

Le mod écrit des lignes compactes dans `duelX_out.txt` (une par événement, bufferisées et vidées toutes les ~1 s), et relit `duelX_in.txt` à la même cadence. Aucun réseau en Lua : c'est l'arbitre qui fait le transport.

### Injection (côté serveur, autorité)

- `addZombiesInOutfit(x, y, z, n, outfit, chance)` et `addZombiesInOutfitArea(x1,y1,x2,y2,z,n,outfit)` — utilisés par le tuto vanilla, donc stables ; spawn serveur-autoritaire confirmé par Zombie Spawn Manager en B42 (à re-tester en 42.20.4 pendant le spike).
- Effets « gratuits » côté serveur : météo (`startrain`/`stoprain`), bruit (`gunshot`, `alarm`), hélico (`chopper`), objets (`additem`, ou spawn au sol), accélération du danger (`createhorde`).
- Limite B42 documentée par Zombie Spawn Manager : plus de setters publics fiables par zombie pour sight/hearing/memory/strength. Donc « sprinter » oui (walk type public), « zombie plus fort/plus voyant » non sans contrôleur runtime.

### Ce que le mod serveur ne peut pas faire

- Pas de pause : le serveur ne se met pas en pause parce qu'un joueur ouvre un menu.
- Pas de `setGameSpeed` : la vitesse est une notion de solo.
- Pas de réseau : tout passe par l'arbitre.

---

## 5. Horloge : ce qui est garanti, ce qui dérive

Garanti par construction :
1. même date/heure de départ (SandboxVars identiques) ;
2. même `DayLength` ;
3. avance liée au temps réel, sans accélération possible côté client (`isClient()` dans `SpeedControlsHandler`).

Sources de dérive réelles :
- redémarrage d'un seul serveur (crash, mise à jour, `quit`) ;
- `PauseEmpty=true` : si un joueur se déconnecte, **ce** monde s'arrête et l'autre continue → dérive immédiate. D'où `PauseEmpty=false` et une politique de déconnexion côté arbitre ;
- machine hôte en veille / charge extrême (ralentissement du tick).

Traitement :
- l'arbitre compare `worldAgeHours` des deux côtés toutes les 10 s et alerte au-delà de ±30 s de jeu ;
- correction fine possible via `GameTime:setTimeOfDay()` / `setDay/setMonth/setYear` sur le serveur en retard (saut visible, à faire de préférence à l'aube et hors combat) — **à valider en spike** : on ne sait pas encore si un setter d'horloge sur un serveur en cours de partie est propre (files d'attente, cultures, spawns planifiés) ;
- à défaut de correction propre, la règle de secours est : match invalidé et relancé depuis la dernière sauvegarde commune. C'est acceptable entre amis, pas pour un classement.

---

## 6. Pause, reprise, abandon

En multi il n'y a pas de pause. Trois mécanismes, à choisir :

- **(a) Pas de pause du tout** — le monde tourne, un joueur qui s'absente laisse son survivant exposé. Simple, brutal, cohérent avec un duel. Recommandé pour la v1.
- **(b) Pause = arrêt propre des deux serveurs** — l'arbitre envoie `save` puis `quit` aux deux (RCON), et relance les deux processus à la reprise. Les mondes reprennent exactement là où ils étaient, l'horloge aussi. Coût : 30-60 s de reprise, et la re-init des mods doit être idempotente.
- **(c) Timeout/(dé)connexion** — un joueur qui tombe : gel du match (pas des serveurs) avec grâce de 2-5 min, puis abandon = défaite, ou match suspendu si mode amical.

Recommandation : (a) + (c) en v1, (b) en option de confort. Jamais (b) sans test de reprise de save sous mod.

---

## 7. Séquence d'un événement, de bout en bout

```
t+0.0   joueur A tue un zombie
t+0.1   mod A : OnZombieDead + getAttackedBy → buffer "kill 7421 1"
t+1.0   mod A vide le buffer dans duelA_out.txt
t+1.1   arbitre lit la ligne, incrémente le compteur de A,
        applique la règle (1 kill → 2 zombies), vérifie caps/cooldowns
t+1.2   arbitre écrit dans duelB_in.txt : "spawn 2 near_player safe=40"
t+1.3   mod B (poll 0,5-1 s) lit, calcule une position à 40 cases,
        addZombiesInOutfit(x,y,z,2,nil,nil) + préavis sonore/HUD
t+2.2   les deux zombies existent côté serveur B
```

Latence réaliste 1-3 s. Le « garbage par lots » (accumuler 10-15 s puis envoyer un paquet) reste préférable pour la lisibilité et pour les perfs — le pipeline ci-dessus ne change pas, seule la cadence d'envoi change.

---

## 8. Budget révisé

| Poste | Jours |
|---|---|
| Spikes go/no-go (voir §9) | 2-3 |
| Industrialisation des 2 serveurs (ini, sandbox, ports, script de lancement, reprise) | 2-3 |
| Mod serveur `duelcore` (capture, attribution, injection, statut, HUD data) | 6-8 |
| Mod client `duelhud` (score, horloge, alerte, écran de fin) | 3-4 |
| Arbitre Python (protocole, horloge, règles, caps, logs, match states) | 4-6 |
| Équilibrage (table de conversion, combos, anti-neige-boule, victoire) | 4-8 |
| Doc + page Workshop + messages d'erreur | 2-3 |
| QA (crash, reload, déconnexion, patch B42, 2 instances) | 4-8 |
| **Total** | **27-43 j** |

Le total reste proche de l'archi A, mais la partie la plus risquée (gating temporel, agent chez chaque joueur) disparaît, et l'installation devient « souscrire au mod, se connecter ». L'incertitude restante est concentrée sur **la stabilité des mods en multi B42** et sur l'équilibrage.

---

## 9. Spike go/no-go révisé (2-3 jours)

Le risque n'est plus l'horloge, c'est le multi.

1. **J1 — Mod serveur en MP.** Un mod Lua serveur sur 42.20.4 : compte les kills avec `OnZombieDead` + `getAttackedBy()`, écrit dans un fichier. Critère : 100 kills, attribution correcte à 100 %, aucun lag perceptible, fichier lisible par un processus externe.
2. **J2 — Spawn serveur.** `addZombiesInOutfit` à 40 cases d'un joueur, déclenché par un fichier externe. Critère : 20 spawns, aucun crash, aucun zombie dans le dos, FPS stable, zombies visibles côté client.
3. **J3 — Deux serveurs + horloge.** Les deux serveurs sur la même machine, même SandboxVars, 30 minutes à deux joueurs connectés. Critère : dérive d'horloge mesurée < 10 s de jeu, et test de correction `setTimeOfDay` sur +2 min pour voir si c'est propre.

Si J1 ou J2 échoue → on retombe sur l'archi A (solo + IPC) ou C (Java). Si J3 échoue seulement sur la correction, on garde B avec « drift accepté + match invalide au-delà du seuil ».

---

## 10. Risques propres à cette architecture

| Risque | Parade |
|---|---|
| Mods multi B42 instables (chargement Workshop, reload Lua qui coupe la session — rapports sur 42.20) | Versionner les mods, pas de `reloadlua` en match, redémarrage propre, tester chaque point release |
| Hôte = administrateur des deux mondes → tentation de triche | Compte admin dédié détenu par l'arbitre (mot de passe RCON non communiqué), joueur hôte sans droits admin ; sinon assumer « entre amis » |
| PC hôte éteint/veille = match perdu | Politique de sauvegarde toutes les 5 min + reprise ; doc claire ; VPS plus tard |
| 4 ports UDP, CGNAT | Doc port forwarding + option tunnel/VPS |
| Date/heure de départ non identiques (création des mondes à des moments différents) | Créer les deux mondes dans la même session d'installation, vérifier `worldAgeHours` identique avant le premier match |
| Dérive après redémarrage d'un seul serveur | L'arbitre refuse de démarrer un match si les deux horloges diffèrent de plus de X ; sinon correction ou monde rejoué |
| Spawn injuste (dos, pièce, sommeil, conduite) | Préavis, distance minimale, liste noire de contextes, cap par minute |

---

## 11. Décisions restantes

1. **Pause** — (a) aucune, le monde tourne *(reco)* · (b) arrêt/relance des deux serveurs · (c) gel du match côté arbitre seulement.
2. **Hôte** — (a) PC du joueur 1, admin détenu par l'arbitre *(reco)* · (b) VPS loué · (c) les deux, configurable.
3. **Transport arbitre ↔ mods** — (a) fichiers + RCON *(reco)* · (b) agent Java serveur (pzJBridge-like) dès le départ.
4. **Granularité des events** — (a) lots de 10-15 s *(reco)* · (b) temps réel event par event.
5. **Correction d'horloge** — (a) aucune, invalidation au-delà de ±60 s *(reco v1)* · (b) `setTimeOfDay` automatique si le spike le valide.

---

## 12. Décisions tranchées (04/11/2026) et conséquences « parties courtes »

| # | Décision | Retenu |
|---|---|---|
| 1 | Pause | **1a** — aucune pause ; matchs courts, le monde tourne |
| 2 | Hôte | **2a** — PC d'un joueur pour le POC ; serveur séparé prévu dans l'archi finale |
| 3 | Transport arbitre ↔ mods | **3a** — fichiers + RCON localhost |
| 4 | Granularité | **4a** au MVP (lots de 10-15 s) ; **4b** (temps réel) à terme |
| 5 | Dérive d'horloge | **5a** — aucune correction, invalidation au-delà de ±60 s |

### 12.1 Le format court change trois réglages, pas l'architecture

`DayLength` est un préréglage numéroté (vérifié dans `media/lua/shared/Translate/EN/Sandbox.json`) :

| Valeur | 1 jour de jeu |
|---|---|
| 1 | 15 min réelles |
| 2 | 30 min |
| 3 | 1 h |
| 4 (défaut Apocalypse) | 1 h 30 |
| 5 / 6 / 7 | 2 h / 3 h / 4 h |

Conséquence : un match de **20-30 min = 1 à 2 jours de jeu** avec `DayLength=1 ou 2`. La horloge de match devient un simple minuteur réel affiché au HUD, et la dérive temporelle sur 30 min sera de l'ordre de la seconde : la règle 5a ne se déclenchera pratiquement jamais. Le moniteur d'horloge reste utile comme diagnostic, pas comme mécanisme de jeu.

### 12.2 Ce qu'il faut trancher avec des matchs courts

1. **Monde, pas arène.** Les joueurs ne se croisent jamais : il n'y a ni contact ni PvP. Le monde doit seulement être survivable et **générateur d'actions** (zombies, loot, matériaux) ; la carte est un réglage de densité d'opportunités, pas de rencontre. Voir `03-mecanismes-et-telemetrie.md`.
2. **Reset entre matchs.** Deux mondes persistants se dégradent (loot consommé, zombies tués, bâtiments défoncés). Pour enchaîner les duels, il faut un **snapshot des deux dossiers de save pris à la création**, restauré serveurs arrêtés entre deux matchs (~1-2 min). C'est un poste de travail réel (+2-3 j) et une étape d'exploitation, pas un détail.
3. **Fin de match.** Avec 1a (aucune pause) et format court : minuteur + score, et **mort = défaite immédiate** (mort subite). Le minuteur doit être visible des deux côtés et identique (il vit dans l'arbitre, pas dans le jeu).
4. **Déconnexion.** 1a implique qu'un joueur qui tombe ne peut pas geler le match : grâce de 2 min puis forfait, match sauvegardé et résultat enregistré. À écrire noir sur blanc dans les règles affichées.
5. **4b plus tard.** Le protocole doit être transport-agnostique : lignes d'événements horodatées, idempotentes, avec numéro de séquence. Passer de « lot 10-15 s » à « event par event » devient alors un changement de cadence + un agent Java côté serveur, sans toucher aux règles.
6. **2a → serveur séparé.** L'arbitre et les mods ne doivent connaître que `localhost`. Le jour où les serveurs partent sur un VPS, seuls les chemins de fichiers et les ports changent.

### 12.3 Budget ajusté

Ajouter 2-3 j pour le snapshot/reset des mondes, retirer ~1 j de surveillance d'horloge devenue cosmétique : **total 28-45 j** pour le MVP, hors map dédiée et hors mode temps réel.
