---
spec: "phase2-windows-packaging-zero-cli"
version: "1.0"
author: "adversarial-plan"
based-on: "adversarial-spec"
findings-input: true
---

# Implementation Plan

Base relevée à `8aa7335` : `pytest` rend 1214 réussis, 5 skips, 1219 collectés (Python 3.14, Linux).

## Décisions transverses

Ces choix valent pour toutes les étapes. Chacun doit être rappelé dans le docstring du module qui
l'implémente ou dans le message de commit, pour ne pas devenir une décision silencieuse.

- **D-A — Tests réservés à Windows : définis, pas sautés.** AC52 exige *exactement* 5 skips sous Linux :
  un `skipif(not windows)` en ajouterait un par test. Les tests qui demandent le vrai Windows sont donc
  définis dans un bloc `if sys.platform == "win32":` de leur module, et n'existent pas sous Linux. Les
  raisons de skip d'AC54 et d'AC62 citent ces noms : `tests/test_platform_windows.py` les vérifie par
  analyse `ast` des sources (blocs `if` compris), pas par la collecte, pour que la mesure tourne aussi
  sous Linux.
- **D-B — Injection de plateforme par paramètre.** `service/platforms.py` expose un `Platform` figé
  (`LINUX`, `WINDOWS`) et `current()`. Les fonctions touchées prennent un paramètre `platform` dont le
  défaut est `current()`. Sous Linux, tout appel existant garde donc son comportement. Les chemins de
  commande Windows se composent avec `ntpath`, importable partout, pas avec `Path`.
- **D-C — Primitives Windows derrière une couture.** `winreg`, `msvcrt`, `ctypes.windll` et `_winapi`
  ne s'importent que dans le corps des fonctions (AC6). Les appels Win32 passent par un petit objet
  `kernel` injectable, pour que les branches s'exécutent sous Linux contre un double (R1).
- **D-D — Mode figé simulé par sous-processus.** `paths.py` se résout à l'import, et les constantes
  (`MOD_SRC`, `PRESETS_DIR`, `UI_DIR`, ...) restent des constantes : les appelants et les valeurs par
  défaut de paramètres ne changent pas. `tests/test_frozen.py` simule donc le figement dans un
  sous-processus qui pose `sys.frozen` et `sys._MEIPASS` avant le premier import de `service`.
- **D-E — Arbre de processus Windows = objet Job.** Chaque serveur démarre suspendu, entre dans un Job
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, puis reprend. Sonde de vie : `ActiveProcesses` du Job. Fin
  forcée du service ou fermeture de sa console : le noyau ferme le handle et tue l'arbre (AC10), sans
  dépendre d'un code du service.
- **D-F — Paliers Windows.** `rcon` et `stdin` inchangés. `sigterm` met fin au seul processus racine
  (le script, y compris bloqué sur `PAUSE`) : c'est un vrai palier intermédiaire, testable, qui laisse
  `kill_grace_s` à `java`. `sigkill` appelle `TerminateJobObject`. `GenerateConsoleCtrlEvent` est
  écarté : il n'atteint pas un processus lancé avec sa propre console invisible.
- **D-G — Lanceur invité sans archive.** Le mod pèse 36 Ko. Le `.bat` porte chaque fichier en base64
  avec son chemin relatif et son SHA-256, triés par nom : pas de zip, donc aucune dépendance à la
  version de zlib pour AC30, et pas d'`Expand-Archive`. La logique est du PowerShell 5.1 (présent sur
  Windows 10 et 11), lu dans le `.bat` lui-même par `$env:PZDUEL_SELF`, sans `-EncodedCommand`.
