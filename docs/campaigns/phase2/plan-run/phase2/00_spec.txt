---
name: "phase2-windows-packaging-zero-cli"
version: "1.0"
author: "adversarial-spec"
status: "draft"
tags: [adversarial, spec, pz-duel, phase2, windows, packaging]
targets:
  - file: service/preflight.py
    description: "Détection de PZ selon la plateforme (disposition Windows, dossier Steam par défaut, Steam enregistré, bibliothèques déclarées), mesure de RAM hors /proc/meminfo, contrôle de ports fiable sous Windows, consigne d'installation du mod adaptée au binaire"
  - file: service/servers.py
    description: "Commande de lancement du serveur par plateforme ; arrêt gradué, sonde de vie et mise à mort de l'arbre de processus sans killpg ni SIGKILL ; écriture du .ini sans fchmod sous Windows"
  - file: service/engine.py
    description: "Lanceur client par plateforme ; dossier des matchs résolu hors du binaire ; adresse d'invitation modifiable ; ouverture et fermeture du partage avec le lobby ; aucune invitation produite sur une adresse inutilisable (boucle locale, non spécifiée, lien local) ; portée de l'invitation exposée à l'UI"
  - file: service/transport.py
    description: "Écriture binaire exacte de `in` sous Windows et remplacement de `session` tolérant un partage de fichier transitoire"
  - file: service/proofs.py
    description: "Remplacement atomique du fichier de preuves tolérant le même partage transitoire"
  - file: service/main.py
    description: "Mode réel par défaut dans le binaire, ouverture du navigateur au premier lancement avec repli consigné, signaux disponibles par plateforme, démarrage sans console valide, instance unique du binaire, sortie demandée par l'UI"
  - file: service/api.py
    description: "Routes d'état de premier lancement, d'installation du mod, de choix du dossier du jeu, de sortie, de téléchargement du lanceur invité ; UI résolue dans le binaire"
  - file: service/rules.py
    description: "runtime.json et effects.json résolus dans le binaire"
  - file: service/runtime.json
    description: "Port du partage d'invitation et délais de la tolérance de remplacement, aucune valeur dans le code"
  - file: service/paths.py
    description: "Nouveau : seul endroit qui distingue sources et binaire figé, pour les ressources embarquées et le dossier de données persistant"
  - file: service/platforms.py
    description: "Nouveau : description par plateforme (scripts de lancement, disposition du jeu, emplacements Steam) et primitives Windows, importable sous Linux et paramétrable par les tests"
  - file: service/guest_launcher.py
    description: "Nouveau : génération du .bat invité (mod embarqué, recherche de PZ, connexion), fonction pure de ses entrées"
  - file: service/share.py
    description: "Nouveau : écouteur réseau de l'invitation, limité au téléchargement du .bat pendant le lobby"
  - file: ui/app.js
    description: "Écran de premier lancement, bouton Quit, invitation par lien, QR et enregistrement du .bat, champ d'adresse publique, portée de l'invitation et ports à ouvrir, avertissement sur les lanceurs périmés par un duel neuf"
  - file: ui/index.html
    description: "Points d'ancrage des écrans ajoutés"
  - file: ui/style.css
    description: "Mise en forme des écrans ajoutés"
  - file: packaging/pz_duel_entry.py
    description: "Nouveau : point d'entrée PyInstaller, `service/main.py` ne pouvant pas l'être à cause de ses imports relatifs"
  - file: packaging/pz-duel.spec
    description: "Nouveau : construction --onefile embarquant ui/, mods/duelcore/, service/presets/, runtime.json et effects.json"
  - file: packaging/requirements-build.txt
    description: "Nouveau : version épinglée de PyInstaller, dépendance de build seulement"
  - file: packaging/smoke_test.py
    description: "Nouveau : test de fumée du binaire produit, stdlib seule, lancé par la CI"
  - file: .github/workflows/tests.yml
    description: "Ajout de la suite sur windows-latest et du test de fumée du binaire sur chaque pull request ; historique complet au checkout (`fetch-depth: 0`) pour la comparaison d'AC53"
  - file: .github/workflows/release.yml
    description: "Nouveau : sur tag v*, construction sur windows-latest, test de fumée, puis pièce jointe à la release GitHub"
  - file: tests/doubles/fake_server.py
    description: "Double du serveur aussi posé en StartServer64.bat, arbre de processus compris"
  - file: tests/test_servers.py
    description: "Paramétrage par plateforme des tests d'arrêt, de signaux et de droits ; ajout des équivalents Windows"
  - file: tests/test_secrets.py
    description: "Assertion 0600 limitée à POSIX, équivalent Windows ajouté, secrets du .bat et du partage balayés"
  - file: tests/test_preflight.py
    description: "Paramétrage des doubles d'installation par plateforme"
  - file: tests/test_api_security.py
    description: "Classement des nouveaux champs de payload, balayage des nouvelles routes et de l'écouteur de partage"
  - file: tests/test_snapshots.py
    description: "`os.killpg`, `signal.SIGKILL` et scripts `#!/bin/sh` paramétrés par plateforme ; équivalent Windows ou skip nommant le test qui le remplace (AC62)"
  - file: tests/test_contract.py
    description: "`crash()` paramétré par plateforme, sans `os.killpg` ni `signal.SIGKILL` sous Windows (AC62)"
  - file: tests/test_modes.py
    description: "Doubles du client et du serveur (`#!/bin/sh` exécutés par `launch_client`) paramétrés par plateforme (AC62)"
  - file: tests/test_campaign_gate.py
    description: "Faux interpréteur `#!/bin/sh` paramétré par plateforme (AC62)"
  - file: tests/test_platform_windows.py
    description: "Nouveau : branches Windows exécutées sous Linux par injection de plateforme (R1, R2, R3) ; mesure des collections d'AC6 et d'AC53 et des raisons de skip d'AC62"
  - file: tests/test_frozen.py
    description: "Nouveau : résolution des ressources et des données en mode figé simulé, démarrage sans console (R4)"
  - file: tests/test_setup_flow.py
    description: "Nouveau : parcours de premier lancement par l'API (R5)"
  - file: tests/test_guest_launcher.py
    description: "Nouveau : contenu du .bat sous Linux, exécution réelle sous Windows uniquement (R6)"
  - file: tests/test_share.py
    description: "Nouveau : écouteur de partage, adresse d'invitation et validité des lanceurs après revanche ou `start` (R7)"
  - file: tests/test_ci_workflows.py
    description: "Nouveau : lecture statique des workflows et du fichier de build (R8)"
  - file: README.md
    description: "Section de démarrage pour joueurs Windows : téléchargement, SmartScreen, pare-feu, redirection de port, invité, lanceurs périmés par un duel neuf"
  - file: README.fr.md
    description: "Même section en français"
  - file: docs/campaigns/phase2/limites-connues.md
    description: "Nouveau : ce qui est prouvé en CI et ce qui ne l'est pas sur le vrai jeu Windows"
  - file: docs/campaigns/phase2/limites-connues.fr.md
    description: "Nouveau : version française"
