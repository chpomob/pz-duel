"""Ce qui diffère entre Windows et Linux, décrit par un `Platform` figé.

D-B : `LINUX`, `WINDOWS` et `current()`. Une fonction touchée prend un paramètre `platform`
dont le défaut est `current()` : sous Linux, tout appel existant garde son comportement, et
une branche Windows s'exécute sous Linux par injection. Un chemin de commande se compose
avec `join()`, donc `ntpath` pour Windows : le texte rendu ne dépend pas du système hôte.

D-C : aucun module réservé à Windows ne s'importe au chargement de ce module (AC6). Les appels
Win32 passent par `WindowsKernel`, une couture : un double la remplace sous Linux.

D-E : sous Windows, l'arbre d'un serveur est un objet Job. `Popen` le crée suspendu, il entre
dans le Job, puis il reprend : un enfant précoce ne peut donc pas lui échapper. Le Job porte
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` et son handle n'est pas héritable : à la fin du service,
même forcée, le noyau ferme ce seul handle et tue l'arbre, sans qu'aucun code du service tourne.

D-F : `terminate` met fin au seul processus racine, même bloqué sur `PAUSE`, et laisse
`kill_grace_s` à `java` ; `kill` met fin au Job. `GenerateConsoleCtrlEvent` n'atteint pas un
processus lancé avec sa propre console invisible.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import posixpath
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from . import paths

MS_PER_S = 1000

# Drapeaux de `CreateProcess` : `subprocess` ne les définit que sous Windows.
CREATE_SUSPENDED = 0x00000004
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
SERVER_CREATION_FLAGS = CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
CLIENT_CREATION_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

# `cmd` interprète les premiers jusqu'entre guillemets, les seconds hors guillemets seulement.
# Les parenthèses n'y figurent pas : au milieu d'un mot, elles sont littérales, et R2 les admet.
CMD_LIVE_ANYWHERE = frozenset('%!"\r\n')
CMD_LIVE_UNQUOTED = frozenset("&|<>^")

# Code de sortie d'un arbre arrêté par `TerminateProcess`, le même que `Popen.kill`.
TERMINATED_EXIT_CODE = 1

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_SUSPEND_RESUME = 0x0800
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004
JOB_BASIC_ACCOUNTING_INFORMATION = 1
JOB_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
INVALID_HANDLE = ctypes.c_void_p(-1).value
RESUME_FAILED = 0xFFFFFFFF
LOCK_BYTES = 1
LOCK_FILE_MODE = 0o600


class ReplaceError(OSError):
    """Cible restée verrouillée jusqu'à l'échéance : un `except OSError` la reçoit aussi."""


class UnsafeArgument(OSError):
    """Argument que `cmd` interpréterait. Le message ne le cite pas : il peut être un secret."""


@dataclass(frozen=True)
class Platform:
    """`name` vaut `sys.platform` : c'est ce que `paths` attend pour son `platform`.

    `game_subdir` sépare l'installation du dossier qui porte `server_script` ; il est vide
    quand c'est le même. `stop_signals` porte des noms : `SIGBREAK` n'existe que sous Windows.
    """

    name: str
    server_script: str
    client_program: str
    client_in_game_dir: bool
    game_subdir: str
    posix_modes: bool
    stop_signals: tuple[str, ...]
    path: ModuleType = field(repr=False)

    def join(self, *parts: str | os.PathLike[str]) -> str:
        return self.path.join(*map(os.fspath, parts))

    def client_path(self, game_dir: str | os.PathLike[str]) -> str:
        """Rend le programme client de `game_dir`, le dossier de `server_script`."""
        folder = os.fspath(game_dir)
        if not self.client_in_game_dir:
            folder = self.path.dirname(folder)
        return self.join(folder, self.client_program)

    def client_file(self, game_dir: Path) -> Path:
        """Le fichier de `client_path` sur l'hôte : c'est lui qu'on sonde et qu'on lance."""
        folder = game_dir if self.client_in_game_dir else game_dir.parent
        return folder / self.client_program

    def server_popen_kwargs(self) -> dict[str, Any]:
        """Lancement d'un serveur. Sous Windows, il attend suspendu d'entrer dans son arbre."""
        if self.name == WINDOWS.name:
            return {"creationflags": SERVER_CREATION_FLAGS}
        return {"start_new_session": True}

    def client_popen_attempts(self) -> tuple[dict[str, Any], ...]:
        """Lancements détachés du client, à essayer dans l'ordre sur `PermissionError`.

        Un Job qui contiendrait le service peut refuser `CREATE_BREAKAWAY_FROM_JOB` : sans ce
        drapeau, le client démarre quand même, au risque de finir avec ce Job.
        """
        if self.name == WINDOWS.name:
            return (
                {"creationflags": CLIENT_CREATION_FLAGS | CREATE_BREAKAWAY_FROM_JOB},
                {"creationflags": CLIENT_CREATION_FLAGS},
            )
        return ({"start_new_session": True},)

    def check_arguments(self, command: Sequence[str]) -> None:
        """Refuse sous Windows ce que `cmd` interpréterait dans la commande d'un script.

        `CreateProcess` confie un `.bat` à `cmd`, pour lequel `list2cmdline` n'échappe rien.
        Un argument n'est entre guillemets que s'il porte un blanc, comme le décide cette
        fonction : `"` étant refusé, ces guillemets restent équilibrés.
        """
        if self.name != WINDOWS.name:
            return
        for position, argument in enumerate(command):
            quoted = " " in argument or "\t" in argument
            live = CMD_LIVE_ANYWHERE if quoted else CMD_LIVE_ANYWHERE | CMD_LIVE_UNQUOTED
            if not live.isdisjoint(argument):
                raise UnsafeArgument(
                    f"Refused to launch {self.server_script}: "
                    f"argument {position} holds a character cmd.exe would interpret."
                )

    def process_tree(
        self, process: subprocess.Popen, kernel: WindowsKernel | None = None
    ) -> ProcessTree:
        """Rend l'arbre de `process`, que `server_popen_kwargs` vient de lancer."""
        if self.name == WINDOWS.name:
            return WindowsProcessTree(process, WindowsKernel() if kernel is None else kernel)
        return PosixProcessTree(process)


