# Phase 2 — Windows, packaging, zéro ligne de commande

Brief de campagne. Écrit le 06/11/2026, après la clôture de la phase 1 (`162f07d`, 1214 tests,
5 skips, dépôt public `chpomob/pz-duel`).

## Contexte

La phase 1 a livré un arbitre Python stdlib, un mod Lua serveur, une UI locale, un journal rejouable.
Elle est prouvée sur le jeu réel pour tout ce qui ne demande pas de joueur : deux serveurs démarrent,
le mod charge, `caps` est vérifié, le contrat passe, l'arrêt RCON est propre.

Mais **rien de tout cela n'est utilisable par un joueur non technique**, et rien ne tourne sous Windows.

## Faits vérifiés le 06/11/2026 (à ne pas re-discuter)

- Le service ne connaît que Linux :
  - `service/preflight.py:27` — `SCRIPT = "start-server.sh"`
  - `service/engine.py:93` — `CLIENT_SCRIPT = "projectzomboid.sh"`
  - aucun `os.name`, `sys.platform` ni `platform` dans `service/` (grep exhaustif).
- Sous Windows, Project Zomboid se lance par `StartServer64.bat` (serveur) et `ProjectZomboid64.exe`
  (client), s'installe par défaut dans `C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid`,
  et `-nosteam` est un argument client reconnu (chaînes `UI_mainscreen_InviteFromSteamToNoSteam`
  présentes dans les traductions du jeu).
- Le dossier de sauvegarde reste `~/Zomboid` : `service/engine.py:1563`, `service/preflight.py:257`
  utilisent `Path.home() / "Zomboid"`, ce qui vaut aussi sous Windows.
- Les ressources embarquées sont résolues depuis `__file__` : `service/api.py:17` (`UI_DIR`),
  `service/preflight.py:24` (`MOD_SRC`). En binaire figé (PyInstaller `--onefile`), ces chemins
  doivent être résolus depuis le dossier d'extraction, sinon l'UI et le mod sont introuvables.
- Le service est **stdlib uniquement**, et un test le vérifie (`tests/test_stdlib_only.py`).
  PyInstaller serait donc une dépendance de BUILD seulement, pas d'exécution.
- Précédent maison : `pz-save-manager` publie un `--onefile` PyInstaller construit sur
  `windows-latest` dans GitHub Actions, attaché à la release, non signé, avec la note SmartScreen
  dans le README. Pas d'installeur, pas de MSI. Un `launcher.bat` sert ceux qui partent des sources.
- La contrainte « pas de pause, pas d'avance rapide » est tenue par le mode multijoueur du jeu
  (`SpeedControlsHandler` : `if isClient() then return end`), pas par notre code. Rien à faire ici.

## Décisions prises par l'opérateur (06/11/2026)

- **Windows d'abord** : c'est la cible des joueurs. Linux doit continuer à fonctionner.
- **L'invité rejoint par un `.bat` généré par l'hôte**, pas par le Workshop. Le Workshop viendra
  plus tard ; la campagne ne doit pas en dépendre ni le préparer à moitié.
- **Pas de signature de code** à ce stade : l'avertissement SmartScreen est assumé et documenté.
- **Pas de relais/NAT traversal** : même réseau local, ou redirection de port documentée par l'hôte.

## Objectif de la phase

Un joueur qui sait installer un jeu Steam doit pouvoir, sans ouvrir de terminal :

1. télécharger un `.exe` depuis GitHub Releases et le double-cliquer ;
2. voir l'outil détecter Project Zomboid, installer le mod, vérifier RAM et ports, et proposer
   « Lancer le duel » ;
3. inviter un ami par QR/lien ;
4. cet ami double-clique un `.bat` reçu de l'hôte, qui installe le mod chez lui et lance son client
   connecté au bon serveur, avec le bon mot de passe.

Et Linux doit rester au moins aussi bon qu'aujourd'hui, avec la même suite verte.

## Non-objectifs explicites

- Publication Workshop du mod (phase ultérieure).
- Signature Authenticode, MSI, installeur avec désinstalleur système.
- Traversée de NAT, relais VPS, jeu hors LAN sans redirection de port.
- Anti-triche, comptes, classement en ligne.
- Toute modification des règles du duel, du protocole ou du format de journal : la phase 1 est
  close, on n'y retouche pas sans un constat.

## Contraintes

- Service **stdlib uniquement** à l'exécution. Une dépendance de build (PyInstaller) est acceptable
  si elle ne rentre pas dans le produit.
- Python ≥ 3.12, CI Linux 3.12/3.13/3.14 déjà en place.
- La vérification Windows se fait **en CI `windows-latest`** : la machine de développement est sous
  Linux. Tout ce qui est spécifique à Windows doit donc être testable sans Windows local, et la CI
  doit exécuter la suite ET un test de fumée du binaire produit.
- Aucune régression des 1214 tests actuels ; les tests qui dépendent de `chmod 0600`, de SIGTERM ou
  de chemins POSIX doivent être paramétrés par plateforme, pas supprimés.
- Les messages **ajoutés** par cette phase (nouveaux écrans, erreurs de premier lancement, `.bat`)
  sont écrits en anglais. La traduction de l'UI existante n'est pas dans le périmètre : elle
  toucherait des tests qui assertent aujourd'hui des chaînes françaises, pour aucun gain de
  jouabilité. Le français reste la langue des documents de campagne.
