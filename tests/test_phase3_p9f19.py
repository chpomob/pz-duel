"""P9F19 : le type de zombie envoyé se choisit dans les Règles, « aléatoire » par défaut.

Le choix traverse catalogue → preset effectif → commande de pop → mod → `addZombiesInOutfit`.
Chaque test cite la mutation qui le fait échouer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_rules_load import T0_MS, SpawnGame, read_preset

from service import rules
from service import settings as world_settings
from service.engine import Engine
from service.protocol import parse_event

PRESET = "duel_rapide"
UI_SCRIPT = Path(__file__).resolve().parent.parent / "ui" / "app.js"
# La sélection retenue avec le propriétaire, dans l'ordre d'affichage.
OUTFITS = [
    ("random", "Aléatoire (comme aujourd'hui)"),
    ("ShellSuit_Pink", "Combinaison rose"),
    ("ShellSuit_Blue", "Combinaison bleue"),
    ("ShellSuit_Green", "Combinaison verte"),
    ("ShellSuit_Teal", "Combinaison turquoise"),
    ("ShellSuit_Black", "Combinaison noire"),
    ("HazardSuit", "Combinaison hazmat (jaune fluo)"),
    ("Inmate", "Détenu (orange)"),
    ("Fireman", "Pompier"),
    ("Police", "Policier"),
    ("Doctor", "Médecin"),
    ("Nurse", "Infirmière"),
    ("PrisonGuard", "Gardien de prison"),
    ("StripperWhite", "Strip-teaseuse, tenue blanche"),
    ("StripperBlack", "Strip-teaseuse, tenue noire"),
    ("CowboyStripper", "Strip-teaseuse cow-girl"),
    ("PoliceStripper", "Strip-teaseuse policière"),
    ("FiremanStripper", "Strip-teaseuse pompière"),
    ("WaiterStripper", "Strip-teaseuse serveuse"),
    ("StripperNaked", "Strip-teaseuse nue — déconseillé en stream"),
]


def outfit_field(fields: list[dict]) -> dict:
    return next(field for field in fields if field["key"] == "zombie_outfit")


def commands_after_one_kill(runtime: dict, settings: dict | None) -> list[dict]:
    """Les enregistrements `command` du journal après un kill de A, réglages appliqués ou non."""
    preset = read_preset(PRESET)
    raw = preset
    if settings is not None:
        raw = world_settings.effective_table(preset, world_settings.validate(settings, preset))
    catalog = rules.load_effects()
    table = rules.load_table(raw, catalog, runtime["limits"], file_stem=PRESET)
    core = rules.MatchCore(table, runtime, catalog, T0_MS, raw["duration_s"])
    kill = {
        "v": 1, "boot": 1, "seq": 1, "t_ms": T0_MS + 1000, "src": "A", "side": "A",
        "kind": "zombie_kill", "data": {"origin": "world"},
    }  # fmt: skip
    records = core.advance(T0_MS + 750)
    core.submit_event(parse_event(kill, catalog))
    records += core.advance(T0_MS + 1000)
    return [record for record in records if record["type"] == "command"]


# -- catalogue -------------------------------------------------------------------------------


def test_catalog_offers_the_outfit_choice_random_by_default():
    """Un champ `choice` des Règles, juste après `zombies_par_kill`, `random` par défaut, et la
    sélection retenue, valeurs en chaînes. Mutation : champ retiré de `FIELDS`, défaut changé,
    ou une tenue ôtée de `OUTFITS`."""
    fields = world_settings.catalog()
    keys = [field["key"] for field in fields]
    assert keys[keys.index("zombies_par_kill") + 1] == "zombie_outfit"
    field = outfit_field(fields)
    assert (field["group"], field["type"], field["defaut"]) == ("regles", "choice", "random")
    assert [(c["value"], c["label"]) for c in field["choices"]] == OUTFITS
    assert all(type(c["value"]) is str for c in field["choices"])
    assert field["help"] == "L'uniforme des zombies envoyés. Aléatoire garde le tirage du jeu."
    # La route de la page sert le même champ, quel que soit le preset : aucun n'a d'uniforme.
    for preset in ("duel_rapide", "demo"):
        served = outfit_field(Engine("mock").settings_catalog(preset)["fields"])
        assert served == field


def test_numeric_choices_are_still_checked_as_numbers():
    """Les choix numériques d'avant ne deviennent pas textuels. Mutation : valider tout champ à
    `choices` par la branche `choice` accepterait `"3"` ou refuserait `3`."""
    assert world_settings.validate({"Zombies": 4})["Zombies"] == 4
    with pytest.raises(world_settings.SettingsRefused, match="Zombies"):
        world_settings.validate({"Zombies": "4"})
    with pytest.raises(world_settings.SettingsRefused, match="borne max 6"):
        world_settings.validate({"Zombies": 7})


# -- refus -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["Clown", "police", "POLICE", " Police", "Police ", "", "Random", 3, None, ["Police"]]
)
def test_unknown_outfit_refuses_the_start_and_names_the_key_and_the_list(value):
    """Égalité exacte ou refus avant le lancement : ni casse corrigée, ni valeur approchante.
    Le message nomme la clé et toute la liste acceptée. Mutation : `_check` qui laisse passer
    une chaîne quelconque, qui compare sans la casse, ou qui écrit « valeur hors des choix »."""
    engine = Engine("mock")
    ok, message = engine.action(
        "start", {"preset": PRESET, "settings": {"zombie_outfit": value}}
    )
    assert not ok
    assert "« zombie_outfit »" in message, message
    assert ", ".join(name for name, _ in OUTFITS) in message, message
    assert engine.snapshot()["phase"] == "idle"
    assert engine._thread is None


@pytest.mark.parametrize("value", ["Bad Name", "a-b", "", "x" * 65, 3, None, True])
def test_table_refuses_an_outfit_that_is_not_a_name(value, runtime):
    """Le chargeur de table contrôle la forme du nom qui ira dans la commande. Mutation :
    `_check_outfit` retiré de `load_table`."""
    raw = {**read_preset(PRESET), "outfit": value}
    with pytest.raises(rules.TableError, match="« outfit »"):
        rules.load_table(raw, rules.load_effects(), runtime["limits"], file_stem=PRESET)


# -- arbitre → commande → journal ------------------------------------------------------------


def test_chosen_outfit_is_written_in_the_played_preset_and_the_journal():
    """Le départ inscrit le choix dans le preset effectif et dans les réglages journalisés ;
    le fichier du preset ne bouge pas. Mutation : `effective_table` sans `table["outfit"]`."""
    engine = Engine("mock")
    ok, message = engine.action(
        "start", {"preset": PRESET, "settings": {"zombie_outfit": "HazardSuit"}}
    )
    assert ok, message
    engine._stop.set()
    assert engine._table["outfit"] == "HazardSuit"
    assert engine.snapshot()["config"]["settings"]["zombie_outfit"] == "HazardSuit"
    assert "uniforme : Combinaison hazmat" in engine.snapshot()["config"]["settings_summary"]
    assert "outfit" not in read_preset(PRESET)


def test_chosen_outfit_reaches_the_pop_command_in_the_journal(runtime):
    """Un kill, dix zombies : l'enregistrement `command` du journal porte `outfit`, avant
    `origin`. Mutation : `OUTFIT` retiré de `POP_SETTINGS`."""
    (record,) = commands_after_one_kill(runtime, {"zombie_outfit": "ShellSuit_Pink"})
    command = record["data"]["command"]
    assert (command["kind"], command["n"]) == ("spawn_pop", 10)
    assert command["outfit"] == "ShellSuit_Pink"
    assert list(command)[-1] == "origin"
    # Le journal est du JSON : le nom s'y relit tel quel.
    assert json.loads(json.dumps(record))["data"]["command"]["outfit"] == "ShellSuit_Pink"


def test_sprinter_command_carries_the_same_outfit(runtime):
    """Le sprinter est rapide, pas habillé autrement : à 100 %, la commande `spawn_sprinter`
    porte le même uniforme. Mutation : `outfit` ajouté aux seules commandes `spawn_pop`."""
    settings = {"zombie_outfit": "Inmate", "sprinter_percent": 100}
    (record,) = commands_after_one_kill(runtime, settings)
    command = record["data"]["command"]
    assert (command["kind"], command["outfit"]) == ("spawn_sprinter", "Inmate")


def test_default_settings_journal_random_and_old_tables_nothing(runtime):
    """Départ réglé sans y toucher : la commande dit `random`. Preset brut (journal d'avant
    P9F19) : aucune clé `outfit`, la commande est celle d'avant. Mutation : une valeur par
    défaut écrite d'office dans toute commande de pop."""
    (settled,) = commands_after_one_kill(runtime, {})
    assert settled["data"]["command"]["outfit"] == "random"
    (plain,) = commands_after_one_kill(runtime, None)
    assert "outfit" not in plain["data"]["command"]


