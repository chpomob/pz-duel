# PZ Duel

*Français — [English version](README.md)*

> ⚠️ **Prototype de concept — aucun duel réel n'a encore été joué.** Les serveurs, le mod et le
> contrat sont prouvés sur le vrai jeu ; tout ce qui demande un joueur reste à constater.

Deux parties solo de Project Zomboid, reliées par un arbitre local : les joueurs ne se croisent
jamais, mais **chaque action de l'un devient une pression dans le monde de l'autre**. Un zombie tué
chez A fait apparaître deux zombies chez B. Un craft réussi chez B envoie une horde chez A.

L'idée est celle d'un duel à la Tetris, transposée à la survie : le monde de chacun est un terrain
de jeu séparé, et la seule chose qui circule est la conséquence de ce qu'on y fait.

## Comment ça marche

```
        ┌──────────────┐                      ┌──────────────┐
        │  Serveur PZ  │  out (JSONL)         │  Serveur PZ  │
        │      A       │ ─────────┐   ┌────── │      B       │
        │  monde A     │          │   │       │  monde B     │
        └──────────────┘          ▼   ▼       └──────────────┘
                              ┌──────────────┐
                              │   Arbitre    │  table de relations
                              │   (Python)   │  horloge, score, journal
                              └──────────────┘
```

- Le mod `duelcore` tourne sur chaque serveur, capture ce qui s'y passe et **publie** des atomes
  (`zombie_kill`, `damage_taken`, `craft`, `player_death`…).
- L'arbitre lit ces atomes, applique une **table de relations** configurable, et écrit des commandes
  dans le monde adverse (`spawn_pop`, `hurt_supply`, `weather_rain`…).
- Aucune connexion entre les deux joueurs, aucun monde miroir : deux sauvegardes séparées, donc du
  loot et des véhicules différents. L'équité est dans les règles et les conditions de départ, pas
  dans l'identité des mondes.

## Démarrage rapide (sans Project Zomboid)

Le mode `mock` rejoue tout le parcours — préflight, serveurs, contrat, décompte, score — sans lancer
un seul processus.

```bash
git clone https://github.com/chpomob/pz-duel.git && cd pz-duel
python3 -m service.main            # ouvre http://127.0.0.1:8765/
```

Python **≥ 3.12**, aucune dépendance : le service est en bibliothèque standard uniquement.

Parcours : `Configurer un duel` → preset **Démo 90 s** → `Lancer le duel` → dans le lobby,
`Simuler l'adversaire`. Tout ce qui est simulé est annoncé comme tel.

## Mode réel

Il faut Project Zomboid **42.21** (le lanceur serveur est livré avec le jeu), 6 ports libres
(jeu 17261/17271, UDP 17262/17272, RCON 27015/27016) et environ 6 Go de RAM disponible — chaque
serveur en prend 2.

```bash
python3 -m service.main --install-mod     # copie mods/duelcore dans ~/Zomboid/mods/
python3 -m service.main --mode real       # détection Steam automatique
python3 -m service.main --mode real --pz-dir /chemin/vers/ProjectZomboid
```

- `--install-mod` ne touche qu'à `~/Zomboid/mods/duelcore/`. À relancer après chaque modification du
  mod : le préflight compare l'empreinte installée à celle du dépôt et refuse tout écart.
- Les serveurs tournent **sans Steam** (`-Dzomboid.steam=0`), le client aussi : le lobby peut lancer
  `projectzomboid.sh -nosteam +connect … +password …`. Si le script manque ou si le client s'arrête
  dans les deux secondes, le bouton rend un échec qui nomme la cause et la commande à passer à la main.
- Chaque camp a son mot de passe ; celui de l'hôte n'apparaît qu'au lobby, celui de l'invité part dans
  l'invitation (QR ou lien).
- **Machine mono-utilisateur uniquement** : l'API locale n'a pas de jeton, tout processus local peut
  lire `/api/state` et piloter le duel. Les mots de passe figurent aussi dans `ps`.

Ce que le mode réel écrit dans `~/Zomboid` : `Server/duelA.ini` et `duelB.ini` (en `0600`, car
`RCONPassword` y est en clair), `duel-snapshots/`, `Saves/Multiplayer/duelA|duelB` (restaurés à
chaque lancement), et les fichiers de transport `Lua/duel_A_*.txt`, `duel_B_*.txt`. Les journaux et
sorties expurgés vont dans `matches/<match_id>/`.

## Rythme de synchronisation

Valeurs du code (`service/runtime.json`, `mods/duelcore/…/DuelCore.lua`), pas du document de conception.

| Étape | Cadence |
|---|---|
| Mod → fichier `out` | tampon **1 s**, vidé **immédiatement** sur un fait concluant (mort, score) |
| Arbitre lit `out` | toutes les **250 ms** |
| Tick du cœur de match | **250 ms** |
| Mod lit `in` | toutes les **1 s** |
| `ack` / `beat` de présence | **5 s** / **10 s** (timeout 30 s) |
| Effets | servis à chaque tick, FIFO par camp, tranches de 30 unités, plafonds `cap_per_min` (max 120/min) |
| Conclusion | **+1,5 s** après la dernière lecture (voir les limites) |
| Dérive tolérée | 60 s, écart d'âge du monde × DayLength × 2,5 |

Latence typique d'un événement, du kill en jeu à la commande écrite chez l'adversaire : **~2,3 s**
(1 s de vidage + 250 ms de lecture + 1 s de lecture du mod), hors attente de plafond.

## Ce qui est prouvé, et ce qui ne l'est pas

Prouvé, sur cette installation (build 42.21.0), par `pytest -m pz_integration` :

- deux serveurs réels démarrent, le mod `duelcore` se charge, `caps` est publié et le contrat est
  vérifié intégralement (`verify(caps) == []`) ;
