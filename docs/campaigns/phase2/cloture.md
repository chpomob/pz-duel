# Phase 2 — closing report

Phase 2 added Windows support, packaging and a no-command-line experience on top of the phase 1
service. This file records what shipped, what proves it, and what remains open. It closes P18
(real CI and release runs) and P19 (whole-branch review).

## What shipped

| Step | Delivered |
|------|-----------|
| P1-P8 | Path resolution, Windows primitives, file and process handling, preflight, binary launch |
| P9-P12 | First-run state and routes, quitting from the UI, guest launcher generation, invitation model |
| P13-P17 | Share listener, packaging (`pz-duel.exe`), CI, release workflow, documentation |
| P18 | Real CI and release runs on GitHub Actions, URLs recorded in `limites-connues.md` |
| P19 | Whole-branch review, two passes, and the fix steps they produced |

## Evidence

- `tests.yml`, run [42368769474](https://github.com/chpomob/pz-duel/actions/runs/42368769474) on
  `cef2be5`: seven jobs, seven success — Linux 3.12/3.13/3.14, Windows 3.12/3.13/3.14, `build-windows`.
- `release.yml`, run [42319708603](https://github.com/chpomob/pz-duel/actions/runs/42319708603) on tag
  `v0.0.0-phase2-test`: pre-release with exactly two assets, `pz-duel.exe` (10 351 421 bytes) and its
  `.sha256`, downloaded binary matching `db754a39…a7608`.
- Local suite: 2 238 passed, 16 skipped (the nine-plus `pwsh` tests run on `ubuntu-latest`, not here).
- The whole record of red runs is kept in `limites-connues.md`: they are the evidence that the Windows
  jobs catch what Linux cannot.

## What the two review passes found

Twelve findings in the first pass (4 major), nine in the second (1 major). All were fixed by a
reviewed step; none was waived.

- **Launch and snapshots (P19F1)**: the host client ran with no working directory; world and mod
  directory renames had no Windows lock tolerance; a failed rollback could delete the guest's mod.
- **Security (P19F3, P19F6)**: the guest `.bat` travels over plain HTTP, so the only control that holds
  is a `certutil` comparison the guest performs **before** opening it — now documented in both READMEs,
  with the code read aloud by the host. The code itself went from 48 to 64 bits.
- **Guest launcher behaviour (P19F6)**: a game that starts and exits at once is now reported as a
  failure, with the previous mod restored; a successful launch keeps the previous `duelcore` as a
  named backup instead of deleting it.
- **Infrastructure (P19F2, P19F5, P19F7)**: file logging for the windowed binary, a release job that
  waits for the test suites, actions pinned by commit, `permissions:` declared, `--require-hashes` on
  the build dependencies, a per-peer cap on the share listener, an origin check on the launcher route,
  and a YAML parse test — which the `--require-hashes` line needed, since `:all:` unquoted had made
  both workflows unloadable and produced two runs with zero jobs.
- **Windows-specific defects found by CI, not by reviewers**: `os.sync` missing, ten failures in the
  process-tree and stop-stage doubles, two waits with no margin, a refusal closed with RST instead of
  FIN, and a test that shelled out to `powershell.exe` and failed when the runner would not answer.

## What remains open

- **A real Windows desktop was never used.** Every Windows claim comes from `windows-latest` runners.
  The `UseShellExecute` hand-off is the one behaviour a runner cannot settle: the launcher's
  "did the game start" check assumes the client process is observable, and that needs one run on a
  real machine with PZ installed.
- **Two findings are inference-only and stay open as such**: whether browsers actually send no `Origin`
  on a cross-site top-level navigation, and the SHA-256 throughput assumed by the code-length
  calibration. The mitigations are in place; the measurements are not.
- **No Workshop publication.** Mods are shipped in the repository; the Workshop path was studied but
  never executed.
- **The phase 1 duel chain is still unproven end to end**: zombie-kill attribution in multiplayer,
  server-side spawn visibility, and crafting via `OnProcessAction` remain unverified against the real
  game. Phase 2 hardened the transport around them; it did not test them.
- **The test release `v0.0.0-phase2-test` is public** and stays so until the operator deletes it.

## Housekeeping, not code

- Three fix commits carry the loop's squash subject (`squash: phase2-p19N — adversarial approved`)
  instead of the conventional one. The harness restores the subject; `adversarial_loop.py` does not.
  The fix belongs in the tool, not in a history rewrite.
