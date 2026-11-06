# P10 — Sortie par l'UI (R5)

commit: feat(phase2): P10 — Sortie par l'UI (R5)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/api.py, service/main.py, tests/test_setup_flow.py, tests/test_api_security.py]
- **Description:** `POST /api/quit`, derrière les gardes : la réponse 200 part, puis la fonction d'arrêt de P8 passe sur son fil. `make_server` garde sa signature : `main` pose `httpd.on_quit`, et sans lui la route rend 409. Sortie par `engine.shutdown()` puis `httpd.shutdown()`, comme un signal. Couvre AC27 (route de sortie), AC28.
- **Dependencies:** [P8, P9]
- **Tests:** AC28 : match réel double, 2 serveurs arrêtés, `main()` rend 0 en moins de `stop_timeout_s` + `kill_grace_s`, journal fermé. AC27 : 403 sur la sortie. Second appel idempotent. Windows réel : AC11 par cette voie.
- **Risks:** Couper avant l'envoi de la réponse ferait croire à l'UI à une panne. `engine.shutdown()` depuis le fil du duel est interdit par son contrat : la route passe par le fil dédié. `STOP_INCOMPLETE` doit garder son code non nul.

## Critères d'acceptation couverts (verbatim de la spec)

- AC11 (R2): Sur `windows-latest`, un arrêt demandé par la sortie normale passe d'abord par RCON quand le double y répond : `stop_stage` vaut `rcon`.
- AC27 (R5): Les routes d'installation, de choix de dossier et de sortie répondent 403 avec un `Host` ou un `Origin` étranger, et les deux premières répondent 409 hors de la phase `idle`, sans rien écrire.
- AC28 (R5): Une requête de sortie pendant un match réel double fait passer le service par l'arrêt complet : les 2 serveurs doubles sont arrêtés, `main()` rend la main avec le code 0 en moins de `stop_timeout_s` + `kill_grace_s` et le journal du match est fermé.

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
- Un seul commit, sujet : `feat(phase2): P10 — Sortie par l'UI (R5)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
