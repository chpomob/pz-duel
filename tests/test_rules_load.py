"""R4 : chargeur strict des tables et presets livrés (AC30, AC31, AC70)."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
from pathlib import Path

import pytest

from service import engine, rules
from service.protocol import dump_line, parse_event
from service.rules import TableError, When, load_table, narrow_table, parse_when, rules_hash

PRESETS_DIR = Path(rules.__file__).resolve().parent / "presets"
SHIPPED = ("demo", "duel_rapide", "hardcore", "pression")
T0_MS = 1_793_830_000_000
CARE_ITEMS = [{"item": "Base.Bandage", "count": 2}, {"item": "Base.Disinfectant", "count": 1}]


def table() -> dict:
    """Table de test valide : une règle `kills`, une caisse `care`, les deux modificateurs."""
    supply = {"kind": "supply_drop", "distance": [25, 40], "warn_s": 3, "items": list(CARE_ITEMS)}
    return copy.deepcopy(
        {
            "id": "essai",
            "label": "Essai",
            "tagline": "table de test",
            "duration_s": 60,
            "day_length": 1,
            "mods": ["duelcore"],
            "atoms": ["zombie_kill", "damage_taken"],
            "rules": [
                {
                    "id": "kills",
                    "when": "atom(zombie_kill).count(3).within(10s)",
                    "effects": [{"kind": "spawn_pop", "n": 2, "distance": [30, 60], "warn_s": 6}],
                },
                {"id": "care", "when": "atom(damage_taken)", "effects": [supply]},
            ],
            "modifiers": {
                "combo": {"per": 0.15, "max": 3.0, "on": "zombie_kill", "reset_on": "damage_taken"},
                "score_gap": {"when_gt": 30, "attacker": 0.6, "defender_bonus": dict(supply)},
            },
            "budget": {
                "threats_per_minute": 6,
                "concurrent_effects": 4,
                "per_effect_cooldown_s": 20,
            },
            "score": {"points": {"zombie_kill": 1}},
            "disconnect_grace_s": 120,
            "summary": ["résumé"],
        }
    )


@pytest.fixture
def catalog():
    """Catalogue livré, plus un atome de source `client` que le chargeur doit refuser."""
    effects = rules.load_effects()
    effects["atoms"]["client_atom"] = {"source": "client"}
    return effects


@pytest.fixture
def limits(runtime):
    return runtime["limits"]


def kills_effect(raw: dict) -> dict:
    return raw["rules"][0]["effects"][0]


def care_effect(raw: dict) -> dict:
    return raw["rules"][1]["effects"][0]


def set_kind(kind: str):
    def mutate(raw):
        raw["rules"][0]["effects"][0] = {"kind": kind, "duration_s": 90, "warn_s": 0}

    return mutate


def cite_atom(atom: str):
    def mutate(raw):
        raw["atoms"].append(atom)
        raw["rules"][0]["when"] = f"atom({atom})"

    return mutate


FORBIDDEN = {
    "effet_fog": (set_kind("fog"), ("règle « kills »", "effects[0].kind", "fog")),
    "atome_craft": (cite_atom("craft"), ("section atoms", "atoms[2]", "craft")),
    "source_client": (cite_atom("client_atom"), ("section atoms", "atoms[2]", "client")),
    "cle_racine": (lambda t: t.update(extra=1), ("section table", "« extra »")),
    "distance_9": (
        lambda t: kills_effect(t).update(distance=[9, 60]),
        ("règle « kills »", "effects[0].distance[0]"),
    ),
    "when_hors_grammaire": (
        lambda t: t["rules"][0].update(when="atom(zombie_kill).count(3)"),
        ("règle « kills »", "« when »"),
    ),
    "budget_absent": (lambda t: t.pop("budget"), ("section table", "« budget »")),
    "cle_regle": (lambda t: t["rules"][0].update(extra=1), ("règle « kills »", "« extra »")),
    "radius_spawn_pop": (
        lambda t: kills_effect(t).update(radius=5),
        ("règle « kills »", "effects[0].radius"),
    ),
    "cle_budget": (lambda t: t["budget"].update(extra=1), ("section budget", "« extra »")),
    "cle_score": (lambda t: t["score"].update(extra=1), ("section score", "« extra »")),
    "cle_combo": (
        lambda t: t["modifiers"]["combo"].update(extra=1),
        ("section modifiers.combo", "« extra »"),
    ),
    "warn_s_manquant": (
        lambda t: kills_effect(t).pop("warn_s"),
        ("règle « kills »", "effects[0].warn_s"),
    ),
    "n_31": (lambda t: kills_effect(t).update(n=31), ("règle « kills »", "effects[0].n")),
    "every_agregat": (lambda t: t["rules"][0].update(every=2), ("règle « kills »", "« every »")),
    "effet_probe_spawn": (
        lambda t: t["rules"][0]["effects"].__setitem__(
            0, {"kind": "probe_spawn", "n": 1, "distance": [30, 60], "warn_s": 0}
        ),
        ("règle « kills »", "effects[0].kind", "probe_spawn"),
    ),
    "sprinter_moins_1": (
        lambda t: t.update(sprinter_percent=-1),
        ("section table", "« sprinter_percent »", "de 0 à 100"),
    ),
    "sprinter_101": (
        lambda t: t.update(sprinter_percent=101),
        ("section table", "« sprinter_percent »", "de 0 à 100"),
    ),
    "sprinter_flottant": (
        lambda t: t.update(sprinter_percent=1.5),
        ("section table", "« sprinter_percent »", "entier attendu"),
    ),
    "sprinter_booleen": (
        lambda t: t.update(sprinter_percent=True),
        ("section table", "« sprinter_percent »", "entier attendu"),
    ),
    "sprinter_texte": (
        lambda t: t.update(sprinter_percent="1"),
        ("section table", "« sprinter_percent »", "entier attendu"),
    ),
    "spacing_moins_1": (
        lambda t: t.update(spacing=-1),
        ("section table", "« spacing »", "de 0 à 20", "reçu -1"),
    ),
    "spacing_21": (
        lambda t: t.update(spacing=21),
        ("section table", "« spacing »", "de 0 à 20", "reçu 21"),
    ),
    "spacing_flottant": (
        lambda t: t.update(spacing=4.5),
        ("section table", "« spacing »", "entier attendu", "reçu 4.5"),
    ),
    "spacing_booleen": (
        lambda t: t.update(spacing=True),
        ("section table", "« spacing »", "entier attendu", "reçu True"),
    ),
    "spacing_texte": (
        lambda t: t.update(spacing="4"),
        ("section table", "« spacing »", "entier attendu", "reçu '4'"),
    ),
    "aggro_negatif": (
        lambda t: t.update(aggro=-0.1),
        ("section table", "« aggro »", "de 0 à 1", "reçu -0.1"),
    ),
    "aggro_1_5": (
        lambda t: t.update(aggro=1.5),
        ("section table", "« aggro »", "de 0 à 1", "reçu 1.5"),
    ),
    "aggro_2": (
        lambda t: t.update(aggro=2),
        ("section table", "« aggro »", "de 0 à 1", "reçu 2"),
    ),
    "aggro_nan": (
        lambda t: t.update(aggro=float("nan")),
        ("section table", "« aggro »", "nombre fini", "reçu nan"),
    ),
    "aggro_infini": (
        lambda t: t.update(aggro=float("inf")),
        ("section table", "« aggro »", "nombre fini", "reçu inf"),
    ),
    "aggro_booleen": (
        lambda t: t.update(aggro=True),
        ("section table", "« aggro »", "nombre fini", "reçu True"),
    ),
    "aggro_texte": (
        lambda t: t.update(aggro="0.6"),
        ("section table", "« aggro »", "nombre fini", "reçu '0.6'"),
    ),
    "threats_manquant": (
        lambda t: t["budget"].pop("threats_per_minute"),
        ("section budget", "threats_per_minute"),
    ),
}


def test_valid_fixture_is_accepted(catalog, limits):
    raw = table()
    loaded = load_table(raw, catalog, limits)
    assert loaded == raw and loaded is not raw
    assert rules_hash(loaded) == rules_hash(raw)


@pytest.mark.parametrize("case", FORBIDDEN, ids=str)
def test_forbidden_tables(case, catalog, limits):
    mutate, expected = FORBIDDEN[case]
    raw = table()
    mutate(raw)
    with pytest.raises(TableError) as refused:
        load_table(raw, catalog, limits)
    for fragment in expected:
        assert fragment in str(refused.value)


def set_items(items):
    return lambda raw: care_effect(raw).update(items=items)


def set_count(count):
    return set_items([{"item": "Base.Bandage", "count": count}])


SUPPLY_CASES = {
    "items_absent": (lambda t: care_effect(t).pop("items"), "effects[0].items"),
    "items_vide": (set_items([]), "effects[0].items"),
    "six_entrees": (
        set_items(
            [
                {"item": item, "count": 1}
                for item in rules.load_effects()["supply_items"]
            ]
        ),
        "effects[0].items",
    ),
    "count_0": (set_count(0), "effects[0].items[0].count"),
    "count_11": (set_count(11), "effects[0].items[0].count"),
    "count_true": (set_count(True), "effects[0].items[0].count"),
    "hors_catalogue": (
        set_items([{"item": "Base.Axe", "count": 1}]),
        "effects[0].items[0].item",
    ),
    "item_deux_fois": (
        set_items([{"item": "Base.Pills", "count": 1}, {"item": "Base.Pills", "count": 2}]),
        "effects[0].items[1].item",
    ),
    "cle_inconnue": (
        set_items([{"item": "Base.Pills", "count": 1, "extra": 1}]),
        "effects[0].items[0].extra",
    ),
}


@pytest.mark.parametrize("case", [*SUPPLY_CASES, "defender_bonus_hors_catalogue"], ids=str)
def test_supply_items_schema(case, catalog, limits):
    raw = table()
    if case == "defender_bonus_hors_catalogue":
        raw["modifiers"]["score_gap"]["defender_bonus"]["items"] = [
            {"item": "Base.Axe", "count": 1}
        ]
        expected = ("section modifiers.score_gap", "defender_bonus.items[0].item")
    else:
        mutate, field = SUPPLY_CASES[case]
        mutate(raw)
        expected = ("règle « care »", field)
    with pytest.raises(TableError) as refused:
        load_table(raw, catalog, limits)
    for fragment in expected:
        assert fragment in str(refused.value)


def read_preset(name: str) -> dict:
    return json.loads((PRESETS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def first_command_n(raw: dict, catalog: dict, runtime: dict) -> int:
    """Rend le `n` de la commande produite par un premier kill de A."""
    table = load_table(raw, catalog, runtime["limits"], file_stem=raw["id"])
    core = rules.MatchCore(table, runtime, catalog, T0_MS, raw["duration_s"])
    line = {
        "v": 1,
        "boot": 1,
        "seq": 1,
        "t_ms": T0_MS,
        "src": "A",
        "side": "A",
        "kind": "zombie_kill",
        "data": {"origin": "world"},
    }
    core.submit_event(parse_event(line, catalog))
    commands = [r for r in core.advance(T0_MS) if r["type"] == "command"]
    assert len(commands) == 1
    return commands[0]["data"]["command"]["n"]


def test_shipped_presets_and_data_driven(runtime):
    catalog = rules.load_effects()
    assert sorted(path.stem for path in PRESETS_DIR.glob("*.json")) == list(SHIPPED)
    for name in SHIPPED:
        load_table(read_preset(name), catalog, runtime["limits"], file_stem=name)

    edited = read_preset("duel_rapide")
    assert kills_effect(edited)["n"] == 10
    kills_effect(edited)["n"] = 5
    loaded = load_table(edited, catalog, runtime["limits"], file_stem="duel_rapide")
    assert kills_effect(loaded)["n"] == 5
    assert rules_hash(loaded) != rules_hash(read_preset("duel_rapide"))
    assert first_command_n(read_preset("duel_rapide"), catalog, runtime) == 10
    assert first_command_n(edited, catalog, runtime) == 5


def effects_by_rule(preset: dict) -> dict[str, list[dict]]:
    return {rule["id"]: rule["effects"] for rule in preset["rules"]}


def test_preset_migration_values():
    rapide = effects_by_rule(read_preset("duel_rapide"))
    # P9F5 : la cadence `sprinter_percent` remplace `kill_streak`, qui ajoutait un sprinter.
    assert list(rapide) == ["kill_baseline", "hurt_supply"]
    assert rapide["hurt_supply"] == [
        {
            "kind": "supply_drop",
            "distance": [25, 40],
            "warn_s": 3,
            "items": CARE_ITEMS,
            "target": "self",
        }
    ]

    preset = read_preset("pression")
    pression = effects_by_rule(preset)
    assert pression["untouched_minute"] == [{"kind": "weather_rain", "duration_s": 90, "warn_s": 0}]
    assert pression["builder_payoff"][0]["n"] == 25
    assert [effect["kind"] for effect in pression["hurt_supply"]] == ["supply_drop"]
    assert preset["modifiers"]["score_gap"]["defender_bonus"]["items"] == CARE_ITEMS
    assert not any(word in " ".join(preset["summary"]) for word in ("brouillard", "secousse"))

    hardcore = effects_by_rule(read_preset("hardcore"))
    assert {"explosion_horde", "repair_noise"} <= set(hardcore)

    for name in SHIPPED:
        assert read_preset(name)["score"] == {"points": {"zombie_kill": 1}}
        assert read_preset(name)["disconnect_grace_s"] == 120
        if name != "pression":
            assert read_preset(name)["modifiers"] == {}


def test_mock_still_reads_presets():
    for preset in engine.load_presets():
        assert {"id", "label", "tagline", "summary", "duration_s", "day_length"} <= set(preset)


def test_parse_when_ast():
    assert parse_when("atom(a)") == When(atom="a")
    assert parse_when("atom(a).count(3).within(10s)") == When("a", 3, 10)
    full = "atom(a).count(3).within(10s).and(not atom(b).within(60s)).then(atom(c), within=30s)"
    assert parse_when(full) == When("a", 3, 10, "b", 60, "c", 30)
    assert parse_when(full).atoms == ("a", "b", "c")
    assert parse_when("atom(a).then(atom(b), within=5s)").is_single_atom is False


@pytest.mark.parametrize(
    "text",
    [
        "",
        "atom()",
        "atom(A)",
        " atom(a)",
        "atom(a)\n",
        "atom(a).within(10s)",
        "atom(a).count(0).within(10s)",
        "atom(a).count(3).within(0s)",
        "atom(a).count(3).within(10)",
        "atom(a).count(٣).within(10s)",
        "atom(a).then(atom(b), within=5s).and(not atom(c).within(5s))",
        "atom(a).then(atom(b),within=5s)",
        f"atom(a).count({'9' * 5000}).within(10s)",
        None,
    ],
)
def test_parse_when_refuses(text):
    with pytest.raises(TableError):
        parse_when(text)


def test_when_atoms_must_be_declared(catalog, limits):
    raw = table()
    raw["rules"][0]["when"] = "atom(zombie_kill).then(atom(build), within=5s)"
    with pytest.raises(TableError, match="règle « kills ».*when.*build"):
        load_table(raw, catalog, limits)


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda t: t["score"]["points"].update(build=1), "points.build"),
        (lambda t: t["modifiers"]["combo"].update(on="build"), "section modifiers.combo"),
        (lambda t: t["modifiers"]["combo"].update(per=True), "« per »"),
        (lambda t: t["modifiers"]["combo"].update(per=float("nan")), "« per »"),
        (lambda t: t["modifiers"]["combo"].update(max=0.5), "« max »"),
        (lambda t: t["modifiers"]["score_gap"]["defender_bonus"].update(target="self"), "target"),
        (lambda t: t["modifiers"].update(extra={}), "section modifiers"),
        (lambda t: t.update(day_length=8), "day_length"),
        (lambda t: t.update(day_length=True), "day_length"),
        (lambda t: t.update(mods=["autre"]), "« mods »"),
        (lambda t: t.update(id="Essai"), "« id »"),
        (lambda t: t["rules"][1].update(id="kills"), "règle « kills »"),
        (lambda t: t["rules"][0].update(sides=["A", "A"]), "sides[1]"),
        (lambda t: t["rules"][0].update(sides=[["A"]]), "sides[0]"),
        (lambda t: t["budget"].update(threats_per_minute=13), "threats_per_minute"),
        (lambda t: t["budget"].update(concurrent_effects=9), "concurrent_effects"),
        (lambda t: t["atoms"].append("zombie_kill"), "atoms[2]"),
        (lambda t: t["atoms"].append(["x"]), "atoms[2]"),
        (lambda t: kills_effect(t).update(target=["self"]), "effects[0].target"),
        (lambda t: kills_effect(t).update(distance=[60, 30]), "effects[0].distance[1]"),
        (lambda t: kills_effect(t).update(kind=["spawn_pop"]), "effects[0].kind"),
        (lambda t: t["rules"][0]["effects"].__setitem__(0, {"kind": "combo_reset"}), "combo_reset"),
    ],
)
def test_other_strict_refusals(mutate, fragment, catalog, limits):
    raw = table()
    mutate(raw)
    with pytest.raises(TableError) as refused:
        load_table(raw, catalog, limits)
    assert fragment in str(refused.value)


def set_combo_per(raw: dict, value) -> None:
    raw["modifiers"]["combo"]["per"] = value


@pytest.mark.parametrize(
    "value", [-(10**400), float("inf"), float("nan")], ids=["huge_negative", "inf", "nan"]
)
def test_number_out_of_float_range_is_refused_with_diagnostic(value, catalog, limits):
    raw = table()
    set_combo_per(raw, value)
    with pytest.raises(TableError) as refused:
        load_table(raw, catalog, limits)
    assert "section modifiers.combo" in str(refused.value)
    assert "« per »" in str(refused.value)


def test_integer_beyond_float_range_does_not_crash_validation(catalog, limits):
    raw = table()
    set_combo_per(raw, 10**400)
    assert load_table(raw, catalog, limits)["modifiers"]["combo"]["per"] == 10**400


def test_id_must_match_file_stem_when_given(catalog, limits):
    load_table(table(), catalog, limits)
    load_table(table(), catalog, limits, file_stem="essai")
    with pytest.raises(TableError, match="« id »"):
        load_table(table(), catalog, limits, file_stem="autre")


def test_active_atoms_restrict_catalog(catalog, limits):
    load_table(table(), catalog, limits, active_atoms=["zombie_kill", "damage_taken"])
    with pytest.raises(TableError, match="damage_taken.*retiré"):
        load_table(table(), catalog, limits, active_atoms=["zombie_kill"])


def test_narrow_table_removes_what_depends_on_the_atom(catalog, limits):
    raw = table()
    sealed = rules_hash(raw)
    raw_atoms = list(raw["atoms"])

    narrowed = narrow_table(raw, ["damage_taken"])

    assert narrowed["atoms"] == ["zombie_kill"]
    assert [rule["id"] for rule in narrowed["rules"]] == [
        rule["id"] for rule in raw["rules"] if "damage_taken" not in rule["when"]
    ]
    assert "damage_taken" not in narrowed["score"]["points"]
    assert "combo" not in narrowed["modifiers"]
    load_table(narrowed, catalog, limits, active_atoms=["zombie_kill"])
    assert raw["atoms"] == raw_atoms and rules_hash(raw) == sealed


def test_narrowed_summary_no_longer_describes_the_rules_it_lost(catalog, limits):
    """`summary` entre dans le hash : il ne peut pas y annoncer ce qui ne se joue plus."""
    narrowed = narrow_table(read_preset("duel_rapide"), ["damage_taken"])

    assert narrowed["summary"] == ["Table réduite, sans damage_taken"]
    load_table(narrowed, catalog, limits, active_atoms=["zombie_kill"])


def test_narrow_table_ignores_an_atom_the_table_never_had():
    raw = table()
    assert narrow_table(raw, ["explosion"]) == raw


def test_narrowed_table_needs_one_atom(catalog, limits):
    narrowed = narrow_table(table(), ["zombie_kill", "damage_taken"])
    with pytest.raises(TableError, match="atoms"):
        load_table(narrowed, catalog, limits, active_atoms=[])


def test_rules_hash_is_canonical():
    raw = {"b": "é", "a": [1, 2]}
    reordered = json.loads('{ "a": [1,  2], "b": "é" }')
    assert rules_hash(raw) == rules_hash(reordered)
    assert rules_hash(raw) == hashlib.sha256('{"a":[1,2],"b":"é"}'.encode("utf-8")).hexdigest()
    assert rules_hash(raw) != rules_hash({"b": "e", "a": [1, 2]})


def test_runtime_path_is_read_at_call_time(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    path.write_text('{"tick_ms": 1}', encoding="utf-8")
    monkeypatch.setattr(rules, "RUNTIME_PATH", path)
    assert rules.load_runtime() == {"tick_ms": 1}


def test_shipped_runtime_has_upnp_section(runtime):
    """R7, R3 : la section `upnp` livrée porte ses 5 clés typées ; l'UPnP du jeu reste coupé."""
    upnp = runtime["upnp"]
    assert set(upnp) == {
        "discovery_timeout_s",
        "request_timeout_s",
        "lease_s",
        "description",
        "max_response_bytes",
    }
    for key in ("discovery_timeout_s", "request_timeout_s"):
        assert isinstance(upnp[key], (int, float)) and not isinstance(upnp[key], bool)
        assert upnp[key] > 0
    for key in ("lease_s", "max_response_bytes"):
        assert isinstance(upnp[key], int) and not isinstance(upnp[key], bool)
    assert 0 < upnp["lease_s"] <= 7200
    assert upnp["max_response_bytes"] > 0
    assert isinstance(upnp["description"], str) and upnp["description"]
    assert runtime["managed_ini"]["UPnP"] == "false"


