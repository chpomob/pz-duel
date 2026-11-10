# P7 — Lobby UI — button, result, address pre-fill (R8, R4, R5)

commit: feat(phase3): P7 — Lobby UI — button, result, address pre-fill (R8, R4, R5)

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

- **Files:** [ui/app.js, ui/style.css, tests/test_setup_flow.py]
- **Description:** In `inviteCard()` (`ui/app.js`, real mode only, next to the `#f-address` field):
  a `ports` block with a button `data-action="open-ports"` whose label names the three ports and
  protocols and the lease from `S.ports_opening` (e.g. "Open ports 17271/UDP, 17272/UDP, 17280/TCP for
  2 h"); while `in_progress`, a disabled state and "Opening ports…"; result text for `mapped` (ports,
  expiry time formatted from `expires_at`, address or `address_note`), `no_gateway` / `refused` /
  `invalid_config` (reason + the `manual` instruction from the API). Click handler POSTs JSON to
  `/api/ports/open` with the same fetch helper as `set-address`. Pre-fill: when a `mapped` result with
  `address` arrives and `addressDraft` is empty, set `addressDraft` (and the input) to it; never
  overwrite a non-empty draft; the invite only changes when the host clicks "Use this address" (R5).
  No block at all in mock mode. All added strings in English, no accented characters. `ui/style.css`:
  `.ports` block and `.ports.mapped/.no_gateway/.refused/.invalid_config` variants.
- **Dependencies:** [P6]
- **Tests:** `tests/test_setup_flow.py`, beside the existing `ui_script()` assertions: AC27 the lobby
  renderer contains the `open-ports` button built from `ports_opening.ports`/`lease_s` with `UDP`/`TCP`,
  and it is inside the `real ?` branch (absent in mock); AC28 the pre-fill code only assigns when the
  draft is empty, the no_gateway/refused branches render `manual`, the mapped branch renders the
  formatted `expires_at`; AC29 a check over the lines added by the phase (diff vs. the phase-2 base
  `3b5f407`'s `ui/app.js`, or a frozen copy of the pre-phase line set) finds no `é è à ç` and no
  `adresse`, `ouvrir`, `échec`.
- **Risks:** Static source-reading tests can pass on dead code; mirror the existing `ui_script()`
  style and assert on the specific branches. The polling re-render may clobber the input while the
  host types — reuse the existing `addressDraft` mechanism (`ui/app.js:588`). Timezone of the expiry
  display: format from epoch with the browser's local time, and assert the raw `expires_at` server-side.

## Critères d'acceptation couverts (verbatim de la spec)

- AC27 (R8): Le rendu du lobby d'un duel réel contient un bouton dont le texte ou le libellé voisin

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
- Un seul commit, sujet : `feat(phase3): P7 — Lobby UI — button, result, address pre-fill (R8, R4, R5)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.

