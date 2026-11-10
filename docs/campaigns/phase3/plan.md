---
spec: "phase3-upnp-port-opening"
version: "1.0"
author: "adversarial-plan"
based-on: "adversarial-spec"
findings-input: true
---

# Implementation Plan

Phase 3 — UPnP port opening, on the host's request. The plan follows the spec targets
(`service/upnp.py`, `service/engine.py`, `service/api.py`, `service/runtime.json`, `ui/app.js`,
`ui/style.css`, `tests/doubles/fake_igd.py`, `tests/test_upnp.py`, `tests/test_setup_flow.py`,
`tests/test_api_security.py`, `README.md`, `README.fr.md`). One file outside that list must change:
`tests/test_modes.py:49` (`NEW_STATE_KEYS`) asserts the exact set of state keys, and AC18 makes a new
`ports_opening` key mandatory; P5 updates it, and nothing else in that file.

Design decisions taken here (flagged, not silent):

- **Config shape** — a new `"upnp"` object in `runtime.json`:
  `{"discovery_timeout_s": 2, "request_timeout_s": 3, "lease_s": 7200, "description": "PZ Duel",
  "max_response_bytes": 65536}`. `managed_ini.UPnP` stays `"false"`.
- **Result shape** — `upnp.py` returns plain dicts (JSON-ready, no new serialization format):
  `{"status": "mapped"|"no_gateway"|"refused"|"invalid_config", ...}`; the engine adds the neutral
  `{"status": "idle"}` and `{"status": "in_progress"}` forms. Fields: `ports` (list of
  `{"port", "protocol"}`), `expires_at` (epoch seconds, from the engine clock), `address` (public IPv4
  or `null`), `address_note` (why no address), `error_code`/`error_text` (UPnP error from the box),
  `failure` (`"timeout"`|`"unreadable"` when the box sent no UPnP error), `still_active` (list of
  `{"port", "protocol"}`), `key` (faulty runtime key for `invalid_config`), `manual` (manual
  instruction naming the 3 ports with UDP/TCP), `lease_s`.
- **Test seam for discovery** — `Engine(..., upnp_target=None)` keyword-only; `None` means the SSDP
  group/port constants, a `(host, port)` tuple sends the M-SEARCH unicast to the fake IGD on loopback.
  This is the only way to satisfy AC30 (0 datagram to `239.255.255.250` in tests) while exercising the
  real discovery code.
- **Concurrency** — the opening runs on its own daemon thread (`ports-open`), never under
  `Engine.lock` while doing network I/O, so `snapshot()` and `action("start")` stay responsive (AC16).
- **Retention of mappings across openings** (finding P1) — the engine keeps one record per couple
  `(port, protocol)` → `Gateway`, never a single list replaced by the last opening. An opening only
  changes the records of the couples it actually touched (see P5); `no_gateway` and `invalid_config`
  change nothing, so mappings placed by an earlier `mapped` opening stay retained for shutdown. Keying
  by couple keeps the R6 bound (at most three retained mappings, at most three shutdown attempts).
- **Shutdown vs. rollback timeout rules** (finding P2) — the "stop at the first timeout" rule belongs
  only to the rollback inside an opening (R3). Shutdown cleanup (R6) makes exactly one attempt per
  retained mapping, whatever happened to the previous ones, and therefore suffers at most three
  request expirations.
- **Public address test** — `ipaddress.IPv4Address(x).is_global` plus an explicit check of the RFC 5737
  documentation blocks and `100.64.0.0/10`, so the decision does not drift between Python 3.12/3.13/3.14.

### Caller table (API changes)

| File | Function/Method | Migration Note |
|------|----------------|----------------|
| `service/main.py` | builds `Engine` through its factory | New kwarg `upnp_target` is keyword-only with default `None`: no change. Signal/UI quit path calls `engine.shutdown()` → now also deletes active mappings (P5); no call-site change. |
| `tests/doubles/run_service.py:22` | `make_engine` → `Engine(...)` | Unchanged; P6 adds an optional env/config hook to pass `upnp_target` for the subprocess AC22 signal test. |
| `tests/test_modes.py`, `tests/test_share.py`, `tests/test_setup_flow.py`, `tests/test_frozen.py`, `tests/test_platform_windows.py`, `tests/test_servers.py`, `tests/test_snapshots.py`, `tests/test_api_security.py` | `Engine(...)` constructions | Unchanged (default kwarg). |
| `tests/test_modes.py:709` | `set(state) == STATE_KEYS \| NEW_STATE_KEYS` | Add `"ports_opening"` to `NEW_STATE_KEYS` (P5). |
| `service/api.py` | `Engine.snapshot()` via `_state_answer`, `GET /api/state` | Snapshot gains `ports_opening`; API passes it through. |
| `ui/app.js` | consumes `S` (the snapshot) | Reads `S.ports_opening` in the lobby (P7). |
| `tests/test_api_security.py:57` | `RecordingEngine` | Must gain `open_ports()` appending `"open_ports"` to `actions`, so the existing `POST_PATHS = tuple(Handler.POST_ROUTES)` sweeps keep working (P6). |
| `tests/test_api_security.py:591` | `POST_PATHS` sweeps (Origin/Host) | Pick up the new route automatically. |
| External consumers of `/api/state` | none known | No external client; the UI is the only consumer. |

## Steps

### P1: Runtime configuration for UPnP (R7, R3)
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

### P2: Fake IGD test double (R9)
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

### P3: Stdlib IGD client — discovery and description (R1, R7, R9)
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

### P4: Stdlib IGD client — mapping sequence, abandon rule and teardown (R1, R3, R4, R5, R6)
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

### P5: Engine integration — action, state, shutdown (R2, R3, R4, R5, R6)
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

### P6: API route and its security sweep (R2)
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

### P7: Lobby UI — button, result, address pre-fill (R8, R4, R5)
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

### P8: Documentation (R8, R6, R3)
- **Files:** [README.md, README.fr.md]
- **Description:** In "Playing over the Internet" (`README.md:96`) and its French counterpart (around
  `README.fr.md:112`): the "Open ports" lobby step — the 3 ports and protocols it opens, the lease and
  displayed expiry (click again before launching if you stayed long in the lobby, or at a rematch), no
  automatic renewal, mappings removed on clean exit, only the lease bounds them after a crash, the
  meaning of a non-public address (double NAT / CGNAT), and the manual instruction when it fails. Note
  in "Known limits" that a shutdown during an opening may take one extra request timeout, and that
  if two openings in one session reach two different boxes, only the latest box's mappings are
  removed at exit (the other ones expire with their lease).
- **Dependencies:** [P7]
- **Tests:** Doc-only; reviewed in P9. If an existing doc test checks README sections, run it.
- **Risks:** README drifting from the actual UI labels — copy the strings from P7.

### P9: Full-branch review gate and full-suite run (R9, all)
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
