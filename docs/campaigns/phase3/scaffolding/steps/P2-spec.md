# P2 — Fake IGD test double (R9)

commit: feat(phase3): P2 — Fake IGD test double (R9)

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

- **Files:** [tests/doubles/fake_igd.py]
- **Description:** New local fake gateway, loopback only:
  - an SSDP responder: UDP socket bound to `127.0.0.1:0`, answers a unicast M-SEARCH with
    `LOCATION: http://127.0.0.1:<http_port>/desc.xml` (configurable location host/name for AC4);
  - an HTTP server (`ThreadingHTTPServer` on `127.0.0.1:0`) serving the device description (service
    type configurable: `WANIPConnection:1`, `WANPPPConnection:1`, none, malformed XML, oversized body)
    and the SOAP control URL handling `DeletePortMapping`, `AddPortMapping`, `GetExternalIPAddress`;
  - modes: success; UPnP error per action and per call index (714 on deletes, 718 on the 3rd add);
    malformed SOAP reply; silence globally, or "silent from call N on" (records the call before going
    silent — AC33), or "silent for deletes only", toggled back to responsive at runtime (AC32);
    per-response delay (AC16, 2 s); external address configurable (`5.6.7.8`, special-use, empty, or
    error);
  - records every call (action, arguments, arrival order) and keeps a mapping table
    `{(port, protocol): args}` updated by add/delete;
  - silent handlers block on a `threading.Event` released by `close()`, so teardown never hangs.
  Never joins a multicast group, never sends to anything but the requester on loopback (R9).
- **Dependencies:** []
- **Tests:** Exercised by P3–P6. A self-test in `tests/test_upnp.py` (written in P3) checks that the
  double answers an M-SEARCH, records calls, and that `close()` releases a silent handler within 1 s.
- **Risks:** Thread leaks from blocked handlers making the suite slow or flaky on Windows; mitigated
  by the release event and `daemon_threads = True`. Windows loopback UDP: bind explicitly to
  `127.0.0.1`, not `0.0.0.0`, to avoid the firewall prompt seen on real Windows.

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
- Un seul commit, sujet : `feat(phase3): P2 — Fake IGD test double (R9)`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.

