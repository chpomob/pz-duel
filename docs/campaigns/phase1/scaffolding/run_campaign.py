#!/usr/bin/env python3
"""Harnais de campagne phase 1 — PZ Duel.

Enchaîne les 24 étapes du plan approuvé (`docs/campaigns/phase1/plan.md`), une
boucle de code par étape (Claude DEV / Codex REVIEW), vérifie la suite de tests
après chaque merge, journalise et s'arrête au premier échec.

Usage :
  .venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --list
  .venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --dry-run
  .venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --start-at P6
  .venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --only P6,P7

Aucune valeur de jeu ici : le harnais ne fait qu'orchestrer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/chpo/pz-duel")
SCAFF = REPO / "docs/campaigns/phase1/scaffolding"
PLAN = REPO / "docs/campaigns/phase1/plan.md"
SPEC = REPO / "docs/campaigns/phase1/spec.md"
STEPS = SCAFF / "steps"
LOGS = SCAFF / "logs"
LOOP_OUT = SCAFF / "loop-out"
CAMPAIGN_LOG = SCAFF / "campaign.log"

PY = str(REPO / ".venv/bin/python")
LOOP = "/home/chpo/.hermes/skills/adversarial-code-loop/scripts/adversarial_loop.py"
DEV_CMD = (
    "python3 /home/chpo/claude-tmux-wrapper/claude-tmux.py "
    "--timeout 2700 --hard-timeout 3300 --max-turns 30 --cwd /home/chpo/pz-duel"
)
REVIEW_CMD = "bash /home/chpo/.config/adversarial/codex-loop-review.sh"

# P24 est la revue de branche complète : elle ne s'exécute PAS comme une boucle de code
# (un DEV produirait le rapport et le reviewer l'écraserait). Elle se lance à part, avec
# adversarial-code-review --diff-git, une fois P1..P23 mergées.
GATE_STEPS = {"P24"}
TEST_CMD = f"{PY} -m pytest -q"

STEP_RE = re.compile(r"^### (P\d+): (.+)$", re.M)
FIELD_RE = {
    "files": re.compile(r"^- \*\*Files:\*\* \[(.*)\]$", re.M),
    "description": re.compile(r"^- \*\*Description:\*\* (.*)$", re.M),
    "dependencies": re.compile(r"^- \*\*Dependencies:\*\* \[(.*)\]$", re.M),
    "tests": re.compile(r"^- \*\*Tests:\*\* (.*)$", re.M),
    "risks": re.compile(r"^- \*\*Risks:\*\* (.*)$", re.M),
}


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, console: bool = True) -> None:
    line = f"[{now()}] {msg}"
    CAMPAIGN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with CAMPAIGN_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if console:
        print(line, flush=True)


def run(cmd: list[str], cwd: Path = REPO, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)


def parse_plan() -> list[dict]:
    text = PLAN.read_text(encoding="utf-8")
    heads = list(STEP_RE.finditer(text))
    steps: list[dict] = []
    for i, m in enumerate(heads):
        block = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        step = {"id": m.group(1), "title": m.group(2).strip(), "block": block.strip()}
        for key, rx in FIELD_RE.items():
            fm = rx.search(block)
            step[key] = fm.group(1).strip() if fm else ""
        steps.append(step)
    return steps


def ac_text() -> dict[str, str]:
    """ACn -> ligne complète, pour citer la loi dans la spec d'étape."""
    out: dict[str, str] = {}
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^- (AC\d+)\b", line)
        if m:
            out[m.group(1)] = line[2:].strip()
    return out


def write_step_spec(step: dict, acs: dict[str, str]) -> Path:
    ids = re.findall(r"AC\d+", step.get("tests", ""))
    quoted = "\n".join(f"- {acs[i]}" for i in sorted(set(ids), key=lambda s: int(s[2:])) if i in acs)
    subject = f"feat(phase1): {step['id']} — {step['title']}"
    body = f"""# {step['id']} — {step['title']}

commit: {subject}

## Contexte

Projet `pz-duel` : mode duel Project Zomboid, deux serveurs dédiés, un mod Lua, un moteur
de règles. La phase 1 remplace le backend simulé par un duel réel.
Autorités, dans cet ordre : `docs/campaigns/phase1/spec.md` (77 AC) — puis
`docs/campaigns/phase1/plan.md`, qui fixe le périmètre de CETTE étape.

## Périmètre (verbatim du plan)

{step['block']}

## Critères d'acceptation couverts (verbatim de la spec)

{quoted or "(aucun AC nommé — l'étape est un socle ; respecte sa Description et ses Tests)"}

## Règles

- Ne modifie QUE les fichiers listés dans `Files` de cette étape, plus les fichiers de test
  qu'elle nomme. Si une dépendance manque, tu la constates dans ta réponse finale, tu ne
  sors pas du périmètre.
- Python 3.11+ ; `service/` reste **stdlib uniquement**. Tests : `{TEST_CMD}`.
- Lua : pas de `os.`/`io.`, aucune dépendance externe.
- Documents et messages d'erreur en français ; identifiants, fichiers et code en anglais.
- Aucun secret (mot de passe admin/RCON) dans les logs, le journal ou l'état exposé.
- Ne touche ni aux documents `01`-`09`, ni à `spec.md`, ni à `plan.md`.
- Toute valeur numérique vient de `service/runtime.json` ou des presets : aucune en dur.

## Definition of done

- Les tests de l'étape existent, portent exactement les noms cités, et passent.
- `{TEST_CMD}` est vert.
- Un seul commit, sujet : `{subject}`.
- Réponse finale en français : fichiers touchés, tests ajoutés, AC couverts, écarts constatés.
"""
    STEPS.mkdir(parents=True, exist_ok=True)
    path = STEPS / f"{step['id']}-spec.md"
    path.write_text(body, encoding="utf-8")
    return path


