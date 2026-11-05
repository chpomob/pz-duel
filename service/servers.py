"""Réglages gérés, lancement, expurgation, démarrage et arrêt gradué des serveurs.

Les fichiers de PZ restent ceux de l'utilisateur : seules les clés gérées changent. Le
texte passe par `surrogateescape` et sans traduction des fins de ligne, si bien qu'un
commentaire français ou un octet invalide ressort à l'identique.

`start-server.sh` lance `java` sans `exec` : le script n'est pas le serveur. Tout ce qui
décide de la fin d'un serveur regarde donc son groupe de processus, jamais le seul script.
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

from .preflight import SCRIPT
from .protocol import SIDES
from .rcon import RconClient, RconError
from .rules import MS_PER_S
from .state import MatchState

ENCODING = "utf-8"
ENCODING_ERRORS = "surrogateescape"
INI_MODE = 0o600
REDACTED = "***"
DAY_LENGTH_KEY = "DayLength"
# Un commentaire commence par `#` ; le `\r` d'une fin CRLF reste dans la valeur capturée.
INI_LINE = re.compile(r"[ \t]*([^\s#=]+)[ \t]*=(.*)")
LINE_BREAK = re.compile(r"[\r\n]")

STARTED_MARKER = "*** SERVER STARTED ****"
BUILD_LINE = re.compile(r"version=(\S+)")
QUIT_LINE = b"quit\n"

STATUS_STARTING = "starting"
STATUS_UP = "up"
STATUS_ERROR = "error"
STATUS_DOWN = "down"
STEP_STATES = {STATUS_STARTING: "running", STATUS_UP: "ok", STATUS_ERROR: "error"}

MODS_SEPARATOR = ";"
CONTRACT_PORT_KEYS = ("DefaultPort", "UDPPort", "RCONPort")
CAPS_SHARED_KEYS = ("protocol", "mod")

STOP_RCON = "rcon"
STOP_STDIN = "stdin"
STOP_SIGTERM = "sigterm"
STOP_SIGKILL = "sigkill"


def _read_text(path: Path) -> str:
    with open(path, encoding=ENCODING, errors=ENCODING_ERRORS, newline="") as handle:
        return handle.read()


def _replace_file(path: Path, text: str, mode: int) -> None:
    """Remplace `path` d'un seul `rename` : un lecteur voit l'ancien ou le nouveau fichier."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(
            fd, "w", encoding=ENCODING, errors=ENCODING_ERRORS, newline=""
        ) as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        os.unlink(temp_name)
        raise


def write_ini(path: str | Path, managed: Mapping[str, Any]) -> None:
    """Réécrit les clés de `managed`, ajoute les absentes et laisse le fichier en `0600`.

    Un fichier absent est créé partiel : PZ le complète à la première création (H1).
    """
    path = Path(path)
    for key, value in managed.items():
        if LINE_BREAK.search(str(value)):
            # La valeur peut être un secret : seule la clé est nommée.
            raise ValueError(f"réglage {key} refusé : retour à la ligne dans la valeur")
    try:
        text = _read_text(path)
    except FileNotFoundError:
        text = ""
        path.parent.mkdir(parents=True, exist_ok=True)

    lines = text.split("\n")
    missing = dict(managed)
    for index, line in enumerate(lines):
        match = INI_LINE.fullmatch(line)
        if match and match[1] in managed:
            key = match[1]
            end = "\r" if line.endswith("\r") else ""
            lines[index] = f"{key}={managed[key]}{end}"
            missing.pop(key, None)
    text = "\n".join(lines)

    if missing:
        eol = "\r\n" if "\r\n" in text else "\n"
        if text and not text.endswith("\n"):
            text += eol
        text += "".join(f"{key}={value}{eol}" for key, value in missing.items())
    _replace_file(path, text, INI_MODE)


