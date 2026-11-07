"""Le `.bat` de l'invité (R6) : ses octets partout, son exécution réelle sous Windows (D-A)."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from doubles.fake_server import compile_windows_client, plan_windows_client
from doubles.launcher_check import CODE_REFUSED, run_embedded_check
from service import guest_launcher
from service.guest_launcher import (
    CLIENT_SETTLE_S,
    CLIENT_START_S,
    FILE_MARK,
    JOIN_MARK,
    MAX_LAUNCHER_BYTES,
    MAX_LINE_BYTES,
    LAUNCHER_CODE_CHARS,
    MOD_MARK,
    PREVIOUS_SUFFIX,
    LauncherRefused,
    build_launcher,
    is_usable_address,
    launcher_code,
)
from service.preflight import MOD_ID, MOD_SRC, mod_fingerprint, steam_from_registry
from service.servers import gen_secret

ADDRESS = "192.168.1.20"
PASSWORD = "0123abcdef"
# AC33, mot pour mot.
HOSTILE_PASSWORDS = (
    "100%",
    "a&b",
    "a^b",
    "a!b",
    'a"b',
    "a b",
    "%PATH%",
    "a|b>c",
    "pässwörd",
    "x&echo pwned>pwned.txt",
)
CRLF = b"\r\n"
HEX_DRAWS = 200


@pytest.fixture
def port(runtime):
    return runtime["ports"]["B"]["game"]


@pytest.fixture
def launcher(port):
    return build_launcher(ADDRESS, port, PASSWORD, MOD_SRC)


def lines_of(launcher: bytes) -> list[bytes]:
    assert launcher.endswith(CRLF)
    return launcher[: -len(CRLF)].split(CRLF)


def unpack_mod(launcher: bytes, target: Path) -> int:
    """Refait le mod à partir du `.bat`, comme son PowerShell, et rend le compte annoncé."""
    lines = [line.decode("ascii") for line in lines_of(launcher)]
    start = next(i for i, line in enumerate(lines) if line.startswith(MOD_MARK))
    announced = int(lines[start].split()[1])
    entries: list[tuple[str, str, list[str]]] = []
    for line in lines[start + 1 :]:
        if line.startswith(FILE_MARK):
            _, digest, name = line.split(" ")
            entries.append((digest, base64.b64decode(name).decode("utf-8"), []))
        else:
            entries[-1][2].append(line)
    assert [name for _, name, _ in entries] == sorted(name for _, name, _ in entries)
    for digest, name, chunks in entries:
        content = base64.b64decode("".join(chunks), validate=True)
        assert hashlib.sha256(content).hexdigest() == digest
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return announced


def alter_payload(launcher: bytes) -> bytes:
    """Change un caractère du contenu d'un fichier : le base64 reste valide."""
    lines = lines_of(launcher)
    first = next(i for i, line in enumerate(lines) if line.startswith(FILE_MARK.encode())) + 1
    replacement = b"B" if lines[first][:1] != b"B" else b"C"
    lines[first] = replacement + lines[first][1:]
    return CRLF.join(lines) + CRLF


# AC30 : les octets -------------------------------------------------------------------------


def test_launcher_is_ascii_with_only_crlf_line_ends(launcher):
    assert launcher.isascii()
    assert launcher.count(b"\n") == launcher.count(CRLF)
    assert launcher.count(b"\r") == launcher.count(CRLF)


def test_launcher_respects_line_and_size_limits(launcher):
    assert max(map(len, lines_of(launcher))) <= MAX_LINE_BYTES == 8000
    assert len(launcher) < MAX_LAUNCHER_BYTES == 256 * 1024


def test_two_generations_are_identical(launcher, port):
    assert build_launcher(ADDRESS, port, PASSWORD, MOD_SRC) == launcher


def test_generation_ignores_where_the_mod_lies(launcher, port, tmp_path):
    copy = tmp_path / "elsewhere" / "copy"
    shutil.copytree(MOD_SRC, copy)
    assert build_launcher(ADDRESS, port, PASSWORD, copy) == launcher


def test_payload_rebuilds_the_exact_mod(launcher, tmp_path):
    announced = unpack_mod(launcher, tmp_path)
    assert announced == sum(path.is_file() for path in MOD_SRC.rglob("*"))
    assert mod_fingerprint(tmp_path) == mod_fingerprint(MOD_SRC)


def test_payload_carries_any_relative_name(port, tmp_path):
    mod = tmp_path / "mod"
    (mod / "dossier é").mkdir(parents=True)
    (mod / "dossier é" / "fichier vide.txt").write_bytes(b"")
    (mod / "binaire").write_bytes(bytes(range(256)))

    launcher = build_launcher(ADDRESS, port, PASSWORD, mod)

    assert launcher.isascii()
    assert unpack_mod(launcher, tmp_path / "out") == 2
    assert mod_fingerprint(tmp_path / "out") == mod_fingerprint(mod)


