# P9 — État de premier lancement et routes (R5)

commit: feat(phase2): P9 — État de premier lancement et routes (R5)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/preflight.py, service/engine.py, service/api.py, tests/test_setup_flow.py, tests/test_api_security.py]
- **Description:** `PreflightResult` gagne `defects: tuple[Defect, ...]` (`kind`, `message`, `action` parmi `install_mod`, `choose_folder`, `retry`) avec un défaut vide : les constructions et `failures` ne changent pas. `Engine.setup_state()` lance `run_preflight` sans rien écrire, sur la table par défaut ; hors `idle`, il rend le dernier état sans lier de port ; en `mock`, il rend `ok: true` sans mesure, pour garder le parcours actuel (R10). Il expose aussi `expected_mod_fingerprint`, lu par le test de fumée. Routes : `GET /api/setup`, `POST /api/setup/install-mod`, `POST /api/setup/pz-dir`. Hors de `ACTIONS` (H6). `do_POST` passe d'une route à une table, avec la même chaîne de gardes (`_refused`, `Content-Type`, lecture bornée). 409 hors `idle` ou en `mock`, sans rien écrire. `Engine.pz_dir` se modifie sous `self.lock` après validation par `detect_pz` : le contrôle et le `start` suivants le voient sans redémarrage. Le choix n'est pas persisté entre deux lancements : la spec ne le demande pas. `folder` est classé dans `NOT_IDENTIFIERS`. Textes ajoutés en anglais. Couvre AC23, AC24, AC25, AC26 (partie serveur), AC27, AC29.
- **Dependencies:** [P3, P4, P5, P8]
- **Tests:** `tests/test_setup_flow.py` : AC23 (4 cas, `~/Zomboid` identique octet pour octet) ; AC24 ; AC25 ; AC26 (`start` refusé rend le même défaut) ; AC27 (403 sur `Host` et `Origin` étrangers, 409 hors `idle`, rien d'écrit) ; AC29 (`ACTIONS` à 5 noms ; en mode figé, aucun message n'a `python`, `--pz-dir`, `--install-mod`, `chmod`). `tests/test_api_security.py` : balayage des nouvelles routes.
- **Risks:** Une UI qui sonde `/api/setup` pendant un duel lierait les ports du serveur : d'où le dernier état hors `idle`. `test_every_field_read_from_the_payload_is_classified` échoue tant que le champ n'est pas classé : c'est voulu. Le préflight liant les ports sur toutes les interfaces peut faire apparaître l'invite du pare-feu au premier contrôle : à documenter en P17.

## Critères d'acceptation couverts (verbatim de la spec)

- AC23 (R5): Avec PZ absent, mod absent, RAM insuffisante ou port occupé (4 cas injectés), la route d'état rend le défaut correspondant et une action nommée parmi `install_mod`, `choose_folder`, `retry`, et les fichiers de `~/Zomboid` restent identiques octet pour octet avant et après l'appel.
- AC24 (R5): Depuis un état sans mod, la route d'installation pose `mods/duelcore` et le contrôle suivant rend une empreinte égale à `mod_fingerprint(MOD_SRC)` ; un autre dossier de `mods/` reste inchangé.
- AC25 (R5): La saisie d'un dossier valide rend PZ `présent` pour le contrôle suivant et pour le `start` suivant, sans redémarrage ; un dossier invalide est refusé et l'état précédent reste. Le chemin saisi est classé dans `tests/test_api_security.py`.
- AC26 (R5): Un `start` refusé par le préflight rend le même défaut que la route d'état, et un test de l'UI vérifie que le bouton « Launch duel » reste désactivé tant que `ok` est faux.
- AC27 (R5): Les routes d'installation, de choix de dossier et de sortie répondent 403 avec un `Host` ou un `Origin` étranger, et les deux premières répondent 409 hors de la phase `idle`, sans rien écrire.
- AC29 (R5): `ACTIONS` garde ses 5 noms, et aucun message d'échec affiché en binaire ne contient `python`, `--pz-dir`, `--install-mod` ni `chmod`.

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
- Un seul commit, sujet : `feat(phase2): P9 — État de premier lancement et routes (R5)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