---

# Phase 2 — Windows, packaging, zéro ligne de commande

## Problem

La phase 1 est prouvée sur le jeu réel pour ce qui ne demande pas de joueur, mais elle ne sert qu'à
un développeur sous Linux. Il faut un terminal, Python et le dépôt ; le service ne connaît que
`start-server.sh` et `projectzomboid.sh`. L'invitation est un lien `pzduel://` qu'aucun programme ne
sait ouvrir : le QR actuel ne mène nulle part.

La phase doit permettre à un joueur qui sait installer un jeu Steam, sans ouvrir de terminal, de
télécharger un `.exe`, de le double-cliquer, de voir PZ détecté, le mod installé, la RAM et les ports
vérifiés, de lancer le duel et d'inviter un ami. Cet ami double-clique un `.bat` reçu de l'hôte, qui
installe le mod et lance son client vers le bon serveur, avec le bon mot de passe. Linux reste au
moins aussi bon qu'aujourd'hui.

### Obstacles constatés dans le code, au-delà du brief

Le brief cite `SCRIPT`, `CLIENT_SCRIPT`, `UI_DIR` et `MOD_SRC`. La lecture du code en ajoute d'autres,
qui casseraient Windows ou le binaire sans bruit :

- `service/servers.py:76` appelle `os.fchmod`, `:379` et `:398` `os.killpg`, `:341` `signal.SIGKILL` :
  aucun des trois n'existe sous Windows.
- `service/transport.py:239` ouvre `in` par `os.open` sans mode binaire : sous Windows, chaque `\n` du
  protocole deviendrait `\r\n`, et `written != len(line)` lèverait une écriture partielle.
- `os.replace` sur `session` (`service/transport.py:300`), les `.ini` et `proofs` échoue sous Windows
  si un autre processus tient la cible ouverte, ce qui n'arrive jamais sous Linux. Or le mod relit
  `session`.
- `MEMINFO_PATH` (`service/preflight.py:25`) n'existe pas sous Windows : le préflight échouerait
  toujours sur la RAM.
- `_bind` (`service/preflight.py:114`) pose `SO_REUSEADDR` sur TCP. Sous Windows, cette option permet
  de lier un port déjà occupé : un port RCON pris serait déclaré libre.
- `MATCHES_DIR` (`service/engine.py:86`) dérive de `__file__`. Dans un `--onefile`, journaux et preuves
  de hook seraient écrits dans le dossier d'extraction, effacé à la sortie. `PRESETS_DIR`,
  `RUNTIME_PATH` et `EFFECTS_PATH` dérivent aussi de `__file__`.
- `service/main.py:78` lance le mode `mock` par défaut, et `INSTALL_HINT` renvoie à `python3 -m ...`,
  ce qu'un joueur du binaire ne peut pas exécuter.
- `local_ip` rend `127.0.0.1` sans route vers Internet : une invitation serait alors inutilisable.
  Elle rend de plus l'adresse locale, jamais celle à donner à un invité passant par une redirection
  de port.
- Rien ne permet aujourd'hui de quitter le service sans terminal, ni n'empêche un second double-clic
  de démarrer un second service qui se battrait pour les mêmes ports de jeu.

### Hypothèses et décisions de cette spec

- **H1 — Le lien et le QR mènent au `.bat`.** Un QR ne peut pas contenir le mod, et `pzduel://`
  supposerait un programme déjà installé chez l'invité, ce que le `.bat` sert justement à éviter. Le
  lien devient donc une URL HTTP servie par l'hôte. C'est le premier écouteur hors boucle locale du
  projet : R7 le limite strictement, et `make_server` reste sur `127.0.0.1`. L'enregistrement du `.bat`
  depuis l'UI, à envoyer par messagerie, reste possible dès qu'une adresse utilisable existe : c'est la
  voie normale pour un invité hors du réseau local. Seule exception, couverte par R7 : sans adresse
  utilisable, le service ne produit ni lien ni `.bat` et demande la saisie.
- **H2 — L'invité est sous Windows.** Le `.bat` est la décision de l'opérateur. Un invité Linux suit
  la procédure manuelle actuelle. Un hôte Linux produit le même `.bat` qu'un hôte Windows.
- **H3 — Sources et binaire divergent sur trois points seulement** : le mode par défaut, le dossier de
  données, l'instance unique. Lancé depuis les sources, le service garde `mock` par défaut, `matches/`
  à la racine du dépôt et les messages actuels.
