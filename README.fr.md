# PZ Duel

*Français — [English version](README.md)*

> ⚠️ **Prototype de concept — aucun duel réel n'a encore été joué.** Les serveurs, le mod et le
> contrat sont prouvés sur le vrai jeu sous Linux ; tout ce qui demande un joueur, et Windows au-delà
> de ses machines de CI, reste à constater.

Deux parties solo de Project Zomboid, reliées par un arbitre local : les joueurs ne se croisent
jamais, mais **chaque action de l'un devient une pression dans le monde de l'autre**. Un zombie tué
chez A fait apparaître deux zombies chez B. Un craft réussi chez B envoie une horde chez A.

L'idée est celle d'un duel à la Tetris, transposée à la survie : le monde de chacun est un terrain
de jeu séparé, et la seule chose qui circule est la conséquence de ce qu'on y fait.

## D'où vient l'idée

Le concept — deux parties de Project Zomboid reliées pour que les actions de l'un deviennent des
menaces chez l'autre — a été décrit par **Harumate**, streameur français sur Project Zomboid, sur sa
chaîne : [twitch.tv/harumate](https://www.twitch.tv/harumate). Ce dépôt est une implémentation
indépendante de cette idée, sans lien avec lui ; l'idée, elle, est la sienne.

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

## Démarrage sous Windows

> ⚠️ **Pas encore essayé avec le vrai jeu sous Windows.** La version Windows passe ses tests sur les
> machines de CI, mais rien de ce qui suit n'a été joué sur un vrai poste Windows ni face au vrai jeu.
> C'est le parcours visé ; les [limites connues de la phase 2](docs/campaigns/phase2/limites-connues.fr.md)
> disent ce qui est prouvé.

Aucun terminal n'est nécessaire. Les deux joueurs ont besoin de Project Zomboid **42.22 ou plus récent**
sur Steam, sous Windows — le mod déclare `versionMin=42.21.0` et refuse donc un build plus ancien.
L'hôte fait tourner les deux serveurs et a besoin d'environ 6 Go de RAM disponible. Rien d'autre à
installer : PZ Duel lance les deux serveurs avec le JRE que le jeu embarque (`jre64\bin\java.exe`) et se
sert de `StartServer64.bat` quand ce script existe.

### Hôte

1. **Téléchargez** `pz-duel.exe` depuis la [page des releases](https://github.com/chpomob/pz-duel/releases).
   Il n'y a rien à installer.
2. **Double-cliquez dessus.** Le fichier n'est pas signé : **SmartScreen** peut afficher « Windows a
   protégé votre ordinateur ». Cliquez sur **Informations complémentaires**, puis sur **Exécuter quand
   même**.
3. **Vérifiez la carte Setup** de la page qui s'ouvre. Pour toute ligne marquée ✕, utilisez le bouton
   proposé : `Install mod`, `Choose folder` ou `Check again`.
4. **Lancez un duel** : `Configurer un duel`, choisissez un preset, puis `Launch duel`.
5. **Répondez à l'invite du pare-feu Windows.** Windows peut demander si PZ Duel ou le serveur du jeu a
   le droit d'utiliser le réseau : cliquez sur **Autoriser l'accès**. PZ Duel ne peut pas le faire à
   votre place. Si vous avez refermé l'invite, votre invité n'entrera pas : ouvrez *Sécurité Windows*,
   *Pare-feu et protection du réseau*, *Autoriser une application via le pare-feu*.
6. **Invitez votre adversaire** depuis le lobby : `Copy link`, ou `Save launcher` pour obtenir
   `pz-duel-join.bat`. Le lobby affiche aussi un **code de vérification** : **lisez-le à voix haute** à
   votre invité, **séparément du lien**, jamais dans le même message que le lien ou le fichier. C'est la
   seule protection contre un fichier remplacé en route. Ensuite, `Ouvrir PZ` vous connecte à votre
   propre serveur.
7. **Partez par `Quit`** dans la page. Fermer l'onglet du navigateur n'arrête pas PZ Duel.

Si `Launch duel` finit sur une erreur, utilisez `Try again`. Si l'écran vous le demande, quittez et
rouvrez PZ Duel.

### Invité

1. **Récupérez le `.bat`** sur le PC Windows qui a le jeu : ouvrez-y le lien de l'hôte, ou enregistrez
   le fichier qu'il vous a envoyé. Le lien ne marche que pendant que l'hôte est dans le lobby. Scanner
   le QR avec un téléphone met le fichier sur le téléphone, où il ne sert à rien.
2. **Vérifiez le fichier avant de l'ouvrir.** Demandez à l'hôte le code de vérification qu'il lit à
   voix haute. Dans le dossier qui contient le fichier, faites un clic droit sur un espace vide,
   choisissez **Ouvrir dans le Terminal** (sous Windows 10, maintenez Maj pendant le clic droit pour
   obtenir une fenêtre du même genre), et tapez `certutil -hashfile pz-duel-join.bat SHA256`.
   **N'ouvrez pas le fichier si le résultat ne commence pas par ce code** : il a été remplacé en route.
   Supprimez-le et demandez-en un nouveau à l'hôte.
3. **Seulement ensuite, double-cliquez sur `pz-duel-join.bat`.** **SmartScreen** peut aussi prévenir
   ici : **Informations complémentaires**, puis **Exécuter quand même**. Ne le faites que pour un
   fichier reçu d'une personne de confiance, vérifié à l'étape 2. Il redemande le code : tapez-le, puis
   Entrée. Un lanceur qui ne le demande pas a été remplacé.
4. Il installe le mod, lance le jeu connecté à l'hôte, et attend quelques secondes pour vérifier que le
   jeu tourne toujours. S'il ne trouve pas le jeu, il demande le dossier. En cas d'échec, ou si le jeu
   se ferme aussitôt, votre mod existant est remis tel quel et la fenêtre reste ouverte. En cas de
   succès, le mod `duelcore` qu'il a remplacé n'est pas supprimé : il est gardé dans
   `Zomboid\duelcore.previous`, dans votre dossier utilisateur, et la fenêtre dit comment y revenir.

### Jouer par Internet

Sur le même réseau local, il n'y a rien de plus à faire. Sinon, l'hôte redirige ces ports de sa box
vers son PC :

- les ports **17271** et **17272** en **UDP**, pour jouer ;
- le port de partage **17280** en **TCP**, pour le lien seulement. Si vous envoyez le `.bat` par
  message, il est inutile.

Saisissez ensuite votre adresse publique dans le lobby, sous *Public address*, et cliquez sur
`Use this address` avant d'envoyer quoi que ce soit. PZ Duel ne peut pas tester cette adresse et ne
fournit aucun relais.

### Ne diffusez jamais le `.bat`

**Ne publiez pas et ne diffusez pas le `.bat`.** Il contient votre adresse et le mot de passe du serveur
de l'invité : quiconque l'a peut entrer. Envoyez-le à votre adversaire seul, en message privé.

### Revanche ou nouveau duel

- **Revanche** (`Revanche`) : un `.bat` déjà envoyé reste valide, tant que votre adresse n'a pas changé.
  L'ancien lien ne marche plus, mais votre invité peut simplement relancer le même `.bat`.
- **Nouveau duel** (`Nouveau duel`) : un nouveau duel peut périmer un `.bat` déjà envoyé, car un nouveau
  mot de passe est tiré. Dans ce cas, il faut renvoyer le nouveau `.bat`. Saisir l'ancien mot de passe
  dans le formulaire du duel garde l'ancien `.bat` valide.

## Essayer depuis les sources (sans Project Zomboid)

Le mode `mock` rejoue tout le parcours — préflight, serveurs, contrat, décompte, score — sans lancer
un seul processus.

```bash
git clone https://github.com/chpomob/pz-duel.git && cd pz-duel
python3 -m service.main            # ouvre http://127.0.0.1:8765/
```

Python **≥ 3.12**, aucune dépendance : le service est en bibliothèque standard uniquement.

Parcours : `Configurer un duel` → preset **Démo 90 s** → `Launch duel` → dans le lobby,
`Simuler l'adversaire`. Tout ce qui est simulé est annoncé comme tel.

## Mode réel depuis les sources

Il faut Project Zomboid **42.22 ou plus récent** (le lanceur serveur est livré avec le jeu ; le mod
déclare `versionMin=42.21.0`), 6 ports libres
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

Prouvé, sur cette installation (build **42.22.2**, la stable du 09/11/2026 ; la même suite passait déjà
sur 42.21.0 avant cette mise à jour), par `pytest -m pz_integration` :

- deux serveurs réels démarrent, le mod `duelcore` se charge, `caps` est publié et le contrat est
  vérifié intégralement (`verify(caps) == []`) ;
- arrêt par **RCON**, ports libérés, fichiers `.ini` en `0600`, **aucun secret** dans les fichiers de
  match, aucun processus résiduel ;
- le lecteur de commandes lit une ligne au plus une fois, y compris ligne coupée ou `\n` manquant ;
- environ 2 000 tests hors jeu couvrent le protocole, la table de relations, le score, le journal, le rejeu,
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
python3 -m pytest -q                          # sans le jeu (environ 2 000 tests, ~2 min 30)
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
packaging/        binaire Windows : point d'entrée et spec PyInstaller, test de fumée
tests/            environ 2 000 tests ; doubles de serveur, de mod et interpréteur Lua réduit
docs/campaigns/   spec (77 critères), plan (24 étapes), 4 revues de branche, 15 étapes de correction
docs/design/      conception en 10 documents, anglais et français (voir l'index)
```

## Limites connues

Le détail daté est dans [`docs/campaigns/phase1/limites-connues.md`](docs/campaigns/phase1/limites-connues.md),
et pour Windows dans [`docs/campaigns/phase2/limites-connues.fr.md`](docs/campaigns/phase2/limites-connues.fr.md).
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
