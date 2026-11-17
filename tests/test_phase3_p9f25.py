"""P9F25 : le sexe des zombies envoyés suit la tenue, et la tenue vient du registre du jeu.

Registre (`clothing.xml`) → offre de `zombie_outfit` → `zombie_sex` → preset effectif → commande
de pop → mod → sixième argument d'`addZombiesInOutfit`, puis le sexe obtenu en retour.
Chaque test cite la mutation qui le fait échouer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from doubles.outfits import outfit_registry  # noqa: F401
from test_phase3_p9f19 import PRESET, UI_SCRIPT, commands_after_one_kill, outfit_field
from test_rules_load import SpawnGame, read_preset
from test_setup_flow import (  # noqa: F401
    FOLDER_ROUTE,
    game,
    healthy,
    request,
    serve,
    setup,
    zomboid_home,
)

from service import paths, rules
from service import settings as world_settings
from service.engine import Engine
from service.preflight import ABSENT, PRESENT

FEMALE_ONLY = ("StripperPink", "StripperBlack", "StripperNaked")
MALE_ONLY = (
    "Inmate", "PrisonGuard", "Stripper", "CowboyStripper", "PoliceStripper", "FiremanStripper",
)  # fmt: skip
BOTH = ("ShellSuit_Pink", "HazardSuit", "Police", "Nurse", "WaiterStripper")
RANDOM_ONLY = [("random", "Aléatoire (comme aujourd'hui)")]


def offer() -> list[tuple[str, str]]:
    field = outfit_field(world_settings.catalog())
    return [(choice["value"], choice["label"]) for choice in field["choices"]]


def registry_without(source: Path, target: Path, *names: str) -> Path:
    """Copie le registre des tests sans les tenues `names`, dans les deux listes."""
    kept: list[str] = []
    block: list[str] = []
    for line in source.read_text(encoding="utf-8-sig").splitlines(keepends=True):
        block.append(line)
        if line.strip() in ("</m_FemaleOutfits>", "</m_MaleOutfits>", "<outfitManager>"):
            if not any(f"<m_Name>{name}</m_Name>" in "".join(block) for name in names):
                kept += block
            block = []
    target.write_text("".join(kept + block), encoding="utf-8")
    return target


def pop_command(runtime: dict, settings: dict) -> dict:
    (record,) = commands_after_one_kill(runtime, settings)
    return record["data"]["command"]


# -- registre ---------------------------------------------------------------------------------


def test_registry_tells_for_which_sex_an_outfit_exists(outfit_registry):
    """Les deux listes du fichier donnent `female`, `male` ou `both`, et un nom que le jeu n'a
    pas n'y est pas. Mutation : ne lire que `m_FemaleOutfits`, ou rendre `both` pour tout nom."""
    registry = world_settings.read_outfit_registry(outfit_registry)
    assert {registry[name] for name in FEMALE_ONLY} == {"female"}
    assert {registry[name] for name in MALE_ONLY} == {"male"}
    assert {registry[name] for name in BOTH} == {"both"}
    assert "StripperWhite" not in registry
    # Le registre porte plus que la sélection : un nom lu n'est pas pour autant offert.
    assert registry["Naked"] == "female"
    assert "Naked" not in dict(offer())


def test_offer_has_the_real_female_names_and_labels_the_male_ones():
    """`StripperWhite` n'est plus offert ; les noms féminins réels le sont ; une tenue d'un seul
    sexe le dit dans son libellé. Mutation : `StripperWhite` remis dans `OUTFIT_SELECTION`
    (signalé, donc `notice`), ou `ONLY_SUFFIX` vidé."""
    labels = dict(offer())
    assert "StripperWhite" not in labels
    for name in ("StripperPink", "StripperBlack", "StripperNaked", "WaiterStripper"):
        assert name in labels
    for name in FEMALE_ONLY:
        assert labels[name].endswith(" — femmes seulement"), labels[name]
    for name in MALE_ONLY:
        assert labels[name].endswith(" — hommes seulement"), labels[name]
    for name in BOTH:
        assert "seulement" not in labels[name], labels[name]
    assert labels["Stripper"].startswith("Strip-teaseur")
    assert "notice" not in outfit_field(world_settings.catalog())