def test_a_long_file_is_cut_under_the_line_limit(port, tmp_path):
    mod = tmp_path / "mod"
    mod.mkdir()
    (mod / "gros").write_bytes(b"\xff" * (MAX_LINE_BYTES * 2))

    launcher = build_launcher(ADDRESS, port, PASSWORD, mod)

    assert max(map(len, lines_of(launcher))) <= MAX_LINE_BYTES
    assert unpack_mod(launcher, tmp_path / "out") == 1


def test_oversized_mod_is_refused(port, tmp_path):
    mod = tmp_path / "mod"
    mod.mkdir()
    (mod / "gros").write_bytes(b"\0" * MAX_LAUNCHER_BYTES)
    with pytest.raises(LauncherRefused):
        build_launcher(ADDRESS, port, PASSWORD, mod)


def test_password_too_long_for_a_line_is_refused(port):
    with pytest.raises(LauncherRefused):
        build_launcher(ADDRESS, port, "a" * MAX_LINE_BYTES, MOD_SRC)


@pytest.mark.parametrize(
    "make", [lambda path: None, lambda path: path.mkdir()], ids=["absent", "vide"]
)
def test_mod_without_files_is_refused(port, tmp_path, make):
    folder = tmp_path / "mod"
    make(folder)
    with pytest.raises(LauncherRefused):
        build_launcher(ADDRESS, port, PASSWORD, folder)


# Le format que lit `cmd`, puis PowerShell ---------------------------------------------------


def batch_header(launcher: bytes) -> list[bytes]:
    lines = lines_of(launcher)
    return lines[: lines.index(guest_launcher.SCRIPT_MARK.encode())]


def test_batch_records_its_own_path_without_delayed_expansion(launcher):
    header = batch_header(launcher)
    assert b'set "PZDUEL_SELF=%~f0"' in header
    assert any(b"DisableDelayedExpansion" in line for line in header)
    assert not any(b"EnableDelayedExpansion" in line for line in header)


def test_batch_pauses_on_failure_and_returns_the_code(launcher):
    header = batch_header(launcher)
    assert b'if not "%PZDUEL_CODE%"=="0" pause' in header
    assert header[-1] == b"exit /b %PZDUEL_CODE%"


def test_batch_never_finds_powershell_in_the_current_folder(launcher):
    (call,) = [line for line in batch_header(launcher) if b"powershell.exe" in line]
    assert call.startswith(b'"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" ')
    assert b"-EncodedCommand" not in call
    assert call.count(b'"') == 4


def test_script_text_is_the_same_for_every_duel(launcher, port):
    other = build_launcher("example.org", port + 1, "autre", MOD_SRC)
    head = launcher[: launcher.index(JOIN_MARK.encode())]
    assert other[: other.index(JOIN_MARK.encode())] == head
    assert PASSWORD.encode() not in head


def test_values_travel_on_one_data_line_only(launcher, port):
    assert launcher.count(PASSWORD.encode()) == 1
    assert f"{JOIN_MARK} {ADDRESS} {port} {PASSWORD}".encode() in lines_of(launcher)


def test_script_asks_the_client_for_the_specified_arguments(launcher):
    assert b"'-nosteam +connect ' + $Join[1] + ':' + $Join[2] + ' +password ' + $Join[3]" in launcher


def test_script_messages_are_english_ascii():
    assert guest_launcher.POWERSHELL.isascii()
    assert guest_launcher.BATCH.isascii()


def test_markers_stand_alone_for_powershell(launcher):
    """L'en-tête les cite dans une ligne plus longue : seule la ligne exacte compte."""
    lines = lines_of(launcher)
    for mark in (guest_launcher.SCRIPT_MARK, guest_launcher.DATA_MARK):
        assert lines.count(mark.encode()) == 1


# Le code de vérification --------------------------------------------------------------------


def test_code_is_the_start_of_the_sha256_of_the_whole_file(launcher):
    assert launcher_code(launcher) == hashlib.sha256(launcher).hexdigest()[:LAUNCHER_CODE_CHARS]
    assert f"$CodeChars = {LAUNCHER_CODE_CHARS}\n" in guest_launcher.POWERSHELL


def test_code_is_64_bits():
    """P19F6 : 48 bits laissaient un faux fichier à quelques heures de GPU."""
    assert LAUNCHER_CODE_CHARS * 4 == 64
    assert len(launcher_code(b"x")) == 16


