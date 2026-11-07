READ:e5e51ac1fb4e9547
READ: /tmp/review-phase2-final-1/READ:e5e51ac1fb4e9547

# Phase 2 Final Review: Synthesis

## Verdict

**REQUEST_CHANGES**

## Summary

This synthesis merges the Architect review (5 findings, APPROVE), the Inspector review (0 findings, REJECT), and two cross-review rounds (4 and 6 items). Before writing it, I reread the cited code: README.md:55-90, service/engine.py:180-196, service/api.py:176-295, service/guest_launcher.py:1-40 and 205-310, service/http_kit.py:100-135, service/share.py:95-110 and .github/workflows/release.yml. After removing duplicates, 9 findings remain: 1 major, 5 minor and 3 nits. The read confirmed every code-based claim below.

I set aside the Inspector's REJECT. Cross-Review 1 (X1) challenged it, and the challenge is valid. The REJECT cited no defect. Its only stated reason was that git was off limits, and the worktree files it did read are where the new modules live. A verdict with no findings behind it cannot outweigh reviews that cite concrete evidence. I did not count it as a finding.

One confirmed major issue drives the verdict. The README's guest steps go straight to double-clicking the `.bat`. They never mention the verification code or the `certutil` check, which the design calls the only protection against a replaced file. The minor findings cover a launcher that reports success even when the game exits at once, the hardening of the release supply chain, availability of the LAN share listener, and a cross-site download vector. The core security boundaries hold: the token is compared in constant time, connections are capped, head reads have an absolute deadline, and the local API checks Host and Origin. If the two inference-only findings are excluded, the verdict stays REQUEST_CHANGES, because the major finding is code-based and confirmed.

## Epistemic Distribution

Retained, deduplicated findings: **9**

| Confidence | Count |
|---|---|
| high | 4 |
| medium | 5 |
| low | 0 |

| Basis | Count |
|---|---|
| spec | 0 |
| code | 7 |
| inference | 2 |
| external | 0 |

Inference-only findings: **2** (A4, R1-X3). Both are minor, both need corroboration, and neither affects the verdict.

The distribution supplied with the inputs (high 1, medium 4, code 4, inference 1) counts only the Architect's five findings. The table above also counts the findings the cross-reviews added. No label was missing or invalid, so none needed normalizing. I kept the original labels except where noted below.

## Critical Findings

### 1. [MAJOR] The README's guest steps skip the verification code and certutil, the only control against a replaced `.bat` [high/code]
- **Location:** README.md:74-81 (guest steps 1-3) and README.md:67-68 (host step 6); service/guest_launcher.py:15-21; service/engine.py:187-193.
- **Source:** Cross-Review 1, X2 (ADD). No reviewer challenged it.
- **Evidence:** The guest_launcher.py docstring says comparing the hex code with `certutil` BEFORE opening the file is "the only check that holds against an attacker". The check inside the `.bat` runs in the file being checked, so a replaced `.bat` can simply leave it out. The file travels over plain `http://` on all interfaces. Yet README guest step 2 says "Double-click `pz-duel-join.bat`" right after obtaining it, with no step to verify it. Host step 6 never says to read the code aloud.
- **Mitigation:** The host's in-app text, LAUNCHER_CODE_TEXT (engine.py:187-193), does give the certutil procedure. That only protects the guest if the host relays it, and it contradicts the documented guest steps.
- **Risk level:** Confirmed by evidence (documentation-to-design mismatch on the primary integrity control).
- **Fix:** Add a guest step before step 2: "Ask the host for the code, run `certutil -hashfile pz-duel-join.bat SHA256`, and do not open the file unless the result starts with that code." Add "read the code aloud, separately from the link" to host step 6. Mirror both changes in README.fr.md.

No blockers were found.

## Minor Findings

