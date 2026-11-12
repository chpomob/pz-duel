"""P9F15 : réglages du monde et des règles avant le départ, catalogue servi par le service.

Aucun vrai serveur : le moteur réel tourne sur les doubles de `test_snapshots`, et le sandbox
et le preset effectifs se lisent sur les fichiers écrits et dans le journal.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import test_snapshots
from doubles.fake_mod import FakeMod
from test_contract import (  # noqa: F401
    BUILD,
    HOOK,
    PLAYERS,
    SIDES,
    FrozenClock,
    Lobby,
    SteppedWall,
    zomboid_home,
)
from test_snapshots import (  # noqa: F401
    PLAYER_PASSWORD,
    engine_rig,
    make_world,
    world_db,
    world_dir,
    worlds_state,
)
from service import engine as engine_module
from service import settings as world_settings
from service.api import make_server
from service.engine import Engine
from service.servers import read_sandbox, write_sandbox

PRESET = "duel_rapide"
SPEC_DEFAULTS = {
    "Zombies": 3,
    "ZombieLore.Sight": 2,
    "ZombieLore.Hearing": 2,
    "ZombieLore.Strength": 2,
    "ZombieLore.Toughness": 2,
    "ZombieLore.Cognition": 3,
    "ZombieLore.Memory": 2,
    "ZombieLore.Transmission": 1,
    "StartTime": 2,
    "StartMonth": 7,
    "DayLength": 7,
    "monde_neuf": True,
    "zombies_par_kill": 10,
    "sprinter_percent": 1,
    "pop_distance": [12, 20],
    "aggro": 0.6,
    "spacing": 4,
    "warn_s": 6,
    "soin_sur_blessure": True,
    "duration_s": 1500,
}
SPEC_SUMMARY = (
    "Élevé (3) · vision 2 · ouïe 2 · zombies normaux · 10 z par kill · 1 % sprinter "
    "· pops à 12–20 cases · monde neuf · 25 min"
)
LOCKED = ("ZombieLore.Speed", "ZombieLore.SprinterPercentage")
# Le `SandboxVars` du double, avec les clés que PZ 42 écrit : `Speed` et `SprinterPercentage`
# y sont faux exprès, et `MultiplierConfig.Strength` est le voisin à ne pas confondre.
FULL_SANDBOX = (
    r"DayLength = 4,\nStartYear = 3,\nStartMonth = 1,\nStartDay = 2,\nStartTime = 5,\n"
    r"Zombies = 4,\nZombieLore = {\n    -- Speed = 2, commentaire\n    Speed = 1,\n"
    r"    SprinterPercentage = 30,\n    Strength = 2,\n    Toughness = 4,\n"
    r"    Transmission = 1,\n    Cognition = 3,\n    Memory = 2,\n    Sight = 2,\n"
    r"    Hearing = 2,\n},\nZombieConfig = {\n    PopulationMultiplier = 0.65,\n"
    r"    PopulationStartMultiplier = 1.0,\n},\nMultiplierConfig = {\n    Strength = 1.0,\n},\n"
)


def sandbox_text() -> str:
    return FULL_SANDBOX.replace(r"\n", "\n")


# -- catalogue -------------------------------------------------------------------------------


def test_catalog_defaults_are_the_owner_values():
    """Mutation : `ZombieLore.Toughness` remis à 4 (aléatoire), ou `monde_neuf` à faux, échoue.

    `StartTime` est l'indice d'option de PZ : 2 vaut 9 h, l'heure demandée.
    """
    fields = world_settings.catalog()
    assert [field["key"] for field in fields] == list(SPEC_DEFAULTS)
    assert {field["key"]: field["defaut"] for field in fields} == SPEC_DEFAULTS
    assert world_settings.START_HOURS[SPEC_DEFAULTS["StartTime"]] == 9
    for field in fields:
        assert set(field) >= {"key", "label", "group", "type", "min", "max", "defaut", "unit",
                              "help"}
        assert field["group"] in ("monde", "regles")
    # Le preset livré donne les mêmes défauts : il est la source des règles.
    preset = Engine("mock").preset(PRESET)
    assert world_settings.defaults(preset) == SPEC_DEFAULTS


def test_catalog_is_served_by_the_api():
    """La page ne connaît aucun champ : elle lit `GET /api/settings`. Mutation : route retirée."""
    engine = Engine("mock")
    server = make_server(engine)
    port = server.server_address[1]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings") as response:
            body = json.load(response)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings?preset=demo") as res:
            demo = json.load(res)
    finally:
        server.shutdown()
        server.server_close()
    assert body["preset"] == PRESET
    assert [g["id"] for g in body["groups"]] == ["monde", "regles"]
    assert {f["key"]: f["defaut"] for f in body["fields"]} == SPEC_DEFAULTS
    assert body["summary"] == SPEC_SUMMARY
    # Les défauts des règles suivent le preset choisi : `demo` envoie 2 zombies par kill.
    assert {f["key"]: f["defaut"] for f in demo["fields"]}["zombies_par_kill"] == 2


def test_page_names_no_setting():
    """Aucun nombre ni aucune clé de réglage dans la page. Mutation : l'ancien sélecteur de durée."""
    page = (Path(__file__).parent.parent / "ui" / "app.js").read_text(encoding="utf-8")
    # `cfg.duration_s` affiche la durée du match en cours, lue dans l'état : elle n'est pas réglée.
    for key in [key for key in SPEC_DEFAULTS if key != "duration_s"]:
        assert key not in page, key
    assert "draft.duration_s" not in page and "day_length" not in page
    assert "1500" not in page and "[300, 600" not in page
    assert "<details" not in page.split("function settingsPanel")[1].split("\nfunction ")[0]


