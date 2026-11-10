"""R4 : chargeur strict des tables et presets livrés (AC30, AC31, AC70)."""

from __future__ import annotations

import copy
import hashlib
import json
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
        budget = changes.pop("budget", {})
        raw["budget"].update(budget)
        kill_effect = changes.pop("kill_effect", {})
        kills_effect(raw).update(kill_effect)
        raw.update(changes)
        self.catalog = rules.load_effects()
        table = load_table(raw, self.catalog, runtime["limits"], file_stem=name)
        self.core = rules.MatchCore(table, runtime, self.catalog, T0_MS, raw["duration_s"])
        self.records: list[dict] = []
        self.seq = {"A": 0, "B": 0}

    def kill(self, te_s: float, side: str = "A") -> None:
        self.to(te_s - 0.25)
        self.seq[side] += 1
        line = {
            "v": 1,
            "boot": 1,
            "seq": self.seq[side],
            "t_ms": T0_MS + round(te_s * 1000),
            "src": side,
            "side": side,
            "kind": "zombie_kill",
            "data": {"origin": "world"},
        }
        self.core.submit_event(parse_event(line, self.catalog))
        self.to(te_s)

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


def test_duel_rapide_ships_one_percent_and_two_per_kill():
    preset = read_preset("duel_rapide")
    assert preset["sprinter_percent"] == 1
    assert kills_effect(preset)["n"] == 2 and kills_effect(preset)["kind"] == "spawn_pop"


def test_one_kill_pops_exactly_two_zombies_at_the_opponent(runtime):
    """Le contrat « 1 kill = 2 zombies », envoyé au tick même du kill : rien n'attend le budget.

    Mutation : un sprinter ajouté au pop au lieu d'en remplacer un zombie donnerait 3 au
    pop du 100e zombie (test suivant) ; ici, un `n` du preset changé donne autre chose que 2.
    """
    duel = Duel(runtime)
    duel.kill(1)
    assert duel.sent_at(1) == [("B", "spawn_pop", 2)]


def test_the_hundredth_zombie_is_a_sprinter_in_place_of_one(runtime):
    """À 1 %, le 50e kill porte les zombies 99 et 100 : un pop, un sprinter, deux en tout.

    Kills espacés de 21 s : chacun part à son tick, hors du cooldown de 20 s. Mutation : un
    sprinter ajouté (`n` gardé à 2 pour `spawn_pop`) envoie 3 zombies ; un tirage au hasard
    ou une cadence par kill déplace le sprinter.
    """
    duel = Duel(runtime)
    for kill in range(1, 51):
        duel.kill(21 * kill)
        expected = [("B", "spawn_pop", 2)]
        if kill == 50:
            expected = [("B", "spawn_pop", 1), ("B", "spawn_sprinter", 1)]
        assert duel.sent_at(21 * kill) == expected, kill
    fired = duel.of_type("rule_fired")
    assert [r["data"]["index"] for r in fired] == list(range(2, 101, 2))
    assert [r["data"]["sprinter"] for r in fired] == [False] * 49 + [True]


@pytest.mark.parametrize("cooldown_s", [0, 20, 45])
def test_every_kill_is_two_zombies_whatever_the_budget(runtime, cooldown_s):
    """100 kills à 5 s d'écart donnent 200 zombies chez B, 198 pops et 2 sprinters, quel que
    soit `per_effect_cooldown_s` : le budget regroupe les unités en lots, il n'en jette
    aucune avant la fin du match. Mutation : un lot servi qui garde `n` au lieu de le cumuler.
    """
    duel = Duel(runtime, budget={"per_effect_cooldown_s": cooldown_s})
    for kill in range(1, 101):
        duel.kill(5 * kill)
    duel.to(1400)
    assert duel.units("B", "spawn_pop") == 198
    assert duel.units("B", "spawn_sprinter") == 2
    assert duel.of_type("effect_capped") == []
    assert duel.units("A", "spawn_pop") == duel.units("A", "spawn_sprinter") == 0


def test_the_sprinter_count_is_per_target_side(runtime):
    """Chaque camp a son propre `k` : les kills de B ne rapprochent pas le sprinter de B.

    Mutation : un compteur unique pour les deux camps place le premier sprinter au 25e kill
    de chacun.
    """
    duel = Duel(runtime)
    for kill in range(1, 51):
        duel.kill(21 * kill, "A")
        duel.kill(21 * kill + 10, "B")
    fired = duel.of_type("rule_fired")
    for side, target in (("A", "B"), ("B", "A")):
        mine = [r["data"] for r in fired if r["side"] == side]
        assert [data["index"] for data in mine] == list(range(2, 101, 2))
        assert [data["sprinter"] for data in mine].index(True) == 49
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
        assert duel.units("B", "spawn_sprinter") == 3
    assert seen == [[2, 4, 6], [2, 4, 6]]


@pytest.mark.parametrize("percent, sprinters", [(0, 0), (100, 2)])
def test_zero_never_and_hundred_always(runtime, percent, sprinters):
    duel = Duel(runtime, sprinter_percent=percent)
    duel.kill(1)
    assert duel.units("B", "spawn_sprinter") == sprinters
    assert duel.units("B", "spawn_pop") == 2 - sprinters
    assert duel.of_type("rule_fired")[0]["data"]["sprinter"] is bool(sprinters)


def test_cap_per_min_counts_sprinters_in_the_total(runtime):
    """40 kills dans la même seconde : 80 zombies demandés, 60 admis, tous sprinters à 100 %.

    Mutation : découper la part sprinter avant `_admit`, ou la lui faire contourner, en
    laisserait passer 80.
    """
    duel = Duel(runtime, sprinter_percent=100, budget={"per_effect_cooldown_s": 0})
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