def test_name_missing_from_the_registry_leaves_the_offer_and_is_reported(tmp_path, outfit_registry):
    """Un nom de la sélection que le registre n'a pas : retiré de l'offre, nommé dans le champ
    servi au lobby, et refusé au départ — jamais envoyé. Mutation : `offered` qui reprend toute
    la sélection, ou `notice` non posé sur le champ."""
    path = registry_without(outfit_registry, tmp_path / "clothing.xml", "HazardSuit", "Inmate")
    report = world_settings.use_outfit_registry(path)
    assert (report["error"], report["withdrawn"]) == (None, ["HazardSuit", "Inmate"])
    names = [name for name, _ in offer()]
    assert "HazardSuit" not in names and "Inmate" not in names
    assert "Police" in names and "StripperPink" in names
    engine = Engine("mock")
    served = outfit_field(engine.settings_catalog(PRESET)["fields"])
    assert "HazardSuit, Inmate" in served["notice"]
    assert "absents du registre" in served["notice"]
    ok, message = engine.action(
        "start", {"preset": PRESET, "settings": {"zombie_outfit": "HazardSuit"}}
    )
    assert not ok and "« zombie_outfit »" in message, message
    assert engine.snapshot()["phase"] == "idle"
    with pytest.raises(world_settings.SettingsRefused, match="zombie_outfit"):
        world_settings.validate({"zombie_outfit": "Inmate"})


@pytest.mark.parametrize("content", [None, "<outfitManager><m_FemaleOutfits>", "<outfitManager/>"])
def test_unreadable_registry_reduces_the_offer_to_random_and_starts(tmp_path, content):
    """Fichier absent, XML coupé ou registre vide : l'offre est `random` seul, le champ le dit,
    et un départ aux réglages par défaut passe. Mutation : `use_outfit_registry` qui laisse
    monter l'exception, ou qui garde la sélection entière faute de registre."""
    path = tmp_path / "clothing.xml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    report = world_settings.use_outfit_registry(path)
    assert report["error"]
    assert report["withdrawn"] == []
    assert offer() == RANDOM_ONLY
    engine = Engine("mock")
    served = outfit_field(engine.settings_catalog(PRESET)["fields"])
    assert "illisible" in served["notice"] and str(path) in served["notice"]
    assert served["defaut"] == "random"
    with pytest.raises(world_settings.SettingsRefused, match="zombie_outfit"):
        world_settings.validate({"zombie_outfit": "Police"})
    settings = world_settings.validate({}, read_preset(PRESET))
    assert (settings["zombie_outfit"], settings["zombie_sex"]) == ("random", "auto")
    try:
        ok, message = engine.action("start", {"preset": PRESET, "settings": {}})
        assert ok, message
    finally:
        engine.action("stop", {})


def test_game_folder_gives_the_registry_path(tmp_path):
    """`paths.clothing_registry` suit `--pz-dir`, puis `PZ_INSTALL_DIR`, puis Steam ; le fichier
    est sous `projectzomboid/` (Linux) ou à la racine du jeu (Windows). Mutation : un seul des
    deux sous-dossiers, ou l'option ignorée au profit de la variable."""
    tail = Path("media", "clothing", "clothing.xml")
    linux, windows, steam = tmp_path / "linux", tmp_path / "windows", tmp_path / "home"
    in_steam = steam / ".steam/steam/steamapps/common/ProjectZomboid/projectzomboid" / tail
    for file in (linux / "projectzomboid" / tail, windows / tail, in_steam):
        file.parent.mkdir(parents=True)
        file.write_text("<outfitManager/>", encoding="utf-8")
    env = {"PZ_INSTALL_DIR": str(windows)}
    found = paths.clothing_registry("linux", env, steam, [])
    assert found == windows / tail
    for argv in (["--pz-dir", str(linux)], [f"--pz-dir={linux}"]):
        assert paths.clothing_registry("linux", env, steam, argv) == linux / "projectzomboid" / tail
    assert paths.clothing_registry("linux", {}, steam, []) == in_steam
    # Un dossier désigné et faux ne se replie pas sur Steam : le chemin rendu dit où il manque.
    missing = paths.clothing_registry("linux", {"PZ_INSTALL_DIR": str(tmp_path / "x")}, steam, [])
    assert missing is not None and not missing.exists() and tmp_path / "x" in missing.parents
    assert paths.clothing_registry("win32", {}, steam, []) is None


