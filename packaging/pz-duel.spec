# Construction `--onefile` de PZ Duel (R4, R8).
#
#   pyinstaller packaging/pz-duel.spec --distpath <dossier> --workpath <dossier>
#
# La sortie va hors du dépôt, sous `$RUNNER_TEMP` en CI : `.gitignore` ne change pas.
# `console=False` (D-I) : pas de fenêtre de terminal, les erreurs vont au fichier de log, que
# `open_log_file` de `pz_duel_entry.py` branche sur `logging` au démarrage.
# Les destinations sont celles que `service/paths.py` attend sous `sys._MEIPASS`.

from pathlib import Path

ROOT = Path(SPECPATH).parent

analysis = Analysis(
    [str(ROOT / "packaging" / "pz_duel_entry.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "ui"), "ui"),
        (str(ROOT / "mods" / "duelcore"), "mods/duelcore"),
        (str(ROOT / "service" / "presets"), "service/presets"),
        (str(ROOT / "service" / "runtime.json"), "service"),
        (str(ROOT / "service" / "effects.json"), "service"),
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
