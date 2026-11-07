# PZ Duel phase 2: consolidated review

## Verdict

**REQUEST_CHANGES**

One code-backed major finding drives this verdict: the guest's executable `.bat` arrives over plain HTTP with no authenticated integrity check, and the resulting code-execution risk is undocumented. The verdict is the same if the three inference-only findings are excluded.

## Summary

Phase 2 has no confirmed data-loss blocker, and no finding shows a defect in the core match flow. The only retained major is finding 1. An active on-path attacker can replace the launcher and run code under the guest's account when the guest opens it. Plain HTTP is required by AC37, so the transport itself does not violate the spec. The gap is that the known limits describe only the confidentiality risk, and the guest has no out-of-band way to check the file.

The other findings are minor or nit. The most useful fixes are a test gate and pinned inputs for the release, a log sink for the windowed binary, removal of the stale "blocker" text from both READMEs and both limits documents, and draining the connection on every refusal issued before the body is read. The cross-reviews lowered three findings from major to minor. For two of them (A1 and A2) the defect in the code is real, but the claimed impact is unproven. For the third (B1) the lost directory is regenerable and was about to be replaced anyway.

Coverage has limits. The brief announces 5 review passes but names and supplies 4 (Architect, Inspector, two cross-reviews). `HEAD` is `8d75f0f`, so the requested commit range is empty and all phase 2 work was reviewed as uncommitted files in the worktree. For this synthesis I re-read the cited code and checked the line references. I did not run the test suite or repeat the cross-review socket and fault-injection measurements, so those results are quoted as the reviewers reported them. Nothing here was tested with the real game or on a desktop Windows machine.

## Epistemic Distribution

Counts cover the 13 retained, deduplicated findings. A10 is not counted because it became a coverage note (see Dispute Resolution).

| Confidence | Count |
|---|---:|
| high | 10 |
| medium | 3 |
| low | 0 |

| Basis | Count |
|---|---:|
| spec | 0 |
| code | 10 |
| inference | 3 |
| external | 0 |

**Inference-only findings: 3** (findings 8, 9 and 10). All three are medium/inference, need corroboration, and have no effect on the verdict. No finding is low-confidence, so the down-weighting of low-confidence inference removes nothing. No labels were missing or invalid.

The distribution supplied with the input was high 9, medium 3, low 0 and code 9, inference 3, spec 0, external 0 (`high/code` 9, `medium/inference` 3). It describes the 12 original Architect and Inspector findings before cross-review, so it differs from the table above.

Label changes made during synthesis:

- **B2, medium/inference to high/code.** X2 and X3 report measurements on the real server, and the code confirms the path: `_body` returns at `service/api.py:259` and `do_POST` returns at `:337` without `_hang_up`. Response loss on Windows stays unproven.
- **A5 kept at high/code, against X9's medium/inference.** X9's open assumption was that the packaged entry point might configure logging. A grep for `basicConfig`, `FileHandler` and `addHandler` across `service/` and `packaging/` finds nothing, and `packaging/pz_duel_entry.py` does not use `logging`.

## Dispute Resolution

| Item | Challenge | Outcome |
|---|---|---|
| A1 / X5 | Missing `cwd` is real, but launch failure is unproven | **Valid.** Major to minor. |
| A2 / X6 | Missing retries are real, but scanner disruption is unproven | **Valid.** Major to minor. Rollback exists at `service/snapshots.py:73-83`, and staging is preserved at `:214-217`. |
| B1 / X1 | Rollback loss is real, but major overstates it | **Valid.** Major to minor. The lost directory is the app's own `duelcore`, and `MOD_SRC` can reinstall it. |
| A9 / X13 | Shared helpers do not show a security defect | **Valid.** Kept as an optional nit. |
| A10 / X14 | Repository state is scope, not a product finding | **Valid on classification.** Moved to the coverage note. X14's doubt about the facts is resolved: `HEAD` is `8d75f0f` and the worktree is dirty. |

X2, X7, X8, X9, X10, X11 and X12 validated their originals, and their refinements are merged below. X3 and X4 added new material, and X15 added a new finding.

