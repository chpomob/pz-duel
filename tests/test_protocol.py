"""AC1 : contrat v1, exemples de la spec copiés littéralement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service.protocol import (
    ProtocolError,
    dump_line,
    parse_ack,
    parse_beat,
    parse_command,
    parse_event,
    parse_session,
)

EFFECTS_PATH = Path(__file__).resolve().parent.parent / "service" / "effects.json"
CATALOG = json.loads(EFFECTS_PATH.read_text(encoding="utf-8"))

SESSION = '{"v":1,"match":"8f3a2c","side":"A","boot":2,"cmd_floor":17}'
EVENT = '{"v":1,"boot":2,"seq":42,"t_ms":1793830000123,"src":"A","side":"A","kind":"zombie_kill","data":{"w":"Base.Axe","x":10500,"y":9900,"origin":"world"}}'
BEAT = '{"v":1,"boot":2,"seq":43,"t_ms":1793830010123,"src":"A","side":"A","kind":"beat","data":{"age":2.4657,"players":["durand"],"hp":87,"kills":41}}'
CAPS = '{"v":1,"boot":2,"seq":1,"t_ms":1793830000000,"src":"A","side":"A","kind":"caps","data":{"build":"42.21.0","mod":"0.1.0","protocol":1,"hooks":{"OnZombieDead":true,"OnWeaponHitCharacter":false},"functions":{"addZombiesInOutfit":true},"atoms":["zombie_kill"],"effects":["spawn_pop"]}}'
COMMAND = '{"v":1,"seq":7,"kind":"spawn_pop","n":2,"distance":[30,60],"warn_s":6,"origin":{"rule":"kill_baseline","boot":2,"event_seq":42}}'
SUPPLY = '{"v":1,"seq":8,"kind":"supply_drop","distance":[25,40],"warn_s":3,"items":[{"item":"Base.Bandage","count":2},{"item":"Base.Disinfectant","count":1}],"origin":{"rule":"hurt_supply","boot":2,"event_seq":51}}'
ACK = '{"v":1,"boot":2,"seq":7}'
COMMAND_PARAMS = {
    "n": 2,
    "radius": 20,
    "distance": [30, 60],
    "warn_s": 6,
    "duration_s": 90,
    "items": [{"item": "Base.Bandage", "count": 2}],
}


def parse_catalog_event(raw):
    return parse_event(raw, CATALOG)


def parse_catalog_command(raw):
    return parse_command(raw, CATALOG)


CONTRACT_EXAMPLES = [
    pytest.param(parse_session, SESSION, id="session"),
    pytest.param(parse_catalog_event, EVENT, id="event"),
    pytest.param(parse_catalog_event, BEAT, id="beat-as-event"),
    pytest.param(parse_beat, BEAT, id="beat"),
    pytest.param(parse_catalog_event, CAPS, id="caps"),
    pytest.param(parse_catalog_command, COMMAND, id="command"),
    pytest.param(parse_catalog_command, SUPPLY, id="supply"),
    pytest.param(parse_ack, ACK, id="ack"),
]


def event(**overrides):
    return {**json.loads(EVENT), **overrides}


def beat_data(**overrides):
    return {**json.loads(BEAT), "data": {**json.loads(BEAT)["data"], **overrides}}


def command_for(kind):
    """Commande minimale d'un effet du catalogue, ses seuls paramètres obligatoires."""
    required = CATALOG["effects"][kind]["required"]
    params = {key: COMMAND_PARAMS[key] for key in required}
    return {"v": 1, "seq": 7, "kind": kind, "origin": {"rule": "kill_baseline"}, **params}


def assert_rejected(code, parse, raw):
    with pytest.raises(ProtocolError) as caught:
        parse(raw)
    assert caught.value.code == code
    assert caught.value.detail


@pytest.mark.parametrize("parse, line", CONTRACT_EXAMPLES)
def test_contract_examples_and_version(parse, line):
    assert parse(line) == json.loads(line)
    assert parse(line.encode("utf-8") + b"\n") == json.loads(line)

    assert_rejected("bad_version", parse, {**json.loads(line), "v": 2})


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_version_is_strictly_the_integer_one(version):
    assert_rejected("bad_version", parse_ack, {"v": version, "boot": 2, "seq": 7})