LINUX = Platform(
    name="linux",
    server_script="start-server.sh",
    client_program="projectzomboid.sh",
    client_in_game_dir=False,
    game_subdir="projectzomboid",
    posix_modes=True,
    stop_signals=("SIGINT", "SIGTERM"),
    path=posixpath,
)
WINDOWS = Platform(
    name=paths.WINDOWS,
    server_script="StartServer64.bat",
    client_program="ProjectZomboid64.exe",
    client_in_game_dir=True,
    game_subdir="",
    posix_modes=False,
    stop_signals=("SIGINT", "SIGBREAK"),
    path=ntpath,
)


def current() -> Platform:
    return WINDOWS if sys.platform == WINDOWS.name else LINUX


class ProcessTree(Protocol):
    """Tout ce qu'un serveur a lancé. Le script seul ne dit rien : `java` peut lui survivre."""

    def alive(self) -> bool:
        """Vrai tant qu'un processus de l'arbre subsiste."""

    def terminate(self) -> None:
        """Palier `sigterm` : une demande que l'arbre peut encore honorer lui-même."""

    def kill(self) -> None:
        """Palier `sigkill` : la fin de tout l'arbre."""


class PosixProcessTree:
    """Le groupe de processus dont `process` est le chef."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self._gone = False

    def alive(self) -> bool:
        if self._gone:
            # Un groupe éteint libère son numéro : le sonder viserait peut-être un inconnu.
            return False
        # Le script non ramassé compterait encore dans le groupe.
        self._process.poll()
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError:
            self._gone = True
            return False
        except PermissionError:
            return True
        return True

    def terminate(self) -> None:
        self._send(signal.SIGTERM)

    def kill(self) -> None:
        # `signal.SIGKILL` n'existe pas sous Windows : ce nom reste dans le corps de la méthode.
        self._send(signal.SIGKILL)

    def _send(self, signum: signal.Signals) -> None:
        try:
            os.killpg(self._process.pid, signum)
        except ProcessLookupError:
            pass


class WindowsProcessTree:
    """Le Job qui contient `process`, suspendu depuis son lancement et repris ici (D-E).

    Un échec tue `process` : suspendu et hors de tout Job, il ne finirait jamais seul.
    """

    def __init__(self, process: subprocess.Popen, kernel: WindowsKernel) -> None:
        self._process = process
        self._kernel = kernel
        self._root: int | None = None
        self._job: int | None = None
        try:
            self._root = kernel.open_process(process.pid)
            self._job = kernel.create_job()
            kernel.assign(self._job, self._root)
            kernel.resume(self._root, process.pid)
        except BaseException:
            process.kill()
            self._close()
            raise

    def alive(self) -> bool:
        if self._job is None:
            return False
        self._process.poll()
        try:
            active = self._kernel.active_processes(self._job)
        except OSError:
            # Une sonde en panne ne prouve aucune fin : `stop` reste borné par ses échéances.
            return True
        if active:
            return True
        self._close()
        return False

    def terminate(self) -> None:
        try:
            self._kernel.terminate_process(self._root, TERMINATED_EXIT_CODE)
        except OSError:
            # La racine est déjà sortie et le reste de l'arbre vit : `kill` suit.
            pass

    def kill(self) -> None:
        try:
            self._kernel.terminate_job(self._job, TERMINATED_EXIT_CODE)
        except OSError:
            # L'arbre reste en vie : `alive` le dira, et `stop` rendra faux.
            pass

    def _close(self) -> None:
        """Le Job ne se ferme qu'une fois vide ou raté : fermé plus tôt, il tuerait l'arbre."""
        for handle in (self._job, self._root):
            if handle is not None:
                self._kernel.close(handle)
        self._job = self._root = None


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


def _declare(library: Any, signatures: Mapping[str, tuple[Any, ...]]) -> None:
    """Pose les prototypes : sans eux, `ctypes` tronquerait un handle à 32 bits."""
    for name, (restype, *argtypes) in signatures.items():
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = argtypes


def _win_error(call: str) -> OSError:
    return OSError(0, f"{call} failed", None, ctypes.get_last_error())


class WindowsKernel:
    """Les appels Win32 de `WindowsProcessTree`. Un handle y est un entier.

    `kernel32` et `ntdll` servent aux tests ; `ctypes.WinDLL` n'existe que sous Windows.
    """

    def __init__(self, kernel32: Any = None, ntdll: Any = None) -> None:
        self._kernel32 = (
            ctypes.WinDLL("kernel32", use_last_error=True) if kernel32 is None else kernel32
        )
        self._ntdll = ctypes.WinDLL("ntdll") if ntdll is None else ntdll
        handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        _declare(
            self._kernel32,
            {
                "OpenProcess": (handle, dword, boolean, dword),
                "OpenThread": (handle, dword, boolean, dword),
                "CloseHandle": (boolean, handle),
                "CreateJobObjectW": (handle, ctypes.c_void_p, ctypes.c_wchar_p),
                "SetInformationJobObject": (boolean, handle, ctypes.c_int, ctypes.c_void_p, dword),
                "QueryInformationJobObject": (
                    boolean, handle, ctypes.c_int, ctypes.c_void_p, dword, ctypes.c_void_p,
                ),
                "AssignProcessToJobObject": (boolean, handle, handle),
                "TerminateJobObject": (boolean, handle, ctypes.c_uint32),
                "TerminateProcess": (boolean, handle, ctypes.c_uint32),
                "CreateToolhelp32Snapshot": (handle, dword, dword),
                "Thread32First": (boolean, handle, ctypes.c_void_p),
                "Thread32Next": (boolean, handle, ctypes.c_void_p),
                "ResumeThread": (dword, handle),
            },
        )  # fmt: skip

    def open_process(self, pid: int) -> int:
        access = PROCESS_TERMINATE | PROCESS_SET_QUOTA | PROCESS_SUSPEND_RESUME
        handle = self._kernel32.OpenProcess(access, False, pid)
        if not handle:
            raise _win_error("OpenProcess")
        return handle

    def create_job(self) -> int:
        """Sans attributs de sécurité, le handle n'est pas héritable : un second détenteur
        dans un enfant empêcherait le noyau de fermer le Job à la mort du service."""
        job = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _win_error("CreateJobObjectW")
        limits = _JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            job, JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = _win_error("SetInformationJobObject")
            self.close(job)
            raise error
        return job

    def assign(self, job: int, process: int) -> None:
        if not self._kernel32.AssignProcessToJobObject(job, process):
            raise _win_error("AssignProcessToJobObject")

    def resume(self, process: int, pid: int) -> None:
        """`Popen` a fermé le handle du fil principal : reste `NtResumeProcess` ou Toolhelp32.

        `NtResumeProcess` est stable mais non documenté : son absence ou son refus passent
        à l'énumération des fils.
        """
        nt_resume = getattr(self._ntdll, "NtResumeProcess", None)
        if nt_resume is not None:
            nt_resume.restype = ctypes.c_long
            nt_resume.argtypes = [ctypes.c_void_p]
            if nt_resume(process) == 0:
                return
        self._resume_threads(pid)

    def _resume_threads(self, pid: int) -> None:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == INVALID_HANDLE:
            raise _win_error("CreateToolhelp32Snapshot")
        try:
            entry = _ThreadEntry(dwSize=ctypes.sizeof(_ThreadEntry))
            resumed = 0
            more = self._kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    self._resume_thread(entry.th32ThreadID)
                    resumed += 1
                more = self._kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.close(snapshot)
        if not resumed:
            raise OSError(f"No thread of process {pid} to resume.")

    def _resume_thread(self, thread_id: int) -> None:
        thread = self._kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, thread_id)
        if not thread:
            raise _win_error("OpenThread")
        try:
            if self._kernel32.ResumeThread(thread) == RESUME_FAILED:
                raise _win_error("ResumeThread")
        finally:
            self.close(thread)

    def active_processes(self, job: int) -> int:
        accounting = _JobAccounting()
        if not self._kernel32.QueryInformationJobObject(
            job,
            JOB_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise _win_error("QueryInformationJobObject")
        return accounting.ActiveProcesses

    def terminate_process(self, process: int, code: int) -> None:
        if not self._kernel32.TerminateProcess(process, code):
            raise _win_error("TerminateProcess")

    def terminate_job(self, job: int, code: int) -> None:
        if not self._kernel32.TerminateJobObject(job, code):
            raise _win_error("TerminateJobObject")

    def close(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)


def replace_file(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    policy: Mapping[str, Any] | None = None,
    platform: Platform | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
) -> None:
    """Remplace `dst` par `src` d'un seul `rename`, sans jamais effacer `dst`.

    Sous Windows, un autre processus qui tient `dst` ouvert fait échouer le remplacement :
    il est réessayé sur `PermissionError` jusqu'à `replace_timeout_s`, tous les
    `replace_retry_ms`, puis `ReplaceError` nomme le fichier. La dernière attente est tronquée
    à l'échéance, où a lieu un dernier essai. `policy` vaut `load_runtime()` par défaut.

    `src` reste en place sur un échec : le temporaire appartient à l'appelant, qui le retire.
    """
    platform = current() if platform is None else platform
    if platform.name != WINDOWS.name:
        os.replace(src, dst)
        return
    if policy is None:
        from .rules import load_runtime

        policy = load_runtime()
    timeout_s = policy["replace_timeout_s"]
    retry_s = policy["replace_retry_ms"] / MS_PER_S
    deadline = clock() + timeout_s
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            remaining_s = deadline - clock()
            if remaining_s <= 0:
                raise ReplaceError(
                    f"Could not replace {os.fspath(dst)}: "
                    f"file still in use after {timeout_s} s."
                ) from exc
        sleep(min(retry_s, remaining_s))


def _lock(descriptor: int, platform: Platform, module: Any) -> bool:
    """Rend faux si le verrou est tenu. `module` remplace `msvcrt` ou `fcntl` dans un test."""
    if platform.name == WINDOWS.name:
        if module is None:
            import msvcrt as module
        try:
            module.locking(descriptor, module.LK_NBLCK, LOCK_BYTES)
        except OSError:
            # Sur un descripteur tout juste ouvert, seul un verrou tenu fait échouer l'appel,
            # et son `errno` ne se lit pas partout : wine le laisse à 0.
            return False
        return True
    if module is None:
        import fcntl as module
    try:
        module.flock(descriptor, module.LOCK_EX | module.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def take_instance_lock(
    path: Path, *, platform: Platform | None = None, module: Any = None
) -> int | None:
    """Rend le descripteur qui tient le verrou de `path`, ou `None` si un autre le tient.

    N'attend jamais : c'est à l'appelant de décider. Le noyau rend le verrou à la fin du
    processus, même forcée, et le descripteur n'est pas héritable : un serveur lancé ne le
    garde pas. Sous POSIX, une autre panne que le verrou tenu lève : `flock` peut manquer
    sur un dossier réseau, et ce n'est pas un rival à attendre.
    """
    platform = current() if platform is None else platform
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, LOCK_FILE_MODE)
    taken = False
    try:
        taken = _lock(descriptor, platform, module)
    finally:
        if not taken:
            os.close(descriptor)
    return descriptor if taken else None