def git(*args: str) -> str:
    r = run(["git", *args])
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def require_clean() -> None:
    dirty = git("status", "--porcelain")
    if dirty:
        # une boucle peut laisser .gitignore modifié en restaurant son workdir ; ce n'est
        # pas une modification de l'étape, on la rétablit plutôt que d'arrêter la campagne.
        paths = [line.split(maxsplit=1)[-1].strip() for line in dirty.splitlines() if line.strip()]
        if paths and all(p == ".gitignore" for p in paths):
            git("checkout", "--", ".gitignore")
            dirty = git("status", "--porcelain")
    if dirty:
        raise SystemExit(f"arbre sale, rien n'est lancé :\n{dirty}")


def run_loop(step: dict, spec_path: Path) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = LOGS / f"{step['id']}.log"
    cmd = [
        PY, LOOP,
        "--spec", str(spec_path),
        "--workdir", str(REPO),
        "--feature", f"phase1-{step['id'].lower()}",
        "--dev-cmd", DEV_CMD,
        "--review-cmd", REVIEW_CMD,
        "--test-cmd", TEST_CMD,
        "--max-loops", "3",
        "--no-arbiter",
        "--timeout", "3600",
        "--out", str(LOOP_OUT),
    ]
    with logfile.open("w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        proc = subprocess.run(cmd, cwd=REPO, stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


def tail(path: Path, n: int = 12) -> str:
    if not path.exists():
        return "(pas de log)"
    return "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:])


def gate(step: dict) -> tuple[bool, str]:
    """Vérifie ce qu'un APPROVE de revue ne prouve pas : le commit, la suite, les invariants."""
    subject = git("log", "-1", "--format=%s")
    expected = f"feat(phase1): {step['id']} — {step['title']}"
    notes = [f"HEAD: {subject}"]
    if subject != expected:
        # la boucle squash-merge avec son propre sujet ; on rétablit la convention du plan
        amend = run(["git", "commit", "--amend", "-q", "-m", expected])
        if amend.returncode != 0:
            return False, "amend impossible: " + (amend.stderr.strip() or amend.stdout.strip())
        notes.append(f"sujet rétabli: {expected}")
        subject = expected
    r = run([PY, "-m", "pytest", "-q"], timeout=1800)
    summary = (r.stdout.strip().splitlines() or ["(aucune sortie)"])[-1]
    notes.append(f"pytest: {summary}")
    if r.returncode != 0:
        notes.append("SUITE ROUGE")
        return False, " · ".join(notes)
    # invariants mécaniques de la phase
    stdlib = run([PY, "-m", "pytest", "-q", "tests/test_stdlib_only.py"], timeout=600)
    if stdlib.returncode != 0:
        notes.append("stdlib_only ROUGE")
        return False, " · ".join(notes)
    return True, " · ".join(notes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-at", default=None)
    ap.add_argument("--only", default=None, help="liste P6,P7")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="génère les specs d'étape et s'arrête")
    args = ap.parse_args()

    steps = parse_plan()
    if not steps:
        print("aucune étape trouvée dans plan.md", file=sys.stderr)
        return 2
    if args.list:
        for s in steps:
            print(f"{s['id']:>4}  {s['title']}  [{s['files'][:60]}…]")
        return 0

    acs = ac_text()
    paths = {s["id"]: write_step_spec(s, acs) for s in steps}
    print(f"{len(steps)} specs d'étape écrites dans {STEPS}")
    if args.dry_run:
        return 0

    selected = [s for s in steps if s["id"] not in GATE_STEPS]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        selected = [s for s in selected if s["id"] in wanted]
    elif args.start_at:
        # l'index se cherche dans la liste FILTRÉE : sinon P24 (gate) revient dans la sélection
        idx = next((i for i, s in enumerate(selected) if s["id"] == args.start_at), None)
        if idx is None:
            print(f"étape inconnue: {args.start_at}", file=sys.stderr)
            return 2
        selected = selected[idx:]

    require_clean()
    log(f"campagne phase1 : {len(selected)} étape(s), de {selected[0]['id']} à {selected[-1]['id']}")
    if GATE_STEPS:
        log(f"exclu du harnais : {', '.join(sorted(GATE_STEPS))} (revue de branche, à lancer séparément)")

    for step in selected:
        log(f"{step['id']}: démarrage — {step['title']}")
        before = git("rev-parse", "HEAD")
        code = run_loop(step, paths[step["id"]])
        if code != 0:
            log(f"{step['id']}: ÉCHEC boucle (code {code}) — arbre laissé en l'état, arrêt")
            log(tail(LOGS / f"{step['id']}.log"))
            return code
        # la boucle merge par défaut ; on s'assure d'être sur master
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "master":
            git("checkout", "master")
        ok, notes = gate(step)
        log(f"{step['id']}: {'OK' if ok else 'GATE ROUGE'} — {notes}")
        if not ok:
            log(tail(LOGS / f"{step['id']}.log"))
            return 1
        after = git("rev-parse", "HEAD")
        if after == before:
            log(f"{step['id']}: aucun commit produit — arrêt")
            return 1

    log("campagne phase1 : toutes les étapes demandées sont passées")
    return 0


if __name__ == "__main__":
    sys.exit(main())