def test_code_is_stable_over_two_generations_and_follows_each_input(launcher, port, tmp_path):
    mod = tmp_path / "mod"
    shutil.copytree(MOD_SRC, mod)
    code = launcher_code(launcher)
    assert launcher_code(build_launcher(ADDRESS, port, PASSWORD, mod)) == code

    (mod / "ajout.txt").write_bytes(b"x")
    others = (
        build_launcher("192.168.1.21", port, PASSWORD, MOD_SRC),
        build_launcher(ADDRESS, port + 1, PASSWORD, MOD_SRC),
        build_launcher(ADDRESS, port, PASSWORD + "0", MOD_SRC),
        build_launcher(ADDRESS, port, PASSWORD, mod),
    )
    assert code not in map(launcher_code, others)


@pytest.mark.parametrize("where", [0, 0.5, -3], ids=["en-tete", "milieu", "fin"])
def test_any_changed_byte_changes_the_code(launcher, where):
    index = int(len(launcher) * where) if where >= 0 else where
    changed = bytearray(launcher)
    changed[index] ^= 1
    assert launcher_code(bytes(changed)) != launcher_code(launcher)


def test_code_is_not_in_the_file_and_shows_no_secret(launcher):
    code = launcher_code(launcher)
    assert code.encode() not in launcher
    assert PASSWORD not in code


def test_batch_asks_the_code_with_no_default_before_powershell(launcher):
    header = batch_header(launcher)
    (ask,) = [i for i, line in enumerate(header) if line.startswith(b"set /p ")]
    call = next(i for i, line in enumerate(header) if b"powershell.exe" in line)
    assert header[ask - 1] == b'set "PZDUEL_TYPED="'
    assert header[ask].startswith(b'set /p "PZDUEL_TYPED=')
    assert ask < call


def test_cmd_never_expands_what_the_guest_types(launcher):
    assert b"%PZDUEL_TYPED%" not in launcher
    assert b"!PZDUEL_TYPED!" not in launcher


def test_check_runs_before_anything_is_looked_for_or_written():
    script = guest_launcher.POWERSHELL
    check = script.index("if (-not (Test-Code $env:PZDUEL_SELF $env:PZDUEL_TYPED))")
    assert check < script.index("$client = Find-Client")
    assert check < script.index("Write-Mod $Lines $data $staged")


def test_launcher_never_shows_the_code_it_computes():
    """L'invité doit saisir ce que l'hôte lit, pas recopier ce que le fichier affiche."""
    body = guest_launcher.POWERSHELL.split("function Test-Code")[1].split("function ")[0]
    assert "Write-" not in body


def test_launcher_says_trusted_network_and_only_protection(launcher):
    text = b" ".join(batch_header(launcher))
    assert b"trusted network" in text
    assert b"only protection" in text
    assert b"certutil -hashfile" in text


POLICY_PROBE = (
    "powershell.exe", "-NoProfile", "-Command", "Get-ExecutionPolicy -List | Out-String"
)


def assert_policy_only_on_the_process_line(launcher: bytes) -> None:
    """Preuve statique : la seule politique est le drapeau du processus `powershell`.

    PowerShell ignore la casse : `set-executionpolicy`, `$env:PSExecutionPolicyPreference` ou une
    clé `...\\ExecutionPolicy` du registre contiennent tous `executionpolicy`, en un mot.
    """
    lowered = launcher.lower()
    assert lowered.count(b"executionpolicy") == 1, "une politique s'ajoute au drapeau du processus"
    (call,) = [line for line in batch_header(launcher) if b"powershell.exe" in line]
    flags, command = call.split(b" -Command ", 1)
    assert flags.endswith(b" -ExecutionPolicy Bypass")
    assert b"executionpolicy" not in command.lower()
    assert b"set-executionpolicy" not in lowered


def execution_policies() -> bytes:
    """Sonde le runner ; s'il ne répond pas, cette moitié est sautée, motif à l'appui."""
    try:
        done = subprocess.run(POLICY_PROBE, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"sonde de politique d'exécution impossible : {error!r}")
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        first = lines[0] if lines else "<stderr vide>"
        pytest.skip(f"sonde de politique d'exécution refusée (code {done.returncode}) : {first}")
    return done.stdout


def test_execution_policy_stays_a_process_flag(launcher):
    """`-ExecutionPolicy` ne vaut que pour le processus : rien de persistant ne doit s'y ajouter."""
    assert launcher.count(b"-ExecutionPolicy Bypass") == 1
    assert_policy_only_on_the_process_line(launcher)


@pytest.mark.parametrize(
    "added",
    [
        b"Set-ExecutionPolicy Bypass -Scope CurrentUser -Force",
        b"set-executionpolicy unrestricted",
        b"$env:PSExecutionPolicyPreference='Bypass'",
    ],
)
def test_a_persisted_policy_in_the_launcher_is_caught(launcher, added):
    """Mutation (a) gardée : ce qu'on glisserait dans le script ou le `-Command` est vu."""
    script = guest_launcher.SCRIPT_MARK.encode() + CRLF
    in_script = launcher.replace(script, script + added + CRLF, 1)
    in_command = launcher.replace(b"$ErrorActionPreference='Stop';", added + b";", 1)
    for mutant in (in_script, in_command):
        assert mutant != launcher
        with pytest.raises(AssertionError):
            assert_policy_only_on_the_process_line(mutant)


