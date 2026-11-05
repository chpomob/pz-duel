# Harnais de campagne — phase 1

Enchaîne les 24 étapes du plan approuvé, une boucle de code par étape
(Claude DEV / Codex REVIEW), avec vérification de la suite après chaque merge.

## Lancer

```bash
cd /home/chpo/pz-duel
.venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --list     # les étapes
.venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --dry-run  # génère les specs
.venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py            # tout
.venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --start-at P6
.venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --only P6,P7
```

## Ce qu'il fait

1. Lit `../plan.md`, découpe les étapes `### Pn:`, et écrit pour chacune une spec
   d'étape dans `steps/Pn-spec.md` : périmètre verbatim du plan, critères
   d'acceptation verbatim de la spec, règles du projet, definition of done.
2. Pour chaque étape, lance `adversarial_loop.py` avec :
   - DEV = `claude-tmux.py` (TUI, jamais `claude -p`, pas de `--model` forcé) ;
   - REVIEW = `codex-loop-review.sh` ;
   - `--test-cmd` = pytest du venv du projet ;
   - 3 boucles max, sans arbitre, timeout 2700 s.
3. Après la boucle : vérifie que HEAD est sur `master`, que le sujet du commit est
   `feat(phase1): Pn — …`, que `pytest -q` est vert et que `tests/test_stdlib_only.py`
   passe. Un APPROVE de revue ne prouve rien de tout cela.
4. Journalise dans `campaign.log` (une ligne par événement) et laisse le log complet
   de chaque étape dans `logs/Pn.log`. S'arrête au premier échec, sans relancer.

## Ce qu'il ne fait pas

- Il ne réécrit ni la spec ni le plan : ce sont les autorités.
- Il ne relance pas une étape en échec : un échec se diagnostique
  (`logs/Pn.log`, `loop-out/phase1-pn/`) avant toute reprise.
- Il ne touche pas aux documents de conception `01`-`09`.

## Reprise

Après un échec corrigé à la main ou une étape passée hors harnais :

```bash
.venv/bin/python docs/campaigns/phase1/scaffolding/run_campaign.py --start-at P<n+1>
```

L'arbre doit être propre : `git status --porcelain` vide.