# P9F5 : pop visible, cadence de sprinters déterministe, « 1 kill = 2 zombies » ------------

MOD_LUA = (
    Path(rules.__file__).resolve().parent.parent
    / "mods/duelcore/42.21/media/lua/server/DuelCore.lua"
)


def test_shipped_pops_land_within_sight(runtime):
    """C1 : 12 à 20 tuiles, là où le premier duel a vu les paquets. Mutation : un preset
    resté à `[30, 60]` ou `[40, 70]`."""
    for name in SHIPPED:
        for rule in read_preset(name)["rules"]:
            for effect in rule["effects"]:
                if effect["kind"] in ("spawn_pop", "spawn_sprinter"):
                    assert effect["distance"] == [12, 20], (name, rule["id"])


def test_loader_floor_is_the_mod_floor(runtime):
    """Le chargeur et le mod refusent sous le même rayon. Mutation : `MIN_DISTANCE` laissé à 25
    dans le mod (il refuserait tout pop en `too_close`), ou `min_distance` laissé à 25."""
    lua = MOD_LUA.read_text(encoding="utf-8")
    (floor,) = re.findall(r"^local MIN_DISTANCE = (\d+)$", lua, flags=re.MULTILINE)
    assert int(floor) == runtime["limits"]["min_distance"] == 10