- arrêt par **RCON**, ports libérés, fichiers `.ini` en `0600`, **aucun secret** dans les fichiers de
  match, aucun processus résiduel ;
- le lecteur de commandes lit une ligne au plus une fois, y compris ligne coupée ou `\n` manquant ;
- 1 214 tests hors jeu couvrent le protocole, la table de relations, le score, le journal, le rejeu,
  l'API locale et l'UI (suite verte sur Python 3.12 et 3.14).

**Jamais exercé**, et c'est le cœur du produit :

- un duel **avec deux joueurs** : attribution des kills en multi (`OnZombieDead` + `getAttackedBy`),
  visibilité côté client d'un spawn lancé par le serveur, craft via `OnProcessAction` ;
- la connexion d'un client `-nosteam` à un serveur lancé par `start-server.sh` ;
- que la santé d'un joueur bouge côté serveur, condition de l'atome `damage_taken` ;
- ce que devient un joueur mort dans le vrai jeu (reste en ligne, respawn, déconnexion).

La procédure de 15 minutes qui tranche ces points est décrite dans [`README`](#procédure-de-verdict-en-jeu)
plus bas ; tant qu'elle n'a pas été déroulée, ces cases restent ouvertes, et les documents le disent.

## Tests

```bash
python3 -m pytest -q                          # sans le jeu (1 214 tests, ~60 s)
python3.12 -m pytest -q                       # plancher de version déclaré
python3 -m pytest -m pz_integration           # vrais serveurs, ~2 min 30
```

`-m pz_integration` **écrit dans le vrai `~/Zomboid`** (mod, réglages, snapshots, restauration des
mondes). Ne le lancez pas pendant un duel : le préflight refuserait les ports.

## Procédure de verdict en jeu

C'est la seule preuve des points ci-dessus. Elle demande deux personnes, l'hôte sur A et l'invité
sur B, et le preset **Duel rapide** (`zombie_kill`, `damage_taken`, `spawn_pop` à 30-60 cases).

1. Lancer en `--mode real`, durée courte, connecter les deux joueurs. Noter si l'invité entre avec le
   mod copié, et si un invité sans le mod est refusé.
2. Le décompte part quand les deux joueurs sont là et que les sondes de spawn ont répondu. Un zombie
   apparaît à 30-60 cases de chacun : première cible, et déjà un constat de spawn.
3. Sur A, tuer un zombie. B doit recevoir un `spawn_pop` : **c'est le joueur B qui vérifie qu'il voit
   les zombies apparaître**, le journal ne prouve que ce que l'API rend.
4. Se faire blesser par un zombie après 5 s sans saigner : l'événement `damage_taken` doit sortir avec
   `hp`, `attacker` et `source`. Noter ce que porte `source` et si un `hook_seen` d'`OnPlayerGetDamage`
   suit.
5. S'endormir ou monter en voiture pendant que l'autre tue : la commande reçue doit finir `refused`
   (`asleep`, `in_vehicle`).
6. Laisser le match aller à son terme, puis contrôler les traces :

```bash
curl -s http://127.0.0.1:8765/api/state > state.json
PZ_LIVE_JOURNAL=matches/<match_id>/journal.jsonl PZ_LIVE_STATE=state.json \
  python3 -m pytest -m pz_integration tests/test_integration_pz.py::test_live_journal_checklist
```

Un `skip` n'est pas une preuve : le point reste ouvert tant que la procédure n'a pas été déroulée.

## Relire un match

```bash
python3 -m service.replay matches/<match_id>/journal.jsonl            # 0 si le rejeu redonne les mêmes décisions
python3 -m service.replay --summary matches/<match_id>/journal.jsonl  # télémétrie par camp
python3 -m service.verdict matches/<match_id>/journal.jsonl           # attribution, spawn, hooks vus
```

Le journal est en JSONL, horodaté, séquencé et idempotent : le rejeu instancie le même cœur de match,
donc rejouer un journal prouve le moteur.

## Structure

```
service/          arbitre : préflight, serveurs, RCON, protocole, transport fichier,
                  table de relations, cœur de match, journal, rejeu, verdict, API locale
mods/duelcore/    mod serveur (Lua) : capture les faits, publie ses capacités, applique les commandes
ui/               interface locale (HTML/CSS/JS, aucune dépendance hors QR vendorisé)
tests/            1 214 tests ; doubles de serveur, de mod et interpréteur Lua réduit
docs/campaigns/   spec (77 critères), plan (24 étapes), 4 revues de branche, 15 étapes de correction
docs/design/      conception en 10 documents, anglais et français (voir l'index)
```

## Limites connues

Le détail daté est dans [`docs/campaigns/phase1/limites-connues.md`](docs/campaigns/phase1/limites-connues.md).
Les plus importantes :

- **La détection des dégâts repose sur un relevé de santé côté serveur** dont on n'a pas constaté
  qu'il tourne pour un joueur connecté. Si non, l'atome est retiré et la règle de blessure disparaît
  — le lobby l'annonce, le journal le porte.
- **La conclusion attend 1,5 s** : le match tourne encore un instant avec une mort déjà journalisée.
  C'est volontaire, pour laisser passer une mort plus ancienne encore dans le tampon du mod.
- **Pas de relance automatique d'un serveur figé**, pas de récupération des serveurs orphelins si
  l'arbitre meurt brutalement, pas de jeton sur l'API locale.
- **Pas d'anti-triche** : le joueur possède ses fichiers. Le classement n'a de sens qu'entre amis.

## Licence

WTFPL v2 — voir [`LICENSE`](LICENSE). Les crédits des mods et auteurs dont ce projet s'inspire sont
dans [`CREDITS.md`](CREDITS.md), à lire avant toute reprise de code.