- **H4 — Ce que `StartServer64.bat` fait de ses arguments n'a pas pu être lu.** L'installation Linux
  ne contient pas ce fichier. Le brief établit qu'il lance le serveur, pas qu'il transmet tous ses
  arguments, qu'il fixe `-Dzomboid.steam=0` comme `start-server.sh`, ni qu'il se termine sans `PAUSE`.
  R2 ne suppose donc pas que le script rend la main, et R9 impose de déclarer ces trois points non
  prouvés. La CI n'a pas le jeu : elle prouve notre code contre un double, pas contre PZ.
- **H5 — Le pare-feu Windows n'est pas contourné.** L'ouvrir exigerait une élévation : R9 documente
  l'invite de Windows, et l'outil ne la pilote pas.
- **H6 — Les opérations de premier lancement ne sont pas des actions de match.** `tests/test_modes.py`
  fixe `ACTIONS` à cinq noms, et cet ensemble ne change pas.
- **H7 — La joignabilité réelle d'une adresse n'est pas vérifiable par le service.** Depuis la machine
  de l'hôte, sans relais ni service tiers (hors périmètre du brief), rien ne prouve qu'un invité
  distant atteindra l'adresse. Le service contrôle donc seulement qu'une adresse est *utilisable* : une
  adresse IPv4 ou un nom d'hôte qui ne désigne ni la boucle locale (`127.0.0.0/8`, `localhost`), ni
  l'adresse non spécifiée (`0.0.0.0`), ni le lien local (`169.254.0.0/16`). Une adresse détectée n'est
  présumée valable que sur le réseau local de l'hôte. Une adresse saisie est la déclaration de l'hôte :
  elle n'est pas testée. Dans les deux cas, l'UI dit ce qui reste à la charge de l'hôte, et R9 déclare
  la joignabilité non prouvée.

Aucune exigence ne contredit une assertion existante sous Linux : la section de liste d'échecs
autorisés est donc absente. `tests/test_api_security.py` demande de classer tout nouveau champ de
payload : c'est une extension prévue par ce test, pas un assouplissement.

### Portée de `targets`

`targets` liste les fichiers que la phase modifie ou crée, et eux seuls. Deux absences sont voulues :

- **Fichiers qui ne doivent pas changer.** `service/protocol.py`, `service/journal.py`,
  `service/presets/`, `service/effects.json`, `mods/duelcore/`, `tests/fixtures/` et
  `tests/test_stdlib_only.py` sont lus ou cités par AC17, AC18, AC31, AC32, AC48 et AC55. Ces critères
  exigent qu'ils restent identiques : les ajouter à `targets` les contredirait.
- **Collections mesurées sur l'arbre.** « Tous les modules de `service/` » (AC6) et « chaque fonction
  de test » (AC53) sont constatés à l'exécution, pas listés. `tests/test_platform_windows.py` mesure
  AC6 et AC53, `tests/test_stdlib_only.py` parcourt `service/` pour AC48. Les modules de ces
  collections absents de `targets`, dont `service/rcon.py`, `service/snapshots.py`, `service/state.py`,
  `service/replay.py` et `service/verdict.py`, ne sont pas à modifier : aucune dépendance POSIX n'y a
  été relevée. `tests/test_integration_pz.py` lit `/proc`, mais seulement dans les tests marqués
  `pz_integration`, qui ne tournent pas en CI : il n'est pas à modifier non plus.

## Requirements

Huit exigences non triviales (R1 à R8). R9 (documentation) et R10 (non-régression) n'ajoutent aucun
comportement.

- R1: **Préflight sous Windows.** Sous Windows, le service trouve PZ dans cet ordre : `--pz-dir`, puis
  `PZ_INSTALL_DIR`, puis le dossier Steam par défaut, l'installation de Steam enregistrée dans le
  système et toutes les bibliothèques qu'elle déclare, y compris sur un autre disque. Un choix explicite
  invalide répond « absent » sans repli (D19). Le dossier rendu contient `StartServer64.bat` et
  `ProjectZomboid64.exe`. La RAM disponible est mesurée par le système, avec le seuil et le refus
  d'aujourd'hui. Un port occupé ou réservé par Windows n'est jamais déclaré libre. Chaque branche
  Windows s'exécute sous Linux par injection de la plateforme, et aucun module réservé à Windows n'est
  importé au chargement de `service/` sous Linux.
- R2: **Processus sous Windows.** Le serveur démarre par `StartServer64.bat` et le client par
  `ProjectZomboid64.exe`, chemins à espaces et à parenthèses compris, sans fenêtre de console
  supplémentaire pour les serveurs. L'arrêt garde ses paliers et leur vocabulaire (`rcon`, `stdin`,
  `sigterm`, `sigkill`) : le dernier palier met fin à tout l'arbre, script et `java` compris, même si le
  script attend une touche. Quelle que soit la façon dont le service se termine, y compris fermeture de
  sa console ou fin forcée, aucun processus de serveur ne survit. Le mot de passe admin et le mot de
  passe client ne paraissent dans aucun message.
- R3: **Fichiers sous Windows.** Les octets écrits dans le transport sont exactement ceux du protocole,
  sans traduction de fin de ligne. Un remplacement atomique (`session`, `.ini`, preuves) tolère pendant
  un délai borné une cible tenue ouverte par un autre processus, puis échoue explicitement sans perdre
  l'ancien contenu ni laisser de fichier temporaire. L'absence de droits POSIX n'est ni une erreur ni
  un défaut signalé : le `.ini` repose sous Windows sur les droits du profil utilisateur. Le format du
  protocole et du journal ne change pas.