class LuaGame:
    """Le vrai `DuelCore.lua` sous `mini_lua`, réduit à un joueur éveillé et à pied."""

    def __init__(self) -> None:
        from test_lua_attribution import Game

        self.game = Game()
        self.game.player.isAsleep = lambda: False
        self.game.player.getVehicle = lambda: None

    def reason(self, distance: list[int]) -> str | None:
        command = {"v": 1, "seq": 1, "kind": "spawn_pop", "n": 2, "distance": distance}
        self.game.commands = dump_line({**command, "warn_s": 0})
        self.game.flush()
        (result,) = self.game.lines("cmd_result")
        return result["data"].get("reason")


@pytest.mark.parametrize(
    "distance, refused", [([9, 20], True), ([10, 20], False), ([12, 20], False)]
)
def test_mod_still_refuses_under_its_floor(distance, refused):
    """Le refus reste possible : sous `MIN_DISTANCE`, `too_close`. Mutation : retirer le test
    `cmd.distance[1] < MIN_DISTANCE` de `refusalReason` fait passer `[9, 20]` ; le laisser à 25
    refuse `[12, 20]`, la distance livrée."""
    assert (LuaGame().reason(distance) == "too_close") is refused


@pytest.mark.parametrize(
    "percent, last, expected",
    [
        (1, 300, [100, 200, 300]),
        (2, 100, [50, 100]),
        (3, 100, [34, 67, 100]),
        (0, 1000, []),
        (100, 5, [1, 2, 3, 4, 5]),
        (33, 10, [4, 7, 10]),
    ],
)
def test_sprinter_ranks_follow_the_floor_rule(percent, last, expected):
    """`floor(k·pct/100) > floor((k−1)·pct/100)`. Mutation : un modulo `k % (100 // pct)`
    donne 33, 66, 99 à 3 % ; un `>=` fait de chaque zombie un sprinter."""
    assert rules.sprinter_ranks(1, last, percent) == expected


