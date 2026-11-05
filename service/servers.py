"""Réglages gérés des serveurs, commande de lancement et expurgation des secrets.

Les fichiers de PZ restent ceux de l'utilisateur : seules les clés gérées changent. Le
texte passe par `surrogateescape` et sans traduction des fins de ligne, si bien qu'un
commentaire français ou un octet invalide ressort à l'identique.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .preflight import SCRIPT

ENCODING = "utf-8"
ENCODING_ERRORS = "surrogateescape"
INI_MODE = 0o600
REDACTED = "***"
DAY_LENGTH_KEY = "DayLength"
# Un commentaire commence par `#` ; le `\r` d'une fin CRLF reste dans la valeur capturée.
INI_LINE = re.compile(r"[ \t]*([^\s#=]+)[ \t]*=(.*)")
LINE_BREAK = re.compile(r"[\r\n]")


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