# -- réglage du sexe --------------------------------------------------------------------------


def test_catalog_offers_the_sex_setting_auto_by_default():
    """Champ `choice` des Règles, juste après l'uniforme, `auto` par défaut. Mutation : champ
    retiré de `FIELDS`, ou défaut passé à `random`."""
    fields = world_settings.catalog()
    keys = [field["key"] for field in fields]
    assert keys[keys.index("zombie_outfit") + 1] == "zombie_sex"
    field = next(field for field in fields if field["key"] == "zombie_sex")
    assert (field["group"], field["type"], field["defaut"]) == ("regles", "choice", "auto")
    assert [choice["value"] for choice in field["choices"]] == ["auto", "female", "male", "random"]


@pytest.mark.parametrize("outfit", FEMALE_ONLY)
def test_auto_with_a_female_only_outfit_sends_sex_100(runtime, outfit):
    """`auto` et une tenue qui n'existe qu'en féminin : la commande de pop porte `sex = 100`,
    juste après `outfit`. Mutation : `outfit_sex` qui rend `None` en `auto`, `effective_table`
    sans `table["sex"]`, ou `SEX` retiré de `POP_SETTINGS`."""
    command = pop_command(runtime, {"zombie_outfit": outfit})
    assert (command["outfit"], command["sex"]) == (outfit, 100)
    assert type(command["sex"]) is int
    keys = list(command)
    assert keys.index("sex") == keys.index("outfit") + 1 < keys.index("origin")


def test_auto_with_a_male_only_outfit_sends_sex_0(runtime):
    """Une tenue masculine seule donne 0, pas l'absence de clé : zéro est une valeur demandée.
    Mutation : `if sex:` au lieu de `if sex is not None` dans `effective_table`."""
    command = pop_command(runtime, {"zombie_outfit": "CowboyStripper"})
    assert command["sex"] == 0 and type(command["sex"]) is int


@pytest.mark.parametrize("outfit", [*BOTH, "random"])
def test_auto_with_an_outfit_of_both_sexes_sends_no_sex(runtime, outfit):
    """`auto` et une tenue des deux sexes, ou le tirage du jeu : aucune clé `sex`, la commande
    d'avant. Mutation : `auto` qui vaudrait `female`, ou une valeur par défaut écrite."""
    command = pop_command(runtime, {"zombie_outfit": outfit})
    assert command["outfit"] == outfit
    assert "sex" not in command


def test_sprinter_command_carries_the_same_sex(runtime):
    """Le sprinter du lot porte le même sexe que les pops. Mutation : `sex` ajouté aux seules
    commandes `spawn_pop`."""
    command = pop_command(runtime, {"zombie_outfit": "StripperPink", "sprinter_percent": 100})
    assert (command["kind"], command["sex"]) == ("spawn_sprinter", 100)


@pytest.mark.parametrize(
    ("outfit", "sex", "expected"),
    [
        ("Police", "female", 100),
        ("Police", "male", 0),
        ("random", "female", 100),
        ("StripperPink", "female", 100),
        ("Inmate", "male", 0),
        ("Police", "random", None),
        # `random` est le comportement d'avant, gardé pour comparer : rien n'est demandé au jeu.
        ("StripperPink", "random", None),
    ],
)
def test_forced_sex_is_sent_for_an_outfit_that_exists_in_it(runtime, outfit, sex, expected):
    """`female` et `male` s'imposent à une tenue qui existe dans ce sexe ; `random` n'envoie
    rien. Mutation : `outfit_sex` qui ignore le réglage et ne suit que la tenue."""
    command = pop_command(runtime, {"zombie_outfit": outfit, "zombie_sex": sex})
    assert command.get("sex") == expected