## Critical Findings

### Blockers

None.

### Majors

#### 1. Executable `.bat` is delivered over plain HTTP with no authenticated integrity check [high/code]

**Sources:** A3, X7. **Risk: confirmed by code. No exploit was run.**
**Location:** `service/guest_launcher.py:57`, `:179`; `service/engine.py` (`_build_invite`); `docs/campaigns/phase2/limites-connues.md:150`

- The invite URL is HTTP, as AC37 requires. The batch header starts PowerShell with `-ExecutionPolicy Bypass` and runs a script block taken from the downloaded file.
- The embedded SHA-256 values travel in that same file. They detect damage to the mod files, but they do not authenticate the batch header, the PowerShell program or the join data.
- An active attacker on the network path can therefore substitute instructions that run when the guest opens the file. The route token stops guessing, not response modification.
- Impact is limited to the guest's privileges and requires the guest to run the file. `-ExecutionPolicy Bypass` is not a privilege escalation.
- The limits say only that the token and `.bat` "cross the network in clear". They do not mention tampering or code execution, although the README covers remote guests by forwarding ports.

**Fix:** document the code-execution risk in the limits and both READMEs. Also give the guest an out-of-band check, such as showing the launcher's SHA-256 in the host UI, or steer remote guests to receiving the saved `.bat` over an authenticated channel.

## Minor Findings

Severity is the first ranking key, then epistemic weight. Code-backed findings come before inference-only ones.

### Minor

#### 2. Release publishes without the test suite and with unpinned build inputs [high/code]

**Sources:** A6, X10. **Risk: confirmed by code.**
**Location:** `.github/workflows/release.yml:5`; `.github/workflows/tests.yml`; `packaging/requirements-build.txt`

Any `v*` tag can publish after a build and smoke test only. `tests.yml` runs only on pushes to `master` and on pull requests, so a tagged commit with failing tests, or one that never reached `master`, can still ship. This meets AC47 but leaves a gap. The job holds `contents: write`, uses actions by floating tag, and pins only `pyinstaller`, with transitive dependencies unpinned and unhashed.

**Fix:** run pytest as steps inside the release job, pin actions by SHA, and use a hashed lock file. A separate prerequisite job would fail the current check: `tests/test_ci_workflows.py:230-231` rejects more than one release job, so that constraint would have to change first.

#### 3. Runtime logger output is lost in the windowed binary [high/code]

**Sources:** A5, X9. **Risk: confirmed by code. Not observed in a packaged run.**
**Location:** `packaging/pz-duel.spec:6`; `service/api.py:173`; `service/share.py:109`; `service/engine.py:1015`, `:1589`, `:1639`

The spec comment says errors go to the log file, but only `main.log_line` writes there. No handler is configured anywhere, and with `console=False` there is no stderr for Python's last-resort handler. Unexpected API exceptions, share launcher failures, and command, journal and proof-store warnings all vanish. Startup errors are still logged explicitly, so R4's strict requirement holds. This is a diagnosability gap on the one platform with no developer console.

**Fix:** attach a `FileHandler` on `paths.log_file()` when frozen, and keep the rule that messages carry no secrets.

#### 4. READMEs and known limits still declare a fixed defect as a blocker [high/code]

**Sources:** A4, X8. **Risk: confirmed by code.**
**Location:** `README.md:46`; `README.fr.md:47`; `docs/campaigns/phase2/limites-connues.md:5`, `:16-24`, `:163-170`; `docs/campaigns/phase2/limites-connues.fr.md:17-27`

- The READMEs say a real match stops at launch until a known defect is fixed, and the limits have a "Blocker" section on `os.sync()`. `service/snapshots.py:119` and `:212` now call `sync_disk`, and the comment at `tests/test_ci_workflows.py:466-468` says the blocker ended at P18F1.
- The limits also contradict themselves: "none recorded yet" for CI runs sits beside a section listing green runs and the release run.
- The tests only require the term `os.sync` to be present, so they cannot catch this.

**Fix:** update all four documents, and keep the separate statement that nothing has been tried with the real game.

#### 5. Refusals issued before the body is read close without draining [high/code]