def test_missing_version_is_rejected():
    assert_rejected("bad_version", parse_ack, {"boot": 2, "seq": 7})


@pytest.mark.parametrize("parse, line", CONTRACT_EXAMPLES)
def test_unknown_fields_are_ignored_and_kept(parse, line):
    extended = {**json.loads(line), "later": {"a": 1}}
    assert parse(extended)["later"] == {"a": 1}


@pytest.mark.parametrize(
    "line",
    [b"", b"{", b"\xff{}", b'{"v":1,"boot":NaN,"seq":1}', b"[" * 100_000],
    ids=["empty", "truncated", "bad-utf8", "nan", "deep"],
)
def test_unreadable_line(line):
    assert_rejected("bad_json", parse_ack, line)


@pytest.mark.parametrize("line", ["[]", "1", "null", '"v"'])
def test_line_must_be_an_object(line):
    assert_rejected("not_object", parse_ack, line)


@pytest.mark.parametrize("field", ["boot", "seq", "t_ms"])
@pytest.mark.parametrize("value", [True, 1.0, "1", None, -1])
def test_event_integers_are_strict(field, value):
    assert_rejected("bad_field", parse_catalog_event, event(**{field: value}))


@pytest.mark.parametrize("field", ["boot", "seq", "t_ms", "src", "side", "kind", "data"])
def test_event_required_fields(field):
    raw = event()
    del raw[field]
    assert_rejected("missing_field", parse_catalog_event, raw)


def test_seq_starts_at_one():
    assert_rejected("bad_field", parse_catalog_event, event(seq=0))


@pytest.mark.parametrize("kind", ["player_death", "zombie_kill", "damage_taken", "build"])
def test_kinds_without_required_data(kind):
    assert parse_catalog_event(event(kind=kind, data={}))["kind"] == kind


def test_kind_is_limited_to_the_given_catalog():
    restricted = {"atoms": {"build": {"source": "server"}}}
    assert parse_event(event(kind="build", data={}), restricted)["kind"] == "build"
    assert_rejected("unknown_kind", lambda raw: parse_event(raw, restricted), event())


@pytest.mark.parametrize("kind", ["spawn_pop", "craft", ""])
def test_kind_outside_contract(kind):
    assert_rejected("unknown_kind", parse_catalog_event, event(kind=kind))


@pytest.mark.parametrize("kind", [1, True, None, ["beat"]])
def test_kind_must_be_a_string(kind):
    assert_rejected("bad_field", parse_catalog_event, event(kind=kind))


def test_null_side_is_accepted():
    assert parse_catalog_event(event(side=None))["side"] is None


@pytest.mark.parametrize(
    "overrides",
    [{"side": "B"}, {"src": "C", "side": None}, {"src": "B", "side": "A"}],
    ids=["other-side", "unknown-src", "crossed"],
)
def test_side_must_match_src(overrides):
    assert_rejected("side_mismatch", parse_catalog_event, event(**overrides))


def test_data_is_only_checked_where_the_service_reads_it():
    data = {"w": 12, "x": "ailleurs", "y": None, "origin": "duel"}
    assert parse_catalog_event(event(data=data))["data"] == data


def test_zombie_origin_is_enumerated():
    assert_rejected("bad_field", parse_catalog_event, event(data={"origin": "ailleurs"}))


def test_hook_seen():
    line = event(kind="hook_seen", data={"hook": "OnWeaponHitCharacter"})
    assert parse_catalog_event(line)["data"]["hook"] == "OnWeaponHitCharacter"
    assert_rejected("missing_field", parse_catalog_event, event(kind="hook_seen", data={}))


@pytest.mark.parametrize(
    "data",
    [
        {"seq": 7, "status": "applied", "reason": "", "spawned": 2},
        {"seq": 7, "status": "refused", "reason": "player_asleep"},
        {"seq": 7, "status": "failed", "reason": "unknown_kind"},
        {"seq": 7, "status": "applied"},
    ],
)
def test_cmd_result_accepted(data):
    assert parse_catalog_event(event(kind="cmd_result", data=data))["data"] == data


