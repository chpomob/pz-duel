"""Le double `mini_lua` : sa surface couvre celle du mod, et sa sémantique suit Lua 5.1."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from doubles.mini_lua import Lua, Table, tokenize

MOD = Path(__file__).resolve().parent.parent / "mods/duelcore/42.21/media/lua/server/DuelCore.lua"
LIBRARIES = ("table", "math", "string", "os")
BASE_FUNCTIONS = {
    "assert", "collectgarbage", "dofile", "error", "getfenv", "getmetatable", "ipairs", "load",
    "loadfile", "loadstring", "module", "next", "pairs", "pcall", "print", "rawequal", "rawget",
    "rawset", "require", "select", "setfenv", "setmetatable", "tonumber", "tostring", "type",
    "unpack", "xpcall",
}  # fmt: skip
STRING_METHODS = {
    "byte", "char", "dump", "find", "format", "gmatch", "gsub", "len", "lower", "match", "rep",
    "reverse", "sub", "upper",
}  # fmt: skip
FIELD_ACCESS = (("op", "."), ("op", ":"))


def surface(source: str) -> tuple[set[str], set[str]]:
    """Rend les fonctions de bibliothèque appelées, et ce que le double ne saurait pas voir.

    Le balayage lit les jetons : un commentaire ou un texte qui cite `table.x` ne compte pas.
    Un alias (`local t = table`) cacherait ses appels, et `s:sub()` ne passe pas par `string.` :
    tous deux sont rendus comme angles morts, pour échouer au lieu de se taire.
    """
    tokens = tokenize(source)
    used: set[str] = set()
    blind: set[str] = set()
    for index, (kind, value) in enumerate(tokens):
        if kind != "name":
            continue
        before = tokens[index - 1] if index else None
        if before == ("op", ":") and value in STRING_METHODS:
            blind.add(f":{value}")
        if before in FIELD_ACCESS:
            continue
        if value in BASE_FUNCTIONS:
            used.add(value)
        if value in LIBRARIES:
            if tokens[index + 1] == ("op", ".") and tokens[index + 2][0] == "name":
                used.add(f"{value}.{tokens[index + 2][1]}")
            else:
                blind.add(value)
    return used, blind


def provided(name: str) -> bool:
    names = Lua().scope.names
    library, _, member = name.partition(".")
    if not member:
        return names.get(name) is not None
    return isinstance(names.get(library), Table) and names[library].get(member) is not None


def lua(source: str) -> list[Any]:
    return Lua().run(source)


def test_double_provides_everything_the_mod_calls():
    used, blind = surface(MOD.read_text(encoding="utf-8"))
    assert {"table.remove", "math.floor", "string.sub", "pcall"} <= used, "balayage aveugle"
    assert sorted(name for name in used if not provided(name)) == []
    assert blind == set()


def test_scan_sees_what_it_must_and_ignores_the_rest():
    source = """
        -- table.commentaire
        local s = "table.texte"
        table.insert(x, os.time())
        return obj.table, obj:size(), select(1, s)
    """
    used, blind = surface(source)
    assert used == {"table.insert", "os.time", "select"}
    assert blind == set()
    assert not provided("table.insert")


@pytest.mark.parametrize(
    ("source", "name"),
    [("local t = table", "table"), ("local s = x:sub(1)", ":sub"), ("f(math)", "math")],
)
def test_scan_reports_its_blind_spots(source, name):
    assert surface(source)[1] == {name}


@pytest.mark.parametrize(
    ("call", "removed", "left"),
    [
        ("table.remove(t, 2)", "b", ["a", "c"]),
        ("table.remove(t, 1)", "a", ["b", "c"]),
        ("table.remove(t, 3)", "c", ["a", "b"]),
        ("table.remove(t)", "c", ["a", "b"]),
        ('table.remove(t, "2")', "b", ["a", "c"]),
        ("table.remove(t, 0)", None, ["a", "b", "c"]),
        ("table.remove(t, 4)", None, ["a", "b", "c"]),
    ],
)
def test_table_remove_shifts_and_returns_the_value(call, removed, left):
    value, size, joined = lua(
        f"""local t = {{ "a", "b", "c" }}
        local value = {call}
        return value, #t, table.concat(t, ",")"""
    )
    assert value == removed
    assert size == len(left)
    assert joined == ",".join(left)


def test_table_remove_keeps_false_and_clears_the_last_slot():
    value, tail = lua("local t = { false, 1 } local v = table.remove(t, 1) return v, t[2]")[:2]
    assert value is False
    assert tail is None


def test_table_remove_on_an_empty_table_returns_nothing():
    assert lua("return table.remove({})") == []


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("math.floor(2.7)", 2),
        ("math.floor(-2.5)", -3),
        ("math.floor(4)", 4),
        ('math.floor("3.7")', 3),
        ("math.ceil(2.1)", 3),
        ("math.ceil(-2.5)", -2),
        ("math.ceil(4)", 4),
        ("math.floor(1e300)", 1e300),
    ],
)
def test_rounding_gives_an_integer_value(expression, expected):
    (value,) = lua(f"return {expression}")
    assert value == expected
    assert isinstance(value, int) or abs(value) >= 2**53


@pytest.mark.parametrize("function", ["floor", "ceil"])
@pytest.mark.parametrize("sign", ["", "-"])
def test_rounding_keeps_infinity(function, sign):
    assert lua(f"return math.{function}({sign}math.huge) == {sign}math.huge") == [True]


@pytest.mark.parametrize("function", ["floor", "ceil"])
def test_rounding_keeps_nan(function):
    (value,) = lua(f"local nan = math.huge - math.huge return math.{function}(nan)")
    assert math.isnan(value)


@pytest.mark.parametrize("function", ["floor", "ceil", "sqrt", "sin", "cos", "rad"])
@pytest.mark.parametrize("value", ["nil", "true", '"abc"', "{}"])
def test_math_refuses_what_is_no_number_as_a_lua_error(function, value):
    ok, message = lua(f"return pcall(math.{function}, {value})")
    assert ok is False
    assert "number expected" in message


@pytest.mark.parametrize(
    "expression", ["math.sqrt(-1)", "math.sin(math.huge)", "math.cos(-math.huge)"]
)
def test_real_functions_give_nan_outside_their_domain(expression):
    (value,) = lua(f"return {expression}")
    assert math.isnan(value)


def test_real_functions_compute():
    root, turn, sine, cosine = lua(
        "return math.sqrt(9), math.rad(180), math.sin(0), math.cos(0)"
    )
    assert (root, sine, cosine) == (3, 0, 1)
    assert turn == pytest.approx(math.pi)
