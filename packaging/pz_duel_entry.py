"""Point d'entrée du binaire : PyInstaller l'exécute comme un script, hors de tout paquet.

`service/main.py` a des imports relatifs et ne peut pas jouer ce rôle : ce fichier l'importe
en absolu.

Un `--onefile` a deux processus : le chargeur extrait les ressources et relance l'exécutable,
et c'est ce second processus qui porte le service. Le chargeur de PyInstaller 6.22.3 le crée
sans objet Job (`pyi_utils_create_child`) : un `taskkill /F` du chargeur le laisserait vivre,
et avec lui le Job des serveurs, alors que R2 veut leur fin quelle que soit la façon de finir.
Le service attend donc son parent, et meurt avec lui comme s'il avait été tué lui-même : le
noyau ferme le handle du Job et tue l'arbre (D-E). Pas d'arrêt ordonné ici : ses paliers
durent plus que les 5 s d'AC10.

Cette attente vit ici et non dans `service/` : elle ne concerne que le binaire. Le journal
fichier aussi : sans console, `sys.stderr` vaut `None`, et le gestionnaire de dernier recours de
`logging` n'a plus nulle part où écrire. Depuis les sources, `stderr` reste là pour ça.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable

from service import paths
from service.main import log_line, main

FORCED_END_CODE = 1
VERSION_FLAG = "--version"
# `ATTACH_PARENT_PROCESS` de l'API Win32 : `(DWORD)-1`.
ATTACH_PARENT_PROCESS = 0xFFFFFFFF
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = 2**64 - 1 if sys.maxsize > 2**32 else 2**32 - 1
PARENT_POLL_S = 1
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class SecretSafeFormatter(logging.Formatter):
    """Garde la trace et le type d'une exception, jamais son message.

    Le fichier reste sur le disque, et un message d'exception pourrait citer un mot de passe.
    """

    def formatException(self, ei) -> str:
        return "".join(traceback.format_tb(ei[2])) + ei[0].__name__


def open_log_file() -> None:
    """En mode figé, envoie `logging` au fichier de log, dès `WARNING`.

    Le gestionnaire s'ouvre ici et non au premier message : une ouverture ratée lèverait alors
    chez l'appelant de `logging`. Un disque plein n'arrête pas le service, comme pour `log_line`.
    """
    if not paths.is_frozen():
        return
    path = paths.log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8", errors="backslashreplace")
    except OSError:
        return
    handler.setLevel(logging.WARNING)
    handler.setFormatter(SecretSafeFormatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)


def wait_for_parent_windows(parent: int) -> None:
    """Rend la main à la fin de `parent`. Un parent déjà parti ne s'ouvre plus."""
    import _winapi

    try:
        handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, parent)
    except OSError:
        return
    _winapi.WaitForSingleObject(handle, _winapi.INFINITE)


def wait_for_parent_posix(parent: int) -> None:
    """Rend la main quand `parent` n'est plus le parent : orphelin, on change de parent."""
    while os.getppid() == parent:
        time.sleep(PARENT_POLL_S)


def end_with_parent(parent: int, wait: Callable[[int], None], end: Callable[[int], None]) -> None:
    wait(parent)
    log_line("The launcher process ended: stopping now")
    end(FORCED_END_CODE)


def watch_parent() -> None:
    """Lance l'attente en mode figé. Sans parent lisible, le service tourne quand même."""
    if not paths.is_frozen():
        return
    try:
        parent = os.getppid()
    except OSError as exc:
        log_line(f"Could not read the launcher process: {exc}")
        return
    wait = wait_for_parent_windows if sys.platform == "win32" else wait_for_parent_posix
    threading.Thread(
        target=end_with_parent, args=(parent, wait, os._exit), name="duel-parent", daemon=True
    ).start()


def parent_pid_windows(pid: int) -> int | None:
    """Rend le parent de `pid` d'après un instantané des processus, `None` s'il n'y figure pas."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        kernel32.CloseHandle(snapshot)


def console_owners(own_parent: Callable[[], int], parent_of: Callable[[int], int | None]):
    """Les processus dont la console est à essayer, du plus proche au plus lointain.

    En `--onefile`, ce processus est l'enfant du chargeur, fenêtré lui aussi et donc sans
    console : `ATTACH_PARENT_PROCESS` y échoue. La console de `cmd` est celle du parent du
    chargeur. Le grand-parent n'est cherché que si le parent a échoué.
    """
    yield ATTACH_PARENT_PROCESS
    try:
        grandparent = parent_of(own_parent())
    except OSError:
        return
    if grandparent:
        yield grandparent


def attach_first_console(owners, attach: Callable[[int], bool]) -> bool:
    """S'attache à la première console qui accepte ; `False` si aucune."""
    return any(attach(owner) for owner in owners)


def attach_parent_console() -> None:
    """Pour `--version` (P9F31) : sans console (`console=False`), `sys.stdout` vaut `None`.

    Lancé depuis `cmd` ou PowerShell sans redirection, le binaire n'a nulle part où écrire : il
    s'attache à la console de son parent, ou à défaut de son grand-parent (le chargeur
    `--onefile` n'en a pas), et écrit sur `CONOUT$`. Redirigé (`> v.txt`, un tube), `sys.stdout`
    existe déjà et rien ne change. Un échec laisse le log seul témoin.
    """
    if sys.platform != "win32" or sys.stdout is not None:
        return
    import ctypes

    try:
        attach = ctypes.windll.kernel32.AttachConsole
        if not attach_first_console(console_owners(os.getppid, parent_pid_windows), attach):
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="backslashreplace")
    except OSError:
        return


if __name__ == "__main__":
    if VERSION_FLAG in sys.argv[1:]:
        # Ni attente du parent ni journal : `--version` rend la main tout de suite.
        attach_parent_console()
        main()
        sys.exit(0)
    open_log_file()
    watch_parent()
    main()
