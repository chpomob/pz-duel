#!/usr/bin/env python3
"""Harnais de campagne phase 2 — PZ Duel (Windows, packaging, zéro ligne de commande).

Enchaîne les étapes du plan approuvé (`docs/campaigns/phase3/plan.md`), une boucle de
code par étape (Claude DEV / Codex REVIEW), vérifie la suite après chaque merge,
journalise et s'arrête au premier échec.

Usage :
  .venv/bin/python docs/campaigns/phase3/scaffolding/run_campaign.py --list
  .venv/bin/python docs/campaigns/phase3/scaffolding/run_campaign.py --dry-run
  .venv/bin/python docs/campaigns/phase3/scaffolding/run_campaign.py --start-at P6
  .venv/bin/python docs/campaigns/phase3/scaffolding/run_campaign.py --only P6,P7
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/chpo/pz-duel")
SCAFF = REPO / "docs/campaigns/phase3/scaffolding"
PLAN = REPO / "docs/campaigns/phase3/plan.md"
SPEC = REPO / "docs/campaigns/phase3/spec.md"
STEPS = SCAFF / "steps"
LOGS = SCAFF / "logs"
LOOP_OUT = SCAFF / "loop-out"
CAMPAIGN_LOG = SCAFF / "campaign.log"

PY = str(REPO / ".venv/bin/python")
LOOP = "/home/chpo/.hermes/skills/adversarial-code-loop/scripts/adversarial_loop.py"
DEV_CMD = (
    "python3 /home/chpo/claude-tmux-wrapper/claude-tmux.py "
    "--timeout 3600 --hard-timeout 4200 --max-turns 40 --cwd /home/chpo/pz-duel"
)
REVIEW_CMD = "bash /home/chpo/.config/adversarial/codex-loop-review.sh"

# Une seule étape ne s'exécute PAS comme une boucle de code :
# - P9 est la revue de toute la branche : un DEV produirait le rapport, le reviewer l'écraserait.
#   Elle se lance à part avec adversarial-code-review --diff-git après P1..P8, et c'est elle qui
#   produit `docs/campaigns/phase3/review/`.
# Ne jamais remettre P9 dans STEP_IDS.
GATE_STEPS = {"P9"}
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


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, timeout=timeout)


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
    subject = f"feat(phase3): {step['id']} — {step['title']}"
    body = f"""# {step['id']} — {step['title']}

commit: {subject}

## Contexte

Projet `pz-duel` : duel Project Zomboid à deux serveurs, un arbitre Python stdlib, un mod Lua.
Les phases 1 et 2 sont closes : le produit tourne sous Windows et monte jusqu'au lobby sur un vrai
poste (match `c3f1b335`, 42.22.2). La phase 3 ouvre les ports de la box par UPnP, à la demande
explicite de l'hôte, et lit son adresse publique.

Autorités, dans cet ordre : `docs/campaigns/phase3/spec.md` (9 exigences, 33 critères, amendement A1
compris) — puis `docs/campaigns/phase3/plan.md`, qui fixe le périmètre de CETTE étape.

Décisions de l'opérateur, non négociables : UPnP implémenté par nous en stdlib, **jamais
automatique**, **trois mappings seulement** (les deux UDP du camp B et le TCP de partage), bail borné
à 7 200 s avec suppression à l'arrêt, adresse publique lue mais gardée locale, **aucun test contre
un vrai routeur ni contre Internet**, un échec ne bloque jamais un duel.

## Périmètre (verbatim du plan)

{step['block']}

## Critères d'acceptation couverts (verbatim de la spec)

{quoted or "(aucun AC nommé — l'étape est un socle ; respecte sa Description et ses Tests)"}

## Règles

- Ne modifie QUE les fichiers listés dans `Files` de cette étape, plus les fichiers de test
  qu'elle nomme. Si une dépendance manque, tu la constates dans ta réponse finale, tu ne sors
  pas du périmètre.
- Python 3.12+ ; `service/` reste **stdlib uniquement** (`tests/test_stdlib_only.py` doit rester
  vert). Aucun test ne sort de `127.0.0.0/8` et aucun n'émet de datagramme multicast : le faux IGD
  local est le seul interlocuteur.
