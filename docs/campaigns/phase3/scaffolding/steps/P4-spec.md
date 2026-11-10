# P4 — Stdlib IGD client — mapping sequence, abandon rule and teardown (R1, R3, R4, R5, R6)

commit: feat(phase3): P4 — Stdlib IGD client — mapping sequence, abandon rule and teardown (R1, R3, R4, R5, R6)

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

- **Files:** [service/upnp.py, tests/test_upnp.py]
- **Description:** Add the opening and closing logic:
  - `validate(upnp_cfg)`: before any traffic, missing section or key, wrong type, or
    `lease_s` outside `]0, 7200]` → `{"status": "invalid_config", "key": "upnp.lease_s", ...}` (R3, AC13).
  - `open_ports(cfg, ports, target, cancel)`: `ports` are the 3 tuples
    `(runtime ports.B.game, UDP)`, `(ports.B.udp, UDP)`, `(share_port, TCP)` passed by the caller (no
    port literal in `upnp.py`, R7/AC25). For each mapping: `DeletePortMapping` (any UPnP error,
    including 714, is ignored) then `AddPortMapping` with `NewRemoteHost` empty, external = internal
    port, `NewInternalClient` = local socket address, `NewEnabled` 1, configured description and lease
    (AC10, AC11, AC12). Then `GetExternalIPAddress`.
  - Abandon rule (R3): a `Gateway` object tracks "timed out" — the first request whose timeout
    expires stops the opening; no further delete/add/read is sent. If the expiry is on
    `GetExternalIPAddress`, all 3 adds are confirmed → `mapped` without address. Otherwise a rollback
    makes at most one `DeletePortMapping` per possibly-placed mapping (confirmed adds + the add that got
    no answer), stopping at the first delete that times out; un-confirmed / un-tried ones go to
    `still_active`. A UPnP error on an add → rollback the same way; the result is `refused` with
    `error_code`/`error_text`, or `failure: "timeout"|"unreadable"` when the box sent no UPnP error.
  - Address (R5): `_public(addr)` true only for a dotted IPv4 that is global and outside RFC 5737 and
    `100.64.0.0/10`; otherwise `address: null` with an `address_note` saying the public address is
    unknown or not reachable from the Internet (AC21: 7 cases incl. empty string and SOAP error).
    The address is only placed in the returned dict — never logged by this module.
  - `manual_instruction(ports)`: English text naming the 3 ports with `UDP`/`TCP`, attached to
    `no_gateway`, `refused`, `invalid_config` (R4, AC15).
  - Besides the result dict, `open_ports` returns the `Gateway` it used (or `None`) and `cleared`: the
    couples this opening confirmed absent on that gateway (a confirmed pre-delete not followed by a
    lingering add, or a confirmed rollback delete). The engine needs both to update its retained
    records without losing earlier ones (P5, finding P1). Neither is put in the snapshot.
  - `delete_mappings(gateway, mappings, request_timeout_s)`: for shutdown (R6): exactly one attempt
    per retained mapping to the gateway kept from the opening, no discovery, no description fetch.
    Unlike the R3 rollback, it does **not** stop at the first timeout: a timeout (or UPnP error, or
    unreadable reply) on one mapping is recorded and the next mapping is still attempted, so at most
    three request expirations occur for three mappings (finding P2). Returns the list of failed
    couples (never raises). The rollback inside `open_ports` uses a separate code path
    (`_rollback`) that keeps the stop-at-first-timeout rule; the two must not share a flag.
  - `Gateway` keeps only the control URL host/port/path and service type, so the engine can retain
    it for shutdown.
- **Dependencies:** [P3]
- **Tests:** In `tests/test_upnp.py` (client-level, socket guard on): AC5 accept-but-never-answer
  SOAP → `refused` with `failure == "timeout"`, no code, in ≤ 3 × 0.5 s + 1 s; AC10 exactly 3 adds with
  the configured couples/description/lease, `NewEnabled == 1`, `NewInternalClient == "127.0.0.1"`;
  AC11 each delete before its add, 714 on deletes still `mapped`; AC12 assertion helper run over every
  recorded call of the module (no A/RCON port, no external port 0, empty `NewRemoteHost`); AC13
  lease 7201 and 0 → `invalid_config` with key, 0 calls, 0 datagram; 7200 → `mapped`; AC14 718 on 3rd
  add → `refused`, code 718, 2 rollback deletes, empty table, `still_active == []`; AC21 the 7
  address cases; AC32/AC33 at client level (silent deletes → `still_active` lists the 2 couples;
  silent after first add → `refused` timeout, one rollback delete, `still_active` holds the couple);
  `delete_mappings` with a mute gateway sends 3 deletes (one per mapping, all recorded by the double)
  and returns within 3 × timeout + 1 s with the 3 couples as failures; `delete_mappings` with a double
  silent **only on the first delete** (per-call-index silence) still sends the 2nd and 3rd deletes,
  which succeed — the double's table ends with only the first couple, and the returned failures list
  exactly that couple (finding P2); a rollback test in the same file keeps checking that the R3
  rollback, by contrast, stops after its first timed-out delete (AC32/AC33 counts); `cleared`
  returned for mapped (3 couples confirmed pre-deleted then re-added → empty `cleared`), refused 718
  with confirmed rollback (3 couples), no_gateway (empty, gateway `None`); AC25 AST walk of
  `service/upnp.py`: no int literal equal to any port of `runtime.json`, no literal equal to
  `lease_s` or `description`; only `SSDP_PORT`/`SSDP_GROUP` assign 1900 / the group, once each.
- **Risks:** Off-by-one in the abandon rule (sending a request after a timeout, or failing to retry the
  "add with no answer" in rollback) — covered by AC33's exact counts. Timing tests flaky on loaded CI:
  bounds use the spec's +1 s margin and 0.5 s timeouts; no tighter assertions. UPnP v2 services may
  reject leases > 604800 only — not our range.

## Critères d'acceptation couverts (verbatim de la spec)

- AC5 (R1): Un faux IGD qui accepte la connexion SOAP sans jamais répondre fait rendre au client un

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
- Un seul commit, sujet : `feat(phase3): P4 — Stdlib IGD client — mapping sequence, abandon rule and teardown (R1, R3, R4, R5, R6)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.