@pytest.mark.parametrize("percent", range(0, 101))
def test_sprinter_cadence_never_drifts(percent):
    """Sur 100·m zombies, exactement pct·m sprinters, quel que soit le découpage en pops."""
    whole = rules.sprinter_ranks(1, 1000, percent)
    pieces = [k for first in range(1, 1001, 7) for k in rules.sprinter_ranks(first, 7, percent)]
    assert len(whole) == 10 * percent
    assert pieces[: len(whole)] == whole


class Duel:
    """Un `MatchCore` sur un preset livré, éventuellement retouché : kills datés, commandes."""

    def __init__(self, runtime: dict, name: str = "duel_rapide", **changes) -> None:
        raw = read_preset(name)
        self.raw = raw
        budget = changes.pop("budget", {})
        raw["budget"].update(budget)
        if changes.pop("undirect", False):
            # La règle ordinaire d'avant P9F9 : budget et `cap_per_min` s'y appliquent.
            del raw["rules"][0]["direct"]
        kill_effect = changes.pop("kill_effect", {})
        kills_effect(raw).update(kill_effect)
        for key in changes.pop("drop", ()):
            raw.pop(key)
        raw.update(changes)
        self.catalog = rules.load_effects()
        table = load_table(raw, self.catalog, runtime["limits"], file_stem=name)
        self.core = rules.MatchCore(table, runtime, self.catalog, T0_MS, raw["duration_s"])
        self.records: list[dict] = []
        self.seq = {"A": 0, "B": 0}

    def kill(self, te_s: float, side: str = "A", origin: str = "world") -> None:
        self.to(te_s - 0.25)
        self.submit_kill(te_s, side, origin)
        self.to(te_s)

    def submit_kill(self, te_s: float, side: str = "A", origin: str = "world") -> None:
        """Remet un kill daté de `te_s` sans avancer : il ira au prochain tick exécuté."""
        self.seq[side] += 1
        line = {
            "v": 1,
            "boot": 1,
            "seq": self.seq[side],
            "t_ms": T0_MS + round(te_s * 1000),
            "src": side,
            "side": side,
            "kind": "zombie_kill",
            "data": {"origin": origin},
        }
        self.core.submit_event(parse_event(line, self.catalog))

    def to(self, mt_s: float) -> None:
        self.records += self.core.advance(T0_MS + round(mt_s * 1000))

    def of_type(self, type: str) -> list[dict]:
        return [record for record in self.records if record["type"] == type]

    def units(self, target: str, kind: str) -> int:
        return sum(
            r["data"]["command"]["n"]
            for r in self.of_type("command")
            if r["side"] == target and r["data"]["command"]["kind"] == kind
        )

    def sent_at(self, mt_s: float) -> list[tuple[str, str, int]]:
        return [
            (r["side"], r["data"]["command"]["kind"], r["data"]["command"]["n"])
            for r in self.of_type("command")
            if r["mt"] == mt_s
        ]


def test_duel_rapide_ships_one_percent_and_ten_per_kill_direct():
    """P9F9 : « quand on reçoit kill, on fait pop 10, direct » — règle directe, sans plafond."""
    preset = read_preset("duel_rapide")
    assert preset["sprinter_percent"] == 1
    assert kills_effect(preset)["n"] == 10 and kills_effect(preset)["kind"] == "spawn_pop"
    assert preset["rules"][0]["direct"] is True
    assert "cap_per_min" not in kills_effect(preset)


def test_one_kill_pops_exactly_ten_zombies_at_the_opponent(runtime):
    """Le contrat « 1 kill = 10 zombies », envoyé au tick même du kill : rien n'attend le budget.

    Mutation : un sprinter ajouté au pop au lieu d'en remplacer un zombie donnerait 11 au
    pop du 100e zombie (test suivant) ; ici, un `n` du preset changé donne autre chose que 10.
    """
    duel = Duel(runtime)
    duel.kill(1)
    assert duel.sent_at(1) == [("B", "spawn_pop", 10)]


def test_the_hundredth_zombie_is_a_sprinter_in_place_of_one(runtime):
    """À 1 %, le 10e kill porte les zombies 91 à 100 : 9 en pop, 1 sprinter, dix en tout.

    Kills à 1 s d'écart : chacun part à son tick, la règle étant directe. Mutation : un
    sprinter ajouté (`n` gardé à 10 pour `spawn_pop`) envoie 11 zombies ; un tirage au hasard
    ou une cadence par kill déplace le sprinter.
    """
    duel = Duel(runtime)
    for kill in range(1, 11):
        duel.kill(kill)
        expected = [("B", "spawn_pop", 10)]
        if kill == 10:
            expected = [("B", "spawn_pop", 9), ("B", "spawn_sprinter", 1)]
        assert duel.sent_at(kill) == expected, kill
    fired = duel.of_type("rule_fired")
    assert [r["data"]["index"] for r in fired] == list(range(10, 101, 10))
    assert [r["data"]["sprinter"] for r in fired] == [False] * 9 + [True]