- R4: **Binaire figé.** Dans le `--onefile`, l'UI, le mod, les presets, `runtime.json` et `effects.json`
  sont trouvés dans le dossier d'extraction, et les matchs et preuves vont dans un dossier de données
  persistant de l'utilisateur (`%LOCALAPPDATA%\PZDuel` sous Windows) qui survit à la sortie. Le binaire
  démarre en mode réel par défaut, reste capable de `--mode mock`, fonctionne sans console ni flux
  standard, et consigne ses erreurs de démarrage dans un fichier de ce dossier. Au premier lancement, il
  ouvre une fois le navigateur par défaut sur l'interface, sauf avec `--no-browser` ; si cette ouverture
  échoue, le service reste actif et consigne l'adresse de l'interface dans ce même fichier. Un second
  lancement ouvre l'interface du service déjà actif et se termine sans démarrer de second moteur.
- R5: **Premier lancement sans terminal.** L'UI montre avant tout duel l'état de PZ, du mod, de la RAM
  et des ports, sans rien écrire. Elle permet d'installer ou de mettre à jour le mod, d'indiquer le
  dossier du jeu quand la détection échoue, de relancer les contrôles, et n'active « Launch duel » que si
  ceux-ci sont bons. Chaque défaut affiche une action faisable sans terminal. Un bouton « Quit » arrête
  le match, les serveurs et le service. Ces opérations passent par les gardes `Host` et `Origin` de
  l'API, ne s'ajoutent pas à `ACTIONS` et sont refusées pendant un duel quand elles le perturberaient.
  Les textes ajoutés sont en anglais.
- R6: **Lanceur invité.** Une fois le lobby réel ouvert, le service produit un unique `.bat` autonome qui
  ne demande à l'invité que Windows 10 ou 11 et PZ installé. Il retrouve PZ comme R1 (dossier par
  défaut, Steam enregistré, bibliothèques), puis demande le dossier si la recherche échoue. Il remplace
  `%USERPROFILE%\Zomboid\mods\duelcore` par le mod exact de l'hôte sans toucher aux autres mods, et
  lance `ProjectZomboid64.exe -nosteam +connect <adresse>:<port B> +password <mot de passe>`. Toute
  valeur est soit transmise à l'identique, soit refusée à la génération ; aucune ne peut s'exécuter comme
  commande. Un échec laisse le mod précédent en place, ne lance pas le client, dit la cause en anglais et
  garde la fenêtre ouverte. Le fichier contient un secret : il n'est jamais écrit dans `matches/`, dans
  un journal ou dans un log, et n'existe que dans la réponse qui le livre.
- R7: **Invitation.** Le lien et le QR désignent le même `.bat`, à une adresse HTTP de l'hôte portant un
  jeton aléatoire d'au moins 128 bits, propre à chaque lobby. L'écouteur de partage n'existe que pendant
  le lobby, ne répond à rien d'autre que ce téléchargement, ne journalise pas le jeton et n'expose ni
  l'API ni l'UI. L'UI propose aussi d'enregistrer le `.bat` et permet de saisir l'adresse publique de
  l'hôte, qui remplace l'adresse détectée dans le lien, le QR et le `.bat`. L'adresse d'invitation est
  l'adresse saisie, sinon l'adresse détectée ; elle doit être utilisable au sens de H7. Sans adresse
  utilisable, le service ne produit ni lien ni `.bat`, garde le lobby ouvert et demande la saisie. Il
  ne teste pas la joignabilité de l'adresse. L'UI indique la portée de l'invitation : réseau local
  pour une adresse détectée, avec la consigne de saisir l'adresse publique pour un invité distant ; non
  vérifiée pour une adresse saisie, avec les ports que l'hôte doit rendre accessibles (ports UDP de jeu
  du serveur B pour jouer, port TCP de partage pour le lien). Si l'écouteur ne peut pas démarrer, le
  lobby reste ouvert et l'enregistrement du `.bat` reste offert. `pzduel://` disparaît de l'UI. Un
  `.bat` déjà envoyé reste valide pour une revanche, qui réutilise la configuration et donc le mot de
  passe ; un `start` neuf peut tirer un autre mot de passe et le périme alors, ce que l'UI signale.
- R8: **Vérification et livraison.** Chaque pull request exécute la suite sur `windows-latest` en plus de
  Linux, construit le binaire et exécute son test de fumée. Un tag `v*` construit sur `windows-latest`,
  refait le test de fumée et, seulement s'il passe, attache le `.exe` et son empreinte SHA-256 à la
  release GitHub. Le binaire n'est pas signé. PyInstaller reste une dépendance de build épinglée : rien
  dans `service/` ne l'importe, et `service/` reste stdlib, en syntaxe Python 3.11.
- R9: **Documentation.** `README.md` et `README.fr.md` expliquent le parcours du joueur et de l'invité :
  téléchargement, avertissement SmartScreen sur le `.exe` et sur le `.bat`, invite du pare-feu Windows,
  ports à rediriger pour un invité distant, sensibilité du `.bat`, péremption des lanceurs déjà envoyés
  par un duel neuf. `limites-connues` sépare ce que la CI
  prouve de ce qui n'est pas prouvé sur le vrai jeu Windows.
- R10: **Non-régression.** Sous Linux, depuis les sources, le comportement et la suite restent ceux de la
  phase 1. Aucun test n'est supprimé : les tests liés à `chmod 0600`, à SIGTERM ou aux chemins POSIX sont
  paramétrés par plateforme et gardent un équivalent Windows. Les règles du duel, le protocole, le format
  de journal et le mod ne changent pas.

## Acceptance criteria

### Préflight (R1)

