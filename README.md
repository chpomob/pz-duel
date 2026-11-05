# PZ Duel — service local

Interface de lancement et d'arbitrage d'un duel Project Zomboid : deux mondes séparés, aucune rencontre, chaque action d'un joueur devient une pression pour l'autre.

**État actuel : duel réel, jamais encore joué.** `--mode real` pilote deux serveurs dédiés, le mod `duelcore` et le moteur de règles. Le démarrage, le contrat et l'arrêt sont prouvés sur le vrai jeu, sans joueur ; tout ce qui exige un joueur reste à constater (voir [Premier contact](#premier-contact-avec-le-vrai-jeu)). Le mode `mock`, par défaut, garde le parcours de démonstration.

## Lancer

```bash
cd /home/chpo/pz-duel
python3 -m service.main                # mode mock, ouvre http://127.0.0.1:8765/
python3 -m service.main --port 8766 --no-browser
```

Aucune dépendance Python : stdlib uniquement. Le service écrit `.duel-service.json` (URL, port, pid) pour que les outils retrouvent l'interface.

Parcours de démonstration : `Configurer un duel` → preset **Démo 90 s** → `Lancer le duel` → dans le lobby, `Simuler l'adversaire`.

### Mode réel

```bash
python3 -m service.main --install-mod  # copie mods/duelcore dans ~/Zomboid/mods/, puis quitte
python3 -m service.main --mode real
python3 -m service.main --mode real --pz-dir /chemin/vers/ProjectZomboid
```

- `--install-mod` ne touche qu'à `~/Zomboid/mods/duelcore/`. À relancer après chaque modification du mod : le préflight compare l'empreinte installée à celle du dépôt et refuse tout écart.
- Le jeu se cherche dans `--pz-dir`, sinon `PZ_INSTALL_DIR`, sinon les chemins Steam connus. Un choix explicite invalide échoue sans repli sur Steam. Le chemin désigne `ProjectZomboid/` ou `ProjectZomboid/projectzomboid/`.
- Il faut 6 ports libres (jeu 17261 et 17271, UDP 17262 et 17272, RCON 27015 et 27016) et `min_free_ram_mb` de mémoire disponible : chaque serveur prend 2 Go. L'invité doit pouvoir joindre l'hôte sur le port de jeu de B.
- Dans le lobby, `Ouvrir PZ` connecte l'hôte au serveur A par Steam. L'invitation porte l'adresse, le port de B et le mot de passe.

**Ce que le mode réel écrit dans `~/Zomboid` :**

- `Server/duelA.ini`, `duelB.ini` et leurs `_SandboxVars.lua` : seules les clés gérées changent, mais `RCONPassword` y est en clair, d'où les droits `0600` ;
- `duel-snapshots/` : le premier lancement capture les mondes `duelA` et `duelB` s'ils existent, sinon les crée ;
- `Saves/Multiplayer/duelA`, `duelB` et `db/duelA.db`, `duelB.db` : **chaque lancement les restaure depuis ce snapshot**. Un monde ne survit donc pas d'un match à l'autre. Pour changer de monde d'origine, serveurs arrêtés, supprimez `duel-snapshots/` ;
- `Lua/duel_A_*.txt`, `duel_B_*.txt` : le transport, vidé à chaque lancement.

Journal et sorties des serveurs, expurgés, sont dans `matches/<match_id>/`.

### Copie du mod chez l'invité

Le mod n'est pas sur le Workshop. Un client doit a priori le posséder localement pour rejoindre, même si `duelcore` n'a aucun code client (hypothèse H2, non vérifiée). Avant de se connecter, l'invité copie donc à la main tout le dossier `mods/duelcore/` du dépôt, ou de l'hôte, dans son propre dossier `Zomboid/mods/` :

```bash
scp -r hote:pz-duel/mods/duelcore ~/Zomboid/mods/
```

Le résultat doit être `~/Zomboid/mods/duelcore/mod.info` et `~/Zomboid/mods/duelcore/42.21/…` : sans le sous-dossier de version, le mod est ignoré sans message. La copie doit suivre chaque changement de version du mod.

## Procédure de verdict en jeu

C'est la seule preuve d'AC19, de H2, de l'auteur de `build` et d'`explosion` et de l'attribution des kills. Elle demande deux personnes, l'hôte sur A et l'invité sur B, et le preset **Duel rapide**, qui contient `zombie_kill`, `damage_taken` et un `spawn_pop` à 30-60 cases.