@pytest.mark.parametrize(
    ("outfit", "sex", "only"),
    [
        ("Inmate", "female", "que pour les hommes"),
        ("FiremanStripper", "female", "que pour les hommes"),
        ("StripperNaked", "male", "que pour les femmes"),
    ],
)
def test_incoherent_pair_is_refused_at_the_setting_and_names_the_outfit(outfit, sex, only):
    """Un sexe imposé à une tenue de l'autre sexe seul : refus au réglage, avant tout effet, qui
    nomme l'uniforme — pas de repli, pas de tirage. Mutation : contrôle retiré de `validate`
    (le refus ne viendrait plus du réglage), ou repli sur le sexe de la tenue."""
    with pytest.raises(world_settings.SettingsRefused, match=f"« {outfit} »"):
        world_settings.validate({"zombie_outfit": outfit, "zombie_sex": sex})
    engine = Engine("mock")
    ok, message = engine.action(
        "start", {"preset": PRESET, "settings": {"zombie_outfit": outfit, "zombie_sex": sex}}
    )
    assert not ok
    assert "« zombie_sex »" in message and f"« {outfit} »" in message, message
    assert only in message, message
    assert engine.snapshot()["phase"] == "idle"
    assert engine._thread is None


@pytest.mark.parametrize("value", ["Female", "f", "", 100, None, True])
def test_unknown_sex_value_is_refused(value):
    """Égalité exacte, comme tout champ `choice`. Mutation : `zombie_sex` déclaré `int`."""
    with pytest.raises(world_settings.SettingsRefused, match="« zombie_sex »"):
        world_settings.validate({"zombie_sex": value})


def test_settings_from_before_the_sex_field_keep_their_table():
    """Des réglages sans `zombie_sex` (écrits avant P9F25) redonnent la table d'alors : pas de
    `sex`, donc le même hash. Mutation : `_sex_of` qui suppose `auto` quand la clé manque."""
    preset = read_preset(PRESET)
    settings = world_settings.validate({"zombie_outfit": "StripperPink"}, preset)
    assert world_settings.effective_table(preset, settings)["sex"] == 100
    del settings["zombie_sex"]
    assert "sex" not in world_settings.effective_table(preset, settings)


def test_summary_says_the_forced_sex_and_nothing_by_default():
    """Le résumé dit le sexe quand il est demandé au jeu, et reste celui d'avant sinon.
    Mutation : ligne retirée de `_summary_lines`, ou écrite même sans sexe demandé."""
    preset = read_preset(PRESET)
    forced = world_settings.validate({"zombie_outfit": "StripperBlack"}, preset)
    table = world_settings.effective_table(preset, forced)
    assert "Les zombies envoyés sont tous des femmes" in table["summary"]
    assert "zombies : femmes" in world_settings.summary(forced)
    male = world_settings.validate({"zombie_outfit": "Inmate"}, preset)
    assert "zombies : hommes" in world_settings.summary(male)
    plain = world_settings.validate({"zombie_outfit": "Police"}, preset)
    lines = world_settings.effective_table(preset, plain)["summary"]
    assert not any("tous des" in line for line in lines)
    assert "zombies :" not in world_settings.summary(plain)


@pytest.mark.parametrize("value", [True, 101, -1, "100", 50.0, None])
def test_table_refuses_a_sex_that_is_not_a_percentage(value, runtime):
    """La table n'accepte sous `sex` qu'un entier de 0 à 100. Mutation : contrôle retiré de
    `load_table`."""
    raw = {**read_preset(PRESET), "sex": value}
    with pytest.raises(rules.TableError, match="« sex »"):
        rules.load_table(raw, rules.load_effects(), runtime["limits"], file_stem=PRESET)


# -- mod --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["spawn_pop", "spawn_sprinter"])
def test_mod_passes_the_sex_and_reports_only_females(monkeypatch, kind):
    """`sex = 100` arrive en sixième argument sur chaque case, et le résultat compte le sexe
    lu sur les zombies créés : `female = n, male = 0`. Mutation : `spawnAt` qui garde `nil`
    (le tirage donnerait des mâles), ou `sexCount` retiré du résultat."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(kind=kind, n=6, spacing=4, outfit="StripperPink", sex=100)
    assert (data["status"], data["spawned"]) == ("applied", 6)
    assert spawn.sexes == [100] * 6
    assert data["sex"] == {"female": 6, "male": 0}


def test_mod_passes_sex_0_and_reports_only_males(monkeypatch):
    """Zéro est une valeur, pas une absence : `0` arrive au jeu. Mutation : `sexOf` qui teste
    `sex > 0`, ou un `sex or nil` qui confondrait."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(n=5, outfit="Inmate", sex=0)
    assert spawn.sexes == [0]
    assert data["sex"] == {"female": 0, "male": 5}