@pytest.mark.parametrize(
    "data",
    [
        {"seq": True, "status": "applied"},
        {"seq": 7, "status": "done"},
        {"seq": 7, "status": ["applied"]},
        {"seq": 7, "status": "applied", "reason": 1},
        {"seq": 7, "status": "applied", "spawned": True},
    ],
)
def test_cmd_result_rejected(data):
    assert_rejected("bad_field", parse_catalog_event, event(kind="cmd_result", data=data))


def test_beat_without_player():
    line = beat_data(players=[], hp=None, kills=None)
    assert parse_beat(line)["data"]["players"] == []


def test_beat_accepts_fractional_hp():
    assert parse_beat(beat_data(hp=87.5))["data"]["hp"] == 87.5


@pytest.mark.parametrize(
    "overrides",
    [
        {"age": True},
        {"age": None},
        {"players": "durand"},
        {"players": [1]},
        {"hp": True},
        {"kills": True},
        {"kills": 41.0},
    ],
)
def test_beat_rejects_bad_types(overrides):
    assert_rejected("bad_field", parse_beat, beat_data(**overrides))


def test_infinite_number_is_rejected():
    line = BEAT.replace("2.4657", "1e999")
    assert_rejected("bad_field", parse_beat, line)


def test_huge_integer_does_not_escape_as_overflow():
    """Il atteindrait la dérive, où sa soustraction à un flottant lèverait `OverflowError`."""
    line = BEAT.replace("2.4657", "1" + "0" * 400)
    assert_rejected("bad_field", parse_beat, line)


@pytest.mark.parametrize(
    "number",
    ["1e999", "-1e999", "1" + "0" * 400, "1e300", str(2**53 + 1)],
    ids=["infinity", "minus-infinity", "huge-int", "huge-float", "above-exact-integers"],
)
@pytest.mark.parametrize(
    "template",
    ['"extra":{}', '"nested":{{"list":[1,{}]}}'],
    ids=["field", "nested"],
)
def test_preserved_field_out_of_bounds_is_rejected(template, number):
    field = template.format(number)
    line = EVENT.replace('"origin":"world"', f'"origin":"world",{field}')
    assert_rejected("bad_field", parse_catalog_event, line)


def test_decoded_event_is_bounded_too():
    """Le rejeu repasse par `parse_event` avec un objet déjà décodé."""
    event = json.loads(EVENT)
    event["data"]["extra"] = float("inf")
    assert_rejected("bad_field", parse_catalog_event, event)


def test_preserved_field_too_deep_is_rejected():
    line = EVENT.replace('"origin":"world"', '"origin":"world","deep":' + "[" * 900 + "]" * 900)
    assert_rejected("bad_field", parse_catalog_event, line)


def test_preserved_numbers_within_bounds_stay_untouched():
    line = EVENT.replace('"origin":"world"', f'"origin":"world","big":{2**53},"low":-1.5e15')
    event = parse_catalog_event(line)
    assert (event["data"]["big"], event["data"]["low"]) == (2**53, -1.5e15)
    assert json.loads(dump_line(event)) == event


def test_parse_beat_refuses_other_kinds():
    assert_rejected("unknown_kind", parse_beat, EVENT)


@pytest.mark.parametrize(
    "overrides",
    [{"protocol": True}, {"hooks": {"OnZombieDead": 1}}, {"functions": []}, {"atoms": "zombie_kill"}],
)
def test_caps_rejects_bad_types(overrides):
    line = json.loads(CAPS)
    line["data"].update(overrides)
    assert_rejected("bad_field", parse_catalog_event, line)


def test_probe_spawn_command_from_runtime(runtime):
    command = {"v": 1, "seq": 1, "kind": "probe_spawn", **runtime["probe_spawn"]}
    assert parse_catalog_command(command)["origin"] == {"rule": "probe_spawn"}


def test_unknown_command_kind_is_left_to_the_mod():
    assert parse_catalog_command({**json.loads(COMMAND), "kind": "later"})["kind"] == "later"


