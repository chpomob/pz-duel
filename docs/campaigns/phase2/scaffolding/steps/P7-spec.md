# P7 — Suite sur `windows-latest` (R8, R3, R10)

commit: feat(phase2): P7 — Suite sur `windows-latest` (R8, R3, R10)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [.github/workflows/tests.yml, service/paths.py, service/platforms.py, service/runtime.json, service/transport.py, service/proofs.py, service/servers.py, service/engine.py, service/preflight.py, tests/doubles/fake_server.py, tests/test_frozen.py, tests/test_platform_windows.py, tests/test_servers.py, tests/test_preflight.py, tests/test_secrets.py, tests/test_snapshots.py, tests/test_contract.py, tests/test_modes.py, tests/test_campaign_gate.py]
- **Description:** Ajoute le job `windows-latest` en Python 3.12, 3.13 et 3.14 ; garde les 3 jobs Linux. `actions/checkout` en `fetch-depth: 0` sur tous les jobs (AC53). Sur Windows, `git config --global core.autocrlf false` **avant** le checkout : sinon `tests/fixtures/*.jsonl` et le mod seraient convertis en CRLF et AC17 et AC30 mesureraient autre chose. Ouvre la pull request en brouillon, avec l'accord de l'opérateur (action externe) : c'est le seul déclencheur `pull_request`, et `workflow_dispatch` n'existe pas tant que le fichier n'est pas sur `master`. Corrige ce que le job révèle pour P1 à P6, dans les fichiers listés ici : le workflow est le seul fichier prévu d'avance, les autres ne changent que si le job l'exige. Toutes les étapes suivantes dépendent de P7, pour ne pas s'écrire sur des fichiers en cours de correction. Couvre l'exécution réelle d'AC4, AC5, AC8 à AC11, AC13 à AC17, AC54, AC62.
- **Dependencies:** [P6]
- **Tests:** Job Windows vert, 0 échec, skips conformes à AC54. Les jobs Linux restent verts. `git diff --name-only 1d854d0` ne nomme aucun fichier de code ou de test hors `targets`. L'assertion statique sur ce fichier arrive en P16.
- **Risks:** C'est le premier contact avec le vrai Windows : les étapes P3 à P6 n'y ont jamais tourné, et ce job peut demander plusieurs itérations. Budget à prévoir. Si l'opérateur refuse la PR brouillon, tous les critères « sur `windows-latest` » restent non prouvés et la phase ne peut pas se clore. Un échec dans un fichier de test hors `targets`, dû au test lui-même, arrête l'étape jusqu'à une décision de l'opérateur sur la spec (voir « Respect de `targets` »).

## Critères d'acceptation couverts (verbatim de la spec)

- AC54 (R10): Sur `windows-latest`, `pytest` rend 0 échec, et chaque skip propre à Windows donne pour raison le nom du test Windows équivalent, qui s'exécute dans le même job.

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
- Un seul commit, sujet : `feat(phase2): P7 — Suite sur `windows-latest` (R8, R3, R10)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
