# P9 — Full-branch review gate and full-suite run (R9, all)

commit: feat(phase3): P9 — Full-branch review gate and full-suite run (R9, all)

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
Les phases 1 et 2 sont closes : le produit tourne sous Windows et monte jusqu'au lobby sur un vrai
poste (match `c3f1b335`, 42.22.2). La phase 3 ouvre les ports de la box par UPnP, à la demande
explicite de l'hôte, et lit son adresse publique.

Autorités, dans cet ordre : `docs/campaigns/phase3/spec.md` (9 exigences, 33 critères, amendement A1
compris) — puis `docs/campaigns/phase3/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : UPnP implémenté par nous en stdlib, **jamais
automatique**, **trois mappings seulement** (les deux UDP du camp B et le TCP de partage), bail borné
à 7 200 s avec suppression à l'arrêt, adresse publique lue mais gardée locale, **aucun test contre
un vrai routeur ni contre Internet**, un échec ne bloque jamais un duel.

## Périmètre (verbatim du plan)

- **Files:** [service/upnp.py, service/engine.py, service/api.py, service/runtime.json, ui/app.js, ui/style.css, tests/doubles/fake_igd.py, tests/test_upnp.py, tests/test_setup_flow.py, tests/test_api_security.py, tests/test_modes.py, tests/doubles/run_service.py, tests/test_rules_load.py, README.md, README.fr.md]
- **Description:** Review the whole branch diff against `master` (`git diff master...HEAD`), not commit
  by commit. Per-commit reviews miss cross-file bugs by construction: each step can be correct alone
  while the combination is wrong — e.g. P4 returns `still_active` correctly and P5 stores it, but the
  shutdown in P5 reads a different field; or the UI (P7) names ports from a key the engine (P5) never
  sets; or `runtime.json` (P1) and the AST test (P4) disagree on key names; or P4's `cleared` and
P5's merge disagree so a later failed opening drops earlier cleanup records (finding P1); or P5's
shutdown reuses P4's rollback path and inherits its stop-at-first-timeout rule (finding P2). Checklist: every R1–R9 and
  AC1–AC33 mapped to a passing test; grep for the proposed address flowing into any `log`, `LOGGER`,
  `print`, journal write; no network call outside `open_ports`/`shutdown`; no I/O under `Engine.lock`;
  `tests/test_stdlib_only.py` and `tests/test_servers.py` green (AC26); AC9 (`tests/test_share.py`
  AC58) unchanged and green. Then run the full suite on Linux 3.12/3.13/3.14 and the `pytest-windows`
  jobs (via the existing `.github/workflows/tests.yml` on the delivery branch): > 2 247 tests, 0 failure
  (AC30).
- **Dependencies:** [P1, P2, P3, P4, P5, P6, P7, P8]
- **Tests:** Full `pytest` run; CI `pytest` and `pytest-windows` matrices; record the counts.
- **Risks:** Windows-only failures (loopback UDP behaviour, socket timeouts, firewall) only visible in
  CI; the fake IGD binds to `127.0.0.1` explicitly to limit this. Timing tests may be flaky on shared
  runners — if so, fix the cause, never loosen the spec bounds.

## Ordering rationale

P1 (config) and P2 (fake IGD) have no dependencies and are prerequisites: the client reads its
timeouts from config and cannot be tested without the double. P3 then builds the discovery and
description half of the client, with the socket guard from the start so R9 holds for every later
test. P4 adds the mapping sequence, abandon rule and teardown on top of P3's transport — kept separate
because it carries the trickiest branching (R3's abandon/rollback rule). P5 wires the client into the
engine (state, threading, shutdown); it needs the finished client API. P6 exposes the engine action on
the HTTP route and extends the security sweeps — it needs `Engine.open_ports`. P7 builds the UI on the
snapshot field (P5) and the route (P6). P8 documents the user-visible step once its labels exist. P9 is
the full-branch review and complete test run before delivery, after everything else.

## Critères d'acceptation couverts (verbatim de la spec)

(aucun AC nommé — l'étape est un socle ; respecte sa Description et ses Tests)

## Règles

- Ne modifie QUE les fichiers listés dans `Files` de cette étape, plus les fichiers de test
  qu'elle nomme. Si une dépendance manque, tu la constates dans ta réponse finale, tu ne sors
  pas du périmètre.
- Python 3.12+ ; `service/` reste **stdlib uniquement** (`tests/test_stdlib_only.py` doit rester
  vert). Aucun test ne sort de `127.0.0.0/8` et aucun n'émet de datagramme multicast : le faux IGD
  local est le seul interlocuteur.
- Toute valeur (ports, délais, bail, description du mapping, taille maximale d'une réponse) vient de
  `service/runtime.json` : aucun littéral dans le code. Les constantes du protocole SSDP (1900,
  `239.255.255.250`) sont les seules admises, nommées une fois dans `service/upnp.py`.
- `managed_ini.UPnP` reste `"false"` : on n'active pas l'UPnP du jeu.
- L'adresse publique n'entre ni dans le journal du match, ni dans `state.log`, ni dans un log, ni
  vers l'extérieur.
- Les chaînes **ajoutées** à l'UI sont en anglais ; les documents de campagne restent en français.
- Ne touche ni à `docs/campaigns/phase1/`, ni à `docs/campaigns/phase2/`, ni au protocole, ni aux
  règles du duel, ni à `mods/duelcore/`.

## Definition of done

- Les tests de l'étape existent, portent exactement les noms cités, et passent.
- `/home/chpo/pz-duel/.venv/bin/python -m pytest -q` est vert, le nombre de tests augmente, et le nombre de skips ne dépasse pas les 16
  observés sous Linux au départ de la phase.
- Un seul commit, sujet : `feat(phase3): P9 — Full-branch review gate and full-suite run (R9, all)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.