# -- refus -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "named"),
    [
        ({"zombies_par_kill": 31}, ["zombies_par_kill", "borne max 30", "31"]),
        ({"sprinter_percent": -1}, ["sprinter_percent", "borne min 0", "-1"]),
        ({"aggro": 1.5}, ["aggro", "borne max 1", "1.5"]),
        ({"pop_distance": [20, 12]}, ["pop_distance", "[20, 12]"]),
        ({"pop_distance": [5, 12]}, ["pop_distance", "borne min 10", "[5, 12]"]),
        ({"duration_s": 299}, ["duration_s", "borne min 300", "299"]),
        ({"DayLength": 8}, ["DayLength", "borne max 7", "8"]),
        ({"StartTime": 0}, ["StartTime", "borne min 1", "0"]),
        ({"monde_neuf": "oui"}, ["monde_neuf", "'oui'"]),
        ({"warn_s": 6.5}, ["warn_s", "entier", "6.5"]),
        ({"inconnu": 1}, ["inconnu"]),
        # Un entier de 400 chiffres déborderait `float` : il se refuse par ses bornes.
        ({"zombies_par_kill": 10**400}, ["zombies_par_kill", "borne max 30"]),
        ({"aggro": -(10**400)}, ["aggro", "borne min 0"]),
        ({"pop_distance": [12, 10**400]}, ["pop_distance", "borne max"]),
    ],
)
def test_out_of_bounds_refuses_the_start_and_names_the_key(settings, named):
    """Rien ne part et rien ne s'écrête. Mutation : `_bounded` qui ramène à la borne démarre."""
    engine = Engine("mock")
    ok, message = engine.action("start", {"preset": PRESET, "settings": settings})
    assert not ok
    for part in named:
        assert part in message, message
    assert engine.snapshot()["phase"] == "idle"
    assert engine._thread is None


