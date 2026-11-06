# P17 — Documentation (R9)

commit: feat(phase2): P17 — Documentation (R9)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [README.md, README.fr.md, docs/campaigns/phase2/limites-connues.md, docs/campaigns/phase2/limites-connues.fr.md, tests/test_ci_workflows.py]
- **Description:** Section de démarrage en anglais et en français : téléchargement, SmartScreen sur le `.exe` et le `.bat`, invite du pare-feu (H5), ports 17271 et 17272 en UDP, port de partage en TCP, interdiction de diffuser le `.bat`, revanche et duel neuf (renvoyer le `.bat`). Aucune commande de terminal dans le parcours. `limites-connues` : au moins les 5 points d'AC51 (8 arguments de `StartServer64.bat`, `-Dzomboid.steam` face à `-nosteam`, fin de script, connexion réelle d'un invité, joignabilité depuis Internet), plus ce que P5, P11 et P15 auront relevé. Chaque preuve citée renvoie à un test ou à une exécution de CI. Couvre AC50, AC51, AC61.
- **Dependencies:** [P16]
- **Tests:** Contrôle statique des termes d'AC50, d'AC51 et d'AC61 dans les 4 fichiers, placé dans `tests/test_ci_workflows.py`. Relecture de l'absence de commande de terminal dans la section joueur.
- **Risks:** Une preuve citée sans test derrière serait une fausse assurance : chaque renvoi doit désigner un nom qui existe. Les URL d'AC49 n'existent qu'après P18.

## Critères d'acceptation couverts (verbatim de la spec)

- AC50 (R9): `README.md` et `README.fr.md` contiennent chacun une section de démarrage qui nomme SmartScreen, le pare-feu Windows, les ports 17271 et 17272 en UDP, le port de partage et l'interdiction de diffuser le `.bat`, et aucune commande de terminal dans le parcours du joueur.
- AC51 (R9): `limites-connues.md` et sa version française listent comme non prouvés sur le vrai jeu au moins : la transmission par `StartServer64.bat` de ses 8 arguments, sa valeur de `-Dzomboid.steam` face à un client `-nosteam`, son comportement de fin de script, la connexion effective d'un invité, et la joignabilité depuis Internet d'une adresse saisie, que le service ne teste pas. Chaque preuve citée renvoie à un test ou à une exécution de CI.
- AC61 (R9): `README.md` et `README.fr.md` disent chacun, dans la section de démarrage, qu'une revanche garde valides les `.bat` déjà envoyés et qu'un nouveau duel peut les périmer, auquel cas il faut renvoyer le `.bat`.

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
- Un seul commit, sujet : `feat(phase2): P17 — Documentation (R9)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