def test_other_effects_never_carry_the_outfit(runtime):
    """Une caisse de soin n'a pas d'uniforme. Mutation : `outfit` ajouté à toute commande."""
    preset = read_preset(PRESET)
    settings = world_settings.validate({"zombie_outfit": "Police"}, preset)
    raw = world_settings.effective_table(preset, settings)
    catalog = rules.load_effects()
    table = rules.load_table(raw, catalog, runtime["limits"], file_stem=PRESET)
    core = rules.MatchCore(table, runtime, catalog, T0_MS, raw["duration_s"])
    hurt = {
        "v": 1, "boot": 1, "seq": 1, "t_ms": T0_MS + 1000, "src": "A", "side": "A",
        "kind": "damage_taken", "data": {"hp": 80, "delta": -20},
    }  # fmt: skip
    core.submit_event(parse_event(hurt, catalog))
    commands = [r["data"]["command"] for r in core.advance(T0_MS + 2000) if r["type"] == "command"]
    assert [c["kind"] for c in commands] == ["supply_drop"]
    assert "outfit" not in commands[0]


# -- mod -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["spawn_pop", "spawn_sprinter"])
def test_mod_passes_the_outfit_to_add_zombies(monkeypatch, kind):
    """Le nom arrive tel quel en cinquième argument d'`addZombiesInOutfit`, sur chaque case du
    lot. Mutation : `spawnAt` qui garde `nil`, ou `spawnGroup` qui ne lit pas `cmd.outfit`."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(kind=kind, n=3, spacing=4, outfit="ShellSuit_Blue")
    assert (data["status"], data["spawned"]) == ("applied", 3)
    assert spawn.outfits == ["ShellSuit_Blue"] * 3


@pytest.mark.parametrize("settings", [{"outfit": "random"}, {}, {"outfit": ""}, {"outfit": 7}])
def test_mod_gives_nil_for_random_or_a_missing_key(monkeypatch, settings):
    """`random`, une clé absente (journal rejoué) ou une valeur qui n'est pas un nom : `nil`,
    le tirage du jeu, comme avant le réglage. Mutation : `outfitOf` qui rend `cmd.outfit` tel
    quel passerait la chaîne "random" au jeu."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(n=2, **settings)
    assert (data["status"], data["spawned"]) == ("applied", 2)
    assert spawn.outfits == [None]
    assert spawn.spawns[0][2] == 2