def test_settings_must_be_an_object_at_the_api():
    """`settings` est classé champ objet à la frontière. Mutation : `OBJECT_FIELDS` vidé."""
    engine = Engine("mock")
    server = make_server(engine)
    port = server.server_address[1]
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    body = json.dumps({"action": "start", "preset": PRESET, "settings": [1]}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/action",
        body,
        {"Content-Type": "application/json"},
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request)
    finally:
        server.shutdown()
        server.server_close()
    assert refused.value.code == 400
    assert json.load(refused.value)["error"] == "settings refusé : objet JSON attendu"
    assert engine.snapshot()["phase"] == "idle"


def test_refused_settings_touch_no_world(engine_rig):
    """Le refus précède l'effacement du monde neuf. Mutation : contrôle déplacé dans le fil."""
    engine = engine_rig.build()
    for side in SIDES:
        make_world(engine_rig.zomboid, side)
    before = worlds_state(engine_rig.zomboid)
    ok, message = engine.action(
        "start", {"preset": PRESET, "settings": {"monde_neuf": True, "zombies_par_kill": 0}}
    )
    assert not ok and "zombies_par_kill" in message
    assert worlds_state(engine_rig.zomboid) == before
    assert not engine_rig.launches.exists()


# -- verrous ---------------------------------------------------------------------------------


def test_speed_and_sprinter_percentage_are_never_settable(tmp_path):
    """`Speed = 4` et `SprinterPercentage = 0` hors catalogue, refusés, écrits fixes.

    Mutation : exposer `ZombieLore.SprinterPercentage` au catalogue, ou retirer `LOCKED_SANDBOX`
    de `sandbox_values` : le sandbox garderait 30 % de sprinters du jeu en plus de ceux du mod.
    """
    keys = {field["key"] for field in world_settings.catalog()}
    for key in LOCKED:
        assert key not in keys
        assert key.split(".")[1] not in {k.split(".")[-1] for k in keys}
        with pytest.raises(world_settings.SettingsRefused, match="inconnu"):
            world_settings.validate({key: 50})
    values = world_settings.sandbox_values(world_settings.defaults())
    assert {key: values[key] for key in LOCKED} == {LOCKED[0]: 4, LOCKED[1]: 0}

    path = tmp_path / "duelA_SandboxVars.lua"
    path.write_text(sandbox_text(), encoding="utf-8")
    write_sandbox(path, 7, values)
    found = read_sandbox(path, [*LOCKED, "MultiplierConfig.Strength", "ZombieLore.Strength"])
    assert found == {
        LOCKED[0]: "4",
        LOCKED[1]: "0",
        "MultiplierConfig.Strength": "1.0",
        "ZombieLore.Strength": "2",
    }
    assert "-- Speed = 2, commentaire" in path.read_text(encoding="utf-8")


def test_dotted_key_is_written_inside_its_block_only(tmp_path):
    """`Strength` existe sous `ZombieLore` et `MultiplierConfig`. Mutation : écrire partout."""
    path = tmp_path / "duelA_SandboxVars.lua"
    path.write_text(sandbox_text(), encoding="utf-8")
    write_sandbox(path, 7, {"ZombieLore.Strength": 1})
    found = read_sandbox(path, ["ZombieLore.Strength", "MultiplierConfig.Strength"])
    assert found == {"ZombieLore.Strength": "1", "MultiplierConfig.Strength": "1.0"}


# -- preset effectif -------------------------------------------------------------------------


def test_effective_preset_carries_the_requested_values():
    """Le preset joué porte `n` et `sprinter_percent` demandés ; le fichier reste tel quel.

    Mutation : `effective_table` qui oublie `effect["n"]` laisse 10 zombies par kill.
    """
    path = Path(engine_module.PRESETS_DIR) / f"{PRESET}.json"
    before = path.read_bytes()
    engine = Engine("mock")
    settings = {"zombies_par_kill": 25, "sprinter_percent": 5, "pop_distance": [15, 30],
                "soin_sur_blessure": False, "monde_neuf": False}
    ok, message = engine.action("start", {"preset": PRESET, "settings": settings})
    assert ok, message
    engine._stop.set()
    table = engine._table
    kill = next(rule for rule in table["rules"] if rule["id"] == "kill_baseline")
    assert kill["effects"][0]["n"] == 25
    assert kill["effects"][0]["distance"] == [15, 30]
    assert table["sprinter_percent"] == 5
    assert "hurt_supply" not in [rule["id"] for rule in table["rules"]]
    assert path.read_bytes() == before