**Sources:** B2, X2, X3. **Risk: confirmed on Linux by the cross-review measurements. Response loss on Windows is unverified.**
**Location:** `service/api.py:259`, `:337`; `service/api.py:232` (`_path`)

- `_body` returns after a framing refusal without `_hang_up`, and `do_POST` answers an unknown route the same way. The `_path` 400 branch also returns without draining, though X3 found this by reading only and did not exercise it.
- Reported on Linux with a body of `BODY_MAX`+1 bytes: the 413 with a delayed body raised `BrokenPipeError` 20/20, the 413 with no delay 1/20, the unknown-route 404 6/20, and the 403 control through `_hang_up` 0/20.
- A raw socket that ignored the send error still read the 413, so the response itself was not lost on Linux. A client that stops at its failed write never reads it.

**Fix:** use one helper covering every refusal issued before the body is read, not a fix in `_length` alone. X2 expects `test_hostile_framing_is_refused_before_any_read` to survive, because the drain reads the connection rather than `rfile`. Cost: up to `DRAIN_S` (1 s) of one of 32 slots per refusal.

#### 6. Failed rollback deletes the previous mod and hides the original error [high/code]

**Sources:** B1, X1. **Risk: confirmed by X1's fault injection and by reading the code. Severity lowered from major.**
**Location:** `service/preflight.py:537-547`; `service/engine.py:1719-1727`

If installing the staged directory fails and restoring `previous` also raises `OSError`, leaving the `TemporaryDirectory` deletes `previous`. Two failures in a row are required. What is lost is the app's own `duelcore`, which a successful run also deletes. `engine.install_mod` catches the `OSError`, and a retry reinstalls from `MOD_SRC`. The real defects are that the mod stays absent until then, and that the rollback error replaces the original cause in the message. No test covers this path.

**Fix:** keep the stage when rollback fails, and report both errors.

#### 7. Interrupted initial capture bypasses creation recovery [high/code]

