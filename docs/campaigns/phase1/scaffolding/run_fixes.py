#!/usr/bin/env python3
"""Round de correction après la revue de branche complète.

Enchaîne les étapes F1..F4 de `fixes/` : une boucle de code par étape
(Claude DEV / Codex REVIEW), gate mécanique après merge, arrêt au premier échec.

Usage :
  .venv/bin/python docs/campaigns/phase1/scaffolding/run_fixes.py --list
  .venv/bin/python docs/campaigns/phase1/scaffolding/run_fixes.py
  .venv/bin/python docs/campaigns/phase1/scaffolding/run_fixes.py --start-at F2
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/chpo/pz-duel")
SCAFF = REPO / "docs/campaigns/phase1/scaffolding"
FIXES_DIR = SCAFF / "fixes"
LOGS = SCAFF / "logs"
LOOP_OUT = SCAFF / "loop-out"
CAMPAIGN_LOG = SCAFF / "campaign.log"

PY = str(REPO / ".venv/bin/python")
LOOP = "/home/chpo/.hermes/skills/adversarial-code-loop/scripts/adversarial_loop.py"
DEV_CMD = ("python3 /home/chpo/claude-tmux-wrapper/claude-tmux.py "
           "--timeout 3600 --hard-timeout 4200 --max-turns 40 --cwd /home/chpo/pz-duel")
REVIEW_CMD = "bash /home/chpo/.config/adversarial/codex-loop-review.sh"
TEST_CMD = f"{PY} -m pytest -q"


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now()}] {msg}"
    with CAMPAIGN_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, timeout=timeout)


def git(*args: str) -> str:
    r = run(["git", *args])
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def steps() -> list[dict]:
    out = []
    for path in FIXES_DIR.glob("F*.md"):
        first = path.read_text(encoding="utf-8").splitlines()[0]
        title = first.split("—", 1)[1].strip() if "—" in first else path.stem
        out.append({"id": path.stem, "title": title, "path": path})

    # F9 < F10 < F11 : la comparaison de chaînes plaçait F10 avant F9.
    def order(step: dict) -> tuple[str, int]:
        head = step["id"].lstrip("F")
        digits = "".join(ch for ch in head if ch.isdigit())
        return ("F", int(digits) if digits else 0)

    return sorted(out, key=order)


def run_loop(step: dict) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = LOGS / f"{step['id']}.log"
    cmd = [PY, LOOP, "--spec", str(step["path"]), "--workdir", str(REPO),
           "--feature", f"phase1-{step['id'].lower()}", "--dev-cmd", DEV_CMD,
           "--review-cmd", REVIEW_CMD, "--test-cmd", TEST_CMD,
           "--max-loops", "3", "--no-arbiter", "--timeout", "3600", "--out", str(LOOP_OUT)]
    with logfile.open("w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        return subprocess.run(cmd, cwd=REPO, stdout=fh, stderr=subprocess.STDOUT).returncode


def gate(step: dict, before: str) -> tuple[bool, str]:
    after = git("rev-parse", "HEAD")
    if after == before:
        return False, "aucun commit produit pour l'étape"
    subject = git("log", "-1", "--format=%s")
    expected = f"fix(phase1): {step['id']} — {step['title']}"
    notes = [f"HEAD: {subject}"]
    if subject != expected:
        amend = run(["git", "commit", "--amend", "-q", "-m", expected])
        if amend.returncode != 0:
            return False, "amend impossible: " + (amend.stderr.strip() or amend.stdout.strip())
        notes.append(f"sujet rétabli: {expected}")
    r = run([PY, "-m", "pytest", "-q"], timeout=1800)
    summary = (r.stdout.strip().splitlines() or ["(aucune sortie)"])[-1]
    notes.append(f"pytest: {summary}")
    if r.returncode != 0:
        # Un rouge doit rester diagnosticable : la sortie complète et les noms des tests
        # en échec vont dans un fichier, sinon on ne sait pas ce qui a échoué.
        LOGS.mkdir(parents=True, exist_ok=True)
        trace = LOGS / f"{step['id']}.gate.log"
        trace.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
        failed = [line for line in (r.stdout or "").splitlines() if line.startswith("FAILED")]
        notes.append("échecs: " + (" | ".join(failed[:5]) if failed else "non nommés"))
        return False, " · ".join(notes) + f" · SUITE ROUGE (détail: {trace})"
    return True, " · ".join(notes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-at")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    todo = steps()
    if args.list:
        for s in todo:
            print(f"{s['id']} — {s['title']}")
        return 0
    if args.start_at:
        start = args.start_at
        digits = "".join(ch for ch in start.lstrip("F") if ch.isdigit())
        threshold = int(digits) if digits else 0
        todo = [s for s in todo if int("".join(ch for ch in s["id"].lstrip("F") if ch.isdigit()) or 0) >= threshold]

    if git("status", "--porcelain"):
        print("arbre sale, rien n'est lancé", file=sys.stderr)
        return 2

    log(f"round de correction : {len(todo)} étape(s), de {todo[0]['id']} à {todo[-1]['id']}")
    for step in todo:
        before = git("rev-parse", "HEAD")
        log(f"{step['id']}: démarrage — {step['title']}")
        code = run_loop(step)
        if code != 0:
            log(f"{step['id']}: ÉCHEC boucle (code {code}) — arrêt")
            tail = (LOGS / f"{step['id']}.log").read_text(encoding="utf-8", errors="ignore").splitlines()[-10:]
            log("\n".join(tail))
            return code
        if git("rev-parse", "--abbrev-ref", "HEAD") != "master":
            git("checkout", "master")
        ok, notes = gate(step, before)
        log(f"{step['id']}: {'OK' if ok else 'GATE ROUGE'} — {notes}")
        if not ok:
            return 1
    log("round de correction : terminé")
    return 0


if __name__ == "__main__":
    sys.exit(main())
