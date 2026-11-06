# P3 — Fichiers sous Windows (R3)

commit: feat(phase2): P3 — Fichiers sous Windows (R3)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
La phase 1 est close (1214 tests, 5 skips). La phase 2 rend le produit utilisable sous Windows
par un joueur non technique : binaire, zéro ligne de commande, `.bat` pour l'invité.

Autorités, dans cet ordre : `docs/campaigns/phase2/spec.md` (10 exigences, 62 AC) — puis
`docs/campaigns/phase2/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : Windows d'abord, Linux non régressé, `.bat` pour
l'invité, **pas de Workshop**, **pas de signature**, **pas de NAT traversal**.

## Périmètre (verbatim du plan)

- **Files:** [service/transport.py, service/proofs.py, service/servers.py, service/engine.py, tests/test_platform_windows.py, tests/test_servers.py]
- **Description:** `transport.py` : `O_BINARY` via `getattr(os, "O_BINARY", 0)` sur `in` ; `write_session` passe par `replace_file` et retire `session.tmp` en cas d'échec. `proofs.py` : `replace_file`, et retrait du temporaire, absent aujourd'hui (`_store` laisse le fichier sur `OSError`) ; le rapport par `_report` reste l'erreur explicite. `servers.py` : `_replace_file` passe par `replace_file` et ne fait `os.fchmod` que si `platform.posix_modes` ; `_secure_ini` est sans effet sous Windows et laisse `ini_failure` à `None`. L'`Engine` transmet `runtime` à ces trois écrivains pour que le délai testé soit celui d'un `runtime.json` de test. Le format du protocole et du journal ne change pas (AC17). Couvre AC13, AC14, AC15, AC16, AC17.
- **Dependencies:** [P2]
- **Tests:** Sous Linux : `.ini` toujours en `0600`, plateforme Windows injectée sans `fchmod` ni `ini_failure` (AC16). Sous Windows réel (D-A), avec un tiers tenant le fichier sans `FILE_SHARE_DELETE` : AC13 (N octets, aucun `0x0D`), AC14 (`session`, `.ini`, preuves tenus 500 ms), AC15 (tenue au-delà du délai : erreur, ancien contenu intact, 0 temporaire), AC16, AC17 (rejeu de `tests/fixtures` identique).
- **Risks:** `write_ini` a un seul appelant (`engine.py:543`) mais `_replace_file` en a deux (`servers.py:118`, `:144`) : les deux reçoivent la politique. AC17 suppose des fixtures sans conversion CRLF au checkout : voir P7. Écritures Windows non exécutées avant P7.

## Critères d'acceptation couverts (verbatim de la spec)

- AC13 (R3): Sur `windows-latest`, écrire une commande de N octets dans `in` donne un fichier de exactement N octets, sans aucun octet `0x0D`, et aucune exception d'écriture partielle.
- AC14 (R3): Sur `windows-latest`, un autre processus tenant `session` ouvert pendant 500 ms, sans partage de suppression, ne fait pas échouer `write_session` ; le fichier contient ensuite le nouveau contenu. Le test est répété pour un `.ini` et pour les preuves de hook.
- AC15 (R3): Sur `windows-latest`, une cible tenue au-delà du délai configuré dans `runtime.json` produit une erreur explicite, l'ancien contenu reste intact et 0 fichier temporaire reste dans le dossier. Sous Linux, plateforme et échec injectés, le nombre de tentatives est borné et le délai total ne dépasse pas la valeur configurée de plus de 10 %.
- AC16 (R3): Sur `windows-latest`, `write_ini` réussit et `ini_failure` reste `None` une fois le serveur prêt ; sous Linux, le `.ini` reste en `0600`.
- AC17 (R3): Le rejeu des journaux de `tests/fixtures` donne le même résultat sous Linux et sous `windows-latest`, et aucun fichier de `service/protocol.py` ou de `mods/duelcore` n'apparaît dans le diff de la phase.

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
- Un seul commit, sujet : `feat(phase2): P3 — Fichiers sous Windows (R3)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