def read_ini(path: str | Path) -> dict[str, str]:
    """Rend les clés du fichier relu ; une clé répétée vaut sa dernière occurrence."""
    values = {}
    for line in _read_text(Path(path)).split("\n"):
        if match := INI_LINE.fullmatch(line):
            values[match[1]] = match[2].strip()
    return values


def write_sandbox(path: str | Path, day_length: int, start: Mapping[str, Any]) -> None:
    """Fixe `DayLength` et les clés de `start`, nommées exactement et en début de ligne.

    Une clé introuvable refuse l'écriture : le contrat ne pourrait pas tenir.
    """
    path = Path(path)
    text = _read_text(path)
    for key, value in {DAY_LENGTH_KEY: day_length, **start}.items():
        pattern = rf"^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)[^,\r\n]*"
        text, count = re.subn(
            pattern, lambda match: f"{match[1]}{value}", text, flags=re.MULTILINE
        )
        if not count:
            raise ValueError(f"clé {key} introuvable dans {path.name}")
    _replace_file(path, text, stat.S_IMODE(path.stat().st_mode))


def server_name(side: str) -> str:
    """Nom du serveur : le mod en déduit son camp par `getServerName()`."""
    return f"duel{side}"


def mods_setting(table: Mapping[str, Any]) -> str:
    """Valeur de `Mods` pour `table`, écrite au lancement et attendue par le contrat."""
    return MODS_SEPARATOR.join(table["mods"])


def launch_command(side: str, cfg: Mapping[str, Any]) -> list[str]:
    """Commande d'un serveur. `cfg` porte `pz_dir`, `ports` et `admin_password`.

    Elle contient le mot de passe admin : à ne journaliser qu'à travers un `Redactor`.
    """
    ports = cfg["ports"][side]
    return [
        str(Path(cfg["pz_dir"]) / SCRIPT),
        "-servername",
        server_name(side),
        "-port",
        str(ports["game"]),
        "-udpport",
        str(ports["udp"]),
        "-adminpassword",
        cfg["admin_password"],
    ]


def gen_secret() -> str:
    """Tire un secret. L'hexadécimal passe tel quel en argument et dans le `.ini`."""
    return secrets.token_hex()


