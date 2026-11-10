# Synthesis Review — Phase 3 UPnP port opening (working tree vs 90e657c)

## Verdict

**REQUEST_CHANGES**

## Summary

The UPnP work (`service/upnp.py`, the engine's open/release paths, the fake IGD and the tests) is careful about where its requests may go. Requests stay pinned to the responder's host, SSDP answers are not treated as proof of identity, and every step of shutdown has a bounded wait. Two defects backed by spec and code block approval. **First (Y7):** the P9F1/P9F2 change keys retained mappings per router (`_box` = host, port, control path), and that broke R6/AC23's shutdown limit ("at most three retained mappings, at most three request timeouts"). After two openings that reached different boxes, which can happen on one physical router seen at two control paths, shutdown sends up to six or more deletes, plus the handoff deletes and the two joins. AC23 only passes because its test uses a single gateway. **Second (I1/X1):** a single router that answers M-SEARCH with two LOCATIONs becomes two candidate gateways, both report the same public address, and `_select` refuses the click as ambiguous with no mapping written. The same identity question is behind both defects: what counts as "one box". Keying the box by responder host would address both.

The other findings are minor. Lease expiry (M1) is checked against the wall clock even though a monotonic clock is already injected. Both READMEs describe behaviour the code no longer has and understate how long exit can take. An exception in `_merge_opening` can leave the opening stuck and leak mappings with no warning. The release has no overall deadline. A router that ignores the lease needs corroboration, and that finding was correctly downgraded: the spec makes the lease the only bound, and the proposed fix would break R6. Two findings that rest only on inference (X8 port pin, X9 choosing the first WAN service) are listed for corroboration.

Cross-review mostly confirmed the original passes. It corrected three things: I5 (delete-before-add is what R3 requires, so it drops to a nit), I7 (the stated outcome was wrong; such devices are refused, they do not time out) and A1 (severity reduced, and the proposed fix would break the spec's time limit). The rapporteur re-checked Y7 against `docs/campaigns/phase3/spec.md:151-158` (R6) and the AC23 text, and checked `_box` at `service/engine.py:1633-1635` and the retain key at `engine.py:2149`. Both match what the cross-review claimed. The README text at `README.md:318-322` was also confirmed stale.

## Epistemic Distribution

14 findings retained after deduplication. They come from 24 raw entries: 6 Architect, 7 Inspector, 9 in cross-review 1, 7 in cross-review 2, with overlaps merged.

| Confidence | Count |
|---|---|
| high | 5 |
| medium | 7 |
| low | 2 |

| Basis | Count |
|---|---|
| spec | 2 |
| code | 8 |
| inference | 3 |
| external | 1 |

**Inference-only findings: 3** (A1, X9, X8). All three are minor and need corroboration. None of them affects the verdict.

The distribution supplied with the input (13 findings, spec = 0) counted the original passes before synthesis. The differences come from relabelling during synthesis:
- **I1** changes from medium/inference to **medium/code**. X1 traced the whole mechanism in code (`upnp.py:250`, `:646`, `:608`, `:595-596`, `:631`). Only how common such routers are is still inferred.
- **A4/I4** (merged) changes from low/inference to **medium/code**. X4 reproduced the arithmetic from concrete lines (`engine.py:2215`, `:2229`, `:2235`, `:2243`; `upnp.py:1077-1092`). The part about being killed from outside the process stays an inference and is described as such below.
- **I5** changes from medium/code to **medium/spec**. X5 showed the behaviour is required by R3 (spec.md:112-113) and tested by AC11, so the finding is about the spec, not a code defect.
- **Y7** is new: **high/spec**, verified by the rapporteur against spec R6 and AC23.

No labels were missing or invalid, so no normalization was needed.

**Would the verdict change without the inference-only findings?** No. REQUEST_CHANGES rests on Y7 (high/spec) and I1 (medium/code).

## Critical Findings

### 1. [major] Per-router retention breaks R6/AC23's shutdown limit — `service/engine.py:2149`, `:1633-1635`, `:2170-2190`, `:2241-2243` — [high/spec] (Y7; also covers the code half of A3)

- **What the spec requires.** R6 (spec.md:151-158) says: "au plus trois mappings étant retenus, il subit au plus trois expirations de requête". AC23 sets `Engine.shutdown` ≤ 3 × request timeout + 1 s.
- **What the code does.** Since P9F1, `_upnp_retained` is keyed by `(_box(gateway), couple)`, and `_box = (host, port, control_path)`. Two openings that reach two box identities retain 6 entries, and `_remove` sends one delete per entry. A silent router therefore costs 6 timeouts, plus up to 3 handoff deletes, plus the 5 s and 3 s joins.
- **No second router needed.** Because `control_path` is part of the box identity, one physical router discovered under two descriptions (finding 2) produces two box keys for the same three mappings.
- **Why the test does not catch it.** AC23's test uses a single gateway.
- **Risk level:** confirmed by evidence. The spec text and the keying code were both checked.
- **Fix.** Key the box by host only, since port and path are only how to reach it; the comment at `engine.py:1634` already points this way. Also enforce one overall release deadline of 3 × `request_timeout_s`. Alternatively, amend R6/AC23 explicitly to allow N routers.

### 2. [major] One router that publishes two IGD descriptions is refused as ambiguous — `service/upnp.py:250`, `:646`, `:608`, `:595-596`, `:631` — [medium/code] (I1 validated by X1; label raised from inference, see Distribution)

- **Mechanism.** `_replies` deduplicates on `(peer, LOCATION)`. `find_gateway` deduplicates on the whole frozen `Gateway`. Two LOCATIONs on one responder IP therefore reach `_select` as two candidates. Both report the same public address, and `_select` returns ambiguous with no writes. That is a false refusal on one physical box.
- **Second effect (from X1).** All description fetches and all GetExternalIPAddress reads share one `request_timeout_s` budget (`budget_end = window_end + request_timeout_s`). Two candidates mean four round trips in 3 s, so a slow router is also refused as ambiguous.
- **What reduces it.** M-SEARCH sends only the IGD:1 ST (`:195`). That does not stop a box that exposes separate IGD:1 and IGD:2 description files from answering twice.
- **Risk level.** The mechanism is confirmed in code. How common such routers are is inferred, and the both-public test only uses two separate fake IGDs.
- **Fix.** Collapse candidates that share a responder IP before `_select`, or treat "same host, same public address" as resolved. Add a FakeIGD test that sends two SSDP replies with different LOCATION paths. This fix is shared with finding 1.

## Minor Findings

### 3. [minor] Lease expiry (M1) uses the wall clock — `service/engine.py:1668`, `:2077`, `:2104`, `:2183`, `:2218-2225` — [high/code] (A2 + I2 + X2 + Y2 merged; A2 was medium/code, the high/code agreement of I2, X2 and Y2 is kept)

- **Mechanism.** `self._clock = time.time`, even though `self._monotonic` is already injected (`:1669`). If the clock steps backwards by more than the click-to-add gap of about 2-12 s (Y2), shutdown sends DeletePortMapping for a couple whose lease has ended and which a third party may now hold. That is what M1 forbids. If the clock steps forwards, deletes that were still needed are skipped. That only leaks the mapping until the lease ends.
- **Risk level:** confirmed in code. The trigger (a large NTP, DST or timezone step) is uncommon.
- **Fix.** Use a monotonic deadline for the delete decision and keep wall time only for the `expires_at` that is displayed.

### 4. [minor] Both READMEs are stale and understate exit time — `README.md:318-322`, `README.fr.md:327` — [high/code] (A3, validated by Y3)

- **What the README says.** "Only the latest router's mappings are removed" and exit "may take one extra router request timeout (3 s by default)".
- **What the code does.** It deletes on every box still under lease, and the worst case is far above 3 s (see findings 1 and 7).
- **Risk level:** confirmed. The rapporteur read the README text.
- **Fix.** Update the README after finding 1 is fixed, so it describes the final behaviour.

### 5. [minor] An exception in `_merge_opening` can leave the opening stuck and leak mappings silently — `service/engine.py:2128-2151` — [medium/code] (I3, refined by X3)

- **Mechanism.** `_ports_pending` is cleared before the loop that indexes `item["port"]` and `item["protocol"]`, and before `state.log`. None of that is guarded.
- **Consequences if it raises.**
  - The status is stuck at `in_progress`, but only if the raise happens before `:2133`.
  - Later couples are never retained.
  - `_release_ports` neither waits for the opening nor reports it as abandoned. Mappings live on the router are then neither deleted nor reported, against R6/C1. X3 is right that this is worse than the stuck button I3 described.
- **Risk level:** confirmed in code, but unlikely to trigger, because `_as_dicts`/`_refused` always set those keys.
- **Fix.** Wrap the merge in try/except, retain every couple from the result on failure, and publish a terminal status.

### 6. [minor] A router that ignores NewLeaseDuration keeps a permanent mapping — `service/engine.py:2183`, `README.md:117` — [medium/inference] (A1, challenged by Y1). **Needs corroboration.**

- **The challenge is valid.** Y1 makes three points that hold:
  - R6 ends with "seul le bail limite la durée du mapping", so the spec accepts this outcome.
  - The proposed per-delete GetSpecificPortMappingEntry check doubles the round trips per mapping, which would break R6/AC23.
  - A conforming router that cannot keep leases should refuse the add with 725 (OnlyPermanentLeasesSupported), and that falls into the generic refusal path (no 725 case found in `upnp.py`).
- **What remains.** A non-conforming router that answers success and then ignores the lease. That is plausible but unquantified. The severity is reduced from major to minor and the label is kept.
- **Confirm with:** a FakeIGD that never expires the mapping, with the clock advanced past `expires_at`.
- **Cheap fix now:** reword `README.md:117` to "if the router honours the lease".

### 7. [minor] Release has no overall deadline and keeps trying a box that has stopped answering — `service/engine.py:2215`, `:2229`, `:2235`, `:2243`; `service/upnp.py:1077-1092` — [medium/code] (A4 + I4 + X4, with Y4's challenge; label raised, see Distribution)

- **What is confirmed.** About 26 s worst case with the shipped `runtime.json`, even with one box. `delete_mappings` does not stop after a timeout on the same gateway, and nothing in the process cuts the release short. On a single box this goes beyond the R6 limit only through the opening joins, which R6 does not clearly cover. Y7 is the clearer spec breach.
- **The challenge is partly valid.** Y4 argues that CTRL_CLOSE_EVENT is not mapped to SIGINT or SIGBREAK, so closing the console kills the process before `_release_ports` starts. R6 already accepts that case ("seul le bail…"). That argument is itself medium/inference: the CRT behaviour is from memory and was not checked. The claim that a started release gets silently cut off is therefore **unverified**.
- **Fix.** Use one overall release deadline and stop deleting on a gateway after its first timeout. This fix is shared with finding 1.

### 8. [minor] The first WAN\*Connection service is used without checking that it is connected — `service/upnp.py:514-523` — [medium/inference] (X9). **Needs corroboration.**

- **Concern.** Multi-WAN and DSL routers can list an idle connection first. The mapping would then show as "mapped" but no traffic would arrive.
- **Confirm with:** a FakeIGD description holding two WANConnectionDevices where only the second is Connected.
- **Fix if confirmed:** prefer a service whose GetStatusInfo reports Connected, within the existing budget.

### 9. [minor] The control URL must use the same port as the description — `service/upnp.py:319` — [low/inference] (X8). **Needs corroboration.**

- **Concern.** The host pin (`_same_host`, `:298-301`) already provides the AC4/SSRF protection. Requiring the same port as well refuses legitimate devices that serve SOAP on another port of the same IP; they are reported as `foreign_control_url`.
- **To check:** whether AC1/AC4 actually require the same port, using a FakeIGD with split ports.

### 10. [nit] Delete-before-add can remove another LAN host's mapping — `service/upnp.py:973` — [medium/spec] (I5, challenged by X5)

- **The challenge is valid.** R3 (spec.md:112-113) and AC11 require delete-before-add, so the code conforms. The comparison with M1 fails, because R3 runs on an explicit click by the host.
- **Router behaviour.** X5's claim that miniupnpd's secure_mode refuses to touch another client's mapping is from memory and was not verified.
- **Change:** severity reduced from minor to nit. This is at most a spec note.

### 11. [nit] SSDP answers are accepted from any source address — `service/upnp.py:243` — [medium/code] (A5, challenged by Y5)

- **The challenge is valid.**
  - An attacker off the LAN would have to guess an ephemeral port within a window of about 2 s.
  - An attacker on the LAN passes any subnet filter.
  - Requiring a private responder would refuse legitimate gateways whose LAN address is publicly routable.
  - The stdlib has no portable way to read the netmask.
- **Recommendation:** optional, and dropping it is reasonable.

### 12. [nit] `delete_mappings` puts raw malformed items into `failures` — `service/upnp.py:1081` → `engine.py:2142-2144` — [high/code] (A6, validated by Y6)

- **Mechanism.** The callers unpack every failure as `(port, protocol)`. A non-2-tuple would raise ValueError inside `with self.lock` and escape `Engine.shutdown`.
- **Risk level:** latent. Today only couples built by the engine are passed.

### 13. [nit] An explicit port 0 in a URL becomes port 80 — `service/upnp.py:302` — [high/code] (I6, validated by X6)

- **Impact:** small. The host is still pinned to the responder IP.
- **Fix:** use `80 if parts.port is None else parts.port` and reject 0.

### 14. [nit] `<URLBase>` is ignored when resolving a relative controlURL — `service/upnp.py:316-319` — [low/external] (I7, corrected by X7)

- **Source.** UPnP Device Architecture 1.0. UDA 1.1 deprecated URLBase.
- **The challenge is valid.** The outcome I7 predicted (unreadable response or timeout) is wrong. A URLBase on another port is refused as `foreign_control_url` by the port pin (finding 9). Honouring URLBase only matters if finding 9 is also relaxed.

## Operational Risk Assessment

- **Shutdown duration (findings 1, 7).** With the shipped `runtime.json` (discovery 2 s, request 3 s), a clean quit can block for about 26 s on one box, and longer when several box identities are retained. `stop_on_signal` runs `engine.shutdown()` before `httpd.shutdown()`, and a second Ctrl+C is ignored. Users facing an unresponsive or unplugged router will see a hung exit with no feedback. Whether a Windows console close interrupts a release already in progress is unverified (Y4 against X4) and needs a real Windows run. See the memory notes on proving behaviour on real Windows.
- **Router diversity (findings 2, 6, 8, 9, 14).** The design assumes routers that publish one IGD description, honour the lease, list the active WAN connection first and use the same port for description and control. The fake IGD only covers the conforming case. Finding 2 is confirmed in code. The others need testing against routers, or at least targeted fake-IGD variants, before broad release. The likely failure is a false "no gateway/ambiguous" refusal, after which the host falls back to the manual instruction. Silent permanent exposure is unlikely, and only happens with a router that ignores the lease (finding 6).
- **Clock assumptions (finding 3).** M1 safety assumes the wall clock does not step backwards by more than about 2-12 s during a session that can last up to 2 h. Windows machines with a badly set RTC or timezone are the realistic exposure.
- **Third-party mappings.** Two paths can touch another LAN host's mapping: the opening delete (finding 10, required by the spec) and a backwards clock step at shutdown (finding 3). Router secure modes probably limit both, but that is unverified.
- **Documentation.** The README limits section (finding 4) is shown to users and currently misdescribes cleanup and exit time. Update it after findings 1 and 7 are fixed.
- **Test gap.** AC23 and the both-public test use a single gateway or separate gateways only. Add tests for multiple box identities on one host and for a release that hits a box which no longer answers, so the R6 limit is enforced from now on.

## Epistemic label distribution

{
  "basis": {
    "code": 8,
    "external": 1,
    "inference": 4,
    "spec": 0
  },
  "combined": {
    "high/code": 4,
    "low/external": 1,
    "low/inference": 2,
    "medium/code": 4,
    "medium/inference": 2
  },
  "confidence": {
    "high": 4,
    "low": 3,
    "medium": 6
  }
}
