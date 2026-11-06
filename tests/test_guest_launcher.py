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
from service import guest_launcher
from service.guest_launcher import (
    FILE_MARK,
    JOIN_MARK,
    MAX_LAUNCHER_BYTES,
    MAX_LINE_BYTES,
    MOD_MARK,
    LauncherRefused,
    build_launcher,
    is_usable_address,
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
            plan_windows_client(target, self.args_file)
            return folder

        def old_mod(self) -> str:
            self.mod.mkdir(parents=True)
            (self.mod / "old.txt").write_text("old", encoding="utf-8")
            return mod_fingerprint(self.mod)

        def run(
            self, launcher: bytes, answer: bytes = b"", bat: Path | None = None
        ) -> subprocess.CompletedProcess[bytes]:
            target = bat or self.bat
            target.write_bytes(launcher)
            env = dict(os.environ)
            env["USERPROFILE"] = str(self.profile)
            env["PROGRAMFILES(X86)"] = str(self.steam.parent)
            # La ligne de l'Explorateur au double-clic : `list2cmdline` ne protégerait pas `&`.
            return subprocess.run(
                f'cmd.exe /d /c ""{target}" "',
                input=answer,
                capture_output=True,
                cwd=self.work,
                env=env,
                timeout=RUN_TIMEOUT_S,
            )

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
        return [path for path in guest.mods.parent.iterdir() if path != guest.mods]

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
        guest.old_mod()

        assert guest.run(launcher).returncode == 0

        assert not (guest.mod / "old.txt").exists()
        assert mod_fingerprint(guest.mod) == mod_fingerprint(MOD_SRC)

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
