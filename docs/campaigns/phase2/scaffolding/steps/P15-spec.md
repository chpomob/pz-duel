# P15 — Empaquetage et test de fumée (R4, R8)

commit: feat(phase2): P15 — Empaquetage et test de fumée (R4, R8)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [packaging/pz_duel_entry.py, packaging/pz-duel.spec, packaging/requirements-build.txt, packaging/smoke_test.py, tests/test_frozen.py]
- **Description:** `pz_duel_entry.py` importe `service.main` en absolu (les imports relatifs interdisent `main.py` comme point d'entrée). `pz-duel.spec` : `--onefile`, `console=False`, UPX désactivé, données `ui/`, `mods/duelcore/`, `service/presets/`, `runtime.json`, `effects.json`. `requirements-build.txt` : `pyinstaller==6.22.3` (dernière version publiée au moment du plan). `smoke_test.py` (stdlib et `mod_fingerprint` du dépôt) : prend la commande à lancer en argument, pose un `LOCALAPPDATA` temporaire, démarre avec `--no-browser` et un port dédié, vérifie `/` 200 avec l'UI, `/api/state` en `real`, 4 presets, empreinte embarquée via `/api/setup`, puis `POST /api/quit` et code 0 en moins de 30 s. Second passage : `taskkill /F` du seul processus parent, et constat qu'aucun enfant du binaire ne subsiste sous 5 s. Couvre AC46, AC48.
- **Dependencies:** [P10, P14]
- **Tests:** AC46 sans binaire : `tests/test_frozen.py` lance `smoke_test.py` sur un dossier simulé sans `ui/` et constate l'échec, puis sur un dossier complet et constate le succès. AC48 : `tests/test_stdlib_only.py` inchangé et vert, `pyinstaller` absent de `service/`, `==` présent.
- **Risks:** Non triviale. Un `--onefile` a **deux** processus : `taskkill /F` sur le chargeur peut laisser vivre le Python enfant. R2 dit « quelle que soit la façon » et AC10 ne teste que depuis les sources : d'où le second passage. Si l'enfant survit, ajouter dans le service une attente du handle du parent en mode figé. Sortie vers `$RUNNER_TEMP`, pour ne pas toucher `.gitignore`. PyInstaller doit porter la version de Python choisie en P16. `CREATE_BREAKAWAY_FROM_JOB` peut être refusé si le chargeur pose son propre Job : le client de l'hôte mourrait avec le service.

## Critères d'acceptation couverts (verbatim de la spec)

- AC46 (R8): `packaging/smoke_test.py`, lancé sur le `.exe`, le démarre avec `--no-browser`, vérifie que `/` rend 200 avec l'UI, que `/api/state` rend 200 en mode `real`, que les 4 presets sont listés, que l'empreinte du mod embarqué égale `mod_fingerprint` de `mods/duelcore`, puis demande la sortie et vérifie un code 0 en moins de 30 s. Il échoue sur un binaire construit sans `ui/` (vérifié par une construction volontairement incomplète ou un dossier simulé).
- AC48 (R8): `tests/test_stdlib_only.py` passe sans modification, `pyinstaller` n'apparaît pas dans `service/`, et `packaging/requirements-build.txt` porte une version exacte (`==`).

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
- Un seul commit, sujet : `feat(phase2): P15 — Empaquetage et test de fumée (R4, R8)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