@pytest.mark.parametrize(
    "budget",
    [
        {"per_effect_cooldown_s": 0},
        {"per_effect_cooldown_s": 20},
        {"per_effect_cooldown_s": 45, "threats_per_minute": 1, "concurrent_effects": 1},
    ],
    ids=["sans-cooldown", "livre", "le-plus-serre"],
)
def test_every_kill_is_ten_zombies_at_its_tick_whatever_the_budget(runtime, budget):
    """100 kills à 5 s d'écart donnent 1 000 zombies chez B, 990 pops et 10 sprinters, chaque
    lot parti au tick de son kill, quel que soit le budget : il ne s'applique pas à la règle
    directe. Mutation : servir `_direct_lots` par `_may_send`, ou ranger leurs lots dans
    `_lots`, retient le second kill derrière le cooldown et le total n'est plus atteint à 500 s.
    """
    duel = Duel(runtime, budget=budget)
    for kill in range(1, 101):
        duel.kill(5 * kill)
        assert pop_units_at(duel, 5 * kill) == 10, kill
    assert duel.units("B", "spawn_pop") == 990
    assert duel.units("B", "spawn_sprinter") == 10
    assert duel.of_type("effect_capped") == []
    assert duel.units("A", "spawn_pop") == duel.units("A", "spawn_sprinter") == 0


def pop_units_at(duel: Duel, mt_s: float) -> int:
    return sum(n for _, kind, n in duel.sent_at(mt_s) if kind in rules.POP_KINDS)


def test_the_sprinter_count_is_per_target_side(runtime):
    """Chaque camp a son propre `k` : les kills de B ne rapprochent pas le sprinter de B.

    Mutation : un compteur unique pour les deux camps place le premier sprinter au 5e kill
    de chacun.
    """
    duel = Duel(runtime)
    for kill in range(1, 11):
        duel.kill(2 * kill, "A")
        duel.kill(2 * kill + 1, "B")
    fired = duel.of_type("rule_fired")
    for side, target in (("A", "B"), ("B", "A")):
        mine = [r["data"] for r in fired if r["side"] == side]
        assert [data["index"] for data in mine] == list(range(10, 101, 10))
        assert [data["sprinter"] for data in mine].index(True) == 9
        assert duel.units(target, "spawn_sprinter") == 1


def test_the_count_starts_again_with_each_match(runtime):
    """`k` repart de 1 à chaque match : deux duels identiques ont le même sprinter."""
    seen = []
    for _ in range(2):
        duel = Duel(runtime, sprinter_percent=50)
        for kill in range(1, 4):
            duel.kill(21 * kill)
        duel.to(200)
        seen.append([r["data"]["index"] for r in duel.of_type("rule_fired")])
        assert duel.units("B", "spawn_sprinter") == 15
    assert seen == [[10, 20, 30], [10, 20, 30]]


@pytest.mark.parametrize("percent, sprinters", [(0, 0), (100, 10)])
def test_zero_never_and_hundred_always(runtime, percent, sprinters):
    duel = Duel(runtime, sprinter_percent=percent)
    duel.kill(1)
    assert duel.units("B", "spawn_sprinter") == sprinters
    assert duel.units("B", "spawn_pop") == 10 - sprinters
    assert duel.of_type("rule_fired")[0]["data"]["sprinter"] is bool(sprinters)


def test_cap_per_min_counts_sprinters_in_the_total(runtime):
    """40 kills dans la même seconde : 80 zombies demandés, 60 admis, tous sprinters à 100 %.

    Règle ordinaire, d'avant P9F9 : `cap_per_min` vaut encore pour elle. Mutation : découper
    la part sprinter avant `_admit`, ou la lui faire contourner, en laisserait passer 80.
    """
    duel = Duel(
        runtime,
        undirect=True,
        kill_effect={"n": 2, "cap_per_min": 60},
        sprinter_percent=100,
        budget={"per_effect_cooldown_s": 0},
    )
    for kill in range(40):
        duel.kill(1 + kill * 0.001)
    duel.to(120)
    assert duel.units("B", "spawn_sprinter") == 60
    assert duel.units("B", "spawn_pop") == 0
    assert sum(r["data"]["units"] for r in duel.of_type("effect_capped")) == 20
    fired = [r["data"] for r in duel.of_type("rule_fired")]
    assert [data["index"] for data in fired] == list(range(2, 61, 2)) + [None] * 10
    assert [data["sprinter"] for data in fired] == [True] * 30 + [False] * 10


def test_tables_without_the_key_keep_their_journal(runtime):
    """Sans `sprinter_percent`, `rule_fired` reste tel quel : aucun sprinter, aucun rang."""
    duel = Duel(runtime, "demo")
    duel.kill(1)
    (fired,) = duel.of_type("rule_fired")
    assert set(fired["data"]) == {"rule", "event", "multipliers"}
    assert duel.sent_at(1) == [("B", "spawn_pop", 2)]


# P9F8 : pops dispersés et ciblage à l'apparition, réglés par le preset ---------------------


@pytest.mark.parametrize(
    "spacing, aggro", [(0, 0), (20, 1), (4, 0.6), (4, 1.0), (0, 0.0)]
)
def test_pop_settings_accept_their_bounds(catalog, limits, spacing, aggro):
    """Bornes incluses : 0 et 20 cases, 0 et 1 de poids, entier ou flottant."""
    raw = {**table(), "spacing": spacing, "aggro": aggro}
    assert load_table(raw, catalog, limits) == raw


def test_shipped_presets_carry_the_pop_settings():
    """Quatre presets : 4 cases d'écart partout, ciblage à 0,6 sauf `demo`, simulé, à 0."""
    for name in SHIPPED:
        preset = read_preset(name)
        assert preset["spacing"] == 4, name
        assert preset["aggro"] == (0 if name == "demo" else 0.6), name


def pop_commands(duel: Duel) -> list[dict]:
    return [r["data"]["command"] for r in duel.of_type("command")]


def test_pop_commands_carry_spacing_and_aggro(runtime):
    """Le preset voyage dans la commande : le mod n'a rien à connaître d'avance. Mutation :
    des réglages lus du runtime ou omis laissent la commande sans `spacing` ni `aggro`."""
    duel = Duel(runtime, spacing=7, aggro=0.25)
    duel.kill(1)
    (command,) = pop_commands(duel)
    assert command["kind"] == "spawn_pop" and command["n"] == 10
    assert (command["spacing"], command["aggro"]) == (7, 0.25)
    assert list(command)[-1] == "origin"


def test_sprinter_commands_carry_them_too(runtime):
    """Le sprinter est un pop découpé : il porte les mêmes réglages."""
    duel = Duel(runtime, sprinter_percent=100, spacing=3, aggro=1)
    duel.kill(1)
    (command,) = pop_commands(duel)
    assert command["kind"] == "spawn_sprinter"
    assert (command["spacing"], command["aggro"]) == (3, 1)