### 2. [MINOR] A game that starts and exits at once is reported as success, and the previous mod is deleted [high/code]
- **Location:** service/guest_launcher.py:216-223 (Start-Client), with the success path and cleanup in the lines that follow.
- **Source:** Cross-Review 2, X6 (ADD).
- **Evidence:** `[void][Diagnostics.Process]::Start($info)` discards the process object and never checks whether it exits. If it does not throw, `$code = 0`. The `finally` block then removes `$stage`, which holds `previous`, the guest's earlier mod, and the inner rollback never runs. This conflicts with README.md:80-81, "If anything fails, your existing mod is left as it was".
- **Caveat:** With `UseShellExecute=$true`, the PZ client may hand off to a child process, so a plain exit-code check may not be reliable. Confirming this needs a run on real Windows.
- **Risk level:** Confirmed in code. The impact on real Windows has not been verified.

### 3. [MINOR] The release job runs unpinned actions and dependencies while holding `contents: write` [high/code]
- **Location:** .github/workflows/release.yml:11-25.
- **Source:** Architect A2, validated by Cross-Review 2 X2.
- **Evidence:** The `release` job grants `contents: write`, uses `actions/checkout@v4` and `actions/setup-python@v5` (mutable tags), and runs `pip install -r packaging/requirements-build.txt` without `--require-hashes`. The published `.sha256` is computed from whatever was built, so it would vouch for a compromised build too. The file has no workflow-level `permissions:` block. Whether the `pytest` jobs inherit write access depends on repository settings, which nobody checked. That part stays conditional.
- **Note:** A comment in the file (lines 8-10) says the action versions are kept in step with tests.yml on purpose. Pinning by SHA would have to change both files together.
- **Risk level:** Confirmed configuration. No compromise was observed; this is hardening.

### 4. [MINOR] Slow LAN clients can starve the share listener [medium/code]
- **Location:** service/share.py:153 (binds all interfaces); service/http_kit.py:105-130.
- **Source:** Architect A1, validated and refined by Cross-Review 2 X1.
- **Evidence:** The semaphore admits connections without regard to source address, and the token is checked only after headers are parsed (share.py:99-103), so unauthenticated peers take up slots. The cross-review correctly narrows the Architect's attack: the absolute head deadline stops drip-fed bytes from holding one connection open indefinitely, so the attacker has to keep reconnecting. The cross-review also adds that `_turn_away` drains each refused connection for up to 0.5 s on the accept thread, which can add queueing delay.
- **Label:** Kept at medium rather than the cross-review's high. The mechanism is confirmed in code, but the effect on guest availability has not been measured.
- **Risk level:** Theoretical. It needs a hostile host on the same LAN, which the documented "trusted network" scope already accepts.

### 5. [MINOR] Cross-site top-level navigation can trigger a launcher download [medium/inference]
- **Location:** service/api.py:184-191 (`_refused`) and service/api.py:256-279 (`/api/invite/launcher`, `_get_launcher`).
- **Source:** Architect A4, validated by Cross-Review 2 X4 with scope narrowed.
- **Evidence:** The code confirms that a request with no Origin header and a matching Host passes `_refused`, and that the response is sent as an attachment. Whether browsers actually send no Origin on such navigations, and whether the download lands silently, is browser behaviour that nobody tested. The impact is an unwanted file containing the server password landing in Downloads. The page cannot read the response, and the file does not run on its own.
- **Risk level:** **Inference-only. Needs corroboration** in the supported browsers. Possible fix: require `Sec-Fetch-Site: same-origin` or a custom header on this route.

### 6. [MINOR] The 48-bit verification code may have less safety margin than claimed [medium/inference]
- **Location:** service/guest_launcher.py:40 (`LAUNCHER_CODE_CHARS = 12`).
- **Source:** Cross-Review 1, X3 (ADD).
- **Evidence:** The estimate assumes ~1e10 SHA-256 per second per GPU. On that basis a 48-bit second-preimage on the prefix takes hours on one GPU and less on a rented cluster. Lobbies have no time limit, and the trailing mod lines give an attacker bytes they can vary. The limit is documented (limites-connues.md:141-142, and the code comment at line 39), so this is a calibration note. 16 hex characters would add margin at little cost.
- **Risk level:** **Inference-only. Needs corroboration** with a current SHA-256 throughput benchmark.

