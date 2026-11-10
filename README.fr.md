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
7. **Partez par `Quit`** dans la page. Si vous fermez simplement l'onglet du navigateur, PZ Duel se
   ferme seul quand plus personne ne l'a regardé depuis `idle_quit_s` (120 s par défaut, réglé dans
   `service/runtime.json` ; `0` le désactive) : fermer l'onglet suffit à déclencher ce compte à
   rebours. Cela n'arrive **jamais** pendant un duel (décompte ou match en cours), même sans aucun
   onglet ouvert — c'est le jeu que vous regardez, pas la page ; le compte repart en entier à la fin
   du duel. Le lobby et un écran d'erreur ne le retiennent pas. Une page ouverte garde PZ Duel en
   marche, même en arrière-plan, sauf si le navigateur met l'onglet en veille : alors, dans les
   30 dernières secondes, la page prévient que PZ Duel va se fermer faute de spectateur, et un clic
   n'importe où dans la page suffit à rester. La fermeture est celle de `Quit` : serveurs arrêtés
   et, si des ports ont été ouverts, redirections retirées de la box.

Si `Launch duel` finit sur une erreur, utilisez `Try again`. Si l'écran vous le demande, quittez et
rouvrez PZ Duel.

**Où sont les fichiers.** `pz-duel.exe` est un fichier unique : à chaque lancement, il déballe ce
qu'il transporte, mod compris, dans un dossier temporaire `%LOCALAPPDATA%\Temp\_MEI…\`, effacé à une
sortie normale (un arrêt forcé peut l'y laisser). Cette copie n'est pas le mod qu'utilise le jeu.
`Install mod` la copie dans `%USERPROFILE%\Zomboid\mods\duelcore` : c'est le mod installé, le dossier
que le jeu charge. `%LOCALAPPDATA%\PZDuel` ne contient que le journal de PZ Duel (`pz-duel.log`), le
dossier `matches` et le verrou d'instance unique (avec le port où il écoute).

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

#### Ouvrir les ports depuis le lobby (UPnP)

Si votre box gère l'UPnP, le lobby d'un duel réel peut ouvrir ces ports pour vous. Il ne le fait
jamais de lui-même : seulement quand l'hôte clique sur le bouton placé sous *Public address*. Avant le
clic, le bouton nomme ce qu'il va ouvrir et pour combien de temps, par exemple
`Open ports 17271/UDP, 17272/UDP, 17280/TCP for 2 h`. Il demande exactement trois redirections et rien
d'autre : 17271 et 17272 en UDP, pour jouer, et le port de partage 17280 en TCP, pour le lien. Ni plage
de ports, ni DMZ, ni autre port.

- **Le bail et son expiration.** Chaque redirection est ouverte pour un bail borné (2 h par défaut,
  jamais plus). Après une ouverture réussie, le lobby affiche `Ports … are open until` suivi de l'heure
  d'expiration. Un duel dure au plus une heure : si vous êtes resté longtemps au lobby depuis le clic,
  cliquez de nouveau avant de lancer. De même à une revanche : le lobby suivant n'ouvre rien tout seul.
  Un nouveau clic sur la même box repose ses trois redirections, il n'en ajoute pas. Si, entre deux
  clics, votre PC a changé de réseau et donc de box, le second clic ouvre aussi trois redirections
  sur cette box-là : celles de la première ne sont pas remplacées, elles y restent jusqu'à ce que
  PZ Duel se ferme proprement et les retire, ou jusqu'au terme de leur bail.
- **Pas de renouvellement automatique.** PZ Duel ne prolonge jamais le bail sans vous. À son terme, la
  box retire les redirections.
- **Fermeture.** Les redirections sont supprimées quand PZ Duel se ferme proprement, par `Quit` ou
  seul après `idle_quit_s` sans personne qui regarde. Il n'y
  a pas de bouton séparé pour les fermer. Si PZ Duel est tué ou plante, seul le bail les borne : elles
  restent ouvertes sur la box jusqu'à son terme.
- **L'adresse publique.** La box donne aussi son adresse publique. Quand c'en est vraiment une, le lobby
  affiche `Public address read from the router:` et la place dans le champ *Public address*, sauf si
  vous y avez déjà saisi quelque chose. Ce n'est qu'une proposition : cliquez vous-même sur
  `Use this address`, comme ci-dessus. L'adresse reste sur votre PC : elle n'entre ni dans le journal
  du match ni dans les logs.
- **Une adresse non publique.** Si la box donne une adresse injoignable depuis Internet (une adresse
  privée, ou une de l'espace partagé `100.64.0.0/10`), le lobby le dit et ne propose rien. Votre box
  est elle-même derrière un autre routeur (double NAT), ou votre opérateur partage une adresse publique
  entre plusieurs clients (CGNAT). Les ports ont pu être ouverts sur votre box, mais un invité hors de
  votre réseau n'entrera sans doute pas par là : demandez à votre opérateur, ou ouvrez les ports sur
  le routeur en amont.
- **En cas d'échec.** Si aucune box compatible ne répond, ou si la box refuse, le lobby affiche
  `Ports could not be opened automatically` avec la raison, puis la consigne manuelle : `Open these
  ports by hand in your router's settings and forward them to this computer`, suivie des trois ports
  et protocoles. Faites-le comme décrit ci-dessus, puis saisissez votre adresse publique. Un échec ne
  bloque jamais le duel : vous pouvez le lancer quand même.

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
- **Ouverture des ports (UPnP) et arrêt** : si vous quittez pendant qu'une ouverture est en cours,
  la sortie attend au plus la fenêtre de découverte plus un délai de requête vers la box (5 s par
  défaut) que cette ouverture finisse et enregistre ce qu'elle a posé, puis continue. La suppression
  entière prend au plus la fenêtre de découverte plus `(3 redirections + 2)` délais de requête — 17 s
  avec les réglages livrés — donc une box devenue muette ne retient pas `Quit`. Toutes les
  redirections enregistrées pendant la session sont supprimées, y compris celles posées sur une
  seconde box : la sortie n'a plus besoin de redécouvrir une box, elle garde l'adresse de chacune de
  celles qu'elle a utilisées. Une redirection que la box refuse de supprimer — ou dont la propriété
  est incertaine, parce que le bail a pu expirer — est tout de même tentée, et un refus est accepté :
  elle reste alors jusqu'au terme de son bail. PZ Duel ne suppose jamais que la box respecte ce bail.
- **Quand certains ports n'ont pas pu être refermés** : après un échec partiel, le lobby nomme les
  couples qui peuvent être encore ouverts (`Ports 17271/UDP may still be open on your router: their
  removal was not confirmed`) et dit le reste : PZ Duel réessaie à l'arrêt, sinon ils se ferment au
  terme de leur bail, *si votre box le respecte*. Vérifiez la page de redirection de ports de votre
  box en cas de doute.

## Merci

**Naylo** est la première personne à avoir lancé ce projet sur un vrai poste Windows avec le jeu
installé — et la première à l'avoir cassé utilement : sa carte Setup réclamait un `StartServer64.bat`
qu'une installation 42.22 saine ne livre pas. C'est ce rapport qui a fait passer le service au JRE
embarqué dans le jeu pour lancer les serveurs, au lieu d'exiger ce script. Il est contributeur de ce
projet, pas une note de bas de page.

## Licence

WTFPL v2 — voir [`LICENSE`](LICENSE). Les crédits des mods et auteurs dont ce projet s'inspire sont
dans [`CREDITS.md`](CREDITS.md), à lire avant toute reprise de code.