@pytest.mark.parametrize("settings", [{}, {"sex": "100"}, {"sex": 101}, {"sex": -1}, {"sex": True}])
def test_mod_gives_nil_without_a_sex_and_still_reports(monkeypatch, settings):
    """Un service d'avant P9F25 n'envoie pas `sex` : `nil`, l'appel d'avant, et le résultat
    rapporte quand même le tirage du jeu, mâles et femelles. Mutation : `sexOf` qui rend la
    valeur telle quelle, ou un défaut de 50."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(n=12, **settings)
    assert (data["status"], data["spawned"]) == ("applied", 12)
    assert spawn.sexes == [None]
    assert data["sex"]["female"] + data["sex"]["male"] == 12
    assert data["sex"]["female"] > 0 and data["sex"]["male"] > 0


def test_mod_reports_a_sex_asked_and_not_obtained_without_failing(monkeypatch):
    """Le jeu n'obéit pas : la commande reste `applied`, et le résultat dit ce qui est né, pas
    ce qui était demandé. Mutation : `sex` du résultat recopié de la commande au lieu d'être
    lu sur les zombies."""
    spawn = SpawnGame(monkeypatch)
    spawn.obeys_sex = False
    data = spawn.apply(n=12, outfit="StripperPink", sex=100)
    assert (data["status"], data["spawned"]) == ("applied", 12)
    assert data["sex"]["male"] > 0
    assert data["sex"] == {
        "female": sum(zombie.female for zombie in spawn.zombies),
        "male": sum(not zombie.female for zombie in spawn.zombies),
    }


def test_mod_counts_nothing_for_a_zombie_that_does_not_answer(monkeypatch):
    """`isFemale` absent d'une version du jeu : les zombies naissent, aucun n'est compté, et
    l'écart avec `spawned` le dit. Mutation : appel hors `pcall` (la commande échouerait)."""
    spawn = SpawnGame(monkeypatch, missing=("isFemale",))
    data = spawn.apply(n=3, sex=100)
    assert (data["status"], data["spawned"]) == ("applied", 3)
    assert data["sex"] == {"female": 0, "male": 0}


# -- dossier du jeu retenu par le préflight ----------------------------------------------------


def game_registry(game_dir: Path, source: Path, *without: str) -> Path:
    """Pose dans `game_dir` le registre des tests, sans les tenues `without`."""
    target = paths.game_clothing_registry(game_dir)
    target.parent.mkdir(parents=True)
    return registry_without(source, target, *without)


def served_outfits(port: int) -> dict:
    status, answer = request(port, "/api/settings")
    assert status == 200
    return outfit_field(answer["fields"])


def test_registry_of_an_accepted_game_folder_is_read_once(tmp_path, outfit_registry, monkeypatch):
    """`follow_game_dir` lit `media/clothing/clothing.xml` du dossier donné, ne relit pas le
    registre déjà en place, et réessaie tant que la lecture a échoué. Mutation : `follow_game_dir`
    qui ne compare rien (relecture à chaque préflight), ou qui retient une lecture en échec."""
    reads: list[str] = []
    read = world_settings.read_outfit_registry
    monkeypatch.setattr(
        world_settings, "read_outfit_registry", lambda path: reads.append(str(path)) or read(path)
    )
    folder = tmp_path / "jeu"
    report = world_settings.follow_game_dir(folder)
    assert report["error"] and offer() == RANDOM_ONLY
    path = game_registry(folder, outfit_registry)
    report = world_settings.follow_game_dir(folder)
    assert (report["error"], report["path"], report["notice"]) == (None, str(path), "")
    assert "Police" in dict(offer())
    assert world_settings.follow_game_dir(folder) == report
    assert reads == [str(path)] * 2
    assert world_settings.outfit_notice() == ""


