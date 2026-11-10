# P5 — Engine integration — action, state, shutdown (R2, R3, R4, R5, R6)

commit: feat(phase3): P5 — Engine integration — action, state, shutdown (R2, R3, R4, R5, R6)

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

- **Files:** [service/engine.py, tests/test_upnp.py, tests/test_modes.py]
- **Description:**
  - `Engine.__init__`: keyword-only `upnp_target: tuple[str, int] | None = None`; fields
    `_ports_opening = {"status": "idle"}`, `_ports_thread`, `_ports_cancel = threading.Event()`,
    `_upnp_retained: dict[(port, protocol), Gateway]` (replaces the single gateway + list, finding P1). No network I/O at construction, start,
    rematch, lobby open, or `set_invite_address` (R2, AC7, AC9).
  - `Engine.open_ports() -> (ok, message)`: under `self.lock`, refuse without traffic when
    `not self.real` ("not available in simulated mode"), `phase != "lobby"` ("only in the lobby, phase
    X"), `_closing`, or status `in_progress` ("an opening is already in progress") (R2, AC6, AC8).
    Otherwise validate config first (`invalid_config` set synchronously, no thread, AC13); else set
    `in_progress`, record the click time from `self._clock`, start daemon thread `ports-open`.
  - Thread body: build the 3 ports from `self.runtime`, call `upnp.open_ports(...)` **outside** the
    lock, then under the lock store the result, add `expires_at = click + lease_s` for `mapped`, and
    **merge** into `_upnp_retained` (finding P1) — never replace it:
    1. for each couple in `cleared` whose retained gateway equals this opening's gateway (same control
       host/port/path), drop the record — the box confirmed it gone;
    2. for each couple active after this opening (the 3 ports of a `mapped`, or `still_active` of a
       `refused`), set the record to this opening's gateway (same keys, no duplicates, at most 3);
    3. every other record is left untouched — in particular `no_gateway`, `invalid_config`, and a
       `refused` whose pre-delete timed out keep the earlier records.
    Known limit, flagged in P8: if a later opening reaches a *different* box for the same couple, the
    record points to the newest box (R6 caps retention at three mappings, one per couple); the older
    box's mapping is then bounded by its lease only. `state.log` gets a message with the status
    only — never the address (R5, AC20).
  - `snapshot()`: adds `state["ports_opening"]` = copy of `_ports_opening` (neutral `{"status":
    "idle", "ports": [...], "lease_s": N}` so the UI can name ports and lease before the click, R8).
    `invite` is untouched: the proposed address goes only in `ports_opening.address` (R5, AC18, AC19).
    Present in every phase; the UI shows it only in the lobby.
  - `_stop_service()`: after the match shutdown and `_close_share()`, set `_ports_cancel`, join the
    opening thread (bounded by request timeout, it abandons at the next request boundary), merge its
    result as above, then, if `_upnp_retained` is non-empty, group its couples by gateway and call
    `upnp.delete_mappings` per group — one attempt per retained couple, ≤ 3 attempts in total, no
    discovery, a timeout on one couple never skips the others (finding P2). Failures
    are logged with `LOGGER.warning` and `state.log` listing port/protocol only — no address (R6,
    AC22–AC24, AC32, AC33). Nothing is sent when nothing is active (AC24).
  - `tests/test_modes.py`: add `"ports_opening"` to `NEW_STATE_KEYS` (required by AC18).
- **Dependencies:** [P4]
- **Tests:** In `tests/test_upnp.py`, with the existing server doubles (`tests/doubles/fake_server.py`),
  the lobby rig used by `tests/test_share.py` (`Lobby`, `SocketSpy`, `detect`) and the socket guard:
  AC6 engine-level refusals in `idle`, `running`, `ended`, mock mode with 0 packets and a reason;
  AC7 start → lobby → rematch → lobby, 0 IGD call and 0 datagram to port 1900 / multicast; AC8 second
  call during `in_progress` refused, at most 3+3+1 calls total; AC13 engine-level `invalid_config`;
  AC15 status and fields for mapped / no_gateway / refused(718) / invalid_config, `expires_at` = click
  + lease ± 2 s, manual text contains the 3 numbers, `UDP`, `TCP`; AC16 2 s delay → `snapshot()` in
  < 0.5 s returns `in_progress`, `action("start")`-path acceptance (launch action accepted); AC17
  after no_gateway/refused the duel reaches `running` and the invite equals, field by field, a run
  without click; AC18 no click → state equals phase-2 state except `ports_opening` neutral, `invite`
  identical; AC19 `5.6.7.8` proposed, `invite.address` unchanged until `set_invite_address`, then in
  the link; AC20 after the AC19 path to the end of a duel and shutdown, `5.6.7.8` occurs 0 times in
  the match JSONL, `state.logs`, `caplog`, `capsys`, and 0 connection to it; AC22 (`shutdown()` path)
  exactly 3 deletes with the AC10 couples, empty table; AC23 mute IGD at shutdown → `shutdown()`
  returns in ≤ 3 × 0.5 s + 1 s, server doubles stopped, failure logged without address; AC24 no
  opening / no_gateway / invalid_config / refused-with-empty-list → 0 call, 0 datagram at shutdown;
  AC25 changed ports/lease/description in a runtime copy change the args received; AC32 and AC33
  end-to-end through `shutdown()`. Retention across openings (finding P1): `mapped` → second click
  with the fake IGD's SSDP responder stopped (→ `no_gateway`) → `shutdown()` makes the IGD receive
  exactly 3 `DeletePortMapping` with the AC10 couples and empty its table; same with the second click
  rejected as `invalid_config` (lease 0 in a runtime copy swapped in) → 3 deletes at shutdown;
  `mapped` → second click `refused` 718 with confirmed rollback → 0 delete at shutdown (records
  cleared), table empty. Shutdown with the first delete silent (finding P2, engine level): after
  `mapped`, the double goes silent on its first shutdown delete only → it still receives 3 deletes,
  the 2nd and 3rd couples are gone from its table, a failure naming only the first couple is logged
  without address, and `shutdown()` returns within 3 × 0.5 s + 1 s. Existing `test_api_backward_compatible` stays green with the new key.
- **Risks:** Deadlock if network I/O happens under `self.lock` (state polling would freeze, AC16) —
  all UPnP calls run outside it. Race: shutdown during `in_progress` — the cancel event plus join
  makes the opening stop at its next request; its result is still merged so mappings it placed are
  deleted. The AC23 bound only covers the mapped case; a shutdown during an opening can add up to one
  request timeout (documented in README, P8). Losing cleanup records (finding P1): a merge bug that
  replaces instead of merging — covered by the mapped → no_gateway → shutdown test. Leaking the address via `state.log` or an exception
  message — the engine never formats the result dict into a log line.

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
- Un seul commit, sujet : `feat(phase3): P5 — Engine integration — action, state, shutdown (R2, R3, R4, R5, R6)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.

