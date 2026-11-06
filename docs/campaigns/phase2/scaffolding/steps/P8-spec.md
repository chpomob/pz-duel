# P8 — Démarrage du binaire (R4)

commit: feat(phase2): P8 — Démarrage du binaire (R4)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/main.py, service/api.py, service/platforms.py, tests/test_frozen.py]
- **Description:** `--mode` sans défaut fixe : `real` si `paths.is_frozen()`, `mock` sinon. `main(argv, make_engine, *, browser=None)` : le paramètre ajouté a un défaut, `tests/doubles/run_service.py` reste valide. Log de démarrage dans `paths.log_file()` en mode figé ; `print` sûr avec `stdout` et `stderr` à `None` ou cp1252 strict ; toute erreur de démarrage laisse une ligne et un code non nul, `sys.exit(message)` compris, dont le texte serait perdu sans console. Ouverture du navigateur par `open_interface(url, browser)` : un retour faux ou une exception laissent le service actif et consignent l'URL. `STOP_SIGNALS` selon la plateforme (`SIGINT`, `SIGBREAK` sous Windows). `stop_on_signal` rend la fonction d'arrêt idempotente que P10 réutilise. Instance unique, en mode figé seulement (H3) : sonde des 20 ports de `free_port` (`GET /api/state` avec en-tête `Server: PZduel/` et JSON reconnu), ouverture et code 0 si trouvé ; sinon verrou `instance.lock` du dossier de données, tenu toute la vie du processus (`msvcrt.locking` ou `fcntl.flock`, importés localement) ; un perdant attend `instance_wait_s` que le gagnant réponde. `BoundedHTTPServer` pose `allow_reuse_address = False` et `SO_EXCLUSIVEADDRUSE` sous Windows : `HTTPServer` active `SO_REUSEADDR`, le piège de `_bind` existe donc aussi sur l'API. Couvre AC20, AC21, AC22, AC56.
- **Dependencies:** [P1, P2, P5, P7]
- **Tests:** `tests/test_frozen.py` : AC20 (les 3 cas) ; AC21 (flux `None`, cp1252 strict, erreur injectée) ; AC56 (navigateur injecté : 1 appel et `GET` 200 ; `--no-browser` : 0 ; faux ou exception : `/api/state` 200 et URL dans le log) ; AC22 (service actif : 1 appel, code 0, aucun `Engine` ; port occupé par un serveur étranger : port suivant). Depuis les sources, `mock`, pas de verrou, messages actuels.
- **Risks:** Deux double-clics simultanés : le verrou tranche, là où la sonde seule laisserait passer deux services. `signal.signal` lève hors du fil principal : les tests en sous-processus l'évitent. Un fichier de log contenant un secret serait une fuite : seuls URL et messages expurgés y vont.

## Critères d'acceptation couverts (verbatim de la spec)

- AC20 (R4): En mode figé simulé, `main()` sans `--mode` construit l'`Engine` en `real`, avec `--mode mock` en `mock` ; depuis les sources, sans `--mode`, il le construit en `mock`.
- AC21 (R4): `main()` démarre et sert `/api/state` avec `sys.stdout` et `sys.stderr` à `None`, puis avec une sortie cp1252 stricte, sans exception. Une erreur de démarrage injectée laisse 1 ligne au moins dans le fichier de log du dossier de données et un code de sortie non nul.
- AC22 (R4): En mode figé simulé, un second `main()` avec un service actif sur le port par défaut ouvre l'URL de ce service (navigateur injecté, 1 appel), sort avec le code 0 et ne construit aucun `Engine` ; un port occupé par autre chose qu'un service PZ Duel conduit au port suivant, comme aujourd'hui.
- AC56 (R4): En mode figé simulé, un premier `main()` sans `--no-browser` appelle le navigateur injecté exactement 1 fois avec `http://127.0.0.1:<port>/`, et un `GET` de cette URL rend 200 ; avec `--no-browser`, il l'appelle 0 fois. Avec un navigateur injecté qui rend faux ou lève une exception, `/api/state` rend toujours 200 et le fichier de log contient cette URL au moins 1 fois.

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
- Un seul commit, sujet : `feat(phase2): P8 — Démarrage du binaire (R4)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
