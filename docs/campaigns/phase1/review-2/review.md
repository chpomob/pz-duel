# Phase 1 final review: synthesis report

## Verdict

**REQUEST_CHANGES**

The verdict rests on one blocker, A1 `[high/external]`, backed by game bytecode and by the repository's own code path. It does not depend on inference: no retained finding is inference-only, so excluding such findings would not change it.

## Summary

In real mode, the default preset cannot leave the lobby. `damage_taken` is proved only by `OnWeaponHitCharacter`, and the game fires that hook only for a weapon hit by a player, never for a zombie hit on a player. With `MaxPlayers=1` and separate worlds, no second player exists to strike the first, so the proof never arrives. The atom is withdrawn at the deadline and the table is refused. `duel_rapide` (the default) and `pression` both end in `error`, and the README verdict procedure, the only route to closing AC19, cannot succeed as written. The test suite misses this because its doubles fire the hook by hand.

A second serious defect is new in this round: a death before `t0` is never judged, and the mod does not report it again (X14). The other eight findings are hardening work. Five are minor: coarse spawn-probe handling, undocumented local trust assumptions, a stalled-request hold, unguarded file I/O and wall-clock pacing. Three are nits. The cross-reviews were useful: they narrowed B1, reduced B2 to a nit, corrected A3's claim about lost scores, and showed that A4's proposed fix is incomplete.

Scope of this synthesis: the brief says five passes, but four were supplied (Architect, Inspector, Cross-Review 1, Cross-Review 2). I re-read the cited code for every retained finding. I also inspected the installed `projectzomboid.jar` with `javap` to check A1. I did not run the test suite, did not repeat the cross-reviewers' runtime probes, and did not exercise a real PZ server or client. Runtime measurements quoted below are the cross-reviewers' own. An earlier `docs/campaigns/phase1/review/review.md` holds the previous round, so this report was written to `review.md` at the worktree root and leaves that file intact.

## Epistemic Distribution

Counts cover the 10 retained, deduplicated findings.

| Confidence | Count |
|---|---|
| high | 7 |
| medium | 3 |
| low | 0 |

| Basis | Count |
|---|---|
| spec | 0 |
| code | 9 |
| inference | 0 |
| external | 1 |

**Inference-only findings: 0.** Two sub-claims remain inference and need corroboration, but neither stands as a finding: X10's event-time concern (under A4) and X5's link from B1 to descriptor exhaustion (under A3, not retained).

The distribution supplied with the input covers the 9 original findings before synthesis: high 5, medium 4, low 0; code 5, external 1, inference 3, spec 0. The difference comes from four changes:

- X14 is added as a tenth finding.
- A2 moves from medium/inference to high/code.
- A5 moves from medium/inference to high/code.
- A4 moves from medium/inference to medium/code.

Each relabel is explained on its finding. X14's own label also changes, from medium/inference to medium/code. No finding was supported only by low-confidence inference once merged. X5, the only low-confidence item, was folded into A3 for its measured part, and its speculative part was dropped.

## Critical Findings

### Blocker

**A1 (with X6): `damage_taken` cannot fire in a duel, so the default preset never leaves the lobby in real mode** `[high/external]`
Risk level: confirmed by bytecode and code; not yet observed in a live session.
Location: `mods/duelcore/42.21/media/lua/server/DuelCore.lua:437`, `service/effects.json:4`, `service/engine.py:1074`, `service/engine.py:1446`

- **Source:** build 42.21.0 `projectzomboid.jar`, disassembled with `javap` by the Architect on 2026-11-06.
- **Game behaviour:** the hook is triggered only in `IsoGameCharacter.Hit(HandWeapon, IsoGameCharacter, float, boolean, float, boolean)`. A zombie hit takes `ZombieHitPlayerPacket` -> `Bite.process` -> `BodyDamage.AddRandomDamageFromZombie`, and `Bite` never calls `Hit`.
- **Repository behaviour:** `onWeaponHitCharacter` drops non-player targets, and `runtime.json:59` sets `MaxPlayers=1`. `_close_window` revalidates on active atoms and refuses the table.
- **Impact:** `duel_rapide` and `pression` both list the atom and end in `error` after `hook_window_s` (120 s). Only `demo` and `hardcore` can reach the countdown. README procedure step 2 ("Faites-vous frapper par un zombie") cannot succeed, so AC19 cannot be closed by it.
- **Why tests pass:** `tests/test_lua_attribution.py:282` fires the hook by hand with a player target, and `tests/doubles/fake_mod.py:106` emits `hook_seen` itself.

Independent check: I reproduced the trigger site (the only `Hit` overload in `IsoGameCharacter`, with the string otherwise found only in `LuaEventManager`), the absence of a `Hit` call in `Bite`, and the callers `WeaponHit.process` and `IsoZombie.Hit`. My caller search was capped, so it did not reproduce the Architect's `CombatManager.attackCollisionCheck` entry. That entry is a player swing and would not change the conclusion.