def test_other_effects_do_not_carry_the_pop_settings(runtime):
    """Une caisse de soin n'a ni écart ni ciblage. Mutation : ajouter les réglages à toute
    commande les enverrait à `supply_drop`."""
    duel = Duel(runtime)
    hurt = {
        "v": 1, "boot": 1, "seq": 1, "t_ms": T0_MS + 1000, "src": "A", "side": "A",
        "kind": "damage_taken", "data": {"hp": 80, "delta": -20},
    }  # fmt: skip
    duel.core.submit_event(parse_event(hurt, duel.catalog))
    duel.to(2)
    commands = pop_commands(duel)
    assert [c["kind"] for c in commands] == ["supply_drop"]
    assert "spacing" not in commands[0] and "aggro" not in commands[0]


def test_a_table_without_the_settings_sends_none(runtime):
    """Sans les clés, la commande reste celle d'avant P9F8 : le mod replie sur 0 et 0."""
    duel = Duel(runtime, drop=("spacing", "aggro"))
    duel.kill(1)
    (command,) = pop_commands(duel)
    assert "spacing" not in command and "aggro" not in command


def test_a_pop_held_by_the_cooldown_keeps_the_settings(runtime):
    """Le second kill attend le cooldown dans un lot, servi à 21 s : sa commande porte les
    mêmes réglages que la première. Règle ordinaire : la directe n'attend aucun cooldown.
    Mutation : réglages ajoutés au seul envoi immédiat."""
    duel = Duel(runtime, undirect=True, kill_effect={"n": 2})
    duel.kill(1)
    duel.kill(2)
    duel.to(30)
    assert [(c["n"], c["spacing"], c["aggro"]) for c in pop_commands(duel)] == [
        (2, 4, 0.6),
        (2, 4, 0.6),
    ]


def test_the_journal_says_spawned_spread_and_targeted(runtime):
    """`rule_fired`, puis la commande et ses réglages, puis `cmd_result` avec `spawned`,
    `spread` et `targeted` tels que le mod les rend : un lot de 2 sur une seule case se lit
    dans le journal. Mutation : un `cmd_result` réduit à ses champs connus perdrait `spread`."""
    duel = Duel(runtime)
    duel.kill(1)
    (command,) = pop_commands(duel)
    result = {
        "v": 1, "boot": 1, "seq": 1, "t_ms": T0_MS + 8000, "src": "B", "side": "B",
        "kind": "cmd_result",
        "data": {"seq": command["seq"], "status": "applied", "spawned": 2, "spread": 1,
                 "targeted": 2},
    }  # fmt: skip
    duel.core.submit_event(parse_event(dump_line(result), duel.catalog))
    duel.to(9)
    types = [r["type"] for r in duel.records if r["type"] in ("rule_fired", "command", "cmd_result")]
    assert types == ["rule_fired", "command", "cmd_result"]
    (logged,) = duel.of_type("cmd_result")
    assert logged["side"] == "B"
    data = logged["data"]["data"]
    assert (data["spawned"], data["spread"], data["targeted"]) == (2, 1, 2)
    assert duel.of_type("cmd_result_ignored") == []


# Le vrai mod sous `mini_lua` : ce que le double prouve, c'est la logique du lot (cases
# retenues, nombre gardé, appels de ciblage), pas le comportement des zombies dans le jeu.


class SpawnSquare:
    def __init__(self, x: int, y: int, free: bool) -> None:
        self.x, self.y, self.free = int(x), int(y), free

    def getX(self) -> int:
        return self.x

    def getY(self) -> int:
        return self.y

    def isFree(self, _strict: bool) -> bool:
        return self.free


class ZombieList:
    def __init__(self, zombies: list) -> None:
        self.zombies = zombies

    def size(self) -> int:
        return len(self.zombies)

    def get(self, index: int):
        return self.zombies[index]


TARGETING = ("setTarget", "addAggro", "spotted")


def spawned_zombie(missing: tuple[str, ...]):
    """Un zombie créé, qui note les appels de ciblage ; `missing` les retire de sa classe."""
    from doubles.mini_lua import Table

    calls: list[tuple] = []
    methods = {
        name: (lambda name: lambda self, *args: calls.append((name, *args)))(name)
        for name in TARGETING
        if name not in missing
    }
    methods.update(
        getModData=lambda self: self.mod_data,
        setWalkType=lambda self, walk: calls.append(("setWalkType", walk)),
        setSpeedTypeFromWalkType=lambda self: None,
    )
    zombie = type("SpawnedZombie", (), methods)()
    zombie.mod_data = Table()
    zombie.calls = calls
    return zombie


class SpawnGame:
    """Le vrai `DuelCore.lua` avec une cellule où tout est libre et un générateur réglé."""

    def __init__(self, monkeypatch, missing: tuple[str, ...] = (), seed: int = 9) -> None:
        import test_lua_attribution
        from doubles.mini_lua import Lua

        rng = random.Random(seed)
        self.spawns: list[tuple[int, int, int]] = []
        self.zombies: list = []

        def zomb_rand(low, high=None):
            return rng.randrange(int(low)) if high is None else rng.randrange(int(low), int(high))

        def add_zombies(x, y, _z, count, _outfit, _female):
            self.spawns.append((int(x), int(y), int(count)))
            made = [spawned_zombie(missing) for _ in range(int(count))]
            self.zombies += made
            return ZombieList(made)

        extra = {
            "ZombRand": zomb_rand,
            "getCell": lambda: _Cell(),
            "addZombiesInOutfit": add_zombies,
        }
        monkeypatch.setattr(test_lua_attribution, "Lua", lambda env: Lua({**env, **extra}))
        self.game = test_lua_attribution.Game()
        self.game.player.isAsleep = lambda: False
        self.game.player.getVehicle = lambda: None
        self.player = self.game.player

    def apply(self, kind: str = "spawn_pop", n: int = 2, **settings) -> dict:
        command = {"v": 1, "seq": 1, "kind": kind, "n": n, "distance": [12, 20], "warn_s": 0}
        self.game.commands = dump_line({**command, **settings})
        self.game.flush()
        (result,) = self.game.lines("cmd_result")
        return result["data"]


class _Cell:
    def getGridSquare(self, x, y, _z) -> SpawnSquare:
        return SpawnSquare(x, y, True)


def test_mod_spreads_a_pop_over_spaced_squares(monkeypatch):
    """`spacing: 4`, `n: 4` : quatre cases, chacune dans la bande, toutes à 4 cases ou plus
    l'une de l'autre. Mutation : le tas d'avant (`addZombiesInOutfit(…, n)` sur une case)."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(n=4, spacing=4)
    assert (data["status"], data["spawned"], data["spread"]) == ("applied", 4, 4)
    assert [count for *_, count in spawn.spawns] == [1, 1, 1, 1]
    squares = [(x, y) for x, y, _ in spawn.spawns]
    px, py = spawn.player.getX(), spawn.player.getY()
    for x, y in squares:
        assert 12 <= math.dist((x, y), (px, py)) <= 20
    for index, one in enumerate(squares):
        for other in squares[index + 1 :]:
            assert math.dist(one, other) >= 4


def test_mod_without_spacing_keeps_the_single_square(monkeypatch):
    """`spacing` absent : le comportement d'avant, deux zombies sur une case, `spread` à 1."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(n=2)
    assert (data["spawned"], data["spread"], data["targeted"]) == (2, 1, 0)
    assert [count for *_, count in spawn.spawns] == [2]