1. Démarrez en `--mode real`, lancez le duel avec une durée courte, puis connectez les deux joueurs. Notez si l'invité entre avec le mod copié, et si un invité sans le mod est refusé (H2).
2. **Faites-vous frapper par un zombie sur chaque camp dans les `hook_window_s` (120 s) qui suivent l'arrivée des deux joueurs.** Le décompte attend ce coup : `damage_taken` n'est actif que si `OnWeaponHitCharacter` a produit un `hook_seen` sur A et sur B, et le mod ne l'émet qu'au premier coup reçu par le joueur. Frapper un zombie ne prouve rien. Sans cela, l'atome est retiré, et une table qui le contient est refusée (phase `error`, nommant l'atome, le hook et le camp). Un zombie est créé à 30-60 cases par la sonde de spawn à la connexion de chacun : c'est une première cible, et sa présence est déjà un constat de spawn. Le détail de l'étape `Contrat` affiche le hook attendu et le temps restant.
3. En match, tuez un zombie sur A. B doit recevoir un `spawn_pop` à 30-60 cases : **le joueur B vérifie qu'il voit ces zombies apparaître**, car le journal ne prouve que ce que l'API rend.
4. Faites-vous blesser par un zombie sur un camp : une blessure doit donner un `event` `damage_taken`. L'étape 2 prouve qu'un coup sur le joueur tire le hook ; vérifiez ici qu'une morsure le fait aussi.
5. Un joueur s'endort, ou monte en voiture, pendant que l'autre tue des zombies : la commande reçue pendant ce temps doit finir `refused` (`asleep` ou `in_vehicle`). Faites les deux si possible.
6. Laissez le match aller à son terme, sans `Annuler`, puis copiez l'état en phase `over` et contrôlez les traces :

```bash
curl -s http://127.0.0.1:8765/api/state > state.json
PZ_LIVE_JOURNAL=matches/<match_id>/journal.jsonl PZ_LIVE_STATE=state.json \
  .venv/bin/python -m pytest -m pz_integration \
  tests/test_integration_pz.py::test_live_journal_checklist
```

Ce test échoue si un fait manque, ou si l'état et le journal se contredisent sur le retrait de `damage_taken`. S'il a été retiré, ce qui suppose une table sans cet atome, aucune blessure n'est exigée. **Il ne voit ni le spawn côté client, ni un refus manqué : ces constats restent ceux du joueur.** Un `skip` n'est pas une preuve : AC19 reste ouvert tant que cette procédure n'a pas été déroulée.

### Relire un match

```bash
python3 -m service.replay matches/<match_id>/journal.jsonl            # 0 si le rejeu redonne les mêmes décisions
python3 -m service.replay --summary matches/<match_id>/journal.jsonl  # télémétrie par camp, en JSON
python3 -m service.verdict matches/<match_id>/journal.jsonl           # attribution, spawn, repli, hooks vus
```

`service.verdict` répond par `attribution` (`ok`, `ko`, ou `insufficient` sous `attribution_min_kills`, soit 100 kills), `spawn` par camp et `fallback`. Une branche de repli (`java_agent`, `solo_ipc`) n'est que signalée, jamais appliquée.

## Tests

```bash
.venv/bin/python -m pytest -q                   # sans le jeu ; les tests du vrai jeu sont en skip
.venv/bin/python -m pytest -m pz_integration    # vrais serveurs, environ 2 min 30
```

**`-m pz_integration` écrit dans le vrai `~/Zomboid`** : mod `duelcore`, réglages de `duelA` et `duelB`, snapshots, restauration des mondes. Sans PZ, ces tests sont en `skip` avec leur motif. Ne le lancez pas pendant un duel : le préflight refuserait les ports.

## Premier contact avec le vrai jeu

Relevé le 2026-11-05, build 42.21.0, sans joueur. `pytest -m pz_integration` : 4 réussis, 1 `skip` (`test_live_journal_checklist`).