Dispute: X6 validated this at medium/inference because that reviewer did not inspect the bytecode. Its code contribution stands: `selfProved=true` stops generic receipt from supplying the proof, and `beat.hp` is not turned into damage. The stronger external evidence sets the label. The previous round downgraded the hook proof to a boundary the spec accepts; that reasoning no longer holds, because the boundary assumed the hook could fire. One real zombie hit would confirm or refute the premise, and should be run before the fix is finalised.

Fix: derive damage from a signal on the zombie path, such as a server-side health or `BodyDamage` delta in `step()`, and make that signal the `hook_proof`. `OnPlayerGetDamage` is a candidate, but whether it runs on the server is unverified. Until then, remove `damage_taken` from the default preset and update README step 2.

### Major

**X14: a death before `t0` is never judged, and the mod does not report it again** `[medium/code]`
Risk level: confirmed in code; the in-game consequence is untested.
Location: `mods/duelcore/42.21/media/lua/server/DuelCore.lua:461`, `service/engine.py:951`, `service/rules.py:840`

Before `running`, `_take_event` only journals events because `self._core` is `None`. `_enter_running` creates a fresh `MatchCore` without replaying them. `reportDeath` latches `deadPlayers[name]`, and `checkDeaths` clears that latch only once the player is seen alive, so a player who stays dead is not reported again. A death written before `t0` but read after it also fails, because `rules.py:840` requires `te_ms >= 0`. The window covers the lobby as well as the 3 s countdown.

Label change: X14 was inference because its reviewer could not establish two conditions. I checked one of them: `service/engine.py` has no `player_death` handling, and neither `engine.py` nor `rules.py` reads `hp`, so `beat.hp` is validated in `service/protocol.py:161` but never adjudicated. This moves the basis to code. Confidence stays medium because the other condition is unverified: what a dead participant does in PZ (stays online, respawns or disconnects) was not observed.

Severity stays major as the most severe supported version, though the window is narrow. This finding does not decide the verdict by itself. Fix: at `_enter_running`, reject or adjudicate any participant already dead, and add a countdown-death test.

## Minor Findings

### Minor

**A2 (with X7): a one-shot spawn probe turns a transient refusal into a stopped launch and a `solo_ipc` fallback** `[high/code]`
Location: `service/engine.py:1005`

Any status other than `applied` marks the side KO, and `_send_probes` sends one probe per side. `refused` (`asleep`, `in_vehicle`) and `no_square` do not reach `addZombiesInOutfit`, yet the error blames that function. `tests/test_match.py:104-137` asserts this for `asleep`, with both servers down. It matches `spec.md:480-482`, so the weakness is in the design as well as the code.

Label change: raised from medium/inference, because X7's code and test evidence confirm the behaviour directly. How often this happens at first connection remains unknown. X7's note also stands: `solo_ipc` is only reported, and no fallback is launched. Fix: retry within `probe_timeout_s` on `refused` and `no_square`, and amend the spec.

**A5 (with X11): the single-user assumption behind the local API and argv secrets is undocumented** `[high/code]`
Location: `service/api.py:153`, `service/api.py:215`, `service/servers.py:171`

`_refused` filters browsers only. A local process that sends an allowed Host and no Origin can read `/api/state`, including both player passwords, and can POST `start` or `cancel`. The admin password is also in the server's argv. README covers only the client password, and neither README nor `limites-connues.md` mentions the admin one.

Label change: raised from medium/inference, because X11 and my re-read confirm the mechanism in code. Impact still depends on the host: it is small on a single-user desktop, and visibility of another user's argv depends on process-access policy. Fix: document the assumption, or add a per-run token and strip `host_password` from `/api/state` outside the lobby.

**B1 (with X1, X2, X3): a partial request holds a handler thread with no deadline** `[high/code]`
Location: `service/api.py:196`, `service/api.py:57`, `service/api.py:233`

X1 reproduced it: 50 connections with a short body gave 52 threads and no reply. X3 showed the same stall before the body, during the request line and headers, so a fix inside `_body` is incomplete.

Dispute: X2's challenge is valid on reach, and severity stays minor. `main.py:100` binds to the `make_server` default `127.0.0.1`, so "unbounded" applies only to a local client. The read-before-refusal order at `api.py:233` is a deliberate trade-off. The stall holds no engine lock, and X2 measured `GET /api/state` still answering. X2's point about browsers is untested inference. Fix: a class-level `Handler.timeout` covers both phases, but the stdlib closes without a reply and a per-recv timeout does not bound a trickling client, so a real deadline needs extra code.

**A3 (with X8, X5): unguarded file I/O in the running pump ends a match with no result** `[medium/code]`
Location: `service/engine.py:633`, `service/engine.py:1529-1534`, `service/engine.py:1629`, `service/transport.py:85`

Journal writes go through `_write_safely`, but transport calls do not. `prepare_restart`, `poll_ack` and `_read_from_offset` (which catches only `FileNotFoundError`) can raise `OSError` through `pump` to the generic handler in `_run_real`. That handler reports "lancement impossible" for a running match, and `_finish_match` then calls `_fail`, with no `match_end`. The `prepare_restart` path runs exactly when a server has just crashed. X5 measured this with a directory as the reader path: `IsADirectoryError` left `poll()`.

