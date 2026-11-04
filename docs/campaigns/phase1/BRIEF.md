# BRIEF — Campagne phase 1 : du mock au duel réel

## Contexte

`pz-duel` est un projet naissant : un mode duel pour Project Zomboid où deux joueurs vivent dans deux mondes séparés (aucune rencontre) et où chaque action de l'un devient une pression pour l'autre.

Ce qui existe déjà (à lire, à ne pas réécrire) :

- `01-etat-des-lieux.md` … `09-ui-full-ingame.md` : conception, architecture, inventaire des événements observables et générables, play flow, table de relations, spécification MVP, flow du lanceur, UI.
- `service/` : ~550 lignes de Python **stdlib uniquement**. Machine à états (`engine.py`), état (`state.py`), API HTTP locale (`api.py`), point d'entrée (`main.py`), presets JSON.
- `ui/` : page locale (HTML/CSS/JS, QR embarqué MIT).
- `CREDITS.md` : crédits des mods et auteurs dont le projet s'inspire.
- **Le backend est simulé** : aucun serveur PZ n'est lancé, aucun mod n'existe, le préflight affiche des succès fictifs.

## Objectif de la phase 1

Transformer ce prototype en **duel réel de bout en bout**, sans changer l'expérience utilisateur déjà validée :

1. **Cycle de vie réel des serveurs** : le service lance deux serveurs dédiés PZ, attend qu'ils soient prêts, vérifie leur configuration, les arrête proprement.
2. **Mod serveur `duelcore`** : capte les faits de jeu côté serveur, écrit un flux d'événements, lit un flux de commandes, applique les effets (spawn, bruit, météo).
3. **Moteur de règles réel** : atomes → agrégats → séries → effets, avec budgets et plafonds, alimenté par le flux d'événements.
4. **Journal de match** : JSONL horodaté et séquencé, rejouable, qui sert aussi de télémétrie d'équilibrage.
5. **Tests** : le moteur de règles se teste sans le jeu (flux d'événements enregistrés) ; les briques système se testent avec des doubles.

Le mock doit rester disponible (`--mode mock`) pour la démonstration, mais il ne doit **jamais** présenter une vérification fictive comme réelle.

## Faits techniques vérifiés (ne pas les remettre en cause sans preuve)

- Build installé : **42.21.0** (binaire serveur headless lançable depuis l'install client via `start-server.sh`).
- Un serveur dédié démarre sans Steam, écrit son `.ini` dans `~/Zomboid/Server/<servername>.ini`, écoute sur le port passé en `-port`.
- **Un `servername` neuf exige un mot de passe admin** ; sans `-adminpassword` il bloque sur une invite stdin. Le service doit toujours le passer en argument.
- Un mod Lua serveur se charge depuis `~/Zomboid/mods/<id>/<version>/mod.info` + `media/lua/server/` ; sans le sous-dossier de version il est **silencieusement ignoré**.
- Depuis Lua serveur, `getFileWriter`/`getFileReader` opèrent dans `~/Zomboid/Lua/`.
- Deux serveurs lancés sous le même compte partagent ce dossier : c'est le transport du projet (prouvé, deux fichiers distincts, horloges identiques au démarrage).
- `RCONPort` est un réglage propre (défaut 27015), pas « port de jeu + 1 » ; deux serveurs doivent avoir des ports distincts.
- `PauseEmpty=true` par défaut : à passer à `false`, sinon un joueur qui se déconnecte fige son monde et pas l'autre.
- En multi, l'UI de vitesse du client est désactivée (`SpeedControlsHandler.lua` : `if isClient() then return`), donc **pas de pause ni d'accélération** : c'est le comportement voulu.
- Fonctions serveur disponibles (javadoc 42.x) : `addZombiesInOutfit`, `addZombiesInOutfitArea`, `addZombiesInBuilding`, `addZombieSitting`, `addZombiesEating`, `createZombie`, `createHordeFromTo`, `createHordeInAreaTo`, `addVehicle`, `addSound`, `createRandomDeadBody`, `addXpMultiplier`.
- Côté observation : `OnCharacterDeath`, `OnHitZombie`, `OnChildZombie`, `OnWorldSound`, `OnFillContainer`, `OnWeaponHitThumpable`, `OnObjectAdded`, `OnTileRemoved`, `OnMechanicActionDone`, `OnThrowableExplode`, `OnNewFire` sont documentés serveur ou non marqués client.

## Incertitudes à traiter explicitement (pas à supposer)

- `OnZombieDead` + `IsoZombie:getAttackedBy()` : attribution du tueur en multi **non prouvée**. Le code doit tolérer l'absence d'attribution (événement sans camp, journalisé) et ne jamais inventer un camp.
- `OnWeaponHitCharacter` / `OnEnterVehicle` : la doc les marque `(Client)` alors qu'un mod d'analytics les utilise côté serveur. Le code doit sonder au démarrage du mod et journaliser la disponibilité réelle.
- **Craft** : aucun événement serveur fiable connu. La phase 1 ne doit pas en dépendre. Un mécanisme de repli (diff d'inventaire) peut être spécifié mais reste optionnel et désactivé par défaut.
- La disponibilité des fonctions de spawn **côté serveur en multi** est à confirmer par le mod au premier lancement (écrire un résultat, pas une croyance).

## Contraintes

- Service : **Python 3.11+, stdlib uniquement** (pas de dépendance runtime). Tests avec `pytest` en `.venv`.
- Mods : Lua PZ uniquement, aucune dépendance externe, un `mod.info` par variante de version.
- Ne pas modifier les documents `01-…` à `09-…` (ils sont la référence de conception) ; produire la spécification dans `spec.md`.
- Ne pas publier sur Steam, ne pas ajouter d'agent Java, ne pas implémenter l'UI in-game.
- Aucun secret (mot de passe admin, RCON) écrit en clair dans les logs ou l'état exposé.
- Langue : documents et messages d'erreur en français ; identifiants, noms de fichiers et code en anglais.
- Toute valeur d'équilibrage doit rester dans les presets/fichiers de règles, jamais dans le code.

## Ce que la spécification doit contenir

- Des exigences numérotées (`R1…`) et des critères d'acceptation testables (`AC1…`), chacun relié à un test nommé.
- Les contrats d'interface : format d'un événement, d'une commande, d'un heartbeat ; sémantique de l'`ack` et de la troncature ; gestion du redémarrage d'un serveur (déduplication par `seq`, reprise d'offset).
- Le cycle de vie complet : préflight réel (lecture des `.ini`, cohérence des mods, ports libres, RAM), démarrage, attente, contrat, lobby, match, arrêt, snapshots et reprise.
- Le moteur de règles : atomes, agrégats (`count`, `within`), séries (`then`), modificateurs (combo, écart de score), budgets (plafond par minute, effets simultanés, cooldown), et **ce qui est interdit** (effet non déclaré, source non autoritaire).
- La stratégie de test sans le jeu : flux d'événements synthétiques rejoués dans le moteur ; doubles pour les processus serveur ; test d'intégration optionnel marqué `skip` si PZ absent.
- Les critères d'échec explicites : si l'attribution des kills ou le spawn serveur ne fonctionnent pas, quelle est la branche de repli (archi solo + IPC, ou agent Java) et comment le produit le signale.

## Non-objectifs de la phase 1

Publication Steam, matchmaking, classement, éditeur graphique de table, HUD in-game, effets « expérience » côté client, portage du directeur en Lua.
