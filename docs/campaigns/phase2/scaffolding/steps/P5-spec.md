# P5 — Processus sous Windows (R2)

commit: feat(phase2): P5 — Processus sous Windows (R2)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/platforms.py, service/servers.py, service/engine.py, tests/doubles/fake_server.py, tests/test_servers.py, tests/test_platform_windows.py]
- **Description:** `platforms.py` : `ProcessTree` avec deux implémentations. POSIX garde `os.killpg` appelé par le module `os` (`tests/test_servers.py:917` le remplace). Windows implémente D-E et D-F sur la couture `kernel`. `launch_command` prend script et séparateur de la plateforme : 8 arguments inchangés. `ServerProcess` : `Popen` reçoit `platform.server_popen_kwargs()` (`CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` ; `start_new_session` sous POSIX) ; `_group_alive` et `_signal_group` délèguent à l'arbre, `_stop_group` garde ses paliers et son vocabulaire. Avant lancement, chaque argument est contrôlé contre les métacaractères de `cmd` : l'interpréteur reçoit un `.bat`, et `list2cmdline` n'échappe pas pour lui. `launch_client` et `_client_script` passent par la plateforme : même dossier sous Windows, parent sous Linux, drapeaux détachés, `CREATE_BREAKAWAY_FROM_JOB` avec repli. `CLIENT_SCRIPT` disparaît du message d'échec. Le double reçoit `install_fake_server(..., platform)` : `StartServer64.bat` qui appelle Python, `args_file`, `script_tail: PAUSE`, arbre d'enfants. Couvre AC7, AC8, AC9, AC10, AC11, AC12.
- **Dependencies:** [P3, P4]
- **Tests:** Sous Linux, plateforme injectée et `kernel` double : AC7 (`runtime.json` de test, `gen_secret` injecté, 8 arguments, délais appliqués, secret absent des messages, de l'état et du log, commande client à 5 arguments) ; transitions de paliers ; job absent. AC12 sur les deux plateformes (fichier absent, sortie immédiate). Windows réel : AC8 (`Program Files (x86)\Project Zomboid`), AC9 (`PAUSE`, enfant qui ignore), AC10 (`taskkill /F` et fermeture de console, via `tests/doubles/run_service.py`), AC11.
- **Risks:** Non triviale (frontière de processus, invariant non vérifiable par le type). `Popen` ferme le handle du fil principal : la reprise passe par `NtResumeProcess` (`ntdll`, stable mais non documenté) ; sans cette reprise, le serveur reste suspendu et `start` expire. Repli à prévoir : énumération Toolhelp32 + `ResumeThread`. Si le processus entrait dans le Job après avoir démarré, un enfant précoce lui échapperait : c'est la raison de la suspension. Job imbriqué dans celui d'un runner : admis depuis Windows 8. Le handle du Job ne doit pas être héritable.

## Critères d'acceptation couverts (verbatim de la spec)

- AC7 (R2): Sous Linux, plateforme Windows injectée, avec un `runtime.json` de test aux ports et délais modifiés et `gen_secret` injecté, la commande serveur commence par `<dossier>\StartServer64.bat` et porte `-servername`, `-port`, `-udpport` et `-adminpassword`, soit 8 arguments, avec les valeurs qu'utilise le moteur : `server_name(side)` pour `-servername`, les ports `game` et `udp` du camp dans ce `runtime.json` pour `-port` et `-udpport`, le secret admin tiré par `Engine._open_match()` pour ce match pour `-adminpassword`. Les délais ne sont pas des arguments : `stop_timeout_s` et `kill_grace_s` appliqués à ce serveur sont ceux de ce `runtime.json`. Après un lancement réussi, ce secret ne figure ni dans un message rendu, ni dans l'état, ni dans le log du service. La commande client est `ProjectZomboid64.exe` du même dossier, suivie de `-nosteam +connect <adresse> +password <valeur>`, soit 5 arguments.
- AC8 (R2): Sur `windows-latest`, avec un double dans un dossier nommé `Program Files (x86)\Project Zomboid`, le serveur démarre, reçoit ses arguments à l'identique (comparaison sur le fichier d'arguments du double) et atteint l'état prêt par ses marqueurs de sortie.
- AC9 (R2): Sur `windows-latest`, un double dont le script finit par `PAUSE` et dont l'enfant ignore la demande d'arrêt est arrêté au palier `sigkill` : `stop_stage` vaut `sigkill`, `running` vaut faux et 0 processus de son arbre ne subsiste à l'échéance `kill_grace_s`.
- AC10 (R2): Sur `windows-latest`, un service lancé avec un double en cours d'exécution puis tué de force (`taskkill /F` sur le service seul) ne laisse aucun processus du double au bout de 5 s ; la même mesure vaut pour la fermeture de sa console.
- AC11 (R2): Sur `windows-latest`, un arrêt demandé par la sortie normale passe d'abord par RCON quand le double y répond : `stop_stage` vaut `rcon`.
- AC12 (R2): Dans chaque échec de lancement injecté (fichier absent, sortie immédiate), le mot de passe client et le mot de passe admin ne figurent ni dans le message rendu, ni dans l'état, ni dans le log du service.

## Règles

- Ne modifie QUE les fichiers listés dans `Files` de cette étape, plus les fichiers de test
  qu'elle nomme. Si une dépendance manque, tu la constates dans ta réponse finale, tu ne sors
  pas du périmètre.
- Python 3.11+ ; `service/` reste **stdlib uniquement** (aucun `import` de PyInstaller, de
  `winreg` au chargement sous Linux, ni de dépendance tierce). Tests : `/home/chpo/pz-duel/.venv/bin/python -m pytest -q`.
- Rien de spécifique à Windows ne doit être importé au chargement de `service/` sous Linux :
  les modules réservés passent par `service/platforms.py` et sont injectés dans les tests.
- Les messages **ajoutés** par cette phase sont en anglais ; l'UI existante et les documents
  de campagne restent en français.
- Aucun secret (mot de passe admin, mot de passe client, jeton d'invitation) dans un log, un
  journal, un état exposé ou un fichier de match.
- Ne touche ni à `docs/campaigns/phase1/`, ni à `spec.md`, ni à `plan.md`, ni au protocole,
  ni aux règles du duel, ni à `mods/duelcore/`.
- Toute valeur numérique vient de `service/runtime.json` : aucune en dur.

## Definition of done

- Les tests de l'étape existent, portent exactement les noms cités, et passent.
- `/home/chpo/pz-duel/.venv/bin/python -m pytest -q` est vert, et le nombre de skips reste 5 sous Linux (les skips propres à Windows
  nomment le test Windows qui les remplace).
- Un seul commit, sujet : `feat(phase2): P5 — Processus sous Windows (R2)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
