# P6 — API route and its security sweep (R2)

commit: feat(phase3): P6 — API route and its security sweep (R2)

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

- **Files:** [service/api.py, tests/test_api_security.py, tests/doubles/run_service.py, tests/test_upnp.py]
- **Description:** Add `_post_open_ports(self, payload)` → `self._state_answer(*self.engine.open_ports())`
  and register `"/api/ports/open": _post_open_ports` in `Handler.POST_ROUTES`, so it goes through the
  same chain (`_refused` Origin/Host checks, bounded read, JSON `Content-Type`). `GET` on it falls to
  the existing 404. `POST /api/invite/address` unchanged. In `tests/doubles/run_service.py`, accept an
  optional `upnp_target` in the run config and pass it to `Engine(...)`, so the subprocess signal test
  can point at the fake IGD.
- **Dependencies:** [P5]
- **Tests:** `tests/test_api_security.py`: `RecordingEngine.open_ports()` appends `"open_ports"`; the
  existing `POST_PATHS` Origin/Host sweeps now include the route (AC31: 403 for each
  `FOREIGN_ORIGINS`/`FOREIGN_HOSTS`); new cases: non-JSON `Content-Type` → 415, `GET` → 404, and
  `engine.actions == []` in every case; one variant with a real `Engine` + fake IGD asserts 0 IGD call
  and 0 datagram for these refused requests. `tests/test_upnp.py`: AC6 through HTTP (accepted in lobby
  of a real duel, IGD receives calls; refused in other phases/mock mode); AC22 second variant — after
  `mapped`, `POST /api/quit` and a stop signal to the `run_service.py` subprocess each make the IGD
  receive exactly 3 deletes and empty its table.
- **Risks:** Forgetting to add the method to `RecordingEngine` would make the existing sweeps crash
  rather than fail cleanly. The signal test on Windows uses the platform stop signals of
  `service/main.py` (`STOP_SIGNALS`); reuse the existing helpers of `tests/test_modes.py`/
  `tests/test_platform_windows.py` rather than inventing a new kill path.

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
- Un seul commit, sujet : `feat(phase3): P6 — API route and its security sweep (R2)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.

