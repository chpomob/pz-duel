"""R4 : chargeur strict des tables et presets livrés (AC30, AC31, AC70)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from service import engine, rules
from service.protocol import parse_event
from service.rules import TableError, When, load_table, parse_when, rules_hash

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
    "distance_24": (
        lambda t: kills_effect(t).update(distance=[24, 60]),
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
    assert kills_effect(edited)["n"] == 2
    kills_effect(edited)["n"] = 5
    loaded = load_table(edited, catalog, runtime["limits"], file_stem="duel_rapide")
    assert kills_effect(loaded)["n"] == 5
    assert rules_hash(loaded) != rules_hash(read_preset("duel_rapide"))
    assert first_command_n(read_preset("duel_rapide"), catalog, runtime) == 2
    assert first_command_n(edited, catalog, runtime) == 5


def effects_by_rule(preset: dict) -> dict[str, list[dict]]:
    return {rule["id"]: rule["effects"] for rule in preset["rules"]}


def test_preset_migration_values():
    rapide = effects_by_rule(read_preset("duel_rapide"))
    assert list(rapide) == ["kill_baseline", "kill_streak", "hurt_supply"]
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
