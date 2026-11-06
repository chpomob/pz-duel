# P16 — Construction en CI et release (R8)

commit: feat(phase2): P16 — Construction en CI et release (R8)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [.github/workflows/tests.yml, .github/workflows/release.yml, tests/test_ci_workflows.py]
- **Description:** `tests.yml` : job Windows qui installe `packaging/requirements-build.txt`, construit et lance `packaging/smoke_test.py` sur chaque pull request, avec `autocrlf` désactivé. `release.yml` : sur `v*`, même construction, test de fumée, puis fichier `.sha256`, puis une étape de publication qui dépend du succès du test de fumée. Cette étape crée elle-même la destination : aucune release n'est supposée exister. Elle tourne en `shell: bash` avec `GH_TOKEN: ${{ github.token }}` et `permissions: contents: write` sur ce seul job. Elle fait `gh release view "$GITHUB_REF_NAME"` et, si la release manque, `gh release create "$GITHUB_REF_NAME" --verify-tag --title "$GITHUB_REF_NAME" --notes <texte fixe>`, avec `--prerelease` quand le tag contient un `-`. Elle attache ensuite les 2 fichiers par `gh release upload "$GITHUB_REF_NAME" <exe> <sha256> --clobber`. Une release préparée à la main est réutilisée, et relancer le job après un envoi interrompu remplace les pièces au lieu d'échouer. Aucune signature, aucun installeur, aucun Workshop. Python 3.13 pour la construction. Couvre AC45, AC47.
- **Dependencies:** [P7, P15]
- **Tests:** `tests/test_ci_workflows.py`, par lecture textuelle (pas de `yaml` dans la stdlib, et la suite n'ajoute que `pytest`) : AC45 (`pull_request`, `windows-latest` en 3.12, 3.13, 3.14, job de build et de fumée, 3 jobs Linux, `fetch-depth: 0`, `autocrlf`) ; AC47 (tag `v*`, ordre et dépendance, absence de `signtool`, d'installeur, de Workshop). Le test vérifie aussi que `gh release create` et `gh release upload` figurent tous deux dans `release.yml`, après le test de fumée, création avant envoi, avec `--verify-tag` et `--clobber`.
- **Risks:** Un parseur textuel peut être trompé par un commentaire : ignorer les lignes commentées. Une étape ordonnée mais marquée `if: always()` romprait la dépendance : le test l'interdit. Sans `--verify-tag`, `gh release create` pourrait créer un tag sur la branche par défaut. Si l'envoi échoue après la création, une release sans pièce jointe reste visible jusqu'à la relance du job : le job est rouge, et la relance la complète. Le shell par défaut de `windows-latest` est PowerShell, d'où `shell: bash`.

## Critères d'acceptation couverts (verbatim de la spec)

- AC45 (R8): `tests/test_ci_workflows.py` vérifie que `tests.yml` exécute sur `pull_request` un job `windows-latest` en Python 3.12, 3.13 et 3.14 et un job qui construit puis exécute `packaging/smoke_test.py`, et que les 3 jobs Linux restent.
- AC47 (R8): `release.yml` se déclenche sur les tags `v*`, et l'étape qui attache le `.exe` et son fichier `.sha256` dépend du succès du test de fumée. Aucune étape ne signe, ne construit d'installeur et ne touche au Workshop.

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
- Un seul commit, sujet : `feat(phase2): P16 — Construction en CI et release (R8)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
