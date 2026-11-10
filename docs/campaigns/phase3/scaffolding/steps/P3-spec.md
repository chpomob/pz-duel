# P3 — Stdlib IGD client — discovery and description (R1, R7, R9)

commit: feat(phase3): P3 — Stdlib IGD client — discovery and description (R1, R7, R9)

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
- **Description:** New module, stdlib only (`socket`, `http.client`, `urllib.parse`,
  `xml.etree.ElementTree`, `ipaddress`, `time`, `logging`). Named constants `SSDP_PORT = 1900` and
  `SSDP_GROUP = "239.255.255.250"`, each defined once (R7). Contents of this step:
  - `discover(target, timeout_s, max_bytes)`: sends one M-SEARCH (ST `InternetGatewayDevice:1`) to
    `target` (default `(SSDP_GROUP, SSDP_PORT)`), waits at most `discovery_timeout_s` (monotonic
    deadline), takes the first reply that parses, and returns `(responder_ip, location)` or `None`.
  - `_same_host(location, responder_ip)`: the `LOCATION` URL must be `http`, its host an IP literal
    equal to the responder address; a host name or another IP → `no_gateway`, with **no** DNS lookup
    and no connection (AC4). The control URL (relative, or absolute on the same host/port) is checked
    the same way.
  - `_fetch(host, port, path, body, timeout_s, max_bytes)`: one `http.client.HTTPConnection` with
    `timeout`, reads in chunks against a monotonic deadline and the size cap; more than
    `max_bytes` → "unreadable"; returns body and the socket's local address (`sock.getsockname()[0]`,
    used as `NewInternalClient`, never the user-entered address — AC10).
  - `_find_service(xml)`: rejects any `<!DOCTYPE`/`<!ENTITY` before parsing; finds a service whose
    `serviceType` is `WANIPConnection:1`, `WANIPConnection:2` or `WANPPPConnection:1`, and its
    `controlURL`.
  - Every exception (`OSError`, `socket.timeout`, `http.client.HTTPException`, `ET.ParseError`,
    `ValueError`, `UnicodeError`) is caught inside the module and mapped to a result; nothing propagates.
- **Dependencies:** [P1, P2]
- **Tests:** In `tests/test_upnp.py`, with an autouse **socket guard** fixture (wraps
  `socket.socket.connect/connect_ex/sendto` and `socket.getaddrinfo`; fails on any target outside
  `127.0.0.0/8` or equal to `239.255.255.250` — AC30). Tests: AC1 discovery + description for
  `WANIPConnection:1` and `WANPPPConnection:1`; AC2 no reply → `no_gateway` within discovery timeout
  (0.5 s) + 1 s; AC3 oversized by 1 byte / malformed XML / no compatible service → `no_gateway`, no
  exception, 0 SOAP call recorded; AC4 location on `127.0.0.2` or a host name → `no_gateway`, spy shows
  0 connection to that host and 0 resolution of the name. Self-test of the fake IGD (P2).
- **Risks:** `127.0.0.2` is not routable on Windows/macOS — the test only asserts that no connection
  is attempted, so this is harmless. XML entity expansion: DOCTYPE rejection plus the size cap bound
  it. A slow-drip server could exceed a per-socket timeout; the monotonic deadline covers it.

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
- Un seul commit, sujet : `feat(phase3): P3 — Stdlib IGD client — discovery and description (R1, R7, R9)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.

