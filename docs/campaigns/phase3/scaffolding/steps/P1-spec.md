# P1 — Runtime configuration for UPnP (R7, R3)

commit: feat(phase3): P1 — Runtime configuration for UPnP (R7, R3)

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

- **Files:** [service/runtime.json, tests/test_rules_load.py]
- **Description:** Add the `"upnp"` section (`discovery_timeout_s`, `request_timeout_s`, `lease_s`
  = 7200, `description`, `max_response_bytes`). No validation at load time (spec hypothesis
  "Configuration invalide": validation happens at opening, P4). `managed_ini.UPnP` untouched. The
  match journal already copies the whole runtime into `match_start.data.runtime`; nothing else to do.
  Covers R7 (all values from config) and the configurable lease of R3.
- **Dependencies:** []
- **Tests:** In `tests/test_rules_load.py`, assert the shipped `runtime.json` has the 5 `upnp` keys with
  the right types, `0 < lease_s <= 7200`, and `managed_ini.UPnP == "false"` (AC26 stays green via
  `tests/test_servers.py`, unchanged).
- **Risks:** A test fixture that builds a runtime dict by hand without `upnp` would break the engine
  later: P5 reads `runtime["upnp"]` only at opening time and reports a missing section as
  `invalid_config` with key `upnp`, never at construction.

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
- Un seul commit, sujet : `feat(phase3): P1 — Runtime configuration for UPnP (R7, R3)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.