class Redactor:
    """Retire les secrets de toute sortie de serveur et de tout message d'exception."""

    def __init__(self, secret_values: Iterable[str]) -> None:
        # Le plus long d'abord : un secret qui en contient un autre part en entier.
        self._secrets = sorted({value for value in secret_values if value}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def redact_exception(self, exc: BaseException) -> str:
        return self.redact(f"{type(exc).__name__}: {exc}")


class ServerProcess:
    """Un serveur et son groupe de processus. `start` peut resservir une fois l'arrêt fini.

    `cfg` porte ce que demande `launch_command`, plus `rcon_password`, `zomboid_dir` et les
    délais de `runtime.json`. `server_X.log` est ouvert en ajout : la passe de création et
    un redémarrage écrivent à la suite du lancement précédent.
    """

    def __init__(
        self, side: str, cfg: Mapping[str, Any], match_dir: str | Path, redactor: Redactor
    ) -> None:
        self.side = side
        self.build: str | None = None
        self.stop_stage: str | None = None
        self.log_error: str | None = None
        self.log_path = Path(match_dir) / f"server_{side}.log"
        self._cfg = cfg
        self.redactor = redactor
        self._poll_s = cfg["tick_ms"] / MS_PER_S
        self._ini_path = Path(cfg["zomboid_dir"]) / "Server" / f"{server_name(side)}.ini"
        port = cfg["ports"][side]["game"]
        self._listening = re.compile(rf"server is listening on port {port}\b")
        self._tail: deque[str] = deque(maxlen=cfg["log_tail_lines"])
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._started_seen = False
        self._ready = threading.Event()
        self._ini_failure: str | None = None
        self._group_gone = False
        self._stop_requested = False
        self._unexpected = False
        self._state_lock = threading.Lock()
        self._stop_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def exit_code(self) -> int | None:
        """Code de sortie du script, `None` tant qu'il tourne ou s'il n'a pas démarré."""
        return None if self._process is None else self._process.poll()

    @property
    def ini_failure(self) -> str | None:
        """Motif si les droits du `.ini` n'ont pas pu être rétablis après disponibilité."""
        return self._ini_failure

    def start(self) -> None:
        with self._stop_lock:
            if self._process is not None:
                if self._group_alive():
                    raise RuntimeError(f"serveur {self.side} déjà lancé")
                # L'ancien fil ne doit pas écrire dans l'état du nouveau lancement.
                self._reader.join(self._cfg["kill_grace_s"])
                self._close_stdin()
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(self.log_path, "ab", buffering=0)
            try:
                process = subprocess.Popen(
                    launch_command(self.side, self._cfg),
                    cwd=self._cfg["pz_dir"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except BaseException:
                log.close()
                raise
            self.build = None
            self.stop_stage = None
            self.log_error = None
            self._tail.clear()
            self._started_seen = False
            self._ready.clear()
            self._ini_failure = None
            self._group_gone = False
            with self._state_lock:
                self._stop_requested = False
                self._unexpected = False
                self._process = process
            self._reader = threading.Thread(
                target=self._read_output,
                args=(process.stdout, log),
                name=f"server-{self.side}-output",
                daemon=True,
            )
            self._reader.start()

    def log_tail(self) -> list[str]:
        """Dernières lignes expurgées. Attend brièvement le fil, si le script vient de sortir."""
        if self._reader is not None and self.exit_code is not None:
            self._reader.join(self._poll_s)
        return list(self._tail)

    def exited_unexpectedly(self) -> bool:
        """Vrai si le script a quitté sans arrêt demandé, même si `stop` a suivi."""
        with self._state_lock:
            if self.exit_code is not None and not self._stop_requested:
                self._unexpected = True
            return self._unexpected

    def stop(self) -> bool:
        """Arrêt gradué. Rend vrai quand plus aucun processus du groupe ne subsiste.

        Le retour reste borné : un zombie que personne ne ramasse garde `killpg` positif
        même après SIGKILL, et l'appelant ne doit pas rester bloqué pour autant.
        """
        self.exited_unexpectedly()
        with self._state_lock:
            self._stop_requested = True
        with self._stop_lock:
            if self._process is None:
                return True
            stopped = self._stop_group()
            if stopped and self._reader is not None:
                self._reader.join(self._cfg["kill_grace_s"])
            self._close_stdin()
            return stopped

    def _stop_group(self) -> bool:
        if not self._group_alive():
            return True
        deadline = time.monotonic() + self._cfg["stop_timeout_s"]
        self._ask_quit(deadline)
        if self._wait_group_gone(deadline):
            return True
        self.stop_stage = STOP_SIGTERM
        self._signal_group(signal.SIGTERM)
        if self._wait_group_gone(time.monotonic() + self._cfg["kill_grace_s"]):
            return True
        self.stop_stage = STOP_SIGKILL
        self._signal_group(signal.SIGKILL)
        return self._wait_group_gone(time.monotonic() + self._cfg["kill_grace_s"])

    def _ask_quit(self, deadline: float) -> None:
        try:
            port = self._cfg["ports"][self.side]["rcon"]
            password = self._cfg["rcon_password"]
            timeout_s = self._cfg["rcon_timeout_s"]
            with RconClient(port, password, timeout_s, deadline=deadline) as client:
                client.save()
                client.quit()
            self.stop_stage = STOP_RCON
        except RconError:
            self.stop_stage = STOP_STDIN
            self._write_quit()

    def _write_quit(self) -> None:
        stdin = self._process.stdin
        try:
            stdin.write(QUIT_LINE)
            stdin.flush()
        except (OSError, ValueError):
            # Le script est parti, ou le tube est déjà fermé : les signaux prennent le relais.
            pass

    def _close_stdin(self) -> None:
        try:
            self._process.stdin.close()
        except OSError:
            pass

    def _group_alive(self) -> bool:
        if self._group_gone:
            # Un groupe éteint libère son numéro : le sonder viserait peut-être un inconnu.
            return False
        # Le script non ramassé compterait encore dans le groupe.
        self._process.poll()
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError:
            self._group_gone = True
            return False
        except PermissionError:
            return True
        return True

    def _wait_group_gone(self, deadline: float) -> bool:
        while self._group_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(self._poll_s, remaining))
        return True

    def _signal_group(self, signum: signal.Signals) -> None:
        if self._group_alive():
            try:
                os.killpg(self._process.pid, signum)
            except ProcessLookupError:
                pass

    def _read_output(self, stdout: BinaryIO, log: BinaryIO) -> None:
        """Vide le tube jusqu'à sa fermeture : un tube plein bloquerait le serveur sans erreur.

        Une panne d'écriture du log n'interrompt donc jamais la lecture.
        """
        with stdout, log:
            for raw in stdout:
                line = self.redactor.redact(raw.decode(ENCODING, "replace").rstrip("\r\n"))
                self._tail.append(line)
                try:
                    log.write(f"{line}\n".encode(ENCODING))
                except OSError as exc:
                    self.log_error = self.redactor.redact_exception(exc)
                self._scan(line)

    def _scan(self, line: str) -> None:
        if self.build is None and (match := BUILD_LINE.search(line)):
            self.build = match[1]
        if self.ready:
            return
        if not self._started_seen:
            _, marker, line = line.partition(STARTED_MARKER)
            if not marker:
                return
            self._started_seen = True
        if self._listening.search(line):
            self._secure_ini()
            self._ready.set()

    def _secure_ini(self) -> None:
        """PZ peut réécrire le `.ini` au démarrage et en relâcher les droits."""
        try:
            os.chmod(self._ini_path, INI_MODE)
        except FileNotFoundError:
            pass
        except OSError as exc:
            reason = self.redactor.redact_exception(exc)
            self._ini_failure = f"droits de {self._ini_path.name} non rétablis : {reason}"


def apply_status(state: MatchState, side: str, status: str, detail: str = "") -> None:
    """Reporte un changement d'état d'un serveur sur l'état exposé.

    `down` ne touche pas l'étape : un serveur arrêté après un échec reste une étape en erreur.
    """
    state.servers[side] = status
    if status in STEP_STATES:
        state.set_step(f"server{side}", STEP_STATES[status], detail)


class ServerPair:
    """Les deux serveurs d'un match : départ ensemble, et l'échec de l'un arrête l'autre.

    `on_status(side, status, detail)` suit chaque changement ; l'appelant y prend son
    propre verrou, qu'il ne tient donc pas en appelant ces méthodes, et il n'y appelle pas
    `stop`. Après un échec, `failure` donne le message expurgé du premier serveur.

    `_decision` ordonne l'annulation et les décisions de démarrage : une décision prise
    avant `stop` a publié son état quand `stop` commence, et aucune ne suit l'annulation.
    """

    def __init__(
        self,
        servers: Mapping[str, ServerProcess],
        runtime: Mapping[str, Any],
        on_status: Callable[[str, str, str], None] = lambda side, status, detail: None,
    ) -> None:
        self.servers = dict(servers)
        self.failure: str | None = None
        self._start_timeout_s = runtime["server_start_timeout_s"]
        self._poll_s = runtime["tick_ms"] / MS_PER_S
        self._on_status = on_status
        self._deadline = 0.0
        self._up: set[str] = set()
        self._stopping = threading.Event()
        self._closed = threading.Event()
        self._decision = threading.Lock()

    def start(self) -> bool:
        """Lance les deux serveurs. Un lancement impossible arrête celui déjà parti.

        Rend aussi faux, sans rien signaler, si `stop` vient d'un autre fil : le serveur
        suivant ne part alors pas, et `stop` s'occupe de ceux qui sont déjà partis. Après
        `close`, rien ne part : `start` efface `_stopping`, pas `_closed`.
        """
        self.failure = None
        self._up.clear()
        self._stopping.clear()
        self._deadline = time.monotonic() + self._start_timeout_s
        for side, server in self.servers.items():
            with self._decision:
                if self._stopping.is_set() or self._closed.is_set():
                    return False
                self._on_status(side, STATUS_STARTING, "")
                try:
                    server.start()
                except OSError as exc:
                    reason = server.redactor.redact_exception(exc)
                    self._report({side: f"serveur {side} : lancement impossible ({reason})"})
            if self.failure is not None:
                self.stop()
                return False
        return True

    def wait_ready(self) -> bool:
        """Attend les deux serveurs ; un échec arrête la paire avant de rendre faux.

        Rend aussi faux, sans rien signaler, si `stop` vient d'un autre fil : c'est ainsi
        qu'une annulation interrompt l'attente.
        """
        while True:
            with self._decision:
                if self.failure is not None or self._stopping.is_set():
                    return False
                if failures := self._failures():
                    self._report(failures)
                elif self._announce_up():
                    return True
            if failures:
                self.stop()
                return False
            self._stopping.wait(self._poll_s)

    def stop(self) -> bool:
        """Arrêt gradué des deux serveurs en parallèle : le délai ne se cumule pas."""
        with self._decision:
            self._stopping.set()
        with ThreadPoolExecutor(len(self.servers)) as pool:
            results = dict(zip(self.servers, pool.map(ServerProcess.stop, self.servers.values())))
        for side, stopped in results.items():
            if stopped:
                self._on_status(side, STATUS_DOWN, "")
        return all(results.values())

    def close(self) -> bool:
        """Arrêt définitif : un `start` qui suivrait, même de peu, ne relance plus rien."""
        self._closed.set()
        return self.stop()

    def _announce_up(self) -> bool:
        for side, server in self.servers.items():
            if server.ready and side not in self._up:
                self._up.add(side)
                self._on_status(side, STATUS_UP, "serveur disponible")
        return len(self._up) == len(self.servers)

    def _failures(self) -> dict[str, str]:
        expired = time.monotonic() >= self._deadline
        failures = {}
        for side, server in self.servers.items():
            reason = None
            if server.exit_code is not None:
                reason = f"sortie anticipée, code {server.exit_code}"
            elif server.ini_failure:
                reason = server.ini_failure
            elif expired and not server.ready:
                reason = f"pas de disponibilité après {self._start_timeout_s} s"
            if reason:
                failures[side] = "\n".join([f"serveur {side} : {reason}", *server.log_tail()])
        return failures

    def _report(self, failures: Mapping[str, str]) -> None:
        self.failure = next(iter(failures.values()))
        for side, detail in failures.items():
            self._on_status(side, STATUS_ERROR, detail)
        culprits = ", ".join(failures)
        for side in self.servers:
            if side not in failures:
                self._on_status(side, STATUS_ERROR, f"arrêté après l'échec du serveur {culprits}")


def read_sandbox(path: str | Path, keys: Iterable[str]) -> dict[str, str]:
    """Relit `keys` en début de ligne, comme `write_sandbox` ; une clé absente est omise."""
    text = _read_text(Path(path))
    values = {}
    for key in keys:
        pattern = rf"^[ \t]*{re.escape(key)}[ \t]*=[ \t]*([^,\r\n]*)"
        if found := re.findall(pattern, text, flags=re.MULTILINE):
            values[key] = found[-1].strip()
    return values


def _table_effects(table: Mapping[str, Any]) -> set[str]:
    kinds = {effect["kind"] for rule in table["rules"] for effect in rule["effects"]}
    score_gap = table["modifiers"].get("score_gap")
    if score_gap:
        kinds.add(score_gap["defender_bonus"]["kind"])
    return kinds


def _check_ini_contract(
    inis: Mapping[str, Mapping[str, str]],
    table: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> list[str]:
    expected = {
        "Mods": mods_setting(table),
        "WorkshopItems": runtime["managed_ini"]["WorkshopItems"],
        "PauseEmpty": runtime["managed_ini"]["PauseEmpty"],
    }
    failures = []
    for side, ini in inis.items():
        for key, value in expected.items():
            if ini.get(key) != value:
                failures.append(f"{key} sur {side} : {ini.get(key)!r}, attendu {value!r}")
    for key in CONTRACT_PORT_KEYS:
        values = {side: ini.get(key) for side, ini in inis.items()}
        if None in values.values() or len(set(values.values())) < len(values):
            failures.append(f"{key} non distincts entre les camps : {values}")
    return failures


def _check_sandbox_contract(
    side: str, found: Mapping[str, str], expected: Mapping[str, Any]
) -> list[str]:
    return [
        f"{key} sur {side} : {found.get(key)!r}, attendu {str(value)!r}"
        for key, value in expected.items()
        if found.get(key) != str(value)
    ]


def _check_caps_contract(
    caps: Mapping[str, Mapping[str, Any] | None],
    builds: Mapping[str, str | None],
    table: Mapping[str, Any],
    timeout_s: float,
) -> list[str]:
    received = {side: caps[side] for side in SIDES if caps.get(side) is not None}
    missing = [side for side in SIDES if side not in received]
    failures = [f"caps de {side} non reçu après {timeout_s} s" for side in missing]
    if len(received) > 1:
        for key in CAPS_SHARED_KEYS:
            values = {side: data[key] for side, data in received.items()}
            if len(set(values.values())) > 1:
                failures.append(f"{key} différent entre les camps : {values}")
    atoms, effects = set(table["atoms"]), _table_effects(table)
    for side, data in received.items():
        if data["build"] != builds.get(side):
            failures.append(
                f"build sur {side} : caps {data['build']!r}, log {builds.get(side)!r}"
            )
        for label, wanted, present in (
            ("atome", atoms, data["atoms"]),
            ("effet", effects, data["effects"]),
        ):
            for name in sorted(wanted - set(present)):
                failures.append(f"{label} {name} absent de caps sur {side}")
    return failures


def verify_contract(
    zomboid_dir: str | Path,
    table: Mapping[str, Any],
    day_length: int,
    runtime: Mapping[str, Any],
    caps: Mapping[str, Mapping[str, Any] | None],
    builds: Mapping[str, str | None],
) -> list[str]:
    """Rend un message par écart, sur les fichiers relus ; une liste vide vaut conformité.

    `caps` donne par camp le `data` reçu ; un camp à `None` ou absent de `caps` compte comme
    non reçu, et ses fichiers sont relus quand même : le contrat porte toujours sur `SIDES`.
    `builds` donne le build lu dans le log. Aucun `hook_seen` n'est jugé ici. `RCONPassword` n'est jamais lu : seuls les réglages
    du contrat figurent dans les messages.
    """
    server_dir = Path(zomboid_dir) / "Server"
    expected_sandbox = {DAY_LENGTH_KEY: day_length, **runtime["sandbox_start"]}
    failures: list[str] = []
    inis: dict[str, dict[str, str]] = {}
    for side in SIDES:
        name = server_name(side)
        try:
            inis[side] = read_ini(server_dir / f"{name}.ini")
            found = read_sandbox(server_dir / f"{name}_SandboxVars.lua", expected_sandbox)
        except OSError as exc:
            failures.append(f"réglages de {side} illisibles : {exc}")
            continue
        failures += _check_sandbox_contract(side, found, expected_sandbox)
    if len(inis) == len(SIDES):
        failures += _check_ini_contract(inis, table, runtime)
    return failures + _check_caps_contract(caps, builds, table, runtime["contract_timeout_s"])