| Point | Constat |
|---|---|
| AC18, `caps` | Confirmé : première ligne de `out` sur A et B, `build` 42.21.0, bon `src`, avec tous les atomes et effets |
| AC61, démarrage et arrêt | Confirmé : serveurs prêts en 20 s environ, contrat conforme, arrêt par RCON (`save` puis `quit`), aucun processus résiduel, aucun secret dans `matches/` |
| Lecteur de `in` (R1) | Confirmé : `reader:read()` passe par le vrai `LuaManager`. Plancher, attente, relecture, ligne coupée et `\n` manquant tiennent. Le test échoue bien sur un Lua volontairement faussé (ligne sans `\n` rendue, `seq` avancé à l'application) |
| H1, `.ini` partiel | Confirmée à part, sur un `-cachedir` isolé : 12 lignes écrites, 421 après création, clés gérées conservées, droits `0600` gardés, `SandboxVars` créé avec `DayLength` et `Start*` |
| H3, serveur sans joueur | **Contredite** : avec `PauseEmpty=false`, un serveur vide tique. Un `beat` toutes les 10 s, et `age` avance au rythme nominal (0,27 h de jeu en 10 s pour `DayLength`=1). Le mod traite donc ses commandes avant toute connexion. Rien n'en dépend : le service n'exige aucun `beat` avant la première connexion, il en reçoit seulement plus tôt |
| Hooks | Enregistrement confirmé sur les 10 hooks et `OnTick`. **Signatures non prouvées** : aucun `hook_seen` n'a tiré sans joueur ; seule la boucle `OnTick` est vue à l'œuvre |
| Parcours `--mode real` | Lobby atteint en 21 s, invitation sur le port de B, `Annuler` en 10 s, `SIGTERM` propre |

Reste non prouvé, faute de joueur : AC19, H2, les signatures de hooks, le spawn serveur en multi (`addZombiesInOutfit` n'est constaté que présent, `probe_spawn` finit `refused` sans joueur), l'attribution des kills, et la connexion d'un client Steam à un serveur lancé par `start-server.sh`, qui tourne avec `-Dzomboid.steam=0`.

## Structure

```
service/
  main.py        point d'entrée, choix du port, --mode, --install-mod, --pz-dir
  api.py         serveur HTTP local (UI + /api/state, /api/presets, /api/action)
  engine.py      moteur du duel, mock et réel : du préflight à la fin du match
  state.py       état partagé, étapes de lancement, journal d'interface
  preflight.py   détection de PZ, ports, RAM, empreinte du mod, installation du mod
  servers.py     réglages gérés, lancement, arrêt gradué, contrat, expurgation
  rcon.py        client RCON (save, quit)
  snapshots.py   capture et restauration des mondes, passe de création
  protocol.py    contrat v1 : événements, commandes, ack, session
  transport.py   fichiers out, in, ack, session
  rules.py       table de relations, budgets, cœur du match
  journal.py     journal JSONL
  replay.py      rejeu et télémétrie
  verdict.py     attribution, spawn, branches de repli
  runtime.json   ports, délais, seuils
  effects.json   catalogue d'atomes et d'effets
  presets/       duel_rapide, pression, hardcore, demo (règles + résumés)
mods/duelcore/   mod serveur : capte, publie ses capacités, applique les commandes
ui/
  index.html     coquille + crédits
  app.js         rendu, actions, QR
  style.css      thème sombre
  vendor/        qrcode-generator (MIT, © Kazuhiko Arase) + licence
tests/
  doubles/       faux serveur, faux mod, service de test
```

## Ce qui est simulé

**En `--mode real`, rien.** Un échec reste un échec et ne retombe jamais sur le mock ; `simulate_join` y est refusé.

**En mode `mock`, tout**, et chaque vérification s'annonce simulée : préflight, démarrage des serveurs, contrat, connexion des joueurs, décompte, score, dérive et événements. Le mock ne lance aucun processus, n'écrit ni dans `~/Zomboid` ni dans `matches/`.

**Dans les tests sans le jeu**, serveurs et mod sont des doubles (`tests/doubles/`) : ils ne prouvent pas le comportement du vrai Lua, que seul `-m pz_integration` touche.

## Documentation

- `01-etat-des-lieux.md` — faisabilité, écosystème, charges
- `02-archi-deux-serveurs.md` — deux serveurs, horloge, config réelle
- `03-mecanismes-et-telemetrie.md` — atomes/effets, règles, télémétrie
- `04-spike-0-preuves.md` — preuves d'exécution (serveur, IPC)
- `05-observable-vs-generable.md` — ce qui est captable / générable
- `06-play-flow-et-table-de-relations.md` — flow et table
- `07-spec-mvp.md` — spécification consolidée et critères d'acceptation
- `08-flow-fluide-lanceur-qr.md` — lanceur, invitation, deux modes
- `09-ui-full-ingame.md` — écrans, UI locale, cap in-game
- `CREDITS.md` — mods et auteurs dont on s'inspire (à lire avant toute reprise de code)
