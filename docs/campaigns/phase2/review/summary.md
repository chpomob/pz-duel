# Phase 2 — first branch review, findings and closure (P19)

The raw artifacts of this pass (`01_architect.txt`, `02_inspector.txt`, cross-reviews, `final.json`,
`review.md`) are kept alongside this file. The loop harness stashes untracked files before building and
restores them on merge, so they were temporarily absent from the worktree mid-run — they were never
lost. This file remains the compact index; the step that closed each finding is in the last column.

Verdict: **REQUEST_CHANGES** — 4 major, 4 minor, 4 nit (12 findings).

| # | Severity | Finding | Closed by |
|---|----------|---------|-----------|
| A1 | major | `_spawn_detached` launches the host client with no `cwd`, so a double-clicked `pz-duel.exe` runs the game in whatever folder it was downloaded to. | P19F1 |
| A2 | major | World and mod directory renames have no Windows lock tolerance: `_install` then `os.rename` with no retry, while `replace_file` exists for exactly that. | P19F1 |
| A3 | major | The guest `.bat` is delivered over plain HTTP and runs PowerShell with `-ExecutionPolicy Bypass`: anyone on the network path can replace it and execute code. | P19F3 (option 1 + 3, operator decision) |
| A4 | minor | README and known limits still declare a fixed defect as a blocker (`os.sync`), contradicting the tree. | P19F2 |
| A5 | minor | The windowed binary loses every log record: no `basicConfig`, no `FileHandler`, `sys.stderr` is `None`. | P19F2 |
| A6 | minor | `release.yml` publishes without running the tests for the tagged commit. | P19F2, extended in P19F5 |
| A7 | nit | A non-2xx `/api/state` body replaces the UI state, so an error body without `phase` makes the badge lie. | P19F2 |
| A8 | nit | `setup_state` hashes the whole mod while holding the engine lock. | P19F2 |
| A9 | nit | The domain layer depends on the HTTP layer (`engine` imports `LAUNCHER_NAME`, `share` borrows `Handler` internals). | P19F2 (`service/http_kit.py`) |
| A10 | nit | The review ran on an empty diff range: the harness had checked out the base commit, so changed-line membership could not be evaluated. | Procedural: noted, artifact loss above is the same harness weakness |
| B1 | major | If installing the staged mod fails **and** restoring `previous` also raises, the `TemporaryDirectory` unwinds and deletes the guest's original mod. Data loss. | P19F1 |
| B2 | minor | Framing refusals close without draining the request: an oversized body can get a reset instead of the JSON refusal. | P19F2 |

## What this pass was worth

Four of the twelve were real defects a player would have hit: the two launch/rename faults on Windows,
the data loss in B1, and the MITM-executable launcher in A3. None of them was visible to the 2 000+
test suite, because the suite exercised the same assumptions the code made.