def test_unknown_command_kind_has_no_required_parameter():
    raw = {"v": 1, "seq": 7, "kind": "later", "origin": {"rule": "kill_baseline"}}
    assert parse_catalog_command(raw)["kind"] == "later"


@pytest.mark.parametrize("kind", CATALOG["effects"])
def test_known_command_is_accepted_with_its_required_parameters(kind):
    assert parse_catalog_command(command_for(kind))["kind"] == kind


@pytest.mark.parametrize(
    "kind, field",
    [(kind, field) for kind, effect in CATALOG["effects"].items() for field in effect["required"]],
)
def test_known_command_requires_its_parameters(kind, field):
    raw = command_for(kind)
    del raw[field]
    assert_rejected("missing_field", parse_catalog_command, raw)


@pytest.mark.parametrize(
    "kind, field",
    [("horde_area", "radius"), ("noise", "radius"), ("weather_rain", "duration_s")],
)
@pytest.mark.parametrize("value", [True, "20", None, -1, float("inf")])
def test_radius_and_duration_are_strict_numbers(kind, field, value):
    assert_rejected("bad_field", parse_catalog_command, {**command_for(kind), field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"seq": True},
        {"seq": 0},
        {"kind": ""},
        {"n": True},
        {"n": 0},
        {"distance": [30]},
        {"distance": [60, 30]},
        {"distance": [True, 60]},
        {"warn_s": True},
        {"warn_s": -1},
        {"origin": "kill_baseline"},
        {"origin": {"rule": ""}},
        {"origin": {"rule": "kill_baseline", "boot": True}},
    ],
)
def test_command_rejects_bad_fields(overrides):
    assert_rejected("bad_field", parse_catalog_command, {**json.loads(COMMAND), **overrides})


@pytest.mark.parametrize(
    "items",
    [[], "Base.Bandage", [1], [{"item": "", "count": 1}], [{"item": "Base.Pills", "count": True}]],
)
def test_supply_items_rejected(items):
    assert_rejected("bad_field", parse_catalog_command, {**json.loads(SUPPLY), "items": items})


def test_supply_drop_must_carry_items():
    raw = json.loads(SUPPLY)
    del raw["items"]
    assert_rejected("missing_field", parse_catalog_command, raw)


def test_ack_and_session_before_any_command():
    assert parse_ack('{"v":1,"boot":1,"seq":0}')["seq"] == 0
    assert parse_session({**json.loads(SESSION), "boot": 1, "cmd_floor": 0})["cmd_floor"] == 0


@pytest.mark.parametrize("overrides", [{"boot": 0}, {"boot": True}, {"seq": -1}, {"seq": "7"}])
def test_ack_rejects_bad_fields(overrides):
    assert_rejected("bad_field", parse_ack, {**json.loads(ACK), **overrides})


@pytest.mark.parametrize(
    "overrides",
    [{"match": ""}, {"match": 8}, {"side": "C"}, {"side": None}, {"side": ["A"]}, {"cmd_floor": True}],
)
def test_session_rejects_bad_fields(overrides):
    assert_rejected("bad_field", parse_session, {**json.loads(SESSION), **overrides})


def test_parse_leaves_its_input_untouched():
    raw = json.loads(CAPS)
    snapshot = json.loads(CAPS)
    parse_catalog_event(raw)
    assert raw == snapshot


@pytest.mark.parametrize("line", [SESSION, EVENT, BEAT, CAPS, COMMAND, SUPPLY, ACK])
def test_dump_line_matches_contract_bytes(line):
    assert dump_line(json.loads(line)) == line.encode("utf-8") + b"\n"


def test_dump_line_is_utf8_on_a_single_line():
    dumped = dump_line({"v": 1, "name": "Zoé\nB"})
    assert dumped == '{"v":1,"name":"Zoé\\nB"}\n'.encode("utf-8")
    assert dumped.count(b"\n") == 1


def test_dump_line_refuses_non_json_numbers():
    with pytest.raises(ValueError):
        dump_line({"v": float("nan")})
