# P1 — Résolution sources / binaire figé (R4, R10)

commit: feat(phase2): P1 — Résolution sources / binaire figé (R4, R10)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/paths.py, service/rules.py, service/api.py, service/engine.py, service/preflight.py, tests/test_frozen.py]
- **Description:** Crée `service/paths.py`, seul module qui distingue sources et binaire (R4, H3) : `is_frozen()`, `resource_root()` (`sys._MEIPASS` si figé, racine du dépôt sinon), `data_dir(platform)` (`%LOCALAPPDATA%\PZDuel` sous Windows, `$XDG_DATA_HOME/pz-duel` ou `~/.local/share/pz-duel` sous Linux figé), `matches_dir()` (`<dépôt>/matches` depuis les sources), `log_file()`. Il n'importe rien de `service` : `rules.py` en dépend. `RUNTIME_PATH`, `EFFECTS_PATH`, `UI_DIR`, `MOD_SRC`, `PRESETS_DIR`, `MATCHES_DIR` passent par lui, en gardant leurs noms. Le diff de `rules.py` se limite à ces deux lignes et à l'import (AC55). Couvre AC18, AC19.
- **Dependencies:** []
- **Tests:** `tests/test_frozen.py`, par sous-processus (D-D) : AC18 (UI, mod, 4 presets, `runtime.json`, `effects.json` lus du dossier d'extraction, 0 chemin d'écriture dedans) ; AC19 (`matches_dir` avec `LOCALAPPDATA` injecté, journal encore lisible après suppression du dossier d'extraction, `<dépôt>/matches` sans figement). Suite Linux inchangée.
- **Risks:** Une constante oubliée continue de dériver de `__file__` : `grep -n __file__ service/` doit ne laisser que `paths.py`. Import circulaire si `paths.py` importe `rules` ou `platforms` : `data_dir` reçoit donc le nom de plateforme et lit `sys.platform` lui-même.

## Critères d'acceptation couverts (verbatim de la spec)

- AC18 (R4): En mode figé simulé (`sys.frozen` vrai, dossier d'extraction injecté), l'UI, le mod, les 4 presets, `runtime.json` et `effects.json` sont lus depuis ce dossier, et 0 chemin d'écriture du service n'est à l'intérieur de celui-ci.
- AC19 (R4): En mode figé simulé, `matches_dir` vaut `<LOCALAPPDATA>\PZDuel\matches` avec plateforme Windows injectée ; sans figement, il reste `<dépôt>/matches`. Un journal écrit puis la suppression du dossier d'extraction laissent le journal lisible.

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
- Un seul commit, sujet : `feat(phase2): P1 — Résolution sources / binaire figé (R4, R10)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