def test_summary_matches_the_settings():
    """Le résumé suit les réglages. Mutation : un résumé tiré des défauts reste à 10 z par kill."""
    assert world_settings.summary(world_settings.defaults()) == SPEC_SUMMARY
    engine = Engine("mock")
    assert engine.settings_catalog()["summary"] == SPEC_SUMMARY
    settings = {"Zombies": 4, "ZombieLore.Toughness": 3, "zombies_par_kill": 20,
                "sprinter_percent": 0, "pop_distance": [10, 40], "monde_neuf": False,
                "duration_s": 600, "ZombieLore.Sight": 1}
    ok, message = engine.action("start", {"preset": PRESET, "settings": settings})
    assert ok, message
    engine._stop.set()
    assert engine.snapshot()["config"]["settings_summary"] == (
        "Normal (4) · vision 1 · ouïe 2 · zombies fragiles · 20 z par kill · 0 % sprinter "
        "· pops à 10–40 cases · monde gardé · 10 min"
    )


# -- départ réel sur les doubles -------------------------------------------------------------


class SettledLobby(Lobby):
    """`Lobby`, mais le départ porte des réglages : seule la requête `start` change."""

    def __init__(self, rig, settings: dict) -> None:  # noqa: D107
        self.rig = rig
        self.clock = FrozenClock()
        self.wall = SteppedWall(self.clock)
        self.engine = rig.build()
        self.engine._clock = self.wall
        self.engine._monotonic = self.clock
        catalog = self.engine.effects
        data = {
            "build": BUILD,
            "mod": "0.1.0",
            "protocol": 1,
            "hooks": {HOOK: True},
            "functions": {},
            "atoms": list(catalog["atoms"]),
            "effects": list(catalog["effects"]),
        }
        self.mods = {side: FakeMod(rig.zomboid / "Lua", side) for side in SIDES}
        for side, mod in self.mods.items():
            (rig.caps_dir / side).write_bytes(mod.line("caps", data))
            stale = mod.line("caps", {**data, "mod": f"creation pass {side}"})
            (rig.caps_dir / f"stale_{side}").write_bytes(stale)
        payload = {"preset": PRESET, "password": PLAYER_PASSWORD, "settings": settings}
        ok, message = self.engine.action("start", payload)
        assert ok, message
        state = rig.wait("lobby", "error")
        assert state["phase"] == "lobby", state["error"]
        self.match_id = state["match_id"]


@pytest.fixture
def full_sandbox(engine_rig, monkeypatch):
    """Le double crée un `SandboxVars` qui porte les clés réglées, comme PZ 42."""
    monkeypatch.setattr(test_snapshots, "ENGINE_SANDBOX", FULL_SANDBOX)
    engine_rig.sandbox_file.write_text(sandbox_text(), encoding="utf-8")
    for side in SIDES:
        path = engine_rig.zomboid / "Server" / f"duel{side}_SandboxVars.lua"
        path.write_text(sandbox_text(), encoding="utf-8")
    return engine_rig


def sandbox_of(rig, side: str) -> str:
    return (rig.zomboid / "Server" / f"duel{side}_SandboxVars.lua").read_text(encoding="utf-8")