Dispute: X8's challenge is valid. A3 overstated the loss: the last published score and the written journal prefix survive, and `_conclude` (`engine.py:864-868`) already tolerates a failed `match_end` write, so a durable record cannot be guaranteed during a storage failure. What is lost is the terminal result and any unpublished progress. X5's link from B1 to descriptor exhaustion was not demonstrated, and it is not retained. Fix: catch `OSError` during `running` and submit an invalidated result that names the cause. If failing closed is intended, correct the "lancement impossible" message at least.

**A4 (with X9, X10): match cadence and timers run on the wall clock** `[medium/code]`
Location: `service/engine.py:1278`, `service/engine.py:956`, `service/engine.py:1052`

`time.time` is the default clock. It feeds `MatchCore.advance`, `_watch_beats`, `_absence_end`, the probe deadline, the contract window and the countdown end. A forward step can expire startup windows or end a match, and a backward step can freeze the timer.

Label change: basis moved from inference to code on X9's evidence, which traces these uses. Confidence stays medium because no clock step was injected, and whether one occurs during a 25-minute match is an environmental assumption.

Disputes: both challenges are valid. X9 is right that simultaneous absence is conditional, since `pump` reads `out` first and fresh beats can reset `_seen_ms`. X10 is right that A4's fix is incomplete, because Lua stamps events and schedules work with `getTimestampMs`, so a monotonic service clock could diverge from event time. X10 is inference and needs corroboration, by injecting an offset into the service and the emitter separately. Fix: separate pacing from event time.

### Nit

**B2 (with X4): an unterminated output record is reread without a bound** `[high/code]`
Location: `service/transport.py:83`

Severity reduced from minor. X4's challenge is valid: the mechanism is real, since `handle.read()` is unbounded and the offset passes only complete lines, but the trigger is not reachable from the shipped writer. `emit` ends every record with a newline (`DuelCore.lua:290` region), and a crash leaves only a small static tail. A growing tail needs a foreign writer, and X4 measured a 64 MiB tail at 0.027 s per poll against a 250 ms tick. The label is unchanged because the code claim holds; only the severity moved. If capped later, an oversized record must be skipped and reported, or the reader stays stuck.

**A6 (with X12): `open_game` has no phase guard and can launch clients at stopped servers** `[high/code]`
Location: `service/engine.py:1411`, `service/engine.py:1326`

`_open_game_real` checks only for a retained password, and config survives `over`, `error` and `cancel`. Calls run outside the lock and keep no process handle, so repeated calls start several clients. X12 adds that `_closing` is bypassed too. Any guard must keep a reconnect path during `running`, given the disconnect grace.

**A7 (with X13): comments and the spec still state H3 after README recorded it as contradicted** `[high/code]`
Location: `service/engine.py:548`, `service/engine.py:791`, `docs/campaigns/phase1/spec.md:127`, `tests/test_match.py:1190`

`README.md:102` records H3 as contradicted, while these sites still assert it. Dispute: X13's challenge is valid. `_track_age` does not depend on H3, and the test still covers a useful delayed-heartbeat case. Correct the comments, spec and test rationale, but keep the test scenario rather than making its double beat immediately as A7 suggested.

## Operational Risk Assessment

- **Real-mode readiness:** the default launch path fails after 120 s of lobby time, with an error naming the atom and hook. It fails closed, so nothing is corrupted, but no duel can run on `duel_rapide` or `pression` until A1 is fixed.
- **Proof gap:** 1044 passing tests (the Architect's count, not re-run here) did not detect A1, because the doubles assert what the mod would do if the hook fired. Green tests are not evidence for hook behaviour. README already marks the hook signatures as unproven, and AC19 stays open until a two-person session passes.
- **Environmental assumptions that may not hold:**
  - A single-user host (A5).
  - A stable host clock across a 25-minute match, which suspend/resume or NTP correction can break (A4).
  - Chunks loaded and the player awake at first connection (A2).
  - Reliable local disk during a match (A3).
- **Failure modes:**
  - A crash combined with a file error ends a match without a terminal result, but keeps the journal prefix and last published score (A3).
  - A death before `t0` can start a match with a dead participant (X14).
  - Local processes can hold threads and sockets, but the engine lock is not held and remote reach was not shown (B1).
- **Resource limits:** thread growth from stalled requests is bounded only by the local client (B1). The `out` read has no cap, but no reachable trigger exists with the shipped mod (B2).
- **Recommended order:** run one real zombie-hit session to confirm A1, then fix A1 and X14 with tests that do not rely on hand-fired hooks, then rerun the README verdict procedure. Everything else can follow as hardening.

READ:cb6c479983e95d85
READ: /tmp/review-phase1-final-1/READ:cb6c479983e95d85

## Epistemic label distribution

{
  "basis": {
    "code": 5,
    "external": 1,
    "inference": 3,
    "spec": 0
  },
  "combined": {
    "high/code": 4,
    "high/external": 1,
    "medium/code": 1,
    "medium/inference": 3
  },
  "confidence": {
    "high": 5,
    "low": 0,
    "medium": 4
  }
}