**Source:** X15 (added from A2's area). **Risk: state machine confirmed by code. The trigger is a process end in a narrow window.**
**Location:** `service/snapshots.py:65-71`, `:238`, `:242`

`_switch` installs the two snapshots one after the other. If the process ends between them, one snapshot exists. `prepare_worlds` then takes the `elif snapshots` branch at `:238` and refuses before it reaches the marker recovery at `:242`, on every later launch. The refusal is safe, but the user is stuck until the filesystem is repaired by hand.

**Fix:** handle a partial pair explicitly when the creation marker is present, or document the repair procedure.

#### 8. Host client is launched on Windows with no working directory [medium/inference]

**Sources:** A1, X5. **Risk: inference-only, needs corroboration. Severity lowered from major.**
**Location:** `service/engine.py:228`, `:231`

The omission itself is confirmed: both `Popen` calls set no `cwd`, while the guest launcher (`service/guest_launcher.py:189`) and `ServerProcess.start` (`service/servers.py:319`) both set one. **Assumption:** the real client resolves its resources against the working directory. Nothing shows this, and real-client behaviour is unobserved. If the client exits within `CLIENT_START_GRACE_S`, `launch_client` reports a `ClientLaunchError`, so that failure would be visible rather than silent.

**To confirm:** start the real exe from another folder. Passing `cwd=script.parent` is cheap hardening either way.

#### 9. World and mod directory renames have no Windows lock tolerance [medium/inference]

**Sources:** A2, X6. **Risk: inference-only, needs corroboration. Severity lowered from major.**
**Location:** `service/snapshots.py:68`; `service/preflight.py:542-544`

The renames have no retry, unlike `replace_file`. **Assumption:** desktop scanners hold handles that block these renames often enough to matter. No such observation exists. Worlds are protected by rollback, so the risk is availability, not data loss. A failure would stop the launch with "restauration impossible", and a later attempt may succeed.

**To confirm:** hold a handle on a file inside the target during `_switch` on `windows-latest`, or measure on a real desktop. A bounded `PermissionError` retry is reasonable hardening.

#### 10. Cleanup failure after a successful swap reports a failed install [medium/inference]

**Source:** X4. **Risk: inference-only, needs corroboration. The fault was injected, not observed on Windows.**
**Location:** `service/preflight.py:537`

The `TemporaryDirectory` exit runs after both renames, and its error is not separated from install errors. With `__exit__` forced to raise, `install_mod` failed although the new mod was in place, and a `.duelcore-*` directory stayed behind. **Assumption:** a transient handle on Windows defeats the retry logic in `TemporaryDirectory`. Harm is limited: a retry succeeds, and the leftover only costs disk space.

**To confirm:** on `windows-latest`, hold a file open in `previous` during cleanup.

### Nits

#### 11. A non-2xx `/api/state` body replaces the UI state [high/code]

**Sources:** A7, X11. **Risk: confirmed by code, transient.**
**Location:** `ui/app.js:32`, `:37`, `:54` (A7 cited `:31`; the `res.json()` call is at `:32`)

`api` returns parsed JSON whatever the status, and `poll` assigns it to `S` and marks the service healthy. `render` then runs outside the `try` block, so the exception is not caught, and the healthy badge stays until the next good poll.

**Fix:** validate the state response before assigning it. Do not make `api` throw on every non-2xx response, because `answered()` depends on consuming refusals.

#### 12. `setup_state` hashes the mod while holding the engine lock [high/code]

**Sources:** A8, X12. **Risk: confirmed by code, low impact.**
**Location:** `service/engine.py:1717`, `:1779`

`_setup_view` runs `mod_fingerprint`, which reads the whole tree, under the lock that `/api/state` needs. The mod is small, and setup refreshes only on settled-phase changes and explicit actions (`ui/app.js:52-59`).

**Fix:** compute the fingerprint before taking the lock.

#### 13. Engine and share listener depend on HTTP handler internals [high/code]

**Sources:** A9, X13. **Risk: confirmed as a dependency. No runtime or authorization defect shown.**
**Location:** `service/engine.py:32`; `service/share.py:20-28`, `:43`

The imports exist and there is no cycle. `ShareHandler` checks its own token route and does not inherit the loopback API routes. This is an optional refactor: move `LAUNCHER_NAME` to `guest_launcher.py` and the bounded-socket helpers to a small shared module.

## Operational Risk Assessment

- **Network exposure:** a remote guest depends on TCP 17280 being forwarded to the Internet. That makes the listener reachable from outside, and it carries the password and an executable in clear (finding 1). The risk ends only when the lobby closes the listener.
- **Release integrity:** users are told to run a binary past SmartScreen. Today a tag push alone can publish it without the test suite, and part of the build chain is mutable (finding 2).
- **Supportability:** in the shipped binary, runtime failures leave no trace outside the startup log, so field reports will be hard to diagnose (finding 3). Fix this before the first real Windows trials, since it also makes findings 8 to 10 easier to confirm.
- **Windows desktop assumptions:** CI runners show neither real antivirus or indexer behaviour nor the real game. Findings 8, 9 and 10 depend on exactly those conditions, and the `windows-latest` handle tests would settle only part of that.
- **Failure modes:** interrupted snapshot capture needs manual repair (finding 7). Failed mod rollback needs a retry (finding 6). Refusals may be lost on Windows (finding 5, unproven).
- **User-facing accuracy:** the stale "not playable" warning contradicts the code and could mislead early testers about what is actually broken (finding 4). The statement that nothing was tried with the real game is still accurate, and that remains the main release risk.
- **Change control:** phase 2 exists only as uncommitted files on a detached `HEAD`, and a campaign restart has wiped a worktree before. Commit it to a branch before acting on this review, because the commit-based checks could not be evaluated from this state.

READ:504760b5df56afb1
READ: /tmp/review-phase2-full-1/READ:504760b5df56afb1

## Epistemic label distribution

{
  "basis": {
    "code": 9,
    "external": 0,
    "inference": 3,
    "spec": 0
  },
  "combined": {
    "high/code": 9,
    "medium/inference": 3
  },
  "confidence": {
    "high": 9,
    "low": 0,
    "medium": 3
  }
}