def test_folder_chosen_in_the_lobby_brings_its_outfits(
    healthy, serve, game, tmp_path, outfit_registry
):
    """Revue A1. Jeu hors des dossiers connus : l'offre de l'import est `random` seul. Le dossier
    donné à `/api/setup/pz-dir` fait relire SON registre, dès la réponse, sans relancer. Mutation :
    `set_pz_dir` qui ne suit plus le registre — l'offre reste `random` jusqu'au prochain contrôle."""
    game_registry(game, outfit_registry, "HazardSuit")
    assert world_settings.use_outfit_registry(tmp_path / "absent.xml")["error"]
    _, port = serve(zomboid_dir=healthy, pz_dir=game.parent / "absent")
    assert setup(port)["pz"] == ABSENT
    assert [choice["value"] for choice in served_outfits(port)["choices"]] == ["random"]

    status, answer = request(port, FOLDER_ROUTE, {"folder": str(game)})

    assert status == 200 and answer["ok"] is True
    served = served_outfits(port)
    names = [choice["value"] for choice in served["choices"]]
    assert "Police" in names and "StripperPink" in names and "HazardSuit" not in names
    assert "HazardSuit" in served["notice"] and "illisible" not in served["notice"]
    assert world_settings.validate({"zombie_outfit": "Police"})["zombie_outfit"] == "Police"


def test_preflight_follows_the_game_folder_it_accepted(
    healthy, serve, game, tmp_path, outfit_registry
):
    """Revue A1. Le dossier que le préflight retient est celui dont on lit les tenues, d'où qu'il
    vienne (registre Windows, bibliothèque Steam secondaire, option du moteur). Mutation : `_measure`
    qui ne suit plus le registre."""
    game_registry(game, outfit_registry)
    assert world_settings.use_outfit_registry(tmp_path / "absent.xml")["error"]
    _, port = serve(zomboid_dir=healthy, pz_dir=game)

    state = setup(port)

    assert state["pz"] == PRESENT and state["ok"] is True
    assert state["notices"] == []
    served = served_outfits(port)
    assert "notice" not in served and len(served["choices"]) > 1


def test_setup_reports_withdrawn_outfits_and_an_unreadable_registry(
    healthy, serve, game, outfit_registry
):
    """Revue A2. L'état de préparation porte l'avis du registre (`notices`), le même texte que le
    champ, et l'engine le journalise ; il n'empêche pas de lancer. Mutation : `_setup_view` sans
    `notices`, ou un registre illisible compté comme défaut (`ok` faux)."""
    engine, port = serve(zomboid_dir=healthy, pz_dir=game)

    state = setup(port)

    registry = paths.game_clothing_registry(game.resolve())
    assert state["ok"] is True and state["defects"] == []
    (notice,) = state["notices"]
    assert "illisible" in notice and str(registry) in notice
    assert served_outfits(port)["notice"] == notice
    assert [entry["msg"] for entry in engine.snapshot()["logs"]].count(notice) == 1

    game_registry(game, outfit_registry, "HazardSuit", "Inmate")
    state = setup(port)
    (notice,) = state["notices"]
    assert state["ok"] is True
    assert "HazardSuit, Inmate" in notice and "absents du registre" in notice


def test_page_shows_the_setup_notices_and_refreshes_the_catalogue():
    """Test statique du source. `setupCard` affiche `setup.notices`, échappés ; chaque contrôle
    mesuré recharge le catalogue, sinon l'offre affichée resterait celle de l'import ou d'avant le
    choix du dossier. Mutation : l'une des deux lignes retirée."""
    script = UI_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    card = script[script.index("function setupCard()") :]
    card = card[: card.index("\n}\n")]
    assert (
        "(setup.notices || []).map((n) => `<p class=\"notice setup-notice\">${esc(n)}</p>`)" in card
    )
    refresh = script[script.index("async function refreshSetup()") :]
    refresh = refresh[: refresh.index("\n}\n")]
    reload = "if (setup && setup.measured) await loadCatalog(draft ? draft.preset : null);"
    assert refresh.index("await api('/api/setup')") < refresh.index(reload) < refresh.index("render();")
    action = script[script.index("async function setupAction(") :]
    assert "await refreshSetup();" in action[: action.index("\n}\n")]


# -- page -------------------------------------------------------------------------------------


def test_page_shows_the_notice_of_a_field():
    """Test statique du source : `fieldHint` affiche `f.notice`, échappé, après l'aide.
    Mutation : ligne retirée — le retrait d'un uniforme ne se verrait plus dans le lobby."""
    script = UI_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = script[script.index("function fieldHint(f)") :]
    body = body[: body.index("\n}\n")]
    assert "f.notice ? `<small class=\"help notice\">${esc(f.notice)}</small>` : ''" in body
    assert body.rstrip().endswith("+ notice;")
