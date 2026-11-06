# P4 — Préflight sous Windows (R1)

commit: feat(phase2): P4 — Préflight sous Windows (R1)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/preflight.py, tests/test_preflight.py, tests/test_platform_windows.py]
- **Description:** `detect_pz(pz_dir, env, home, *, platform=None, steam_locator=None)` : sous Windows, ordre `--pz-dir`, `PZ_INSTALL_DIR`, dossier Steam par défaut (`ProgramFiles(x86)`), Steam enregistré (`HKCU\Software\Valve\Steam\SteamPath`, puis `HKLM\SOFTWARE\WOW6432Node\Valve\Steam\InstallPath`), puis bibliothèques de `steamapps/libraryfolders.vdf`. Parseur `.vdf` pur et tolérant : vide, tronqué, absent ou registre illisible rendent `absent` sans exception. Un dossier valide contient `StartServer64.bat` et `ProjectZomboid64.exe`, et le message nomme le fichier manquant. Un choix explicite invalide reste sans repli (D19). RAM : `run_preflight` gagne `ram_reader` ; sous Windows, `GlobalMemoryStatusEx` par `ctypes`, seuil et refus inchangés. Ports : `_bind` pose `SO_EXCLUSIVEADDRUSE` sous Windows et jamais `SO_REUSEADDR`. `check_ports` reconnaît `WSAEADDRINUSE` (10048) et `WSAEACCES` (10013), car `errno.EADDRINUSE` n'est pas le code rendu sous Windows. `install_mod` crée `zomboid_dir` s'il manque. `INSTALL_HINT` et les messages citant `--pz-dir` ou `chmod` prennent une variante sans terminal si `paths.is_frozen()`. Couvre AC1, AC2, AC3, AC4, AC5.
- **Dependencies:** [P2, P3]
- **Tests:** Sous Linux, plateforme injectée : AC1 ; AC2 (3 bibliothèques, la troisième porte le jeu ; fichier vide, tronqué, absent ; registre illisible) ; AC3 ; AC4 (6143, 6144, mesure en échec). Doubles de `tests/test_preflight.py` paramétrés par plateforme. Windows réel : AC4 (mesure réelle > 0), AC5 (TCP avec et sans `SO_REUSEADDR` chez le détenteur, UDP lié, port libre, 10013 injectée).
- **Risks:** `SO_EXCLUSIVEADDRUSE` peut signaler occupé un port en TIME_WAIT après un duel, et un faux défaut bloquerait « Launch duel » : à mesurer en P7, avec `retry` comme issue pour le joueur. Les chemins du `.vdf` portent des `\\` échappés. Le `.vdf` peut lister un disque déconnecté : `is_file` rend faux, sans exception.

## Critères d'acceptation couverts (verbatim de la spec)

- AC1 (R1): Sous Linux, plateforme Windows injectée, une arborescence factice contenant `StartServer64.bat` et `ProjectZomboid64.exe` au dossier Steam par défaut donne `présent` et ce dossier exact ; la même arborescence sans `ProjectZomboid64.exe` donne `absent`, avec un message qui nomme le fichier manquant.
- AC2 (R1): Avec un `libraryfolders.vdf` factice déclarant 3 bibliothèques dont seule la troisième contient le jeu, et un dossier par défaut vide, la détection rend le dossier de la troisième ; avec un fichier vide, tronqué ou absent, ou un registre illisible, elle rend `absent` sans lever d'exception.
- AC3 (R1): Avec `--pz-dir` ou `PZ_INSTALL_DIR` invalide et une installation valide au dossier Steam par défaut, la détection rend `absent` et ne rend pas le dossier Steam ; `--pz-dir` valide l'emporte sur `PZ_INSTALL_DIR` valide.
- AC4 (R1): Avec une mesure de RAM injectée et un seuil de 6144 Mo, 6143 Mo produit exactement 1 défaut de RAM, 6144 Mo n'en produit aucun, et une mesure en échec produit 1 défaut avec `mem_available_mb` à `None`. Sur `windows-latest`, la mesure réelle est un entier strictement positif.
- AC5 (R1): Sur `windows-latest`, un port TCP tenu par une socket à l'écoute, avec ou sans `SO_REUSEADDR` chez son détenteur, et un port UDP lié produisent chacun 1 défaut qui nomme le port et son protocole, tandis qu'un port libre n'en produit aucun. Une erreur système 10013 injectée donne un défaut « inutilisable ».

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
- Un seul commit, sujet : `feat(phase2): P4 — Préflight sous Windows (R1)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