def test_mod_source_calls_the_game_with_the_outfit_and_no_skin():
    """Lecture du source : un seul appel, `outfit` en cinquième, `nil` en sixième."""
    lua = Path(rules.__file__).resolve().parent.parent
    lua = lua / "mods" / "duelcore" / "42.21" / "media" / "lua" / "server" / "DuelCore.lua"
    source = lua.read_text(encoding="utf-8")
    calls = [line.strip() for line in source.splitlines() if "addZombiesInOutfit(" in line]
    assert calls == [
        "local zombies = addZombiesInOutfit(square:getX(), square:getY(), 0, count, outfit, nil)"
    ]


# -- page ------------------------------------------------------------------------------------


def test_page_sends_a_text_choice_without_number():
    """Test statique du source : dans `bindSettings`, un champ `choice` part tel quel, et la
    branche vient avant celle des choix numériques. Mutation : ligne `choice` retirée (la
    valeur passerait par `Number()` et deviendrait `NaN`), ou placée après `f.choices`."""
    script = UI_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = script[script.index("function bindSettings()") :]
    body = body[: body.index("\n}\n")]
    lines = [line.strip() for line in body.splitlines()]
    choice = [line for line in lines if "f.type === 'choice'" in line]
    assert choice == [
        "else if (f.type === 'choice') el.onchange = (e) => "
        "{ draft.settings[f.key] = e.target.value; };"
    ]
    assert "Number(" not in choice[0]
    numeric = next(line for line in lines if line.startswith("else if (f.choices)"))
    assert "Number(e.target.value)" in numeric
    assert lines.index(choice[0]) < lines.index(numeric)