def test_new_world_erases_then_journals(full_sandbox):
    """`monde_neuf` efface mondes, bases et snapshots avant les serveurs, et le journalise.

    Mutation : `prepare` sans `_reset_worlds` restaure l'ancien monde et n'écrit pas
    `world_reset` ; sans les snapshots effacés, la restauration rendrait aussi l'ancien monde.
    """
    rig = full_sandbox
    for side in SIDES:
        make_world(rig.zomboid, side, b"old match")
    snapshots = rig.zomboid / "duel-snapshots"
    for side in SIDES:
        (snapshots / f"duel{side}" / "world").mkdir(parents=True)
        (snapshots / f"duel{side}" / "world" / "chunk.bin").write_bytes(b"old snapshot")
        (snapshots / f"duel{side}" / f"duel{side}.db").write_bytes(b"old db")

    lobby = SettledLobby(rig, {"monde_neuf": True, "zombies_par_kill": 12})

    records = lobby.rig.journal(lobby.match_id)
    reset = next(r for r in records if r["type"] == "world_reset")["data"]
    removed = {Path(item["path"]).relative_to(rig.zomboid).as_posix(): item
               for item in reset["removed"]}
    for side in SIDES:
        assert removed[f"Saves/Multiplayer/duel{side}"]["existed"]
        assert removed[f"Saves/Multiplayer/duel{side}"]["bytes"] > 0
        assert removed[f"db/duel{side}.db"]["existed"]
        assert removed[f"duel-snapshots/duel{side}"]["existed"]
        # La passe de création a refait chaque monde : l'ancien a disparu.
        chunk = world_dir(rig.zomboid, side) / "map" / "chunk.bin"
        assert chunk.read_bytes().strip() == b"created"
        assert world_db(rig.zomboid, side).read_bytes().strip() == b"created"
    assert reset["bytes"] == sum(item["bytes"] for item in reset["removed"])
    logs = [line["msg"] for line in lobby.state()["logs"]]
    assert any(msg.startswith("monde neuf : ") and "effacé" in msg for msg in logs)

    start = next(r for r in records if r["type"] == "match_start")["data"]
    assert start["settings"]["zombies_par_kill"] == 12
    assert start["settings"]["monde_neuf"] is True
    kill = next(rule for rule in start["preset"]["rules"] if rule["id"] == "kill_baseline")
    assert kill["effects"][0]["n"] == 12
    # Le sandbox relu par le contrat porte les réglages et les verrous.
    for side in SIDES:
        text = sandbox_of(rig, side)
        assert "    Speed = 4,\n" in text and "    SprinterPercentage = 0,\n" in text
        assert "    Toughness = 2,\n" in text and "\nZombies = 3,\n" in text
        assert "    PopulationMultiplier = 1.2,\n" in text
        assert "\nStartTime = 2,\n" in text and text.startswith("DayLength = 7,\n")


def test_kept_world_is_left_alone(full_sandbox):
    """`monde_neuf` faux : rien n'est effacé, aucun `world_reset`. Mutation : effacer toujours."""
    rig = full_sandbox
    for side in SIDES:
        make_world(rig.zomboid, side, b"kept world")
    before = worlds_state(rig.zomboid)

    lobby = SettledLobby(rig, {"monde_neuf": False})

    records = lobby.rig.journal(lobby.match_id)
    assert not [r for r in records if r["type"] == "world_reset"]
    assert worlds_state(rig.zomboid) == before
    snapshot = rig.zomboid / "duel-snapshots" / "duelA" / "world" / "map" / "chunk.bin"
    assert snapshot.read_bytes().endswith(b"kept world")
    assert not any("monde neuf" in line["msg"] for line in lobby.state()["logs"])


def test_settled_table_passes_the_seal_at_countdown(full_sandbox):
    """Le recontrôle du preset refait la table réglée. Mutation : relire le fichier brut
    dans `_rules_refusal` donne « preset modifié » au décompte."""
    lobby = SettledLobby(full_sandbox, {"monde_neuf": False, "zombies_par_kill": 30})
    lobby.join_and_resolve()
    state = full_sandbox.wait("error", "countdown")
    assert state["phase"] == "countdown", state["error"]