def test_a_probe_that_refuses_to_answer_skips_with_its_reason(monkeypatch):
    """Mutation (b) gardée : un runner muet saute la moitié dynamique, sans la faire échouer."""
    refused = subprocess.CompletedProcess(
        POLICY_PROBE, 1, b"", b"Access denied by policy\r\nsecond line\r\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: refused)
    with pytest.raises(pytest.skip.Exception) as skipped:
        execution_policies()
    assert "code 1" in str(skipped.value)
    assert "Access denied by policy" in str(skipped.value)
    assert "second line" not in str(skipped.value)


@pytest.mark.parametrize("typed", ["", " ", "0" * LAUNCHER_CODE_CHARS, 'x" & echo pwned'])
def test_embedded_check_refuses_a_wrong_code(launcher, tmp_path, typed):
    done = run_embedded_check(launcher, typed, tmp_path)
    assert done.returncode == 1
    assert CODE_REFUSED in done.stdout
    assert not (tmp_path / "profile" / "Zomboid").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="test_windows_launcher_installs_the_mod_and_connects"
)
@pytest.mark.parametrize("shape", [str, str.upper, lambda code: f" {code[:4]} {code[4:]} "])
def test_embedded_check_admits_the_code_whatever_its_case_and_spaces(
    launcher, tmp_path, shape
):
    done = run_embedded_check(launcher, shape(launcher_code(launcher)), tmp_path)
    assert CODE_REFUSED not in done.stdout
    assert b"Project Zomboid was not found" in done.stdout


def test_embedded_check_refuses_the_right_code_on_a_changed_byte(launcher, tmp_path):
    done = run_embedded_check(alter_payload(launcher), launcher_code(launcher), tmp_path)
    assert done.returncode == 1
    assert CODE_REFUSED in done.stdout


# Le jeu doit tourner, et le mod remplacé reste (P19F6) ---------------------------------------

NOT_STARTED = b"Project Zomboid did not start, or closed at once."


def test_start_budgets_match_the_python_constants():
    script = guest_launcher.POWERSHELL
    assert f"$StartSeconds = {CLIENT_START_S}\n" in script
    assert f"$SettleSeconds = {CLIENT_SETTLE_S}\n" in script
    assert CLIENT_START_S > CLIENT_SETTLE_S > 0
    assert f"$ModId + '{PREVIOUS_SUFFIX}'" in script


def test_success_is_decided_by_the_game_process_not_the_started_one():
    """Le lancement passe par le shell : l'objet de `Start` ne prouve pas que le jeu tourne."""
    script = guest_launcher.POWERSHELL
    start = script.index("Start-Client $client $join")
    assert script.index("$before = Get-ClientIds") < start < script.index("Wait-Client $before")
    assert script.index("Wait-Client $before") < script.index("$code = 0")
    assert "[void][Diagnostics.Process]::Start($info)" in script


def plan_posix_client(folder: Path, stay_s: float) -> Path:
    """Le client trouvé par le chemin Steam par défaut de `run_embedded_check`."""
    game = folder / "no-steam" / "Steam" / "steamapps" / "common" / "ProjectZomboid"
    game.mkdir(parents=True)
    client = game / "ProjectZomboid64.exe"
    args = folder / "client-args.txt"
    client.write_text(f'#!/bin/sh\necho "$@" > "{args}"\nsleep {stay_s}\n', encoding="ascii")
    client.chmod(0o755)
    return args


def posix_guest_mod(folder: Path) -> tuple[Path, str]:
    mod = folder / "profile" / "Zomboid" / "mods" / MOD_ID
    mod.mkdir(parents=True)
    (mod / "old.txt").write_text("old", encoding="utf-8")
    return mod, mod_fingerprint(mod)


def stages_of(folder: Path) -> list[Path]:
    return sorted((folder / "profile" / "Zomboid").glob(f".{MOD_ID}-*"))


