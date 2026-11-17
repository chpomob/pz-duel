# Construction `--onefile` de PZ Duel (R4, R8).
#
#   pyinstaller packaging/pz-duel.spec --distpath <dossier> --workpath <dossier>
#
# La sortie va hors du dépôt, sous `$RUNNER_TEMP` en CI : `.gitignore` ne change pas.
# `console=False` (D-I) : pas de fenêtre de terminal, les erreurs vont au fichier de log, que
# `open_log_file` de `pz_duel_entry.py` branche sur `logging` au démarrage.
# Les destinations sont celles que `service/paths.py` attend sous `sys._MEIPASS`.

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))

from service.build_info import STAMP_DIR, STAMP_NAME, make_stamp  # noqa: E402

# P9F31 : l'identité du binaire, d'après les seules variables publiques de GitHub Actions
# (`GITHUB_SHA`, `GITHUB_REF_NAME`, `GITHUB_REF_TYPE`). Écrite sous `workpath`, hors du dépôt,
# et embarquée sous `packaging/_build_stamp.json`, où `service/build_info.py` la relit.
STAMP_ROOT = Path(globals().get("workpath") or tempfile.mkdtemp(prefix="pz-duel-stamp-"))
STAMP = STAMP_ROOT / "stamp" / STAMP_NAME
STAMP.parent.mkdir(parents=True, exist_ok=True)
STAMP.write_text(json.dumps(make_stamp(os.environ)), encoding="utf-8")
print(f"Build stamp: {STAMP.read_text(encoding='utf-8')}")

analysis = Analysis(
    [str(ROOT / "packaging" / "pz_duel_entry.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "ui"), "ui"),
        (str(ROOT / "mods" / "duelcore"), "mods/duelcore"),
        (str(ROOT / "service" / "presets"), "service/presets"),
        (str(ROOT / "service" / "runtime.json"), "service"),
        (str(ROOT / "service" / "effects.json"), "service"),
        (str(STAMP), STAMP_DIR),
    ],
)
archive = PYZ(analysis.pure)
binary = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name="pz-duel",
    console=False,
    upx=False,
)
