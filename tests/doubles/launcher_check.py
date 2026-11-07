"""Exécute le vrai contrôle embarqué du `.bat`, sans `cmd` : son `-Command` tel quel, par `pwsh`.

`pwsh` est livré sur `ubuntu-latest` et `windows-latest`. Sans lui, le test est sauté : un équivalent
Python du contrôle ne prouverait que lui-même.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CODE_REFUSED = b"This is not the code of this file."
COMMAND = re.compile(rb'-Command "(.*)"\r\n')
RUN_TIMEOUT_S = 60


def run_embedded_check(
    launcher: bytes, typed: str, folder: Path
) -> subprocess.CompletedProcess[bytes]:
    """Lance le `.bat` comme `cmd` le ferait après `set /p`, sur un poste sans le jeu."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh absent : le contrôle embarqué n'a pas tourné")
    bat = folder / "pz-duel-join.bat"
    bat.write_bytes(launcher)
    profile = folder / "profile"
    profile.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PZDUEL_SELF": str(bat),
        "PZDUEL_TYPED": typed,
        "USERPROFILE": str(profile),
        "ProgramFiles(x86)": str(folder / "no-steam"),
    }
    command = COMMAND.search(launcher).group(1).decode("ascii")
    return subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=folder,
        env=env,
        timeout=RUN_TIMEOUT_S,
    )
