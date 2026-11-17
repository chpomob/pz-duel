"""Identité du binaire (P9F31) : quelle construction tourne, sans ouvrir l'interface.

La construction (`packaging/pz-duel.spec`) appelle `make_stamp` et dépose le résultat dans
`packaging/_build_stamp.json`, que la construction embarque. Le binaire le relit par `build_id`.

L'empreinte ne porte que trois valeurs publiques : un nom court (`master-53068e1`, `v1.0.0`),
le sha du commit, l'heure de construction en UTC. Ni chemin de machine, ni nom d'utilisateur, ni
jeton : `make_stamp` ne lit que trois variables de GitHub Actions, toutes publiques.

Depuis les sources, ou si le fichier manque ou est illisible, l'identité est `dev` : jamais
d'exception, jamais de démarrage cassé.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from . import paths

STAMP_DIR = "packaging"
STAMP_NAME = "_build_stamp.json"
DEV = "dev"
LOCAL = "local"
STAMP_MAX = 4096
# Ce que le pied du lobby affiche : court, sans balise ni espace, sans rien d'autre à échapper.
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
BUILT_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
SHORT_SHA = 7


def dev_build() -> dict:
    return {"id": DEV, "sha": None, "built_at": None}


def stamp_path() -> Path:
    return paths.resource_root() / STAMP_DIR / STAMP_NAME


def read_stamp(path: Path) -> dict:
    """Rend l'empreinte de `path` si elle est entière et bien formée, `dev` sinon."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(STAMP_MAX + 1)
        if len(raw) > STAMP_MAX:
            return dev_build()
        data = json.loads(raw.decode("utf-8"))
    except Exception:  # absent, illisible, tronqué, pas du JSON : la valeur neutre
        return dev_build()
    if not isinstance(data, dict):
        return dev_build()
    ident, sha, built_at = data.get("id"), data.get("sha"), data.get("built_at")
    if not isinstance(ident, str) or not SAFE_ID.fullmatch(ident):
        return dev_build()
    if sha is not None and not (isinstance(sha, str) and FULL_SHA.fullmatch(sha)):
        return dev_build()
    if built_at is not None and not (isinstance(built_at, str) and BUILT_AT.fullmatch(built_at)):
        return dev_build()
    return {"id": ident, "sha": sha, "built_at": built_at}


def build_id() -> dict:
    """L'identité de ce qui tourne : `{id, sha, built_at}`. `dev` depuis les sources."""
    if not paths.is_frozen():
        return dev_build()
    try:
        return read_stamp(stamp_path())
    except Exception:
        return dev_build()


def describe(build: Mapping) -> str:
    """Une ligne pour `--version` : `pz-duel master-53068e1 (commit <sha>, built <heure>)`."""
    details = []
    if build.get("sha"):
        details.append(f"commit {build['sha']}")
    if build.get("built_at"):
        details.append(f"built {build['built_at']}")
    line = f"pz-duel {build.get('id') or DEV}"
    return f"{line} ({', '.join(details)})" if details else line


def make_stamp(env: Mapping[str, str], now: datetime | None = None) -> dict:
    """L'empreinte d'une construction, d'après les variables publiques de GitHub Actions.

    Un tag donne son nom (`v1.0.0`) ; une branche, `<branche>-<sha court>` (`master-53068e1`) ;
    une pull request, `<numéro>-merge-<sha court>`. Hors de la CI, sans `GITHUB_SHA` : `local`,
    sans sha. Le nom ne garde que des caractères sûrs, et `read_stamp` le relit à l'identique.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    built_at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    sha = env.get("GITHUB_SHA", "").strip().lower()
    if not FULL_SHA.fullmatch(sha):
        return {"id": LOCAL, "sha": None, "built_at": built_at}
    ref = UNSAFE.sub("-", env.get("GITHUB_REF_NAME", "")).strip("-._")
    if env.get("GITHUB_REF_TYPE") == "tag" and ref:
        ident = ref
    else:
        ident = f"{ref}-{sha[:SHORT_SHA]}" if ref else sha[:SHORT_SHA]
    ident = ident[:64]
    if not SAFE_ID.fullmatch(ident):
        ident = sha[:SHORT_SHA]
    return {"id": ident, "sha": sha, "built_at": built_at}