@pytest.mark.skipif(sys.platform == "win32", reason="test_windows_client_closing_at_once_fails")
def test_client_closing_at_once_fails_and_puts_the_old_mod_back(launcher, tmp_path):
    """Constat 2 de P19F6 : un jeu qui se ferme aussitôt était rapporté comme un succès."""
    args = plan_posix_client(tmp_path, 1)
    mod, before = posix_guest_mod(tmp_path)

    done = run_embedded_check(launcher, launcher_code(launcher), tmp_path)

    assert args.exists(), done.stdout
    assert done.returncode == 1, done.stdout
    assert NOT_STARTED in done.stdout
    assert mod_fingerprint(mod) == before
    (stage,) = stages_of(tmp_path)
    assert mod_fingerprint(stage / "rejected") == mod_fingerprint(MOD_SRC)
    assert str(stage / "rejected").encode() in done.stdout.replace(b"\n", b"")
    assert not (mod.parent.parent / f"{MOD_ID}{PREVIOUS_SUFFIX}").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="test_windows_previous_mod_is_replaced_not_merged"
)
def test_running_client_succeeds_and_keeps_the_previous_mod(launcher, tmp_path):
    """Constat 4 de P19F6 : l'ancien `duelcore` est gardé hors de `mods`, et le lanceur le dit."""
    plan_posix_client(tmp_path, CLIENT_SETTLE_S + 3)
    mod, before = posix_guest_mod(tmp_path)
    backup = mod.parent.parent / f"{MOD_ID}{PREVIOUS_SUFFIX}"

    done = run_embedded_check(launcher, launcher_code(launcher), tmp_path)

    assert done.returncode == 0, done.stdout
    assert NOT_STARTED not in done.stdout
    assert mod_fingerprint(mod) == mod_fingerprint(MOD_SRC)
    assert mod_fingerprint(backup) == before
    assert b"was not deleted" in done.stdout
    assert str(backup).encode() in done.stdout.replace(b"\n", b"")
    assert stages_of(tmp_path) == []

    # Une revanche relance le même `.bat` : le mod d'origine de l'invité reste la sauvegarde.
    again = run_embedded_check(launcher, launcher_code(launcher), tmp_path)

    assert again.returncode == 0, again.stdout
    assert mod_fingerprint(backup) == before
    assert mod_fingerprint(mod) == mod_fingerprint(MOD_SRC)
    assert stages_of(tmp_path) == []


# AC33 : mots de passe -----------------------------------------------------------------------


@pytest.mark.parametrize("password", HOSTILE_PASSWORDS)
def test_hostile_password_is_refused_without_being_cited(port, password):
    with pytest.raises(LauncherRefused) as refused:
        build_launcher(ADDRESS, port, password, MOD_SRC)
    assert password not in str(refused.value)
    assert password not in repr(refused.value)


def test_generated_passwords_are_never_refused(port):
    for _ in range(HEX_DRAWS):
        password = gen_secret()
        assert password.encode() in build_launcher(ADDRESS, port, password, MOD_SRC)


@pytest.mark.parametrize("password", ["a", "A.b_c-9", "-x", "..", "0" * 64])
def test_whitelist_admits_its_whole_alphabet(port, password):
    assert password.encode() in build_launcher(ADDRESS, port, password, MOD_SRC)


@pytest.mark.parametrize("password", ["", "a\n", "a\r\nb", "a\tb", "é", "١", None, 12, b"abc"])
def test_whitelist_refuses_the_rest(port, password):
    with pytest.raises(LauncherRefused):
        build_launcher(ADDRESS, port, password, MOD_SRC)


# H7 : adresses et port ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "192.168.1.20",
        "10.0.0.1",
        "8.8.8.8",
        "203.0.113.7",
        "example.org",
        "EXAMPLE.org",
        "example.org.",
        "my-host",
        "a1.b2.example",
        "1.example.org",
    ],
)
def test_usable_addresses(address, port):
    assert is_usable_address(address)
    assert f" {address} ".encode() in build_launcher(address, port, PASSWORD, MOD_SRC)


@pytest.mark.parametrize(
    "address",
    [
        "",
        ".",
        "127.0.0.1",
        "127.9.9.9",
        "127.1",
        "2130706433",
        "0x7f.1",
        "0x7f000001",
        "017700000001",
        "0.0.0.0",
        "169.254.1.1",
        "localhost",
        "LOCALHOST",
        "localhost.",
        "LocalHost.",
        "foo.localhost",
        "256.1.1.1",
        "1.2.3",
        "1.2.3.4.",
        "1.2.3.4.5",
        "010.0.0.1",
        "-host",
        "host-",
        "a..b",
        "a b",
        "a&b",
        "a_b",
        'a"b',
        "%PATH%",
        "é.example",
        "host\n",
        "::1",
        "x" * 64,
        ".".join(["a" * 60] * 5),
        None,
        7,
    ],
)
def test_unusable_addresses(address, port):
    assert not is_usable_address(address)
    with pytest.raises(LauncherRefused) as refused:
        build_launcher(address, port, PASSWORD, MOD_SRC)
    if address and isinstance(address, str):
        assert address not in str(refused.value)