- **D-H — Valeurs du `.bat` par liste blanche.** Mot de passe : `[A-Za-z0-9._-]+`. Adresse : grammaire
  de H7. Tout le reste est refusé à la génération sans citer la valeur (AC33 l'autorise). C'est ce qui
  rend toute exécution impossible, plutôt qu'un échappement `cmd` et PowerShell superposés.
- **D-I — `console=False` pour le `.exe`.** Pas de fenêtre de terminal pour le joueur. La sortie passe
  par « Quit », et les erreurs par le fichier de log du dossier de données.

## Respect de `targets`

La spec dit que `targets` liste les fichiers que la phase modifie ou crée, « et eux seuls ». Ce plan n'a
aucun écart à cette règle : chaque fichier nommé dans `Files` y figure, et un écart ne se règle pas par
une mention dans un commit.

- **`USERPROFILE` sans toucher à `tests/conftest.py`.** `zomboid_home` ne redirige que `HOME`, et
  `Path.home()` lit `USERPROFILE` sous Windows. Or seuls 4 modules utilisent cette fixture, tous dans
  `targets` : `test_contract`, `test_preflight`, `test_snapshots`, `test_modes`. P6 la surcharge donc
  dans chacun de ces modules, en demandant la fixture de même nom pour y ajouter
  `monkeypatch.setenv("USERPROFILE", str(zomboid_home.parent))`. `tests/conftest.py` reste identique.
- **Fichiers de test hors `targets`.** Aucun n'est à modifier. Le relevé par `grep` de `chmod`,
  `st_mode`, `signal.`, `killpg`, `#!`, `fcntl`, `symlink`, `/proc`, `/tmp` et `start_new_session` ne
  trouve rien dans `tests/test_match.py`, `test_transport`, `test_proofs`, `test_journal`, `test_budget`,
  `test_clock`, `test_replay`, `test_rules`, `test_rules_load`, `test_protocol`, `test_verdict`,
  `test_mod_package`, `test_lua_attribution`, `test_mini_lua`, `test_stdlib_only`,
  `tests/doubles/fake_mod.py` et `tests/doubles/run_service.py`, ce qui confirme la spec. Reste une
  inconnue : `write_text` sans encodage (`tests/test_transport.py:486`, `tests/test_replay.py:486`),
  sans effet attendu sur du texte ASCII.
- **Si P7 fait quand même échouer un fichier hors `targets`.** Le défaut est d'abord cherché dans le code
  de production, qui est corrigé dans un fichier de `targets`. Si le test lui-même est propre à POSIX,
  P7 s'arrête sans modifier ce fichier : seul l'opérateur peut amender `targets` dans la spec, et ce
  n'est qu'ensuite que le fichier entre dans le plan. Aucune étape n'autorise cette modification
  d'avance.

## Steps

### P1: Résolution sources / binaire figé (R4, R10)
- **Files:** [service/paths.py, service/rules.py, service/api.py, service/engine.py, service/preflight.py, tests/test_frozen.py]
- **Description:** Crée `service/paths.py`, seul module qui distingue sources et binaire (R4, H3) : `is_frozen()`, `resource_root()` (`sys._MEIPASS` si figé, racine du dépôt sinon), `data_dir(platform)` (`%LOCALAPPDATA%\PZDuel` sous Windows, `$XDG_DATA_HOME/pz-duel` ou `~/.local/share/pz-duel` sous Linux figé), `matches_dir()` (`<dépôt>/matches` depuis les sources), `log_file()`. Il n'importe rien de `service` : `rules.py` en dépend. `RUNTIME_PATH`, `EFFECTS_PATH`, `UI_DIR`, `MOD_SRC`, `PRESETS_DIR`, `MATCHES_DIR` passent par lui, en gardant leurs noms. Le diff de `rules.py` se limite à ces deux lignes et à l'import (AC55). Couvre AC18, AC19.
- **Dependencies:** []
- **Tests:** `tests/test_frozen.py`, par sous-processus (D-D) : AC18 (UI, mod, 4 presets, `runtime.json`, `effects.json` lus du dossier d'extraction, 0 chemin d'écriture dedans) ; AC19 (`matches_dir` avec `LOCALAPPDATA` injecté, journal encore lisible après suppression du dossier d'extraction, `<dépôt>/matches` sans figement). Suite Linux inchangée.
- **Risks:** Une constante oubliée continue de dériver de `__file__` : `grep -n __file__ service/` doit ne laisser que `paths.py`. Import circulaire si `paths.py` importe `rules` ou `platforms` : `data_dir` reçoit donc le nom de plateforme et lit `sys.platform` lui-même.

### P2: Module `platforms` et remplacement atomique tolérant (R1, R3)
- **Files:** [service/platforms.py, service/runtime.json, tests/test_platform_windows.py]
- **Description:** Crée `Platform` (D-B) : script serveur, programme client, client dans le dossier (`Windows`) ou dans le parent (`Linux`), sous-dossier du jeu, `join()`, `posix_modes`, signaux d'arrêt. Ajoute à `runtime.json` : `share_port`, `replace_timeout_s`, `replace_retry_ms`, `instance_wait_s`. Ajoute `replace_file(src, dst, *, policy=None, platform=None, sleep=time.sleep, clock=time.monotonic)` : sous Windows, réessaie sur `PermissionError` jusqu'à l'échéance, puis lève une erreur explicite qui nomme le fichier, sans jamais effacer `dst`. Sous Linux, c'est `os.replace` nu. Sans `policy`, il lit `load_runtime()` par un import local. Le nettoyage du temporaire reste à l'appelant, qui le possède. Couvre AC6, la partie Linux d'AC15.
- **Dependencies:** [P1]
- **Tests:** AC6 (énumération de `service/` à l'exécution par `pkgutil`, import de tous les modules dans un sous-processus, `winreg`, `msvcrt`, `_winapi` absents de `sys.modules`). AC15 partie Linux : plateforme et `PermissionError` injectées, horloge factice, tentatives bornées, durée totale ≤ délai configuré + 10 %.
- **Risks:** Une nouvelle clé de `runtime.json` peut heurter un test qui fige ce fichier : la suite tranche. `replace_retry_ms` trop grand ferait dépasser les 10 % : la dernière attente est tronquée à l'échéance.

### P3: Fichiers sous Windows (R3)
- **Files:** [service/transport.py, service/proofs.py, service/servers.py, service/engine.py, tests/test_platform_windows.py, tests/test_servers.py]
- **Description:** `transport.py` : `O_BINARY` via `getattr(os, "O_BINARY", 0)` sur `in` ; `write_session` passe par `replace_file` et retire `session.tmp` en cas d'échec. `proofs.py` : `replace_file`, et retrait du temporaire, absent aujourd'hui (`_store` laisse le fichier sur `OSError`) ; le rapport par `_report` reste l'erreur explicite. `servers.py` : `_replace_file` passe par `replace_file` et ne fait `os.fchmod` que si `platform.posix_modes` ; `_secure_ini` est sans effet sous Windows et laisse `ini_failure` à `None`. L'`Engine` transmet `runtime` à ces trois écrivains pour que le délai testé soit celui d'un `runtime.json` de test. Le format du protocole et du journal ne change pas (AC17). Couvre AC13, AC14, AC15, AC16, AC17.
- **Dependencies:** [P2]
- **Tests:** Sous Linux : `.ini` toujours en `0600`, plateforme Windows injectée sans `fchmod` ni `ini_failure` (AC16). Sous Windows réel (D-A), avec un tiers tenant le fichier sans `FILE_SHARE_DELETE` : AC13 (N octets, aucun `0x0D`), AC14 (`session`, `.ini`, preuves tenus 500 ms), AC15 (tenue au-delà du délai : erreur, ancien contenu intact, 0 temporaire), AC16, AC17 (rejeu de `tests/fixtures` identique).
- **Risks:** `write_ini` a un seul appelant (`engine.py:543`) mais `_replace_file` en a deux (`servers.py:118`, `:144`) : les deux reçoivent la politique. AC17 suppose des fixtures sans conversion CRLF au checkout : voir P7. Écritures Windows non exécutées avant P7.

### P4: Préflight sous Windows (R1)
- **Files:** [service/preflight.py, tests/test_preflight.py, tests/test_platform_windows.py]
- **Description:** `detect_pz(pz_dir, env, home, *, platform=None, steam_locator=None)` : sous Windows, ordre `--pz-dir`, `PZ_INSTALL_DIR`, dossier Steam par défaut (`ProgramFiles(x86)`), Steam enregistré (`HKCU\Software\Valve\Steam\SteamPath`, puis `HKLM\SOFTWARE\WOW6432Node\Valve\Steam\InstallPath`), puis bibliothèques de `steamapps/libraryfolders.vdf`. Parseur `.vdf` pur et tolérant : vide, tronqué, absent ou registre illisible rendent `absent` sans exception. Un dossier valide contient `StartServer64.bat` et `ProjectZomboid64.exe`, et le message nomme le fichier manquant. Un choix explicite invalide reste sans repli (D19). RAM : `run_preflight` gagne `ram_reader` ; sous Windows, `GlobalMemoryStatusEx` par `ctypes`, seuil et refus inchangés. Ports : `_bind` pose `SO_EXCLUSIVEADDRUSE` sous Windows et jamais `SO_REUSEADDR`. `check_ports` reconnaît `WSAEADDRINUSE` (10048) et `WSAEACCES` (10013), car `errno.EADDRINUSE` n'est pas le code rendu sous Windows. `install_mod` crée `zomboid_dir` s'il manque. `INSTALL_HINT` et les messages citant `--pz-dir` ou `chmod` prennent une variante sans terminal si `paths.is_frozen()`. Couvre AC1, AC2, AC3, AC4, AC5.
- **Dependencies:** [P2, P3]
- **Tests:** Sous Linux, plateforme injectée : AC1 ; AC2 (3 bibliothèques, la troisième porte le jeu ; fichier vide, tronqué, absent ; registre illisible) ; AC3 ; AC4 (6143, 6144, mesure en échec). Doubles de `tests/test_preflight.py` paramétrés par plateforme. Windows réel : AC4 (mesure réelle > 0), AC5 (TCP avec et sans `SO_REUSEADDR` chez le détenteur, UDP lié, port libre, 10013 injectée).
- **Risks:** `SO_EXCLUSIVEADDRUSE` peut signaler occupé un port en TIME_WAIT après un duel, et un faux défaut bloquerait « Launch duel » : à mesurer en P7, avec `retry` comme issue pour le joueur. Les chemins du `.vdf` portent des `\\` échappés. Le `.vdf` peut lister un disque déconnecté : `is_file` rend faux, sans exception.

### P5: Processus sous Windows (R2)
- **Files:** [service/platforms.py, service/servers.py, service/engine.py, tests/doubles/fake_server.py, tests/test_servers.py, tests/test_platform_windows.py]
- **Description:** `platforms.py` : `ProcessTree` avec deux implémentations. POSIX garde `os.killpg` appelé par le module `os` (`tests/test_servers.py:917` le remplace). Windows implémente D-E et D-F sur la couture `kernel`. `launch_command` prend script et séparateur de la plateforme : 8 arguments inchangés. `ServerProcess` : `Popen` reçoit `platform.server_popen_kwargs()` (`CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` ; `start_new_session` sous POSIX) ; `_group_alive` et `_signal_group` délèguent à l'arbre, `_stop_group` garde ses paliers et son vocabulaire. Avant lancement, chaque argument est contrôlé contre les métacaractères de `cmd` : l'interpréteur reçoit un `.bat`, et `list2cmdline` n'échappe pas pour lui. `launch_client` et `_client_script` passent par la plateforme : même dossier sous Windows, parent sous Linux, drapeaux détachés, `CREATE_BREAKAWAY_FROM_JOB` avec repli. `CLIENT_SCRIPT` disparaît du message d'échec. Le double reçoit `install_fake_server(..., platform)` : `StartServer64.bat` qui appelle Python, `args_file`, `script_tail: PAUSE`, arbre d'enfants. Couvre AC7, AC8, AC9, AC10, AC11, AC12.
- **Dependencies:** [P3, P4]
- **Tests:** Sous Linux, plateforme injectée et `kernel` double : AC7 (`runtime.json` de test, `gen_secret` injecté, 8 arguments, délais appliqués, secret absent des messages, de l'état et du log, commande client à 5 arguments) ; transitions de paliers ; job absent. AC12 sur les deux plateformes (fichier absent, sortie immédiate). Windows réel : AC8 (`Program Files (x86)\Project Zomboid`), AC9 (`PAUSE`, enfant qui ignore), AC10 (`taskkill /F` et fermeture de console, via `tests/doubles/run_service.py`), AC11.
- **Risks:** Non triviale (frontière de processus, invariant non vérifiable par le type). `Popen` ferme le handle du fil principal : la reprise passe par `NtResumeProcess` (`ntdll`, stable mais non documenté) ; sans cette reprise, le serveur reste suspendu et `start` expire. Repli à prévoir : énumération Toolhelp32 + `ResumeThread`. Si le processus entrait dans le Job après avoir démarré, un enfant précoce lui échapperait : c'est la raison de la suspension. Job imbriqué dans celui d'un runner : admis depuis Windows 8. Le handle du Job ne doit pas être héritable.

### P6: Paramétrage des tests POSIX (R10)
- **Files:** [tests/doubles/fake_server.py, tests/test_servers.py, tests/test_secrets.py, tests/test_preflight.py, tests/test_snapshots.py, tests/test_contract.py, tests/test_modes.py, tests/test_campaign_gate.py, tests/test_platform_windows.py]
- **Description:** Paramètre par plateforme les tests à `os.killpg`, `signal.SIGKILL`, `#!/bin/sh` et `0600`. Surcharge `zomboid_home` dans `test_contract`, `test_preflight`, `test_snapshots` et `test_modes` pour rediriger aussi `USERPROFILE` (voir « Respect de `targets` ») ; le sous-processus de `tests/test_preflight.py:289` reçoit la même variable. Helpers dans `fake_server.py` : `write_script(path, sh_body, bat_body)`, `kill_tree(server)` (`os.killpg` sous Linux, arrêt par `service/platforms.py` sous Windows). `crash()` de `test_contract`, `client_install` et `write_script` de `test_modes`, faux interpréteur de `test_campaign_gate`. Quand aucun équivalent n'a de sens, `skipif` Windows dont la raison nomme le test qui le remplace. Sous Linux, aucun de ces tests n'est sauté et `os.killpg` reste. Le double de `ProjectZomboid64.exe` doit être un vrai PE : il est compilé dans une fixture par `csc.exe` du .NET Framework, présent sur `windows-latest`, et son absence est une erreur, pas un skip. `tests/test_platform_windows.py` mesure AC53 (extraction de `1d854d0` par `git archive`, `pytest --collect-only` des deux arbres, noms sans identifiants de paramètres ; échec si le commit est inaccessible) et AC62 (noms cités par les raisons, par `ast`, D-A). `test_secrets.py` : assertion `0600` limitée à POSIX, équivalent Windows, et préparation du balayage complété en P11 et P13. Couvre AC53, AC54, AC62.
- **Dependencies:** [P5]
- **Tests:** AC52 : sous Linux, 0 échec, exactement 5 skips, ≥ 1219 collectés. AC53 et AC62 exécutés par `tests/test_platform_windows.py`. Mutation à la main : retirer un test et constater l'échec d'AC53 ; citer un nom inexistant et constater l'échec d'AC62. `git diff --name-only 1d854d0 -- tests/conftest.py` reste vide.
- **Risks:** Fake-done n° 1 à éviter : un `assert exit_code == -signal.SIGTERM` déplacé ne doit pas s'affaiblir sous Linux ; l'assertion reste, et seule une variante Windows s'ajoute. `signal.SIGKILL` évalué au niveau module casserait la collecte Windows : toute référence entre dans le corps des fonctions. Un test de ces 4 modules qui résoudrait le dossier personnel sans passer par `zomboid_home` viserait le vrai profil du runner : le job Windows de P7 le montrerait, et la correction reste dans le module.

### P7: Suite sur `windows-latest` (R8, R3, R10)
- **Files:** [.github/workflows/tests.yml, service/paths.py, service/platforms.py, service/runtime.json, service/transport.py, service/proofs.py, service/servers.py, service/engine.py, service/preflight.py, tests/doubles/fake_server.py, tests/test_frozen.py, tests/test_platform_windows.py, tests/test_servers.py, tests/test_preflight.py, tests/test_secrets.py, tests/test_snapshots.py, tests/test_contract.py, tests/test_modes.py, tests/test_campaign_gate.py]
- **Description:** Ajoute le job `windows-latest` en Python 3.12, 3.13 et 3.14 ; garde les 3 jobs Linux. `actions/checkout` en `fetch-depth: 0` sur tous les jobs (AC53). Sur Windows, `git config --global core.autocrlf false` **avant** le checkout : sinon `tests/fixtures/*.jsonl` et le mod seraient convertis en CRLF et AC17 et AC30 mesureraient autre chose. Ouvre la pull request en brouillon, avec l'accord de l'opérateur (action externe) : c'est le seul déclencheur `pull_request`, et `workflow_dispatch` n'existe pas tant que le fichier n'est pas sur `master`. Corrige ce que le job révèle pour P1 à P6, dans les fichiers listés ici : le workflow est le seul fichier prévu d'avance, les autres ne changent que si le job l'exige. Toutes les étapes suivantes dépendent de P7, pour ne pas s'écrire sur des fichiers en cours de correction. Couvre l'exécution réelle d'AC4, AC5, AC8 à AC11, AC13 à AC17, AC54, AC62.
- **Dependencies:** [P6]
- **Tests:** Job Windows vert, 0 échec, skips conformes à AC54. Les jobs Linux restent verts. `git diff --name-only 1d854d0` ne nomme aucun fichier de code ou de test hors `targets`. L'assertion statique sur ce fichier arrive en P16.
- **Risks:** C'est le premier contact avec le vrai Windows : les étapes P3 à P6 n'y ont jamais tourné, et ce job peut demander plusieurs itérations. Budget à prévoir. Si l'opérateur refuse la PR brouillon, tous les critères « sur `windows-latest` » restent non prouvés et la phase ne peut pas se clore. Un échec dans un fichier de test hors `targets`, dû au test lui-même, arrête l'étape jusqu'à une décision de l'opérateur sur la spec (voir « Respect de `targets` »).

### P8: Démarrage du binaire (R4)
- **Files:** [service/main.py, service/api.py, service/platforms.py, tests/test_frozen.py]
- **Description:** `--mode` sans défaut fixe : `real` si `paths.is_frozen()`, `mock` sinon. `main(argv, make_engine, *, browser=None)` : le paramètre ajouté a un défaut, `tests/doubles/run_service.py` reste valide. Log de démarrage dans `paths.log_file()` en mode figé ; `print` sûr avec `stdout` et `stderr` à `None` ou cp1252 strict ; toute erreur de démarrage laisse une ligne et un code non nul, `sys.exit(message)` compris, dont le texte serait perdu sans console. Ouverture du navigateur par `open_interface(url, browser)` : un retour faux ou une exception laissent le service actif et consignent l'URL. `STOP_SIGNALS` selon la plateforme (`SIGINT`, `SIGBREAK` sous Windows). `stop_on_signal` rend la fonction d'arrêt idempotente que P10 réutilise. Instance unique, en mode figé seulement (H3) : sonde des 20 ports de `free_port` (`GET /api/state` avec en-tête `Server: PZduel/` et JSON reconnu), ouverture et code 0 si trouvé ; sinon verrou `instance.lock` du dossier de données, tenu toute la vie du processus (`msvcrt.locking` ou `fcntl.flock`, importés localement) ; un perdant attend `instance_wait_s` que le gagnant réponde. `BoundedHTTPServer` pose `allow_reuse_address = False` et `SO_EXCLUSIVEADDRUSE` sous Windows : `HTTPServer` active `SO_REUSEADDR`, le piège de `_bind` existe donc aussi sur l'API. Couvre AC20, AC21, AC22, AC56.
- **Dependencies:** [P1, P2, P5, P7]
- **Tests:** `tests/test_frozen.py` : AC20 (les 3 cas) ; AC21 (flux `None`, cp1252 strict, erreur injectée) ; AC56 (navigateur injecté : 1 appel et `GET` 200 ; `--no-browser` : 0 ; faux ou exception : `/api/state` 200 et URL dans le log) ; AC22 (service actif : 1 appel, code 0, aucun `Engine` ; port occupé par un serveur étranger : port suivant). Depuis les sources, `mock`, pas de verrou, messages actuels.
- **Risks:** Deux double-clics simultanés : le verrou tranche, là où la sonde seule laisserait passer deux services. `signal.signal` lève hors du fil principal : les tests en sous-processus l'évitent. Un fichier de log contenant un secret serait une fuite : seuls URL et messages expurgés y vont.

### P9: État de premier lancement et routes (R5)
- **Files:** [service/preflight.py, service/engine.py, service/api.py, tests/test_setup_flow.py, tests/test_api_security.py]
- **Description:** `PreflightResult` gagne `defects: tuple[Defect, ...]` (`kind`, `message`, `action` parmi `install_mod`, `choose_folder`, `retry`) avec un défaut vide : les constructions et `failures` ne changent pas. `Engine.setup_state()` lance `run_preflight` sans rien écrire, sur la table par défaut ; hors `idle`, il rend le dernier état sans lier de port ; en `mock`, il rend `ok: true` sans mesure, pour garder le parcours actuel (R10). Il expose aussi `expected_mod_fingerprint`, lu par le test de fumée. Routes : `GET /api/setup`, `POST /api/setup/install-mod`, `POST /api/setup/pz-dir`. Hors de `ACTIONS` (H6). `do_POST` passe d'une route à une table, avec la même chaîne de gardes (`_refused`, `Content-Type`, lecture bornée). 409 hors `idle` ou en `mock`, sans rien écrire. `Engine.pz_dir` se modifie sous `self.lock` après validation par `detect_pz` : le contrôle et le `start` suivants le voient sans redémarrage. Le choix n'est pas persisté entre deux lancements : la spec ne le demande pas. `folder` est classé dans `NOT_IDENTIFIERS`. Textes ajoutés en anglais. Couvre AC23, AC24, AC25, AC26 (partie serveur), AC27, AC29.
- **Dependencies:** [P3, P4, P5, P8]
- **Tests:** `tests/test_setup_flow.py` : AC23 (4 cas, `~/Zomboid` identique octet pour octet) ; AC24 ; AC25 ; AC26 (`start` refusé rend le même défaut) ; AC27 (403 sur `Host` et `Origin` étrangers, 409 hors `idle`, rien d'écrit) ; AC29 (`ACTIONS` à 5 noms ; en mode figé, aucun message n'a `python`, `--pz-dir`, `--install-mod`, `chmod`). `tests/test_api_security.py` : balayage des nouvelles routes.
- **Risks:** Une UI qui sonde `/api/setup` pendant un duel lierait les ports du serveur : d'où le dernier état hors `idle`. `test_every_field_read_from_the_payload_is_classified` échoue tant que le champ n'est pas classé : c'est voulu. Le préflight liant les ports sur toutes les interfaces peut faire apparaître l'invite du pare-feu au premier contrôle : à documenter en P17.

### P10: Sortie par l'UI (R5)
- **Files:** [service/api.py, service/main.py, tests/test_setup_flow.py, tests/test_api_security.py]
- **Description:** `POST /api/quit`, derrière les gardes : la réponse 200 part, puis la fonction d'arrêt de P8 passe sur son fil. `make_server` garde sa signature : `main` pose `httpd.on_quit`, et sans lui la route rend 409. Sortie par `engine.shutdown()` puis `httpd.shutdown()`, comme un signal. Couvre AC27 (route de sortie), AC28.
- **Dependencies:** [P8, P9]
- **Tests:** AC28 : match réel double, 2 serveurs arrêtés, `main()` rend 0 en moins de `stop_timeout_s` + `kill_grace_s`, journal fermé. AC27 : 403 sur la sortie. Second appel idempotent. Windows réel : AC11 par cette voie.
- **Risks:** Couper avant l'envoi de la réponse ferait croire à l'UI à une panne. `engine.shutdown()` depuis le fil du duel est interdit par son contrat : la route passe par le fil dédié. `STOP_INCOMPLETE` doit garder son code non nul.

### P11: Génération du lanceur invité (R6)
- **Files:** [service/guest_launcher.py, tests/test_guest_launcher.py, tests/test_secrets.py]
- **Description:** `build_launcher(address, port, password, mod_src) -> bytes`, fonction pure de ses entrées (D-G, D-H) : ASCII, CRLF, lignes ≤ 8000 octets, < 256 Kio. `LauncherRefused` ne cite jamais la valeur. Le `.bat` : `set "PZDUEL_SELF=%~f0"`, appel PowerShell, `pause` en cas d'échec, code de sortie propagé. Le PowerShell cherche PZ comme R1, demande le dossier si la recherche échoue, décode et vérifie chaque SHA-256 dans un dossier voisin, puis seulement remplace `%USERPROFILE%\Zomboid\mods\duelcore` avec retour arrière, et lance `ProjectZomboid64.exe -nosteam +connect <adresse>:<port B> +password <mot de passe>`. Le mod précédent reste en place en cas d'échec. Messages en anglais. Couvre AC30, AC31, AC32, AC33, AC34, AC35.
- **Dependencies:** [P6, P7]
- **Tests:** Sous Linux : AC30 (octets, CRLF, longueur, taille, déterminisme) ; AC33 (les 10 mots de passe sont refusés sans être cités ; l'hexadécimal jamais). Windows réel : AC30 (déterminisme), AC31 (`USERPROFILE` avec espace et `é`), AC32, AC33 (aucun `pwned.txt`), AC34 (entrée vide, dossier saisi, payload altéré), AC35. `tests/test_secrets.py` : mot de passe absent de tout message de refus.
- **Risks:** Non triviale (format livré à des tiers). PowerShell en mode de langage restreint ou bloqué par une stratégie : à déclarer dans `limites-connues`. Le `.bat` téléchargé porte le Mark of the Web : SmartScreen (P17). `%` et `!` dans `%~f0` : `set` avec guillemets et sans expansion retardée. `pause` sur entrée fermée rend aussitôt, ce dont AC34 dépend : à constater.

### P12: Modèle d'invitation (R7)
- **Files:** [service/engine.py, service/api.py, tests/test_share.py, tests/test_api_security.py]
- **Description:** Une seule source, `_build_invite()`, remplace les deux blocs (`engine.py:1928`, `:2046`). `is_usable_address` (H7) : IPv4 ou nom d'hôte, sans `127.0.0.0/8`, `localhost` (casse et point final compris), `0.0.0.0`, `169.254.0.0/16` ; un nom dont le dernier label est numérique doit être une IPv4 complète, car `127.1` ou `2130706433` désignent la boucle. `local_ip()` n'est appelé qu'à l'ouverture du lobby et son résultat est gardé : il fait un `connect` vers `8.8.8.8`, qu'AC58 interdit pendant la saisie et la génération. L'invitation garde `ip`, `port`, `password`, `code` (tests existants) et ajoute `address`, `link`, `qr_text`, `scope`, `scope_text`, `share_available`, `needs_address`, `rematch_note`. En `mock`, ni lien ni `.bat`, et `rematch_note` est absent (AC60). Sans adresse utilisable, ni lien ni `.bat`, lobby ouvert. Portée et ports (`ports.B.game`, `ports.B.udp`, `share_port`) viennent de `self.runtime`. `POST /api/invite/address` : refus en gardant l'adresse précédente, 409 hors lobby ; l'adresse saisie reste en mémoire pour la revanche. `address` est classé dans `NOT_IDENTIFIERS`. `GET /api/invite/launcher` : `attachment`, `no-store`, 409 en `mock` et hors lobby ; `.bat` généré à la demande, jamais écrit. Un mot de passe saisi hors liste blanche donne une raison en anglais dans `invite` et aucun `.bat`. Couvre AC36, AC38, AC43, AC44, AC57, AC58, AC59, AC60.
- **Dependencies:** [P9, P10, P11]
- **Tests:** `tests/test_share.py` : AC43 (valides et refusés, l'adresse précédente reste) ; AC44 (3 cas puis saisie) ; AC57 (`runtime.json` de test aux ports modifiés) ; AC58 (sockets espionnées, 0 connexion) ; AC36 (409, aucun fichier ne contient le mot de passe, en-têtes) ; AC38 (`+connect` et `+password` égaux après `rematch`, `Password` du `.ini` de B) ; AC59 (`gen_player_password` injecté à 2 valeurs ; `start` qui ressaisit le mot de passe) ; AC60.
- **Risks:** `tests/test_match.py:335` vérifie que `host_password` n'est pas dans `invite.values()` : aucun champ ajouté ne doit le porter. L'état publié sur la boucle locale porte le lien et le jeton : il ne doit pas aller au journal (`_record_state` n'enregistre que `phase` et `server`, à garder). Les deux formes de lobby (réel, `mock`) doivent rester compatibles avec `test_modes.py:156`.

### P13: Écouteur de partage (R7)
- **Files:** [service/share.py, service/engine.py, tests/test_share.py, tests/test_api_security.py, tests/test_secrets.py]
- **Description:** `ShareServer` : premier écouteur hors boucle locale (H1), lié à toutes les interfaces sur `share_port`, avec son propre `Handler` minimal sur `BoundedHTTPServer` (plafond 32, `REQUEST_READ_S`). Un seul chemin `/<jeton>/pz-duel-join.bat` en `GET`, comparé par `hmac.compare_digest`. Tout le reste : 404 ou 405 à corps vide, sans citer la requête ; `log_message` muet, `send_error` remplacé pour que le chemin ne sorte jamais. Il appelle `engine.guest_launcher()` à chaque requête, sans cache. Jeton `secrets.token_hex(16)` tiré à chaque `_open_lobby`, revanche comprise. Fermeture idempotente `_close_share()` à **toute** sortie du lobby : compte à rebours, `cancel`, panne, `shutdown`, donc dans `_finish_match`, `_cancel_state`, `_fail` et au passage de phase. Échec de liaison : `share_available: false`, texte anglais, lobby ouvert, enregistrement du `.bat` encore offert. `make_server` reste sur `127.0.0.1`. Couvre AC37, AC39, AC40, AC41, AC42.
- **Dependencies:** [P12]
- **Tests:** `tests/test_share.py` : AC37 (lien = `qr_text`, `http://`, mêmes octets que l'API) ; AC39 (les 6 refus, API toujours sur `127.0.0.1`) ; AC40 (non lié avant le lobby, refus sous 2 s pour chaque sortie, paramétré ; plafond 32) ; AC41 (jeton absent des logs, de l'état journalisé, des fichiers de match) ; AC42 (port occupé). `tests/test_api_security.py` et `tests/test_secrets.py` : balayage de l'écouteur.
- **Risks:** Non triviale (surface réseau nouvelle, en clair). `serve_forever` scrute toutes les 0,5 s : `shutdown` + `server_close` tiennent les 2 s d'AC40, à condition de ne pas attendre les fils en cours (`daemon_threads`). Le fil d'arrêt ne doit pas être celui de l'écouteur. Une sortie de lobby oubliée laisserait le port ouvert avec le secret derrière : d'où le test paramétré sur toutes les sorties. Rebind rapide après revanche sous Windows, sans `SO_REUSEADDR` : à constater en CI.

### P14: Interface (R5, R7)
- **Files:** [ui/app.js, ui/index.html, ui/style.css, tests/test_setup_flow.py, tests/test_share.py]
- **Description:** Écran de premier lancement : état de PZ, du mod, de la RAM et des ports, une action par défaut (`install_mod`, `choose_folder`, `retry`). `canLaunch(setup)` désactive « Launch duel » tant que `ok` est faux. Bouton « Quit » et écran de fin. Écran d'invitation : lien, QR construit depuis `invite.qr_text`, enregistrement du `.bat`, champ d'adresse publique, portée, ports à ouvrir, `rematch_note`, partage indisponible, demande d'adresse. `pzduel://` disparaît (`ui/app.js:346`, `:379`). Le bloc `ip:port` et mot de passe en clair reste pour le repli manuel de l'invité Linux (H2). Textes ajoutés en anglais. Couvre AC26 (UI), AC37 (absence de `pzduel://`), AC60 (UI), R5, R7.
- **Dependencies:** [P10, P13]
- **Tests:** Lecture statique de `ui/app.js`, sur le modèle de `tests/test_match.py:2488`, sans dépendance à `node` : AC26 (`canLaunch` commande l'attribut `disabled`), AC37, AC60. Vérification à la main dans un navigateur en mode réel double : les écrans s'affichent, « Quit » arrête le service.
- **Risks:** Le contrôle statique prouve la présence du lien logique, pas le rendu : la vérification à la main doit être rapportée telle quelle. Le QR a une capacité bornée : lien long avec un nom d'hôte de 253 caractères, à tester à cette taille.

### P15: Empaquetage et test de fumée (R4, R8)
- **Files:** [packaging/pz_duel_entry.py, packaging/pz-duel.spec, packaging/requirements-build.txt, packaging/smoke_test.py, tests/test_frozen.py]
- **Description:** `pz_duel_entry.py` importe `service.main` en absolu (les imports relatifs interdisent `main.py` comme point d'entrée). `pz-duel.spec` : `--onefile`, `console=False`, UPX désactivé, données `ui/`, `mods/duelcore/`, `service/presets/`, `runtime.json`, `effects.json`. `requirements-build.txt` : `pyinstaller==6.22.3` (dernière version publiée au moment du plan). `smoke_test.py` (stdlib et `mod_fingerprint` du dépôt) : prend la commande à lancer en argument, pose un `LOCALAPPDATA` temporaire, démarre avec `--no-browser` et un port dédié, vérifie `/` 200 avec l'UI, `/api/state` en `real`, 4 presets, empreinte embarquée via `/api/setup`, puis `POST /api/quit` et code 0 en moins de 30 s. Second passage : `taskkill /F` du seul processus parent, et constat qu'aucun enfant du binaire ne subsiste sous 5 s. Couvre AC46, AC48.
- **Dependencies:** [P10, P14]
- **Tests:** AC46 sans binaire : `tests/test_frozen.py` lance `smoke_test.py` sur un dossier simulé sans `ui/` et constate l'échec, puis sur un dossier complet et constate le succès. AC48 : `tests/test_stdlib_only.py` inchangé et vert, `pyinstaller` absent de `service/`, `==` présent.
- **Risks:** Non triviale. Un `--onefile` a **deux** processus : `taskkill /F` sur le chargeur peut laisser vivre le Python enfant. R2 dit « quelle que soit la façon » et AC10 ne teste que depuis les sources : d'où le second passage. Si l'enfant survit, ajouter dans le service une attente du handle du parent en mode figé. Sortie vers `$RUNNER_TEMP`, pour ne pas toucher `.gitignore`. PyInstaller doit porter la version de Python choisie en P16. `CREATE_BREAKAWAY_FROM_JOB` peut être refusé si le chargeur pose son propre Job : le client de l'hôte mourrait avec le service.

### P16: Construction en CI et release (R8)
- **Files:** [.github/workflows/tests.yml, .github/workflows/release.yml, tests/test_ci_workflows.py]
- **Description:** `tests.yml` : job Windows qui installe `packaging/requirements-build.txt`, construit et lance `packaging/smoke_test.py` sur chaque pull request, avec `autocrlf` désactivé. `release.yml` : sur `v*`, même construction, test de fumée, puis fichier `.sha256`, puis une étape de publication qui dépend du succès du test de fumée. Cette étape crée elle-même la destination : aucune release n'est supposée exister. Elle tourne en `shell: bash` avec `GH_TOKEN: ${{ github.token }}` et `permissions: contents: write` sur ce seul job. Elle fait `gh release view "$GITHUB_REF_NAME"` et, si la release manque, `gh release create "$GITHUB_REF_NAME" --verify-tag --title "$GITHUB_REF_NAME" --notes <texte fixe>`, avec `--prerelease` quand le tag contient un `-`. Elle attache ensuite les 2 fichiers par `gh release upload "$GITHUB_REF_NAME" <exe> <sha256> --clobber`. Une release préparée à la main est réutilisée, et relancer le job après un envoi interrompu remplace les pièces au lieu d'échouer. Aucune signature, aucun installeur, aucun Workshop. Python 3.13 pour la construction. Couvre AC45, AC47.
- **Dependencies:** [P7, P15]
- **Tests:** `tests/test_ci_workflows.py`, par lecture textuelle (pas de `yaml` dans la stdlib, et la suite n'ajoute que `pytest`) : AC45 (`pull_request`, `windows-latest` en 3.12, 3.13, 3.14, job de build et de fumée, 3 jobs Linux, `fetch-depth: 0`, `autocrlf`) ; AC47 (tag `v*`, ordre et dépendance, absence de `signtool`, d'installeur, de Workshop). Le test vérifie aussi que `gh release create` et `gh release upload` figurent tous deux dans `release.yml`, après le test de fumée, création avant envoi, avec `--verify-tag` et `--clobber`.
- **Risks:** Un parseur textuel peut être trompé par un commentaire : ignorer les lignes commentées. Une étape ordonnée mais marquée `if: always()` romprait la dépendance : le test l'interdit. Sans `--verify-tag`, `gh release create` pourrait créer un tag sur la branche par défaut. Si l'envoi échoue après la création, une release sans pièce jointe reste visible jusqu'à la relance du job : le job est rouge, et la relance la complète. Le shell par défaut de `windows-latest` est PowerShell, d'où `shell: bash`.

### P17: Documentation (R9)
- **Files:** [README.md, README.fr.md, docs/campaigns/phase2/limites-connues.md, docs/campaigns/phase2/limites-connues.fr.md, tests/test_ci_workflows.py]
- **Description:** Section de démarrage en anglais et en français : téléchargement, SmartScreen sur le `.exe` et le `.bat`, invite du pare-feu (H5), ports 17271 et 17272 en UDP, port de partage en TCP, interdiction de diffuser le `.bat`, revanche et duel neuf (renvoyer le `.bat`). Aucune commande de terminal dans le parcours. `limites-connues` : au moins les 5 points d'AC51 (8 arguments de `StartServer64.bat`, `-Dzomboid.steam` face à `-nosteam`, fin de script, connexion réelle d'un invité, joignabilité depuis Internet), plus ce que P5, P11 et P15 auront relevé. Chaque preuve citée renvoie à un test ou à une exécution de CI. Couvre AC50, AC51, AC61.
- **Dependencies:** [P16]
- **Tests:** Contrôle statique des termes d'AC50, d'AC51 et d'AC61 dans les 4 fichiers, placé dans `tests/test_ci_workflows.py`. Relecture de l'absence de commande de terminal dans la section joueur.
- **Risks:** Une preuve citée sans test derrière serait une fausse assurance : chaque renvoi doit désigner un nom qui existe. Les URL d'AC49 n'existent qu'après P18.

### P18: Exécutions réelles de CI et de release (R8, R10)
- **Files:** [docs/campaigns/phase2/limites-connues.md, docs/campaigns/phase2/limites-connues.fr.md]
- **Description:** Exécution complète de `tests.yml` sur la PR brouillon, Linux et Windows. Avec l'accord explicite de l'opérateur (action externe, pièce jointe publique), pousse un tag de test en préversion, par exemple `v0.0.0-phase2-test`, pour une exécution de `release.yml`. Le tag porte sur un commit qui contient `release.yml` : un déclenchement par tag lit le workflow de ce commit, sans attendre la fusion sur `master`. Aucune release n'est créée à la main : c'est `release.yml` qui doit la créer (P16), et l'exécution le prouve en partant d'un tag sans release. Consigne les URL dans `limites-connues`. Vérifie le `.exe` attaché et son `.sha256`. Couvre AC49.
- **Dependencies:** [P17]
- **Tests:** Les deux exécutions sont vertes ; avant le tag, `gh release view` échoue ; après, la release existe en préversion avec exactement 2 pièces jointes ; empreinte du `.exe` téléchargé égale au `.sha256`. Le commit des URL relance `tests.yml` : ce dernier passage doit aussi être vert.
- **Risks:** Un rouge ici renvoie à l'étape concernée, sans contournement. Une release publique reste visible après coup : préversion, et suppression à la décision de l'opérateur.

### P19: Revue de toute la branche avant livraison (R10)
- **Files:** [tests/test_platform_windows.py, docs/campaigns/phase2/limites-connues.md]
- **Description:** Porte obligatoire avant de sortir la PR de brouillon : une revue de tous les fichiers changés ensemble, `git diff 1d854d0...HEAD`, et non commit par commit. Une revue par commit rate par construction les bogues inter-fichiers : chaque commit peut être juste seul et cassé dans l'ensemble (décomposition en composants ≠ intégration). Ici, les interactions à risque sont : lobby → écouteur → `.bat` → mot de passe du `.ini` ; Job × chargeur PyInstaller × client détaché ; `paths` × tous les écrivains ; `SO_EXCLUSIVEADDRUSE` × préflight × relance après duel ; `allow_reuse_address` × écouteur × revanche. La revue vérifie aussi les 11 raccourcis de faux achèvement, que `git diff --name-only 1d854d0` ne nomme aucun fichier de code, de test, de CI ou de doc joueur hors `targets` (`tests/conftest.py` compris), et que chaque AC a un test qui s'exécute quelque part. Ajoute à `tests/test_platform_windows.py` le contrôle d'AC55 et d'AC17 (`git diff --name-only 1d854d0` ne contient ni `service/protocol.py`, ni `service/journal.py`, ni `service/presets/`, ni `mods/duelcore/` ; hunks de `service/rules.py` limités aux deux chemins). Couvre AC17, AC52, AC53, AC54, AC55, R10.
- **Dependencies:** [P18]
- **Tests:** AC52 mesuré : `pytest` sous Linux, 0 échec, exactement 5 skips, ≥ 1219 collectés. AC54 et AC62 relus dans le dernier job Windows. `git diff 1d854d0 -- tests/` relu pour toute assertion retirée ou affaiblie (AC53, partie non mécanisable). Une mutation par garde de sécurité : jeton accepté faux, secret journalisé, sortie de lobby sans fermeture ; chacune doit faire échouer un test.
- **Risks:** Une correction issue de la revue relance P18 pour les fichiers touchés. Tout fichier hors `targets` dans le diff est un constat bloquant : il sort du diff, ou l'opérateur amende la spec.

## Table de couverture

| Exigence | Étapes | Critères |
|----------|--------|----------|
| R1 | P2, P4 | AC1–AC6 |
| R2 | P5 | AC7–AC12 |
| R3 | P2, P3 | AC13–AC17 |
| R4 | P1, P8, P15 | AC18–AC22, AC56 |
| R5 | P9, P10, P14 | AC23–AC29 |
| R6 | P11, P12 | AC30–AC36 |
| R7 | P12, P13, P14 | AC37–AC44, AC57–AC60 |
| R8 | P7, P15, P16, P18 | AC45–AC49 |
| R9 | P17 | AC50, AC51, AC61 |
| R10 | P6, P7, P19 | AC52–AC55, AC62 |

## Caller enumeration

Relevé par `grep -rnE` sur `service/` et `tests/` à `8aa7335`, en complément de la table de la spec, qui
reste valable. Angle mort : `getattr` et doubles injectés (`client_launcher`, `make_engine`). Aucun
consommateur externe connu. Tout paramètre ajouté a un défaut qui reproduit le comportement Linux actuel.

| File | Function/Method | Migration Note |
|------|----------------|----------------|
| service/engine.py:543 | `write_ini` | Reçoit la politique de `self.runtime` ; défaut lu dans `runtime.json` (P3) |
| service/servers.py:118, :144 | `_replace_file` | Passe par `replace_file` ; `fchmod` si `posix_modes` (P3) |
| service/engine.py:1568 | `HookProofs(...)` | Paramètre de politique avec défaut (P3) |
| tests/test_proofs.py | `HookProofs(...)` | Appels inchangés |
| service/engine.py, tests/test_transport.py | `CommandChannel(...)` | Paramètre de politique avec défaut ; test inchangé (P3) |
| tests/test_preflight.py, tests/test_integration_pz.py | `detect_pz`, `run_preflight` | `platform`, `steam_locator`, `ram_reader` facultatifs (P4) |
| service/engine.py:1745, :1824 | `detect_pz`, `run_preflight` | Plateforme courante par défaut ; `self.pz_dir` modifiable sous verrou (P4, P9) |
| service/servers.py:268 | `launch_command` | Script et séparateur de la plateforme de `cfg`, Linux par défaut (P5) |
| tests/test_servers.py, tests/test_secrets.py | `launch_command`, `ServerProcess` | Appels inchangés ; assertions POSIX paramétrées (P5, P6) |
| tests/test_servers.py:917 | `os.killpg` remplacé par le test | L'arbre POSIX appelle `os.killpg` par le module (P5) |
| service/engine.py:1571 | `launch_client`, `client_launcher` injecté | Signature `(script, args)` inchangée (P5) |
| service/engine.py:1929, :2047 | `local_ip` | Appel unique à l'ouverture du lobby, par `_build_invite` (P12) |
| tests/test_modes.py:156, tests/test_snapshots.py:688, tests/test_match.py:335, :692, :2681 | `state["invite"]` | `port`, `password`, `code` gardés ; champs ajoutés sans `host_password` (P12) |
| service/main.py:102 | `stop_on_signal` | Rend aussi la fonction d'arrêt (P8) |
| tests/doubles/run_service.py | `main(argv, make_engine)` | Inchangé : `browser` a un défaut (P8) |
| tests/test_api_security.py, tests/test_match.py, tests/test_secrets.py | `make_server` | Signature inchangée ; `on_quit` est un attribut (P10) |
| tests/test_preflight.py et appelants de `PreflightResult` | `PreflightResult(...)` | `defects` a un défaut vide (P9) |

## Ordering rationale

Les fondations passent d'abord : `paths` (P1) et `platforms` (P2) ne dépendent de rien, et tout le reste
s'y branche. Les fichiers (P3) et le préflight (P4) précèdent les processus (P5), car un serveur ne
démarre sans eux ni sous Windows ni dans ses tests.

P6 et P7 arrivent avant les fonctions nouvelles, et c'est le choix qui compte le plus dans ce plan. Rien
de ce qui touche au vrai Windows ne s'exécute sous Linux : tant que le job Windows n'est pas vert, chaque
étape P3 à P5 reste écrite mais non prouvée. Le paramétrage (P6) doit précéder ce job (P7), sinon il
serait rouge pour de mauvaises raisons. Dès lors, P8 à P16 ont un retour réel à chaque poussée.

P8 précède P9 et P10, car la sortie réutilise l'arrêt de `main`. Le générateur (P11) précède le modèle
d'invitation (P12), qui l'appelle ; lequel précède l'écouteur (P13), qui ne sert que ce modèle. L'UI (P14)
vient après toutes les routes qu'elle appelle. L'empaquetage (P15) a besoin de `POST /api/quit` et de l'UI
pour son test de fumée, et la CI de construction (P16) a besoin de l'empaquetage. La documentation (P17)
décrit ce qui existe. P18 consigne des exécutions réelles, donc après tout le code. La revue de branche
(P19) ferme la marche : elle juge l'ensemble, URL de preuve comprises, avant que la PR sorte du brouillon.

Deux étapes qui écrivent le même fichier sont toujours ordonnées par leurs dépendances, directement ou
transitivement : le graphe ne laisse aucune écriture concurrente, et l'ordre d'intégration est celui des
numéros. Les fichiers les plus partagés suivent ces chaînes :

| Fichier | Ordre d'intégration |
|---------|---------------------|
| service/engine.py | P1 → P3 → P5 → P7 → P9 → P12 → P13 |
| service/platforms.py | P2 → P5 → P7 → P8 |
| service/api.py | P1 → P8 → P9 → P10 → P12 |
| tests/test_platform_windows.py | P2 → P3 → P4 → P5 → P6 → P7 → P19 |
| tests/test_api_security.py | P9 → P10 → P12 → P13 |

P4 attend P3 pour `tests/test_platform_windows.py`. P8 attend P5 pour `service/platforms.py`, et P7 parce
que ce job peut corriger tous les fichiers de P1 à P6. P9 attend P3 et P5 pour `service/engine.py`. P12
attend P10 pour `service/api.py` et `tests/test_api_security.py`.
