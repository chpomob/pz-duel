# P19 — Revue de toute la branche avant livraison (R10)

commit: feat(phase2): P19 — Revue de toute la branche avant livraison (R10)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [tests/test_platform_windows.py, docs/campaigns/phase2/limites-connues.md]
- **Description:** Porte obligatoire avant de sortir la PR de brouillon : une revue de tous les fichiers changés ensemble, `git diff 1d854d0...HEAD`, et non commit par commit. Une revue par commit rate par construction les bogues inter-fichiers : chaque commit peut être juste seul et cassé dans l'ensemble (décomposition en composants ≠ intégration). Ici, les interactions à risque sont : lobby → écouteur → `.bat` → mot de passe du `.ini` ; Job × chargeur PyInstaller × client détaché ; `paths` × tous les écrivains ; `SO_EXCLUSIVEADDRUSE` × préflight × relance après duel ; `allow_reuse_address` × écouteur × revanche. La revue vérifie aussi les 11 raccourcis de faux achèvement, que `git diff --name-only 1d854d0` ne nomme aucun fichier de code, de test, de CI ou de doc joueur hors `targets` (`tests/conftest.py` compris), et que chaque AC a un test qui s'exécute quelque part. Ajoute à `tests/test_platform_windows.py` le contrôle d'AC55 et d'AC17 (`git diff --name-only 1d854d0` ne contient ni `service/protocol.py`, ni `service/journal.py`, ni `service/presets/`, ni `mods/duelcore/` ; hunks de `service/rules.py` limités aux deux chemins). Couvre AC17, AC52, AC53, AC54, AC55, R10.
- **Dependencies:** [P18]
- **Tests:** AC52 mesuré : `pytest` sous Linux, 0 échec, exactement 5 skips, ≥ 1219 collectés. AC54 et AC62 relus dans le dernier job Windows. `git diff 1d854d0 -- tests/` relu pour toute assertion retirée ou affaiblie (AC53, partie non mécanisable). Une mutation par garde de sécurité : jeton accepté faux, secret journalisé, sortie de lobby sans fermeture ; chacune doit faire échouer un test.
- **Risks:** Une correction issue de la revue relance P18 pour les fichiers touchés. Tout fichier hors `targets` dans le diff est un constat bloquant : il sort du diff, ou l'opérateur amende la spec.

## Table de couverture

| Exigence | Étapes | Critères |
|----------|--------|----------|
| R1 | P2, P4 | AC1–AC6 |
| R2 | P5 | AC7–AC12 |
| R3 | P2, P3 | AC13–AC17 |
| R4 | P1, P8, P15 | AC18–AC22, AC56 |
| R5 | P9, P10, P14 | AC23–AC29 |
| R6 | P11, P12 | AC30–AC36 |
| R7 | P12, P13, P14 | AC37–AC44, AC57–AC60 |
| R8 | P7, P15, P16, P18 | AC45–AC49 |
| R9 | P17 | AC50, AC51, AC61 |
| R10 | P6, P7, P19 | AC52–AC55, AC62 |

## Caller enumeration

Relevé par `grep -rnE` sur `service/` et `tests/` à `8aa7335`, en complément de la table de la spec, qui
reste valable. Angle mort : `getattr` et doubles injectés (`client_launcher`, `make_engine`). Aucun
consommateur externe connu. Tout paramètre ajouté a un défaut qui reproduit le comportement Linux actuel.

| File | Function/Method | Migration Note |
|------|----------------|----------------|
| service/engine.py:543 | `write_ini` | Reçoit la politique de `self.runtime` ; défaut lu dans `runtime.json` (P3) |
| service/servers.py:118, :144 | `_replace_file` | Passe par `replace_file` ; `fchmod` si `posix_modes` (P3) |
| service/engine.py:1568 | `HookProofs(...)` | Paramètre de politique avec défaut (P3) |
| tests/test_proofs.py | `HookProofs(...)` | Appels inchangés |
| service/engine.py, tests/test_transport.py | `CommandChannel(...)` | Paramètre de politique avec défaut ; test inchangé (P3) |
| tests/test_preflight.py, tests/test_integration_pz.py | `detect_pz`, `run_preflight` | `platform`, `steam_locator`, `ram_reader` facultatifs (P4) |
| service/engine.py:1745, :1824 | `detect_pz`, `run_preflight` | Plateforme courante par défaut ; `self.pz_dir` modifiable sous verrou (P4, P9) |
| service/servers.py:268 | `launch_command` | Script et séparateur de la plateforme de `cfg`, Linux par défaut (P5) |
| tests/test_servers.py, tests/test_secrets.py | `launch_command`, `ServerProcess` | Appels inchangés ; assertions POSIX paramétrées (P5, P6) |
| tests/test_servers.py:917 | `os.killpg` remplacé par le test | L'arbre POSIX appelle `os.killpg` par le module (P5) |
| service/engine.py:1571 | `launch_client`, `client_launcher` injecté | Signature `(script, args)` inchangée (P5) |
| service/engine.py:1929, :2047 | `local_ip` | Appel unique à l'ouverture du lobby, par `_build_invite` (P12) |
| tests/test_modes.py:156, tests/test_snapshots.py:688, tests/test_match.py:335, :692, :2681 | `state["invite"]` | `port`, `password`, `code` gardés ; champs ajoutés sans `host_password` (P12) |
| service/main.py:102 | `stop_on_signal` | Rend aussi la fonction d'arrêt (P8) |
| tests/doubles/run_service.py | `main(argv, make_engine)` | Inchangé : `browser` a un défaut (P8) |
| tests/test_api_security.py, tests/test_match.py, tests/test_secrets.py | `make_server` | Signature inchangée ; `on_quit` est un attribut (P10) |
| tests/test_preflight.py et appelants de `PreflightResult` | `PreflightResult(...)` | `defects` a un défaut vide (P9) |

