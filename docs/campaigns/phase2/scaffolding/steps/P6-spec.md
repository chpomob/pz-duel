# P6 — Paramétrage des tests POSIX (R10)

commit: feat(phase2): P6 — Paramétrage des tests POSIX (R10)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [tests/doubles/fake_server.py, tests/test_servers.py, tests/test_secrets.py, tests/test_preflight.py, tests/test_snapshots.py, tests/test_contract.py, tests/test_modes.py, tests/test_campaign_gate.py, tests/test_platform_windows.py]
- **Description:** Paramètre par plateforme les tests à `os.killpg`, `signal.SIGKILL`, `#!/bin/sh` et `0600`. Surcharge `zomboid_home` dans `test_contract`, `test_preflight`, `test_snapshots` et `test_modes` pour rediriger aussi `USERPROFILE` (voir « Respect de `targets` ») ; le sous-processus de `tests/test_preflight.py:289` reçoit la même variable. Helpers dans `fake_server.py` : `write_script(path, sh_body, bat_body)`, `kill_tree(server)` (`os.killpg` sous Linux, arrêt par `service/platforms.py` sous Windows). `crash()` de `test_contract`, `client_install` et `write_script` de `test_modes`, faux interpréteur de `test_campaign_gate`. Quand aucun équivalent n'a de sens, `skipif` Windows dont la raison nomme le test qui le remplace. Sous Linux, aucun de ces tests n'est sauté et `os.killpg` reste. Le double de `ProjectZomboid64.exe` doit être un vrai PE : il est compilé dans une fixture par `csc.exe` du .NET Framework, présent sur `windows-latest`, et son absence est une erreur, pas un skip. `tests/test_platform_windows.py` mesure AC53 (extraction de `1d854d0` par `git archive`, `pytest --collect-only` des deux arbres, noms sans identifiants de paramètres ; échec si le commit est inaccessible) et AC62 (noms cités par les raisons, par `ast`, D-A). `test_secrets.py` : assertion `0600` limitée à POSIX, équivalent Windows, et préparation du balayage complété en P11 et P13. Couvre AC53, AC54, AC62.
- **Dependencies:** [P5]
- **Tests:** AC52 : sous Linux, 0 échec, exactement 5 skips, ≥ 1219 collectés. AC53 et AC62 exécutés par `tests/test_platform_windows.py`. Mutation à la main : retirer un test et constater l'échec d'AC53 ; citer un nom inexistant et constater l'échec d'AC62. `git diff --name-only 1d854d0 -- tests/conftest.py` reste vide.
- **Risks:** Fake-done n° 1 à éviter : un `assert exit_code == -signal.SIGTERM` déplacé ne doit pas s'affaiblir sous Linux ; l'assertion reste, et seule une variante Windows s'ajoute. `signal.SIGKILL` évalué au niveau module casserait la collecte Windows : toute référence entre dans le corps des fonctions. Un test de ces 4 modules qui résoudrait le dossier personnel sans passer par `zomboid_home` viserait le vrai profil du runner : le job Windows de P7 le montrerait, et la correction reste dans le module.

## Critères d'acceptation couverts (verbatim de la spec)

- AC52 (R10): Sous Linux, depuis les sources, `pytest` rend 0 échec et exactement 5 skips, avec au moins 1219 tests collectés.
- AC53 (R10): Chaque fonction de test présente à `1d854d0` existe encore, même paramétrée (comparaison des noms par `pytest --collect-only`, identifiants de paramètres retirés, faite à l'exécution par `tests/test_platform_windows.py`, qui échoue si ce commit est inaccessible), et aucune assertion existante n'est retirée ou affaiblie sous Linux.
- AC62 (R10): Sur `windows-latest`, chaque test de `tests/test_snapshots.py`, `tests/test_contract.py`, `tests/test_modes.py` et `tests/test_campaign_gate.py` qui passe sous Linux par `os.killpg`, `signal.SIGKILL` ou un script `#!/bin/sh` s'exécute par un équivalent Windows (arrêt de l'arbre par `service/platforms.py`, double en `.bat`), ou bien est en `skip` avec pour raison le nom du test Windows qui le remplace. Ces 4 fichiers rendent 0 échec dans ce job, et `tests/test_platform_windows.py` vérifie que chaque nom cité par une telle raison désigne un test existant. Sous Linux, ces tests s'exécutent sans `skip` et gardent `os.killpg`.

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
- Un seul commit, sujet : `feat(phase2): P6 — Paramétrage des tests POSIX (R10)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