- Toute valeur (ports, délais, bail, description du mapping, taille maximale d'une réponse) vient de
  `service/runtime.json` : aucun littéral dans le code. Les constantes du protocole SSDP (1900,
  `239.255.255.250`) sont les seules admises, nommées une fois dans `service/upnp.py`.
- `managed_ini.UPnP` reste `"false"` : on n'active pas l'UPnP du jeu.
- L'adresse publique n'entre ni dans le journal du match, ni dans `state.log`, ni dans un log, ni
  vers l'extérieur.
- Les chaînes **ajoutées** à l'UI sont en anglais ; les documents de campagne restent en français.
- Ne touche ni à `docs/campaigns/phase1/`, ni à `docs/campaigns/phase2/`, ni au protocole, ni aux
  règles du duel, ni à `mods/duelcore/`.

## Definition of done

- Les tests de l'étape existent, portent exactement les noms cités, et passent.
- `{TEST_CMD}` est vert, le nombre de tests augmente, et le nombre de skips ne dépasse pas les 16
  observés sous Linux au départ de la phase.
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


def read_gitignore() -> bytes | None:
    path = REPO / ".gitignore"
    return path.read_bytes() if path.exists() else None


def require_clean(gitignore_before: bytes | None = None) -> None:
    dirty = git("status", "--porcelain")
    if dirty:
        paths = [line.split(maxsplit=1)[-1].strip() for line in dirty.splitlines() if line.strip()]
        if paths and all(p == ".gitignore" for p in paths):
            if gitignore_before is None or read_gitignore() != gitignore_before:
                raise SystemExit(".gitignore modifié hors de l'étape, rien n'est restauré : "
                                 "à commiter ou à annuler à la main")
            git("checkout", "HEAD", "--", ".gitignore")
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
        "--feature", f"phase3-{step['id'].lower()}",
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


def gate(step: dict, before: str) -> tuple[bool, str]:
    """Vérifie ce qu'un APPROVE de revue ne prouve pas : le commit, la suite, les invariants."""
    if git("rev-list", "--count", f"{before}..HEAD") == "0":
        return False, f"aucun commit produit pour l'étape (HEAD toujours à {before[:12]})"
    subject = git("log", "-1", "--format=%s")
    expected = f"feat(phase3): {step['id']} — {step['title']}"
    notes = [f"HEAD: {subject}"]
    if subject != expected:
        body = git("log", "-1", "--format=%b")
        message = ["-m", expected, *(["-m", body] if body else [])]
        amend = run(["git", "commit", "--amend", "-q", *message])
        if amend.returncode != 0:
            return False, "amend impossible: " + (amend.stderr.strip() or amend.stdout.strip())
        notes.append(f"sujet rétabli: {expected}")
        subject = expected
    r = run([PY, "-m", "pytest", "-q"], timeout=1800)
    summary = (r.stdout.strip().splitlines() or ["(aucune sortie)"])[-1]
    notes.append(f"pytest: {summary}")
    if r.returncode != 0:
        LOGS.mkdir(parents=True, exist_ok=True)
        trace = LOGS / f"{step['id']}.gate.log"
        trace.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
        failed = [line for line in (r.stdout or "").splitlines() if line.startswith("FAILED")]
        notes.append("échecs: " + (" | ".join(failed[:5]) if failed else "non nommés"))
        notes.append("SUITE ROUGE")
        return False, " · ".join(notes) + f" · détail: {trace}"
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
        idx = next((i for i, s in enumerate(selected) if s["id"] == args.start_at), None)
        if idx is None:
            print(f"étape inconnue: {args.start_at}", file=sys.stderr)
            return 2
        selected = selected[idx:]

    require_clean()
    log(f"campagne phase3 : {len(selected)} étape(s), de {selected[0]['id']} à {selected[-1]['id']}")
    if GATE_STEPS:
        log(f"exclu du harnais : {', '.join(sorted(GATE_STEPS))} (revue de branche, à lancer séparément)")

    for step in selected:
        log(f"{step['id']}: démarrage — {step['title']}")
        before = git("rev-parse", "master")
        gitignore_before = read_gitignore()
        code = run_loop(step, paths[step["id"]])
        if code != 0:
            log(f"{step['id']}: ÉCHEC boucle (code {code}) — arbre laissé en l'état, arrêt")
            log(tail(LOGS / f"{step['id']}.log"))
            return code
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "master":
            git("checkout", "master")
        ok, notes = gate(step, before)
        log(f"{step['id']}: {'OK' if ok else 'GATE ROUGE'} — {notes}")
        if not ok:
            log(tail(LOGS / f"{step['id']}.log"))
            return 1
        require_clean(gitignore_before)

    log("campagne phase3 : toutes les étapes demandées sont passées")
    return 0


if __name__ == "__main__":
    sys.exit(main())