### 7. [NIT] The host-facing text says the guest "must see this code in their launcher" [medium/code]
- **Location:** service/engine.py:188.
- **Source:** Architect A3 (minor), challenged by Cross-Review 2 X3. **The challenge is valid**, so this is downgraded to nit.
- **Evidence:** The first sentence contradicts the docstring ("the code is never shown by the launcher"). But engine.py:189-193 goes straight on to the full certutil procedure and warns that a replaced file can skip the in-launcher check. The wording is misleading, but the message as a whole does not undermine the control. Reword the first sentence.

### 8. [NIT] `_build_invite` reads and hashes mod files while holding the engine lock [medium/code]
- **Location:** service/engine.py:2033-2034; guest_launcher.py `_mod_lines`.
- **Source:** Architect A5, validated by Cross-Review 2 X5.
- **Evidence:** The disk I/O runs under `self.lock`, which `snapshot` and `guest_launcher` also take. The impact is limited because downloads serve cached bytes. Only lobby opening and address changes rebuild the launcher. The label stays medium: the cross-review's line checks are consistent with it, but the latency on slow or AV-scanned disks has not been measured.

### 9. [NIT] A successful launch silently deletes the guest's previous `duelcore` [high/code]
- **Location:** service/guest_launcher.py:285 and 300-304 (the move to `$stage\previous`, then the recursive `Remove-Item` in `finally`).
- **Source:** Cross-Review 1, X4 (ADD).
- **Evidence:** This matches the README, which promises preservation only on failure. Still, a guest who edited their mod loses it without warning. Finding 2 is the more severe case of the same cleanup path.

### Disputes resolved
- **Inspector REJECT (no findings), challenged by R1-X1:** the challenge is valid. The verdict has no evidence behind it and is set aside.
- **A3 severity, challenged by R2-X3:** the challenge is valid. A3 is downgraded from minor to nit.
- **A1 mechanism, refined by R2-X1:** the refinement is accepted. The attacker must keep reconnecting rather than drip-feed bytes, and the accept-thread drain adds delay. The severity stays minor.
- **A4 basis:** both reviewers label it inference. It stays inference-only.

## Operational Risk Assessment

- **Deployment model:** The tool assumes a trusted LAN or port-forwarded peer. The share listener (TCP 17280) binds all interfaces over plain HTTP. Integrity of the `.bat` depends entirely on the code being relayed out of band and on certutil being run (finding 1). Until the README documents that step, guests following the docs have no protection against a replaced file.
- **Windows-only behaviour not checked here:** The PowerShell launcher has not been run in this review: no check that the game process survives startup (finding 2), no test of mod rollback, SmartScreen or Firewall prompts. Earlier campaign notes also say the win32 test blocks have not been run on real Windows. Finding 2 needs real-Windows confirmation.
- **Supply chain:** Published binaries are unsigned, and users are told to check them against a `.sha256` produced by the same job. Mutable action tags and unhashed pip installs inside a `contents: write` job are the main residual risk (finding 3).
- **Availability:** The cap of 32 connections and the accept-thread drain mean a hostile LAN peer can degrade the invite link for as long as the lobby is open (finding 4). Sending the `.bat` by message avoids the listener entirely.
- **Local browser surface:** The local API on 127.0.0.1:8765 (default) relies on Host and Origin checks. Downloads triggered by originless navigation are an unverified edge case (finding 5).
- **Review coverage limits:** The Inspector ran no tests and inspected no diff. Cross-Review 1 and this synthesis worked from worktree files. No test suite was run as part of this synthesis.

## Epistemic label distribution

{
  "basis": {
    "code": 4,
    "external": 0,
    "inference": 1,
    "spec": 0
  },
  "combined": {
    "high/code": 1,
    "medium/code": 3,
    "medium/inference": 1
  },
  "confidence": {
    "high": 1,
    "low": 0,
    "medium": 4
  }
}
