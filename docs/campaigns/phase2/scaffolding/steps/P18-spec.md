# P18 — Exécutions réelles de CI et de release (R8, R10)

commit: feat(phase2): P18 — Exécutions réelles de CI et de release (R8, R10)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [docs/campaigns/phase2/limites-connues.md, docs/campaigns/phase2/limites-connues.fr.md]
- **Description:** Exécution complète de `tests.yml` sur la PR brouillon, Linux et Windows. Avec l'accord explicite de l'opérateur (action externe, pièce jointe publique), pousse un tag de test en préversion, par exemple `v0.0.0-phase2-test`, pour une exécution de `release.yml`. Le tag porte sur un commit qui contient `release.yml` : un déclenchement par tag lit le workflow de ce commit, sans attendre la fusion sur `master`. Aucune release n'est créée à la main : c'est `release.yml` qui doit la créer (P16), et l'exécution le prouve en partant d'un tag sans release. Consigne les URL dans `limites-connues`. Vérifie le `.exe` attaché et son `.sha256`. Couvre AC49.
- **Dependencies:** [P17]
- **Tests:** Les deux exécutions sont vertes ; avant le tag, `gh release view` échoue ; après, la release existe en préversion avec exactement 2 pièces jointes ; empreinte du `.exe` téléchargé égale au `.sha256`. Le commit des URL relance `tests.yml` : ce dernier passage doit aussi être vert.
- **Risks:** Un rouge ici renvoie à l'étape concernée, sans contournement. Une release publique reste visible après coup : préversion, et suppression à la décision de l'opérateur.

## Critères d'acceptation couverts (verbatim de la spec)

(aucun AC nommé — l'étape est un socle ; respecte sa Description et ses Tests)

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
- Un seul commit, sujet : `feat(phase2): P18 — Exécutions réelles de CI et de release (R8, R10)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