## Ordering rationale

Les fondations passent d'abord : `paths` (P1) et `platforms` (P2) ne dépendent de rien, et tout le reste
s'y branche. Les fichiers (P3) et le préflight (P4) précèdent les processus (P5), car un serveur ne
démarre sans eux ni sous Windows ni dans ses tests.

P6 et P7 arrivent avant les fonctions nouvelles, et c'est le choix qui compte le plus dans ce plan. Rien
de ce qui touche au vrai Windows ne s'exécute sous Linux : tant que le job Windows n'est pas vert, chaque
étape P3 à P5 reste écrite mais non prouvée. Le paramétrage (P6) doit précéder ce job (P7), sinon il
serait rouge pour de mauvaises raisons. Dès lors, P8 à P16 ont un retour réel à chaque poussée.

P8 précède P9 et P10, car la sortie réutilise l'arrêt de `main`. Le générateur (P11) précède le modèle
d'invitation (P12), qui l'appelle ; lequel précède l'écouteur (P13), qui ne sert que ce modèle. L'UI (P14)
vient après toutes les routes qu'elle appelle. L'empaquetage (P15) a besoin de `POST /api/quit` et de l'UI
pour son test de fumée, et la CI de construction (P16) a besoin de l'empaquetage. La documentation (P17)
décrit ce qui existe. P18 consigne des exécutions réelles, donc après tout le code. La revue de branche
(P19) ferme la marche : elle juge l'ensemble, URL de preuve comprises, avant que la PR sorte du brouillon.

Deux étapes qui écrivent le même fichier sont toujours ordonnées par leurs dépendances, directement ou
transitivement : le graphe ne laisse aucune écriture concurrente, et l'ordre d'intégration est celui des
numéros. Les fichiers les plus partagés suivent ces chaînes :

| Fichier | Ordre d'intégration |
|---------|---------------------|
| service/engine.py | P1 → P3 → P5 → P7 → P9 → P12 → P13 |
| service/platforms.py | P2 → P5 → P7 → P8 |
| service/api.py | P1 → P8 → P9 → P10 → P12 |
| tests/test_platform_windows.py | P2 → P3 → P4 → P5 → P6 → P7 → P19 |
| tests/test_api_security.py | P9 → P10 → P12 → P13 |

P4 attend P3 pour `tests/test_platform_windows.py`. P8 attend P5 pour `service/platforms.py`, et P7 parce
que ce job peut corriger tous les fichiers de P1 à P6. P9 attend P3 et P5 pour `service/engine.py`. P12
attend P10 pour `service/api.py` et `tests/test_api_security.py`.

## Critères d'acceptation couverts (verbatim de la spec)

- AC52 (R10): Sous Linux, depuis les sources, `pytest` rend 0 échec et exactement 5 skips, avec au moins 1219 tests collectés.
- AC53 (R10): Chaque fonction de test présente à `1d854d0` existe encore, même paramétrée (comparaison des noms par `pytest --collect-only`, identifiants de paramètres retirés, faite à l'exécution par `tests/test_platform_windows.py`, qui échoue si ce commit est inaccessible), et aucune assertion existante n'est retirée ou affaiblie sous Linux.
- AC54 (R10): Sur `windows-latest`, `pytest` rend 0 échec, et chaque skip propre à Windows donne pour raison le nom du test Windows équivalent, qui s'exécute dans le même job.
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
- Un seul commit, sujet : `feat(phase2): P19 — Revue de toute la branche avant livraison (R10)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