@pytest.mark.parametrize("value", [0, -1, 65536, True, "17271", 17271.0, None])
def test_invalid_port_is_refused(value):
    with pytest.raises(LauncherRefused):
        build_launcher(ADDRESS, value, PASSWORD, MOD_SRC)


# Vrai Windows (D-A) -------------------------------------------------------------------------

if sys.platform == "win32":
    RUN_TIMEOUT_S = 120
    ARGS_WAIT_S = 20
    POLL_S = 0.05
    GAME_FOLDER = Path("steamapps") / "common" / "ProjectZomboid"
    CLIENT_NAME = "ProjectZomboid64.exe"
    # Le lanceur n'annonce un succès qu'après `CLIENT_SETTLE_S` secondes de jeu (P19F6).
    CLIENT_STAY_MS = (CLIENT_SETTLE_S + 5) * 1000

    @pytest.fixture(scope="session")
    def compiled_client(tmp_path_factory):
        return compile_windows_client(tmp_path_factory.mktemp("client") / "client.exe")

    class Guest:
        """Le poste d'un invité : profil avec espace et `é`, Steam par défaut sans PZ."""

        def __init__(self, root: Path, client: Path) -> None:
            self.root = root
            self.client = client
            self.profile = root / "Guest é user"
            self.steam = root / "Program Files (x86)" / "Steam"
            self.work = root / "work"
            self.args_file = root / "client-args.txt"
            self.mods = self.profile / "Zomboid" / "mods"
            self.mod = self.mods / MOD_ID
            self.bat = root / "pz-duel-join.bat"
            for folder in (self.profile, self.steam, self.work):
                folder.mkdir(parents=True)

        def install_game(self, library: Path | None = None) -> Path:
            return self.place_client((library or self.steam) / GAME_FOLDER)

        def place_client(self, folder: Path) -> Path:
            folder.mkdir(parents=True)
            target = folder / CLIENT_NAME
            shutil.copyfile(self.client, target)
            plan_windows_client(target, self.args_file, stay_ms=CLIENT_STAY_MS)
            return folder

        def old_mod(self) -> str:
            self.mod.mkdir(parents=True)
            (self.mod / "old.txt").write_text("old", encoding="utf-8")
            return mod_fingerprint(self.mod)

        def run(
            self,
            launcher: bytes,
            answer: bytes = b"",
            bat: Path | None = None,
            typed: bytes | None = None,
        ) -> subprocess.CompletedProcess[bytes]:
            """Saisit le code du fichier lancé, sauf `typed` ; `b""` ne laisse rien à lire."""
            target = bat or self.bat
            target.write_bytes(launcher)
            typed = launcher_code(launcher).encode("ascii") if typed is None else typed
            feed = typed + CRLF + answer if typed else b""
            env = dict(os.environ)
            env["USERPROFILE"] = str(self.profile)
            env["PROGRAMFILES(X86)"] = str(self.steam.parent)
            # La ligne de l'Explorateur au double-clic : `list2cmdline` ne protégerait pas `&`.
            command = f'cmd.exe /d /c ""{target}" "'
            options = {"capture_output": True, "cwd": self.work, "env": env}
            if not answer:
                return subprocess.run(command, input=feed, timeout=RUN_TIMEOUT_S, **options)
            # `set /p` lit par blocs et ne rembobine qu'un fichier : un tube perdrait la réponse.
            feed_file = self.root / "typed.txt"
            feed_file.write_bytes(feed)
            with feed_file.open("rb") as stdin:
                return subprocess.run(command, stdin=stdin, timeout=RUN_TIMEOUT_S, **options)

        def client_args(self) -> list[str]:
            deadline = time.monotonic() + ARGS_WAIT_S
            while not (self.args_file.exists() and self.args_file.read_text(encoding="utf-8")):
                assert time.monotonic() < deadline, "le double n'a pas été lancé"
                time.sleep(POLL_S)
            return self.args_file.read_text(encoding="utf-8").split()

    @pytest.fixture
    def guest(tmp_path, compiled_client):
        assert not steam_from_registry(), "Steam réel présent : ces tests lanceraient le vrai jeu"
        return Guest(tmp_path, compiled_client)

    def expected_args(port: int, password: str = PASSWORD) -> list[str]:
        return ["-nosteam", "+connect", f"{ADDRESS}:{port}", "+password", password]

    def staging_left(guest: Guest) -> list[Path]:
        """Ce que le lanceur laisse dans `Zomboid`, hors `mods` et la sauvegarde annoncée."""
        backup = guest.mods.parent / f"{MOD_ID}{PREVIOUS_SUFFIX}"
        return [path for path in guest.mods.parent.iterdir() if path not in (guest.mods, backup)]

    def test_windows_two_generations_are_identical(port):
        first = build_launcher(ADDRESS, port, PASSWORD, MOD_SRC)
        assert build_launcher(ADDRESS, port, PASSWORD, MOD_SRC) == first

    def test_windows_launcher_installs_the_mod_and_connects(guest, launcher, port):
        guest.install_game()

        done = guest.run(launcher)

        assert done.returncode == 0, done.stdout
        assert mod_fingerprint(guest.mod) == mod_fingerprint(MOD_SRC)
        assert guest.client_args() == expected_args(port)
        assert staging_left(guest) == []

    def test_windows_other_mod_stays_and_second_run_is_stable(guest, launcher):
        guest.install_game()
        other = guest.mods / "autremod"
        other.mkdir(parents=True)
        (other / "mod.info").write_text("name=autre\n", encoding="utf-8")
        before = mod_fingerprint(other)

        assert guest.run(launcher).returncode == 0
        first = mod_fingerprint(guest.mod)
        assert guest.run(launcher).returncode == 0

        assert mod_fingerprint(other) == before
        assert mod_fingerprint(guest.mod) == first == mod_fingerprint(MOD_SRC)

    def test_windows_previous_mod_is_replaced_not_merged(guest, launcher):
        guest.install_game()
        before = guest.old_mod()

        done = guest.run(launcher)

        assert done.returncode == 0, done.stdout
        assert not (guest.mod / "old.txt").exists()
        assert mod_fingerprint(guest.mod) == mod_fingerprint(MOD_SRC)
        backup = guest.mods.parent / f"{MOD_ID}{PREVIOUS_SUFFIX}"
        assert mod_fingerprint(backup) == before
        assert b"was not deleted" in done.stdout

    def test_windows_client_closing_at_once_fails(guest, launcher, port):
        folder = guest.install_game()
        plan_windows_client(folder / CLIENT_NAME, guest.args_file, code=1, stay_ms=500)
        before = guest.old_mod()

        done = guest.run(launcher)

        assert done.returncode != 0
        assert NOT_STARTED in done.stdout
        assert guest.client_args() == expected_args(port)
        assert mod_fingerprint(guest.mod) == before
        (stage,) = staging_left(guest)
        assert mod_fingerprint(stage / "rejected") == mod_fingerprint(MOD_SRC)
        assert not (guest.mods.parent / f"{MOD_ID}{PREVIOUS_SUFFIX}").exists()

    @pytest.mark.parametrize("password", HOSTILE_PASSWORDS)
    def test_windows_hostile_password_leaves_no_file(guest, port, password):
        """Refusé, ou reçu à l'identique : le `pwned.txt` n'existe dans aucun des deux cas."""
        guest.install_game()
        try:
            launcher = build_launcher(ADDRESS, port, password, MOD_SRC)
        except LauncherRefused as refused:
            assert password not in str(refused)
        else:
            assert guest.run(launcher).returncode == 0
            assert guest.client_args() == expected_args(port, password)
        assert list(guest.root.rglob("pwned.txt")) == []

    @pytest.mark.parametrize("password", ["A.b_c-9", "-x", "0f" * 32])
    def test_windows_admitted_password_arrives_unchanged(guest, port, password):
        guest.install_game()

        done = guest.run(build_launcher(ADDRESS, port, password, MOD_SRC))

        assert done.returncode == 0, done.stdout
        assert guest.client_args() == expected_args(port, password)
        assert password.encode() not in done.stdout + done.stderr

    def test_windows_empty_answer_fails_and_keeps_the_mod(guest, launcher):
        before = guest.old_mod()

        done = guest.run(launcher)

        assert done.returncode != 0
        assert b"not found" in done.stdout
        assert mod_fingerprint(guest.mod) == before
        assert not guest.args_file.exists()
        assert staging_left(guest) == []

    def test_windows_empty_answer_creates_nothing(guest, launcher):
        assert guest.run(launcher).returncode != 0
        assert not guest.mods.parent.exists()

    def test_windows_typed_folder_succeeds(guest, launcher, port):
        folder = guest.place_client(guest.root / "Jeux" / "PZ")

        done = guest.run(launcher, f"{folder}\r\n".encode("ascii"))

        assert done.returncode == 0, done.stdout
        assert guest.client_args() == expected_args(port)
        assert mod_fingerprint(guest.mod) == mod_fingerprint(MOD_SRC)

    def test_windows_wrong_typed_folder_fails_and_keeps_the_mod(guest, launcher):
        before = guest.old_mod()

        done = guest.run(launcher, f"{guest.work}\r\n".encode("ascii"))

        assert done.returncode != 0
        assert mod_fingerprint(guest.mod) == before
        assert not guest.args_file.exists()

    def test_windows_altered_payload_fails_like_a_missing_game(guest, launcher):
        missing = guest.run(launcher).returncode
        guest.install_game()
        before = guest.old_mod()

        done = guest.run(alter_payload(launcher))

        assert done.returncode == missing != 0
        assert b"damaged" in done.stdout
        assert mod_fingerprint(guest.mod) == before
        assert not guest.args_file.exists()
        assert staging_left(guest) == []

    def test_windows_unstartable_client_puts_the_old_mod_back(guest, launcher):
        folder = guest.install_game()
        (folder / CLIENT_NAME).write_bytes(b"not a program")
        before = guest.old_mod()

        done = guest.run(launcher)

        assert done.returncode != 0
        assert mod_fingerprint(guest.mod) == before
        assert PASSWORD.encode() not in done.stdout + done.stderr
        assert staging_left(guest) == []

    def assert_refused_untouched(guest: Guest, done, before: str) -> None:
        assert done.returncode != 0
        assert CODE_REFUSED in done.stdout
        assert b"not found" not in done.stdout
        assert mod_fingerprint(guest.mod) == before
        assert not guest.args_file.exists()
        assert staging_left(guest) == []

    @pytest.mark.parametrize("typed", [b"", b"0" * 16, b'x" & echo pwned>pwned.txt'])
    def test_windows_wrong_code_stops_before_the_game(guest, launcher, typed):
        guest.install_game()
        before = guest.old_mod()

        done = guest.run(launcher, typed=typed)

        assert_refused_untouched(guest, done, before)
        assert list(guest.root.rglob("pwned.txt")) == []

    def test_windows_changed_byte_is_refused_with_the_code_of_the_host(guest, launcher):
        guest.install_game()
        before = guest.old_mod()

        done = guest.run(alter_payload(launcher), typed=launcher_code(launcher).encode("ascii"))

        assert_refused_untouched(guest, done, before)

    def test_windows_inherited_value_is_no_default_for_the_code(guest, launcher, monkeypatch):
        guest.install_game()
        before = guest.old_mod()
        monkeypatch.setenv("PZDUEL_TYPED", launcher_code(launcher))

        done = guest.run(launcher, typed=b"")

        assert_refused_untouched(guest, done, before)

    def test_windows_code_matches_what_certutil_prints(guest, launcher):
        """Le contrôle fait avant d'ouvrir le fichier : un `.bat` substitué ne le fausse pas."""
        guest.bat.write_bytes(launcher)
        done = subprocess.run(
            ["certutil", "-hashfile", str(guest.bat), "SHA256"], capture_output=True, check=True
        )
        digest = done.stdout.splitlines()[1].replace(b" ", b"").decode("ascii")
        assert digest.startswith(launcher_code(launcher))

    def test_windows_execution_policy_is_unchanged_after_a_run(guest, launcher):
        assert_policy_only_on_the_process_line(launcher)
        guest.install_game()
        before = execution_policies()

        assert guest.run(launcher).returncode == 0

        assert execution_policies() == before

    def test_windows_second_library_is_found_without_input(guest, launcher, port):
        first = guest.root / "first library"
        second = guest.root / "other root" / "Games é"
        vdf = guest.steam / "steamapps" / "libraryfolders.vdf"
        vdf.parent.mkdir(parents=True)
        escaped = [str(folder).replace("\\", "\\\\") for folder in (first, second)]
        vdf.write_text(
            '"libraryfolders"\n{\n'
            f'\t"0"\n\t{{\n\t\t"path"\t\t"{escaped[0]}"\n\t}}\n'
            f'\t"1"\n\t{{\n\t\t"path"\t\t"{escaped[1]}"\n'
            '\t\t"apps"\n\t\t{\n\t\t\t"108600"\t\t"1"\n\t\t}\n\t}\n}\n',
            encoding="utf-8",
        )
        guest.install_game(second)

        done = guest.run(launcher)

        assert done.returncode == 0, done.stdout
        assert b"not found" not in done.stdout
        assert guest.client_args() == expected_args(port)

    @pytest.mark.parametrize("name", ["100% sûr", "a!b", "x&y", "(z) ^w"])
    def test_windows_self_path_with_cmd_characters(guest, launcher, port, name):
        """`%`, `!`, `&` et `^` dans `%~f0` : le `.bat` se relit quand même (risque de P11)."""
        guest.install_game()
        folder = guest.root / name
        folder.mkdir()

        done = guest.run(launcher, bat=folder / "pz-duel-join.bat")

        assert done.returncode == 0, done.stdout
        assert guest.client_args() == expected_args(port)

    def test_windows_closed_input_does_not_hang_the_pause(guest, launcher):
        """`pause` sur une entrée fermée rend aussitôt : AC34 en dépend (risque de P11)."""
        started = time.monotonic()
        done = guest.run(launcher)
        assert done.returncode != 0
        assert time.monotonic() - started < RUN_TIMEOUT_S
        assert b"launcher failed" in done.stdout
