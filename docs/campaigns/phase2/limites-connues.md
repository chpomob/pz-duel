# Known limits of phase 2

*English — [version française](limites-connues.fr.md)*

Phase 2 was written on Linux. **No step of it has been tried on a real Windows desktop, and no step has
run against the real Windows game.** The CI runs on `windows-latest` but has no game: it proves our
code against a double, not against Project Zomboid.

How to read this file: a proof is written `file::test`, and `tests/test_ci_workflows.py` checks that
each name exists. A name in a `win32` block only proves something once it has run on `windows-latest`.

## Proof status

- **Linux**: `python -m pytest -q` is green, with Windows branches run through an injected platform.
- **Windows**: the `pytest-windows` and `build-windows` jobs of `.github/workflows/tests.yml` have run
  on `windows-latest`. A test of a `win32` block cited here proves something from the runs recorded in
  [Real CI and release runs (P18)](#real-ci-and-release-runs-p18) on, for what a runner can show.
- **CI runs (AC49)**: their URLs are in that same section.

## Disk durability on Windows (as of 2026-11-07)

A real match no longer stops when worlds are prepared: since P18F1, `service/snapshots.py` calls
`sync_disk`, in `begin_creation` and in the staged world replacement, and never `os.sync`, which does
not exist on Windows. What remains is a narrower guarantee on that system. `sync_disk` flushes each
file of a world one by one, so directory entries, read-only files and the targets of links stay outside
it: `tests/test_disk_sync.py::test_windows_flushes_every_file_without_os_sync` and
`tests/test_disk_sync.py::test_windows_skips_a_read_only_file`. No power cut has been simulated on a
real Windows disk.

## Not proven on the real game

### `StartServer64.bat` passing on its 8 arguments

The Linux install does not contain this file, so it has never been read (H4). Nothing shows that it
hands `-servername`, `-port`, `-udpport`, `-adminpassword` and their values to the server. If it drops
one, the server starts on the wrong name or port, or the launch times out.

What is proven is our side only: the command we build, and its delivery to a double.

- `tests/test_platform_windows.py::test_windows_command_keeps_its_eight_arguments`
- `tests/test_platform_windows.py::test_engine_launches_windows_servers_and_client_as_specified`

### The value of `-Dzomboid.steam` against a `-nosteam` client

`start-server.sh` sets `-Dzomboid.steam=0`, and we do not pass that option ourselves. Which value
`StartServer64.bat` sets is unknown. If it starts the server with Steam on, a `-nosteam` client may be
refused.

What is proven: the arguments the client receives, and that the server command adds no such option.

- `tests/test_guest_launcher.py::test_script_asks_the_client_for_the_specified_arguments`
- `tests/test_platform_windows.py::test_windows_command_keeps_its_eight_arguments`

### Its end-of-script behaviour

Whether the script ends with a `PAUSE`, and what it does once the server exits, is unknown. The stop
path therefore does not assume it returns: it ends the whole tree through a Job object.

What is proven, against a double whose script ends with `PAUSE`:

- `tests/test_platform_windows.py::test_windows_stop_ends_a_paused_tree_at_the_sigkill_stage`
- `tests/test_platform_windows.py::test_windows_early_child_cannot_escape_the_job`

### A guest actually connecting

No guest has ever joined a server launched by PZ Duel, on any system. A `.bat` has only ever started a
double of `ProjectZomboid64.exe`. The mod check on the client side, the `+connect` and `+password`
arguments on the real client, and the handshake itself are all unobserved. The in-game procedure in
the README is still the only way to settle this.

What is proven: the mod lands intact and the double receives the expected arguments.

- `tests/test_guest_launcher.py::test_windows_launcher_installs_the_mod_and_connects`

### Reaching an entered address from the Internet

The service does not test an entered address (H7): it only refuses unusable ones, then writes the
address into the link and the `.bat`. Router, ISP (CGNAT included) and firewall are the host's business.
There is no relay and no NAT traversal.

What is proven: no connection is attempted, the address is used unchanged, and the host is told which
ports to open.

- `tests/test_share.py::test_ac58_entry_and_generation_touch_no_network`
- `tests/test_share.py::test_ac43_entered_address_reaches_link_qr_and_launcher`
- `tests/test_share.py::test_ac57_entered_address_names_the_shipped_ports`

## Never observed on Windows

- **The Windows Firewall prompt (H5).** PZ Duel neither opens nor drives the firewall, since that would
  need elevation. Which programs trigger a prompt, and when, has not been seen: the game's server, the
  share listener and the preflight's port check all bind beyond the loopback. A dismissed prompt can
  leave the guest unable to connect, with nothing in PZ Duel to say so.
- **SmartScreen.** The binary and the `.bat` are unsigned by decision. The wording of the warning and
  the steps in the README come from how Windows usually behaves, not from a test here.

## Guest `.bat` (P11)

No Windows PowerShell 5.1 has ever run it: its logic ran on PowerShell 7 under Linux and its `cmd` layer
under wine with a stand-in. These are observations, not proofs. The first real run is the `win32` block
of `tests/test_guest_launcher.py`.

- **PowerShell blocked by a policy, or in constrained language mode**: the `.bat` then fails. Not tested.
- **Mark of the Web**: a downloaded `.bat` carries it, hence SmartScreen. Not tested.
- **Antivirus heuristics**: a `.bat` with PowerShell and base64 may be flagged or removed. Not tested.
- **The client password is visible** on the game's command line, to any process of the guest's session.
- **`%` in the path of the `.bat`**: wine drops it, so only real Windows can answer:
  `tests/test_guest_launcher.py::test_windows_self_path_with_cmd_characters`.
- **`Read-Host` on redirected input**:
  `tests/test_guest_launcher.py::test_windows_closed_input_does_not_hang_the_pause` and
  `tests/test_guest_launcher.py::test_windows_empty_answer_fails_and_keeps_the_mod`.
- **Typed passwords**: only the whitelist can travel in a `.bat`; anything else gets no `.bat`:
  `tests/test_guest_launcher.py::test_hostile_password_is_refused_without_being_cited`.
- **A guest on Linux** gets no `.bat` and joins by hand (H2):
  `tests/test_share.py::test_manual_block_stays_for_a_linux_guest`.

## Guest `.bat` verification code (P19F3)

The invitation is **meant for a trusted network**. The link is plain HTTP, as AC37 requires, and the
`.bat` runs PowerShell: someone on the network path can replace the answer, and what they serve then runs
under the guest's account. The SHA-256 sums inside the `.bat` only tie its embedded files to each other.
**The code is the only protection against a replaced file.**

- **What the code is**: the first 12 hexadecimal characters of the SHA-256 of the whole `.bat`, as served.
  The lobby shows it next to the invitation, and the host reads it to the guest, never next to the link:
  `tests/test_share.py::test_shown_code_comes_from_the_very_bytes_served` and
  `tests/test_share.py::test_changed_byte_no_longer_matches_the_code_and_the_check_refuses`.
- **Only a comparison made before opening the file stops an attacker.** The guest runs
  `certutil -hashfile pz-duel-join.bat SHA256`, and the result must start with the code. A replaced file
  gives another result, and the guest refuses to open it:
  `tests/test_guest_launcher.py::test_windows_code_matches_what_certutil_prints`.
- **The check inside the `.bat` is no protection against an attacker.** It refuses a code that does not
  match the file, before anything is installed:
  `tests/test_guest_launcher.py::test_embedded_check_refuses_the_right_code_on_a_changed_byte` and
  `tests/test_guest_launcher.py::test_windows_changed_byte_is_refused_with_the_code_of_the_host`. But it
  runs inside the file it checks: a replaced `.bat` can leave it out, accept any code, or run its own
  commands first. It only stops a damaged file, or a change that leaves the check in place.
- **A guest who skips `certutil` is not protected**, and nothing in PZ Duel can make them run it.
- **The code protects the file, not the secrecy of the invitation.** The password still crosses the
  network in clear. Someone who can read the traffic can use it, whatever the code says.
- **48 bits**: a replaced file with the same code is possible at the cost of about 2^48 hashes. That is
  hours of GPU time, far longer than a lobby usually lasts, but not out of reach.
- **The code covers the password**, since it covers the whole file, but it reveals nothing the `.bat`
  does not already give the guest. `host_password` is not in the `.bat`.
- **`-ExecutionPolicy Bypass` was not tightened, because there was nothing to tighten.** The flag only
  applies to the PowerShell process it starts and to that process's children. It writes no persistent
  setting, a Group Policy overrides it, and the policy does not govern `-Command` text anyway:
  `tests/test_guest_launcher.py::test_execution_policy_stays_a_process_flag` and
  `tests/test_guest_launcher.py::test_windows_execution_policy_is_unchanged_after_a_run`.
- **The check has run on PowerShell 7 under Linux only.** `set /p`, `certutil` and PowerShell 5.1 are first
  exercised by the `win32` block:
  `tests/test_guest_launcher.py::test_windows_wrong_code_stops_before_the_game`.

## Processes (P5)

- **Resuming a suspended server** goes through `NtResumeProcess`, stable but undocumented, with a fallback
  on the process's threads. Proven against an injected kernel only:
  `tests/test_platform_windows.py::test_resume_goes_through_nt_resume_process` and
  `tests/test_platform_windows.py::test_resume_falls_back_on_the_threads_of_the_process`.
- **Servers dying with the service** rests on the Job:
  `tests/test_platform_windows.py::test_windows_forced_end_of_the_service_leaves_no_double` and
  `tests/test_platform_windows.py::test_windows_closed_console_leaves_no_double`.
- **The host's client** leaves the Job if Windows allows it. Otherwise it dies with the service:
  `tests/test_platform_windows.py::test_windows_client_falls_back_when_the_job_refuses_breakaway`.
- **A character `cmd` would interpret** in an argument refuses the launch rather than risk an injection.
  The check reads every argument, game folder included; the test uses the password:
  `tests/test_platform_windows.py::test_interpreted_password_is_refused_without_being_cited`.

## Binary (P15)

- **A forced stop leaves the `_MEI*` folder in `%TEMP%`.** The onefile loader cleans it only on a normal
  exit. The space is lost until someone deletes it.
- **A forced stop of the loader** ends the service, because the entry point watches the loader:
  `packaging/smoke_test.py::check_forced_end`. It passed on Windows builds under wine, Python 3.11.9
  and 3.13.1; real `windows-latest` still has to confirm it.
- **On a Linux frozen binary** the same watch does not stop the servers, since there is no Job.

## Share listener (P13)

- **Rebinding the share port for a rematch right after a download** is proven on Linux only. Under
  Windows, `SO_EXCLUSIVEADDRUSE` stays and a connection in TIME_WAIT may refuse the bind:
  `tests/test_share.py::test_rematch_rebinds_right_after_a_download`. If it does, the lobby still opens
  without a link, and the `.bat` can still be saved:
  `tests/test_share.py::test_ac42_taken_port_keeps_the_lobby_and_the_save`.
- **The link is plain HTTP.** The token and the `.bat`, password included, cross the network in clear.
- **The link lives in the lobby only**, and each lobby has its own token:
  `tests/test_share.py::test_ac40_every_exit_closes_the_listener` and
  `tests/test_share.py::test_ac38_rematch_keeps_the_launcher_and_changes_the_token`.

## Interface (P14)

- **A failed preflight stays in the `error` phase.** Setup routes then refuse, and `Try again` only reruns
  the checks. A mod or folder defect needs Quit and reopen, as the screen says:
  `tests/test_setup_flow.py::test_error_screen_can_run_the_checks_again`. The fix belongs in
  `service/engine.py`.
- **The interface mixes French and English**: phase 2 screens are in English, older ones in French.

## Real CI and release runs (P18)

Everything below was produced by GitHub Actions on the public repository, not locally. Dates are UTC.

- **`tests.yml`** runs on `master`, Linux 3.12/3.13/3.14 plus `pytest-windows` on the same three
  versions and `build-windows`:
  - [42316172795](https://github.com/chpomob/pz-duel/actions/runs/42316172795) — commit `5bfa440`,
    **green twice** (the second pass was a rerun, because the defect fixed in P18F4 was a race).
- **`release.yml`** run [42319708603](https://github.com/chpomob/pz-duel/actions/runs/42319708603)
  on tag `v0.0.0-phase2-test` created release
  [v0.0.0-phase2-test](https://github.com/chpomob/pz-duel/releases/tag/v0.0.0-phase2-test), marked
  **pre-release**, with exactly two assets: `pz-duel.exe` (10 351 421 bytes) and
  `pz-duel.exe.sha256`. The downloaded binary matches its checksum:
  `db754a39e7a8d6a2b2bea76514ae90916ee76d5a459efcf9e8bf7e1a4b6a7608`.
- **The Windows jobs are the reference for anything platform-specific.** They caught five waves of
  defects that 2 000+ Linux tests could not see, each fixed by its own reviewed step: `os.sync`
  missing on Windows (P18F1), ten failures in the process-tree and stop-stage doubles (P18F2), two
  waits with no margin (P18F3), and a connection refusal closed with RST instead of FIN (P18F4).
  Earlier red runs stay in the history as evidence:
  [42296912989](https://github.com/chpomob/pz-duel/actions/runs/42296912989),
  [42309448633](https://github.com/chpomob/pz-duel/actions/runs/42309448633),
  [42313084724](https://github.com/chpomob/pz-duel/actions/runs/42313084724).
- **The test release stays public until the operator deletes it.** It is a pre-release on a
  `v0.0.0-*` tag, contains an unsigned binary, and exists to prove `release.yml` end to end.
- **No Windows machine was ever touched by hand.** All Windows evidence comes from
  `windows-latest` runners; a real desktop was never used to play a duel.
