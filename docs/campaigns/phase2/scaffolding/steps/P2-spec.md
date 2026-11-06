# P2 — Module `platforms` et remplacement atomique tolérant (R1, R3)

commit: feat(phase2): P2 — Module `platforms` et remplacement atomique tolérant (R1, R3)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/platforms.py, service/runtime.json, tests/test_platform_windows.py]
- **Description:** Crée `Platform` (D-B) : script serveur, programme client, client dans le dossier (`Windows`) ou dans le parent (`Linux`), sous-dossier du jeu, `join()`, `posix_modes`, signaux d'arrêt. Ajoute à `runtime.json` : `share_port`, `replace_timeout_s`, `replace_retry_ms`, `instance_wait_s`. Ajoute `replace_file(src, dst, *, policy=None, platform=None, sleep=time.sleep, clock=time.monotonic)` : sous Windows, réessaie sur `PermissionError` jusqu'à l'échéance, puis lève une erreur explicite qui nomme le fichier, sans jamais effacer `dst`. Sous Linux, c'est `os.replace` nu. Sans `policy`, il lit `load_runtime()` par un import local. Le nettoyage du temporaire reste à l'appelant, qui le possède. Couvre AC6, la partie Linux d'AC15.
- **Dependencies:** [P1]
- **Tests:** AC6 (énumération de `service/` à l'exécution par `pkgutil`, import de tous les modules dans un sous-processus, `winreg`, `msvcrt`, `_winapi` absents de `sys.modules`). AC15 partie Linux : plateforme et `PermissionError` injectées, horloge factice, tentatives bornées, durée totale ≤ délai configuré + 10 %.
- **Risks:** Une nouvelle clé de `runtime.json` peut heurter un test qui fige ce fichier : la suite tranche. `replace_retry_ms` trop grand ferait dépasser les 10 % : la dernière attente est tronquée à l'échéance.

## Critères d'acceptation couverts (verbatim de la spec)

- AC6 (R1): Un test de `tests/test_platform_windows.py` lancé sous Linux énumère à l'exécution les modules de `service/`, les importe tous dans un sous-processus et vérifie que `winreg`, `msvcrt` et `_winapi` sont absents de `sys.modules`.
- AC15 (R3): Sur `windows-latest`, une cible tenue au-delà du délai configuré dans `runtime.json` produit une erreur explicite, l'ancien contenu reste intact et 0 fichier temporaire reste dans le dossier. Sous Linux, plateforme et échec injectés, le nombre de tentatives est borné et le délai total ne dépasse pas la valeur configurée de plus de 10 %.

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
- Un seul commit, sujet : `feat(phase2): P2 — Module `platforms` et remplacement atomique tolérant (R1, R3)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