def test_mod_never_lowers_the_count_when_squares_run_out(monkeypatch):
    """12 zombies à 20 cases d'écart dans une bande de 12 à 20 : la place manque, mais les 12
    naissent, superposés sur les cases retenues. Mutation : abandonner les zombies sans case
    à eux rendrait `spawned` égal à `spread`."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(n=12, spacing=20)
    assert data["status"] == "applied" and data["spawned"] == 12
    assert 1 <= data["spread"] < 12
    assert sum(count for *_, count in spawn.spawns) == 12
    assert len({(x, y) for x, y, _ in spawn.spawns}) == data["spread"]


def test_mod_targets_every_created_zombie(monkeypatch):
    """`aggro: 0.6` : chaque zombie reçoit `setTarget`, `addAggro` à 0,6 puis `spotted`."""
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(n=3, spacing=4, aggro=0.6)
    assert data["targeted"] == 3
    player = spawn.player
    for zombie in spawn.zombies:
        assert zombie.calls == [
            ("setTarget", player),
            ("addAggro", player, 0.6),
            ("spotted", player, True),
        ]


def test_mod_sprinters_are_spread_and_targeted_too(monkeypatch):
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(kind="spawn_sprinter", n=2, spacing=4, aggro=1)
    assert (data["spawned"], data["spread"], data["targeted"]) == (2, 2, 2)
    for zombie in spawn.zombies:
        assert zombie.calls[0][0] == "setWalkType"
        assert [call[0] for call in zombie.calls[1:]] == list(TARGETING)


def test_mod_aggro_zero_targets_nobody(monkeypatch):
    spawn = SpawnGame(monkeypatch)
    data = spawn.apply(n=2, spacing=4, aggro=0)
    assert (data["spawned"], data["targeted"]) == (2, 0)
    assert all(zombie.calls == [] for zombie in spawn.zombies)


@pytest.mark.parametrize("missing", TARGETING)
def test_mod_spawn_survives_a_missing_targeting_function(monkeypatch, missing):
    """Une fonction de ciblage absente de la version du jeu : le pop reste appliqué, les
    zombies créés, et `targeted` dit 0. Mutation : un appel hors `pcall` rend `lua_error`."""
    spawn = SpawnGame(monkeypatch, missing=(missing,))
    data = spawn.apply(n=2, spacing=4, aggro=0.6)
    assert (data["status"], data["spawned"], data["spread"]) == ("applied", 2, 2)
    assert data["targeted"] == 0
    for zombie in spawn.zombies:
        assert [call[0] for call in zombie.calls] == [m for m in TARGETING if m != missing]


def test_mod_still_refuses_too_close_with_the_new_settings(monkeypatch):
    spawn = SpawnGame(monkeypatch)
    command = {"v": 1, "seq": 1, "kind": "spawn_pop", "n": 2, "distance": [9, 20], "warn_s": 0}
    spawn.game.commands = dump_line({**command, "spacing": 4, "aggro": 0.6})
    spawn.game.flush()
    (result,) = spawn.game.lines("cmd_result")
    assert result["data"]["reason"] == "too_close" and spawn.spawns == []


# P9F9 : un kill, un lot de dix, sans budget ni perte ---------------------------------------

LOT = 10
# (écart entre deux rafales en s, kills par rafale, rafales) : 2 s, 1 s (vidage du mod),
# 250 ms (un tick), et 5 kills dans le même tick.
CADENCES = {
    "2s": (2.0, 1, 12),
    "1s": (1.0, 1, 12),
    "250ms": (0.25, 1, 12),
    "rafale": (1.0, 5, 3),
}


def pop_units(duel: Duel, target: str) -> int:
    return duel.units(target, "spawn_pop") + duel.units(target, "spawn_sprinter")


def kill_rules(duel: Duel, side: str) -> list[dict]:
    fired = duel.of_type("rule_fired")
    return [r for r in fired if r["side"] == side and r["data"]["rule"] == "kill_baseline"]


def play_cadence(duel: Duel, cadence: str, side: str, origins: tuple[str, ...]) -> int:
    """Joue la cadence pour `side`, origines en alternance ; rend le nombre de kills."""
    period_s, per_tick, bursts = CADENCES[cadence]
    count = 0
    for burst in range(bursts):
        te_s = 1 + burst * period_s
        duel.to(te_s - 0.25)
        for _ in range(per_tick):
            duel.submit_kill(te_s, side, origins[count % len(origins)])
            count += 1
        duel.to(te_s)
    return count


@pytest.mark.parametrize(
    "origins", [("world",), ("duel",), ("world", "duel")], ids=["monde", "duel", "mixte"]
)
@pytest.mark.parametrize("cadence", list(CADENCES))
def test_every_kill_fires_its_lot_of_ten_at_its_tick(runtime, cadence, origins):
    """N kills donnent N `kill_baseline` et 10 × N zombies chez l'adversaire, envoyés au tick
    du dernier kill : rien n'attend un budget, rien n'est plafonné, limité ni jeté.

    `origins` couvre la perte mesurée sur 734610ca : 21 des 35 kills y visaient des zombies
    envoyés par le duel, et aucun n'a déclenché. Le sprinter remplace un zombie du lot (P9F5) :
    le total compte `spawn_pop` et `spawn_sprinter`, `spawn_pop` seul vaut 10 × N moins eux.
    Mutations : rendre le filtre `origin: duel` à une règle `direct` (zéro règle sur un kill du
    duel) ; servir les lots directs par `_may_send` (le cooldown de 20 s les retient) ; remettre
    `n: 2` ou `cap_per_min` au preset.
    """
    duel = Duel(runtime)
    kills = play_cadence(duel, cadence, "A", origins)
    percent = read_preset("duel_rapide")["sprinter_percent"]
    sprinters = len(rules.sprinter_ranks(1, LOT * kills, percent))
    assert len(kill_rules(duel, "A")) == kills
    assert pop_units(duel, "B") == LOT * kills
    assert duel.units("B", "spawn_pop") == LOT * kills - sprinters
    assert pop_units(duel, "A") == 0
    for limited in ("effect_capped", "effect_limited", "effect_dropped"):
        assert duel.of_type(limited) == [], limited


@pytest.mark.parametrize(
    "origins", [("world", "duel"), ("duel", "world"), ("world", "world")], ids=str
)
@pytest.mark.parametrize("cadence", list(CADENCES))
def test_both_sides_get_exactly_the_same(runtime, cadence, origins):
    """N kills de A et N kills de B aux mêmes instants : mêmes règles, mêmes lots, mêmes ticks,
    chacun chez l'autre, même si l'un ne tue que des zombies envoyés par le duel (6bebf6c6 :
    25 kills de A → 331 zombies chez B, 10 kills de B → 1 chez A).

    Mutation : rendre le filtre `origin: duel` à la règle directe laisse le camp aux kills
    « duel » sans règle ni zombie ; servir les lots directs de B derrière la file budgétée
    décale ses commandes.
    """
    duel = Duel(runtime)
    period_s, per_tick, bursts = CADENCES[cadence]
    for burst in range(bursts):
        te_s = 1 + burst * period_s
        duel.to(te_s - 0.25)
        for _ in range(per_tick):
            for side, origin in zip("AB", origins):
                duel.submit_kill(te_s, side, origin)
        duel.to(te_s)
    kills = per_tick * bursts

    def mirror(side: str, target: str) -> tuple:
        fired = [
            (r["mt"], r["data"]["index"], r["data"]["sprinter"]) for r in kill_rules(duel, side)
        ]
        sent = [
            (r["mt"], r["data"]["command"]["kind"], r["data"]["command"]["n"])
            for r in duel.of_type("command")
            if r["side"] == target
        ]
        return fired, sent

    assert mirror("A", "B") == mirror("B", "A")
    assert len(kill_rules(duel, "A")) == len(kill_rules(duel, "B")) == kills
    assert pop_units(duel, "A") == pop_units(duel, "B") == LOT * kills


def test_a_late_kill_still_fires_its_lot(runtime):
    """Un kill lu deux secondes après son tick est journalisé `late_event`, et fait son lot de
    dix au tick qui le traite. Mutation : un retour anticipé sur `_is_late` le perdrait."""
    duel = Duel(runtime)
    duel.to(3)
    duel.submit_kill(1, "B", "duel")
    duel.to(3.25)
    assert len(duel.of_type("late_event")) == 1
    assert len(kill_rules(duel, "B")) == 1
    assert duel.sent_at(3.25) == [("A", "spawn_pop", 10)]


@pytest.mark.parametrize("ending", ["timer", "submitted"])
def test_a_kill_of_the_conclusion_tick_sends_its_lot(runtime, ending):
    """Le kill traité au tick de fin part avant `match_end`, au lieu d'un `effect_dropped`.

    Mutation : ne plus appeler `_serve_direct` dans `_finish` rend le `effect_dropped` de
    10 unités.
    """
    duel = Duel(runtime, duration_s=60)
    duel.to(59.75)
    duel.submit_kill(60 if ending == "timer" else 59.9, "A", "duel")
    if ending == "submitted":
        duel.core.submit_end({"outcome": "forfeit", "winner": "A", "reason": "absence de B"})
    duel.to(60)
    tail = [(r["type"], r["side"]) for r in duel.records if r["mt"] == 60]
    assert tail == [("event", "A"), ("rule_fired", "A"), ("command", "B"), ("match_end", None)]
    assert duel.sent_at(60) == [("B", "spawn_pop", 10)]


def test_a_restarting_target_gets_its_lots_when_back(runtime):
    """Une relance retient les lots directs, sans en rien jeter : ils partent à son retour."""
    duel = Duel(runtime)
    duel.to(1)
    duel.core.submit_state("B", "restarting")
    duel.kill(2)
    duel.kill(3)
    assert pop_units(duel, "B") == 0
    duel.core.submit_state("B", "running")
    duel.to(3.25)
    assert duel.sent_at(3.25) == [("B", "spawn_pop", 20)]
    assert duel.of_type("effect_dropped") == []


def test_direct_pops_leave_the_budget_to_the_other_effects(runtime):
    """Vingt lots directs chez A, puis une blessure de A : sa caisse part au tick, le budget le
    plus serré (1 menace/min, 1 effet en cours) lui étant resté entier. Mutation : compter un envoi
    direct dans `_sent`, ou un effet direct dans `concurrent_effects`, la retient."""
    duel = Duel(runtime, budget={"threats_per_minute": 1, "concurrent_effects": 1})
    for kill in range(1, 21):
        duel.kill(kill * 0.25, "B")
    hurt = {
        "v": 1, "boot": 1, "seq": 99, "t_ms": T0_MS + 6000, "src": "A", "side": "A",
        "kind": "damage_taken", "data": {"hp": 80, "delta": -20},
    }  # fmt: skip
    duel.to(5.75)
    duel.core.submit_event(parse_event(hurt, duel.catalog))
    duel.to(6)
    sent = [(r["side"], r["data"]["command"]["kind"]) for r in duel.of_type("command")]
    assert [entry for entry in sent if entry[1] == "supply_drop"] == [("A", "supply_drop")]
    assert [r["mt"] for r in duel.of_type("command")][-1] == 6
    assert pop_units(duel, "A") == 200


def test_the_safety_bound_is_far_above_any_kill_rate(runtime):
    """Borne livrée : 6 000 zombies par camp visé et par minute, soit 10 kills par seconde
    pendant une minute. Deux minutes à un kill par tick (4/s), des deux camps, n'y touchent
    pas. Mutation : une borne ramenée sous 2 400 journalise un `effect_limited` ici."""
    assert runtime[rules.DIRECT_SAFETY] == 6000
    duel = Duel(runtime)
    ticks = 2 * 60 * 4
    for tick in range(1, ticks + 1):
        duel.to(tick * 0.25 - 0.25)
        for side in "AB":
            duel.submit_kill(tick * 0.25, side, "duel")
        duel.to(tick * 0.25)
    assert duel.of_type("effect_limited") == []
    assert pop_units(duel, "A") == pop_units(duel, "B") == LOT * ticks


def test_the_safety_bound_limits_and_says_from_which_volume(runtime):
    """Borne ramenée à 25 : trois kills d'un tick en demandent 30, 25 partent, 5 sont
    journalisés `effect_limited` avec la borne ; une minute plus tard, la fenêtre est libre.
    Mutation : une borne qui jette sans journaliser, ou qui compte les deux camps ensemble."""
    duel = Duel({**runtime, rules.DIRECT_SAFETY: 25})
    duel.to(0.75)
    for _ in range(3):
        duel.submit_kill(1, "A")
    duel.submit_kill(1, "B")
    duel.to(1)
    assert pop_units(duel, "B") == 25 and pop_units(duel, "A") == 10
    (limited,) = duel.of_type("effect_limited")
    assert limited["side"] == "B"
    assert limited["data"] == {
        "rule": "kill_baseline", "kind": "spawn_pop", "units": 5, "limit": 25, "window_s": 60,
    }  # fmt: skip
    assert len(kill_rules(duel, "A")) == 3
    duel.kill(61, "A")
    assert duel.sent_at(61) == [("B", "spawn_pop", 10)]


def test_a_journal_without_the_bound_replays_without_one(runtime):
    """Un runtime d'avant P9F9, sans la clé, rejoue sans borne : rien n'y est limité."""
    old = {key: value for key, value in runtime.items() if key != rules.DIRECT_SAFETY}
    duel = Duel(old)
    duel.to(0.75)
    for _ in range(30):
        duel.submit_kill(1, "A")
    duel.to(1)
    assert pop_units(duel, "B") == 300
    assert duel.of_type("effect_limited") == []


@pytest.mark.parametrize(
    "change, field",
    [
        ({"direct": False}, "direct"),
        ({"direct": 1}, "direct"),
        ({"cap": 60}, "effects[0].cap_per_min"),
    ],
)
def test_a_direct_rule_is_true_and_has_no_cap(catalog, limits, change, field):
    """`direct` vaut `true`, et une règle directe refuse `cap_per_min` : rien ne tronque son
    lot dans la table. Mutation : retirer `_check_direct` laisse passer le plafond."""
    raw = read_preset("duel_rapide")
    rule = raw["rules"][0]
    if "cap" in change:
        rule["effects"][0]["cap_per_min"] = change["cap"]
    else:
        rule.update(change)
    with pytest.raises(TableError) as refused:
        load_table(raw, catalog, limits, file_stem="duel_rapide")
    assert field in str(refused.value)


def test_an_ordinary_rule_still_ignores_duel_spawns(runtime):
    """Sans `direct`, le zombie du duel ne déclenche rien, comme avant : la boucle reste un
    choix de la table. Mutation : supprimer le filtre pour toutes les règles."""
    duel = Duel(runtime, undirect=True)
    duel.kill(1, origin="duel")
    assert kill_rules(duel, "A") == []
    duel.kill(2, origin="world")
    assert len(kill_rules(duel, "A")) == 1