- AC1 (R1): Sous Linux, plateforme Windows injectée, une arborescence factice contenant `StartServer64.bat` et `ProjectZomboid64.exe` au dossier Steam par défaut donne `présent` et ce dossier exact ; la même arborescence sans `ProjectZomboid64.exe` donne `absent`, avec un message qui nomme le fichier manquant.
- AC2 (R1): Avec un `libraryfolders.vdf` factice déclarant 3 bibliothèques dont seule la troisième contient le jeu, et un dossier par défaut vide, la détection rend le dossier de la troisième ; avec un fichier vide, tronqué ou absent, ou un registre illisible, elle rend `absent` sans lever d'exception.
- AC3 (R1): Avec `--pz-dir` ou `PZ_INSTALL_DIR` invalide et une installation valide au dossier Steam par défaut, la détection rend `absent` et ne rend pas le dossier Steam ; `--pz-dir` valide l'emporte sur `PZ_INSTALL_DIR` valide.
- AC4 (R1): Avec une mesure de RAM injectée et un seuil de 6144 Mo, 6143 Mo produit exactement 1 défaut de RAM, 6144 Mo n'en produit aucun, et une mesure en échec produit 1 défaut avec `mem_available_mb` à `None`. Sur `windows-latest`, la mesure réelle est un entier strictement positif.
- AC5 (R1): Sur `windows-latest`, un port TCP tenu par une socket à l'écoute, avec ou sans `SO_REUSEADDR` chez son détenteur, et un port UDP lié produisent chacun 1 défaut qui nomme le port et son protocole, tandis qu'un port libre n'en produit aucun. Une erreur système 10013 injectée donne un défaut « inutilisable ».
- AC6 (R1): Un test de `tests/test_platform_windows.py` lancé sous Linux énumère à l'exécution les modules de `service/`, les importe tous dans un sous-processus et vérifie que `winreg`, `msvcrt` et `_winapi` sont absents de `sys.modules`.

### Processus (R2)

- AC7 (R2): Sous Linux, plateforme Windows injectée, avec un `runtime.json` de test aux ports et délais modifiés et `gen_secret` injecté, la commande serveur commence par `<dossier>\StartServer64.bat` et porte `-servername`, `-port`, `-udpport` et `-adminpassword`, soit 8 arguments, avec les valeurs qu'utilise le moteur : `server_name(side)` pour `-servername`, les ports `game` et `udp` du camp dans ce `runtime.json` pour `-port` et `-udpport`, le secret admin tiré par `Engine._open_match()` pour ce match pour `-adminpassword`. Les délais ne sont pas des arguments : `stop_timeout_s` et `kill_grace_s` appliqués à ce serveur sont ceux de ce `runtime.json`. Après un lancement réussi, ce secret ne figure ni dans un message rendu, ni dans l'état, ni dans le log du service. La commande client est `ProjectZomboid64.exe` du même dossier, suivie de `-nosteam +connect <adresse> +password <valeur>`, soit 5 arguments.
- AC8 (R2): Sur `windows-latest`, avec un double dans un dossier nommé `Program Files (x86)\Project Zomboid`, le serveur démarre, reçoit ses arguments à l'identique (comparaison sur le fichier d'arguments du double) et atteint l'état prêt par ses marqueurs de sortie.
- AC9 (R2): Sur `windows-latest`, un double dont le script finit par `PAUSE` et dont l'enfant ignore la demande d'arrêt est arrêté au palier `sigkill` : `stop_stage` vaut `sigkill`, `running` vaut faux et 0 processus de son arbre ne subsiste à l'échéance `kill_grace_s`.
- AC10 (R2): Sur `windows-latest`, un service lancé avec un double en cours d'exécution puis tué de force (`taskkill /F` sur le service seul) ne laisse aucun processus du double au bout de 5 s ; la même mesure vaut pour la fermeture de sa console.
- AC11 (R2): Sur `windows-latest`, un arrêt demandé par la sortie normale passe d'abord par RCON quand le double y répond : `stop_stage` vaut `rcon`.
- AC12 (R2): Dans chaque échec de lancement injecté (fichier absent, sortie immédiate), le mot de passe client et le mot de passe admin ne figurent ni dans le message rendu, ni dans l'état, ni dans le log du service.

### Fichiers (R3)

- AC13 (R3): Sur `windows-latest`, écrire une commande de N octets dans `in` donne un fichier de exactement N octets, sans aucun octet `0x0D`, et aucune exception d'écriture partielle.
- AC14 (R3): Sur `windows-latest`, un autre processus tenant `session` ouvert pendant 500 ms, sans partage de suppression, ne fait pas échouer `write_session` ; le fichier contient ensuite le nouveau contenu. Le test est répété pour un `.ini` et pour les preuves de hook.
- AC15 (R3): Sur `windows-latest`, une cible tenue au-delà du délai configuré dans `runtime.json` produit une erreur explicite, l'ancien contenu reste intact et 0 fichier temporaire reste dans le dossier. Sous Linux, plateforme et échec injectés, le nombre de tentatives est borné et le délai total ne dépasse pas la valeur configurée de plus de 10 %.
- AC16 (R3): Sur `windows-latest`, `write_ini` réussit et `ini_failure` reste `None` une fois le serveur prêt ; sous Linux, le `.ini` reste en `0600`.
- AC17 (R3): Le rejeu des journaux de `tests/fixtures` donne le même résultat sous Linux et sous `windows-latest`, et aucun fichier de `service/protocol.py` ou de `mods/duelcore` n'apparaît dans le diff de la phase.

### Binaire (R4)

