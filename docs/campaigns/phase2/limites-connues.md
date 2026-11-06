# Known limits of phase 2

*English — [version française](limites-connues.fr.md)*

Phase 2 was written on Linux. **No step of it has run on a real Windows machine, and no step has run
against the real Windows game.** The CI has no game: it proves our code against a double, not against
Project Zomboid.

How to read this file: a proof is written `file::test`, and `tests/test_ci_workflows.py` checks that
each name exists. A name in a `win32` block only proves something once it has run on `windows-latest`.

## Proof status

- **Linux**: `python -m pytest -q` is green, with Windows branches run through an injected platform.
- **Windows**: the `pytest-windows` and `build-windows` jobs of `.github/workflows/tests.yml` exist, but
  no run has been recorded yet. Until then, every test of a `win32` block cited here is written, not
  proven.
- **CI runs (AC49)**: none recorded yet. Step P18 adds their URLs to this section.

## Blocker: a real match cannot start on Windows

`service/snapshots.py` calls `os.sync()` in `begin_creation` and in the staged world replacement.
`os.sync` does not exist on Windows, so **every real match stops with `AttributeError` when worlds are
prepared**, before any server starts. It also keeps `windows-latest` red, which blocks the release
that the README's download step relies on. Measured locally under wine: 168 failures and 15 errors
out of 1,418 tests without a stand-in for `os.sync`, 3 failures with one. That is an observation, not a
proof.

`service/snapshots.py` is outside the phase's `targets`, so only the operator can open this fix. A
no-op stand-in would silently drop the durability that file relies on.

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