- AC18 (R4): En mode figé simulé (`sys.frozen` vrai, dossier d'extraction injecté), l'UI, le mod, les 4 presets, `runtime.json` et `effects.json` sont lus depuis ce dossier, et 0 chemin d'écriture du service n'est à l'intérieur de celui-ci.
- AC19 (R4): En mode figé simulé, `matches_dir` vaut `<LOCALAPPDATA>\PZDuel\matches` avec plateforme Windows injectée ; sans figement, il reste `<dépôt>/matches`. Un journal écrit puis la suppression du dossier d'extraction laissent le journal lisible.
- AC20 (R4): En mode figé simulé, `main()` sans `--mode` construit l'`Engine` en `real`, avec `--mode mock` en `mock` ; depuis les sources, sans `--mode`, il le construit en `mock`.
- AC21 (R4): `main()` démarre et sert `/api/state` avec `sys.stdout` et `sys.stderr` à `None`, puis avec une sortie cp1252 stricte, sans exception. Une erreur de démarrage injectée laisse 1 ligne au moins dans le fichier de log du dossier de données et un code de sortie non nul.
- AC56 (R4): En mode figé simulé, un premier `main()` sans `--no-browser` appelle le navigateur injecté exactement 1 fois avec `http://127.0.0.1:<port>/`, et un `GET` de cette URL rend 200 ; avec `--no-browser`, il l'appelle 0 fois. Avec un navigateur injecté qui rend faux ou lève une exception, `/api/state` rend toujours 200 et le fichier de log contient cette URL au moins 1 fois.
- AC22 (R4): En mode figé simulé, un second `main()` avec un service actif sur le port par défaut ouvre l'URL de ce service (navigateur injecté, 1 appel), sort avec le code 0 et ne construit aucun `Engine` ; un port occupé par autre chose qu'un service PZ Duel conduit au port suivant, comme aujourd'hui.

### Premier lancement (R5)

- AC23 (R5): Avec PZ absent, mod absent, RAM insuffisante ou port occupé (4 cas injectés), la route d'état rend le défaut correspondant et une action nommée parmi `install_mod`, `choose_folder`, `retry`, et les fichiers de `~/Zomboid` restent identiques octet pour octet avant et après l'appel.
- AC24 (R5): Depuis un état sans mod, la route d'installation pose `mods/duelcore` et le contrôle suivant rend une empreinte égale à `mod_fingerprint(MOD_SRC)` ; un autre dossier de `mods/` reste inchangé.
- AC25 (R5): La saisie d'un dossier valide rend PZ `présent` pour le contrôle suivant et pour le `start` suivant, sans redémarrage ; un dossier invalide est refusé et l'état précédent reste. Le chemin saisi est classé dans `tests/test_api_security.py`.
- AC26 (R5): Un `start` refusé par le préflight rend le même défaut que la route d'état, et un test de l'UI vérifie que le bouton « Launch duel » reste désactivé tant que `ok` est faux.
- AC27 (R5): Les routes d'installation, de choix de dossier et de sortie répondent 403 avec un `Host` ou un `Origin` étranger, et les deux premières répondent 409 hors de la phase `idle`, sans rien écrire.
- AC28 (R5): Une requête de sortie pendant un match réel double fait passer le service par l'arrêt complet : les 2 serveurs doubles sont arrêtés, `main()` rend la main avec le code 0 en moins de `stop_timeout_s` + `kill_grace_s` et le journal du match est fermé.
- AC29 (R5): `ACTIONS` garde ses 5 noms, et aucun message d'échec affiché en binaire ne contient `python`, `--pz-dir`, `--install-mod` ni `chmod`.

### Lanceur invité (R6)

- AC30 (R6): Le `.bat` généré, pour le mod actuel, ne contient que des octets ASCII, des fins de ligne CRLF, aucune ligne de plus de 8000 octets, et pèse moins de 256 Kio. Deux générations aux mêmes entrées sont identiques octet pour octet, sous Linux comme sous `windows-latest`.
- AC31 (R6): Sur `windows-latest`, avec `USERPROFILE` contenant une espace et un `é`, et un double de `ProjectZomboid64.exe` dans un dossier `Program Files (x86)\Steam\steamapps\common\ProjectZomboid`, le `.bat` sort avec le code 0, `mod_fingerprint` du dossier installé égale `mod_fingerprint(MOD_SRC)`, et le double a reçu exactement `-nosteam`, `+connect`, `<adresse>:17271`, `+password`, `<mot de passe>`.
- AC32 (R6): Sur `windows-latest`, un dossier `mods/autremod` préexistant est inchangé après exécution, et une seconde exécution rend la même empreinte.
- AC33 (R6): Pour chacun des mots de passe `100%`, `a&b`, `a^b`, `a!b`, `a"b`, `a b`, `%PATH%`, `a|b>c`, `pässwörd` et `x&echo pwned>pwned.txt`, le générateur refuse la valeur sans la citer, ou bien, sur `windows-latest`, le double reçoit la valeur à l'identique. Dans les deux cas, aucun fichier `pwned.txt` n'existe et le mot de passe généré par défaut (hexadécimal) n'est jamais refusé.
- AC34 (R6): Sur `windows-latest`, sans PZ trouvable et sans saisie (entrée vide), le `.bat` sort avec un code non nul, le double n'est pas lancé et `mods/duelcore` reste tel qu'il était ; avec un dossier saisi valide, il réussit. Le même code non nul et le même mod intact valent pour un payload altéré.
- AC35 (R6): Sur `windows-latest`, PZ situé uniquement dans la deuxième bibliothèque d'un `libraryfolders.vdf` factice sur un autre dossier racine est trouvé sans saisie.
- AC36 (R6): La génération répond 409 en mode `mock` et hors du lobby. Après une génération en mode réel, aucun fichier sous `matches_dir` ni sous le dossier de données ne contient le mot de passe, et la réponse porte `Content-Disposition: attachment` et `Cache-Control: no-store`.

### Invitation (R7)

- AC37 (R7): Le lien exposé par `invite` et le texte du QR sont identiques, commencent par `http://`, et le `GET` de ce lien rend les mêmes octets que l'enregistrement depuis l'UI. `pzduel://` n'apparaît plus dans `ui/`.
- AC38 (R7): Le jeton compte au moins 128 bits (32 caractères hexadécimaux ou équivalent) et 2 lobbys successifs, revanche comprise, ont des jetons différents. Cas vérifié ici : la revanche, sans nouvelle adresse saisie. Après `rematch`, le `+connect` et le `+password` du `.bat` du lobby précédent égalent ceux du `.bat` du nouveau lobby, et ce mot de passe est le `Password` écrit dans le `.ini` du serveur B. Le cas du `start` est celui d'AC59.
- AC39 (R7): Sur l'écouteur de partage, `/`, `/api/state`, `/api/action`, un jeton faux, un jeton juste avec un autre chemin et toute méthode autre que `GET` répondent 404 ou 405, sans corps contenant le jeton, le mot de passe ou une ressource de l'UI. Le port de l'API reste lié à `127.0.0.1`.
- AC40 (R7): L'écouteur refuse la connexion dans les 2 s qui suivent la sortie du lobby, et le port de partage n'est pas lié avant l'ouverture du lobby. Le nombre de connexions simultanées reste plafonné comme sur l'API (32).
- AC41 (R7): Après un téléchargement réussi et un refus, ni les logs du service, ni l'état journalisé, ni les fichiers de match ne contiennent le jeton.
- AC42 (R7): Avec le port de partage occupé, le lobby s'ouvre, `invite` signale le partage indisponible en anglais, et l'enregistrement du `.bat` fonctionne.
- AC43 (R7): Une adresse publique `82.64.1.2` ou `duel.example.net` saisie se retrouve à l'identique dans le lien, dans le QR et dans le `+connect` du `.bat`. Une adresse vide, contenant un espace, `&`, `%`, `/`, `:` ou `"`, de plus de 253 caractères, ou égale à `127.0.0.1`, `127.1.2.3`, `localhost`, `0.0.0.0` ou `169.254.1.1`, est refusée, et l'adresse précédente reste en vigueur.
- AC44 (R7): Avec `local_ip` rendant `127.0.0.1`, `0.0.0.0` ou `169.254.1.1` (3 cas) et sans adresse saisie, aucun lien ni `.bat` n'est produit, le lobby reste ouvert et l'UI demande l'adresse ; après saisie, les deux le sont.
- AC57 (R7): Avec `local_ip` rendant `192.168.1.20` et sans adresse saisie, le lien et le `.bat` portent cette adresse et `invite` annonce en anglais une portée limitée au réseau local. Avec `82.64.1.2` saisie, `invite` annonce une adresse non vérifiée et nomme les ports `17271` et `17272` en UDP et le port de partage de `runtime.json` en TCP. Les 2 valeurs de ports viennent de `runtime.json` : un `runtime.json` de test aux ports modifiés change le message.
- AC58 (R7): Une adresse saisie sans machine derrière elle (`192.0.2.1`) produit le lien et le `.bat` sans attente, et 0 connexion n'est tentée vers cette adresse ni vers un tiers pour la valider (sockets espionnées pendant la saisie et la génération).
- AC59 (R7): Cas vérifié ici : le `start` neuf, qu'AC38 ne couvre pas. Après un lobby réel, un `start` sans mot de passe saisi, `gen_player_password` injecté rendant 2 valeurs distinctes, écrit dans le `.ini` du serveur B un `Password` différent du `+password` du `.bat` précédent : ce `.bat` est périmé. Un `start` qui saisit le mot de passe du lobby précédent garde ce `Password`.
- AC60 (R7): Dans un lobby réel, `invite` annonce en anglais qu'un `.bat` déjà envoyé reste valide pour une revanche et qu'un nouveau duel peut le périmer, et `ui/app.js` affiche ce texte dans l'écran d'invitation. Hors lobby réel, `invite` ne porte pas cette annonce.

### Vérification et livraison (R8)

- AC45 (R8): `tests/test_ci_workflows.py` vérifie que `tests.yml` exécute sur `pull_request` un job `windows-latest` en Python 3.12, 3.13 et 3.14 et un job qui construit puis exécute `packaging/smoke_test.py`, et que les 3 jobs Linux restent.
- AC46 (R8): `packaging/smoke_test.py`, lancé sur le `.exe`, le démarre avec `--no-browser`, vérifie que `/` rend 200 avec l'UI, que `/api/state` rend 200 en mode `real`, que les 4 presets sont listés, que l'empreinte du mod embarqué égale `mod_fingerprint` de `mods/duelcore`, puis demande la sortie et vérifie un code 0 en moins de 30 s. Il échoue sur un binaire construit sans `ui/` (vérifié par une construction volontairement incomplète ou un dossier simulé).
- AC47 (R8): `release.yml` se déclenche sur les tags `v*`, et l'étape qui attache le `.exe` et son fichier `.sha256` dépend du succès du test de fumée. Aucune étape ne signe, ne construit d'installeur et ne touche au Workshop.
- AC48 (R8): `tests/test_stdlib_only.py` passe sans modification, `pyinstaller` n'apparaît pas dans `service/`, et `packaging/requirements-build.txt` porte une version exacte (`==`).
- AC49 (R8): Une exécution complète de `tests.yml` sur `windows-latest` et sur Linux, et une exécution de `release.yml` sur un tag de test, sont vertes, et leurs URL sont consignées dans `docs/campaigns/phase2/limites-connues.md`.

### Documentation (R9)

- AC50 (R9): `README.md` et `README.fr.md` contiennent chacun une section de démarrage qui nomme SmartScreen, le pare-feu Windows, les ports 17271 et 17272 en UDP, le port de partage et l'interdiction de diffuser le `.bat`, et aucune commande de terminal dans le parcours du joueur.
- AC51 (R9): `limites-connues.md` et sa version française listent comme non prouvés sur le vrai jeu au moins : la transmission par `StartServer64.bat` de ses 8 arguments, sa valeur de `-Dzomboid.steam` face à un client `-nosteam`, son comportement de fin de script, la connexion effective d'un invité, et la joignabilité depuis Internet d'une adresse saisie, que le service ne teste pas. Chaque preuve citée renvoie à un test ou à une exécution de CI.
- AC61 (R9): `README.md` et `README.fr.md` disent chacun, dans la section de démarrage, qu'une revanche garde valides les `.bat` déjà envoyés et qu'un nouveau duel peut les périmer, auquel cas il faut renvoyer le `.bat`.

### Non-régression (R10)

- AC52 (R10): Sous Linux, depuis les sources, `pytest` rend 0 échec et exactement 5 skips, avec au moins 1219 tests collectés.
- AC53 (R10): Chaque fonction de test présente à `1d854d0` existe encore, même paramétrée (comparaison des noms par `pytest --collect-only`, identifiants de paramètres retirés, faite à l'exécution par `tests/test_platform_windows.py`, qui échoue si ce commit est inaccessible), et aucune assertion existante n'est retirée ou affaiblie sous Linux.
- AC54 (R10): Sur `windows-latest`, `pytest` rend 0 échec, et chaque skip propre à Windows donne pour raison le nom du test Windows équivalent, qui s'exécute dans le même job.
- AC55 (R10): Le diff de la phase ne modifie ni `service/protocol.py`, ni `service/rules.py` hors de la résolution de ses deux fichiers de données, ni `service/journal.py`, ni `service/presets/`, ni `mods/duelcore/`.
- AC62 (R10): Sur `windows-latest`, chaque test de `tests/test_snapshots.py`, `tests/test_contract.py`, `tests/test_modes.py` et `tests/test_campaign_gate.py` qui passe sous Linux par `os.killpg`, `signal.SIGKILL` ou un script `#!/bin/sh` s'exécute par un équivalent Windows (arrêt de l'arbre par `service/platforms.py`, double en `.bat`), ou bien est en `skip` avec pour raison le nom du test Windows qui le remplace. Ces 4 fichiers rendent 0 échec dans ce job, et `tests/test_platform_windows.py` vérifie que chaque nom cité par une telle raison désigne un test existant. Sous Linux, ces tests s'exécutent sans `skip` et gardent `os.killpg`.

## Caller enumeration

Recherche par `grep -rnE` sur `service/` et `tests/`, à `1d854d0`. Angle mort : aucun appel dynamique n'a été
trouvé, mais `getattr` et les doubles injectés (`client_launcher`, `make_engine`) ne seraient pas vus par le nom
de la fonction. Tout nouveau paramètre doit garder un défaut qui reproduit le comportement Linux actuel.

| File | Function/Method | Migration Note |
|------|----------------|----------------|
| service/engine.py | `Engine._client_script` → `detect_pz` | Prend le nom et l'emplacement du client depuis la plateforme : même dossier sous Windows, dossier parent sous Linux |
| service/engine.py | `run_preflight` (appel du mode réel) | Inchangé, mesure de RAM choisie par la plateforme |
| service/engine.py | `Engine._open_game_real` → `launch_client` | Lance le client de la plateforme ; message d'échec sans `CLIENT_SCRIPT` figé |
| service/engine.py | invitations aux lignes 1928 et 2046 → `local_ip` | Remplacées par une seule source d'invitation portant adresse et lien |
| service/engine.py | `MATCHES_DIR`, `PRESETS_DIR` | Résolus par `service/paths.py` |
| service/main.py | `main` → `detect_pz`, `free_port`, `make_server` | Mode par défaut, instance unique ; `make_server` garde son hôte `127.0.0.1` |
| service/main.py | `run_install_mod` → `install_mod`, `MOD_SRC` | Inchangé, `MOD_SRC` résolu par `service/paths.py` |
| service/servers.py | `launch_command` → `SCRIPT` | Nom du script issu de la plateforme ; défaut `start-server.sh` |
| service/rules.py | `load_runtime`, `load_effects` via `RUNTIME_PATH`, `EFFECTS_PATH` | Résolus par `service/paths.py` |
| service/api.py | `UI_DIR` | Résolu par `service/paths.py` |
| tests/test_preflight.py | `detect_pz`, `run_preflight`, `install_mod`, `SCRIPT`, `free_port` | Appels inchangés : défauts Linux |
| tests/test_integration_pz.py | `detect_pz`, `run_preflight`, `install_mod`, `SCRIPT` | Inchangé, marqué `pz_integration` |
| tests/test_servers.py, tests/test_secrets.py | `launch_command` | Appels inchangés ; assertions POSIX paramétrées |
| tests/test_modes.py | `launch_client`, `install_mod`, `free_port`, `make_server`, `ACTIONS` | Appels inchangés ; `client_install` et `write_script` paramétrés par plateforme (AC62) |
| tests/test_snapshots.py | `install_mod`, `free_port` | Appels inchangés |
| tests/test_snapshots.py | `os.killpg`, `signal.SIGKILL`, `script.chmod(0o755)` sur les scripts `#!/bin/sh` | Paramétrés par plateforme : équivalent Windows ou skip nommant son remplaçant (AC62) |
| tests/test_contract.py | `crash` → `os.killpg`, `signal.SIGKILL` | Paramétré par plateforme : équivalent Windows ou skip nommant son remplaçant (AC62) |
| tests/test_campaign_gate.py | fixture `harness` → `fake_python.chmod(0o755)` sur un script `#!/bin/sh` | Paramétrée par plateforme (AC62) |
| tests/test_match.py, tests/test_rules_load.py, tests/test_rules.py | `PRESETS_DIR` | Le nom reste exporté par `service/engine.py` |
| tests/test_api_security.py, tests/test_match.py, tests/test_secrets.py | `make_server` | Signature inchangée |
