"""Interprète Lua réduit : exécute le vrai `DuelCore.lua` sans le jeu ni dépendance.

Il couvre la syntaxe de Lua 5.1 et la seule bibliothèque que le mod appelle. Il ne prouve
rien de Kahlua ni des classes du jeu : les objets Java y sont des objets Python écrits par
le test. Les nombres sont ceux de Python, les chaînes aussi : `#` y compte des caractères.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from typing import Any

KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in",
    "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while",
}
TOKEN = re.compile(
    r"""(?P<space>\s+|--[^\n]*)
    |(?P<number>0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?)
    |(?P<name>[^\W\d]\w*)
    |(?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
    |(?P<op>\.\.\.|\.\.|==|~=|<=|>=|[-+*/%^\#<>=(){}\[\];:,.])""",
    re.VERBOSE,
)
ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
BINARY = {
    "or": 1, "and": 2, "<": 3, ">": 3, "<=": 3, ">=": 3, "~=": 3, "==": 3, "..": 4,
    "+": 5, "-": 5, "*": 6, "/": 6, "%": 6, "^": 8,
}
UNARY_PRIORITY = 7
RIGHT_ASSOCIATIVE = ("..", "^")


class LuaError(Exception):
    """Erreur `error()` du script ou de son exécution : `pcall` l'attrape."""


class _Break(Exception):
    pass


class _Return(Exception):
    def __init__(self, values: list[Any]) -> None:
        self.values = values


class Table:
    def __init__(self) -> None:
        self.items: dict[Any, Any] = {}

    def get(self, key: Any) -> Any:
        return self.items.get(key)

    def set(self, key: Any, value: Any) -> None:
        if value is None:
            self.items.pop(key, None)
        else:
            self.items[key] = value

    def length(self) -> int:
        size = 0
        while size + 1 in self.items:
            size += 1
        return size


def _unescape(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        if code.isdigit():
            return chr(int(code))
        return ESCAPES.get(code, code)

    return re.sub(r"\\(\d{1,3}|.)", replace, text[1:-1], flags=re.DOTALL)


def _tokens(source: str) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    index = 0
    while index < len(source):
        match = TOKEN.match(source, index)
        if match is None:
            raise LuaError(f"caractère inattendu à l'offset {index}")
        index = match.end()
        kind, text = match.lastgroup, match.group()
        if kind == "space":
            continue
        if kind == "number":
            found.append(("number", int(text, 16) if text[:2] in ("0x", "0X") else _number(text)))
        elif kind == "string":
            found.append(("string", _unescape(text)))
        elif kind == "name" and text not in KEYWORDS:
            found.append(("name", text))
        else:
            found.append(("op", text))
    found.append(("eof", None))
    return found


def _number(text: str) -> int | float:
    value = float(text)
    return int(value) if value.is_integer() and abs(value) < 2**53 else value


class _Parser:
    """Rend l'arbre sous forme de tuples, dont le premier élément nomme le nœud."""

    def __init__(self, source: str) -> None:
        self.tokens = _tokens(source)
        self.at = 0

    def peek(self, text: str) -> bool:
        return self.tokens[self.at] == ("op", text)

    def accept(self, text: str) -> bool:
        if self.peek(text):
            self.at += 1
            return True
        return False

    def expect(self, text: str) -> None:
        if not self.accept(text):
            raise LuaError(f"`{text}` attendu, trouvé {self.tokens[self.at]}")

    def name(self) -> str:
        kind, value = self.tokens[self.at]
        if kind != "name":
            raise LuaError(f"nom attendu, trouvé {self.tokens[self.at]}")
        self.at += 1
        return value

    def block(self) -> list[tuple]:
        statements = []
        while not any(self.peek(word) for word in ("end", "else", "elseif", "until")):
            if self.tokens[self.at][0] == "eof":
                break
            if self.accept("return"):
                values = []
                if not self._block_ends():
                    values = self.expressions()
                self.accept(";")
                statements.append(("return", values))
                break
            if self.accept("break"):
                statements.append(("break",))
                continue
            statements.append(self.statement())
            self.accept(";")
        return statements

    def _block_ends(self) -> bool:
        return self.tokens[self.at][0] == "eof" or any(
            self.peek(word) for word in ("end", "else", "elseif", "until", ";")
        )

    def statement(self) -> tuple:
        if self.accept("if"):
            return self._if()
        if self.accept("while"):
            test = self.expression()
            self.expect("do")
            body = self.block()
            self.expect("end")
            return ("while", test, body)
        if self.accept("repeat"):
            body = self.block()
            self.expect("until")
            return ("repeat", body, self.expression())
        if self.accept("do"):
            body = self.block()
            self.expect("end")
            return ("do", body)
        if self.accept("for"):
            return self._for()
        if self.accept("function"):
            target: tuple = ("name", self.name())
            method = False
            while self.peek(".") or self.peek(":"):
                method = self.peek(":")
                self.at += 1
                target = ("index", target, ("const", self.name()))
            return ("assign", [target], [self.function(method)])
        if self.accept("local"):
            if self.accept("function"):
                name = self.name()
                return ("localfunction", name, self.function(False))
            names = [self.name()]
            while self.accept(","):
                names.append(self.name())
            return ("local", names, self.expressions() if self.accept("=") else [])
        first = self.suffixed()
        if self.peek("=") or self.peek(","):
            targets = [first]
            while self.accept(","):
                targets.append(self.suffixed())
            self.expect("=")
            return ("assign", targets, self.expressions())
        return ("call", first)

    def _if(self) -> tuple:
        branches = []
        test = self.expression()
        self.expect("then")
        branches.append((test, self.block()))
        otherwise: list[tuple] = []
        while True:
            if self.accept("elseif"):
                test = self.expression()
                self.expect("then")
                branches.append((test, self.block()))
            elif self.accept("else"):
                otherwise = self.block()
                self.expect("end")
                break
            else:
                self.expect("end")
                break
        return ("if", branches, otherwise)

    def _for(self) -> tuple:
        names = [self.name()]
        if self.accept("="):
            start = self.expression()
            self.expect(",")
            stop = self.expression()
            step = self.expression() if self.accept(",") else ("const", 1)
            self.expect("do")
            body = self.block()
            self.expect("end")
            return ("fornum", names[0], start, stop, step, body)
        while self.accept(","):
            names.append(self.name())
        self.expect("in")
        values = self.expressions()
        self.expect("do")
        body = self.block()
        self.expect("end")
        return ("forin", names, values, body)

    def function(self, method: bool) -> tuple:
        self.expect("(")
        names = ["self"] if method else []
        vararg = False
        while not self.peek(")"):
            if self.accept("..."):
                vararg = True
            else:
                names.append(self.name())
            if not self.accept(","):
                break
        self.expect(")")
        body = self.block()
        self.expect("end")
        return ("function", names, vararg, body)

    def expressions(self) -> list[tuple]:
        values = [self.expression()]
        while self.accept(","):
            values.append(self.expression())
        return values

    def expression(self, limit: int = 0) -> tuple:
        if self.accept("not"):
            left: tuple = ("unary", "not", self.expression(UNARY_PRIORITY))
        elif self.accept("-"):
            left = ("unary", "-", self.expression(UNARY_PRIORITY))
        elif self.accept("#"):
            left = ("unary", "#", self.expression(UNARY_PRIORITY))
        else:
            left = self.simple()
        while True:
            kind, value = self.tokens[self.at]
            priority = BINARY.get(value) if kind == "op" else None
            if priority is None or priority <= limit:
                return left
            self.at += 1
            right = self.expression(priority - 1 if value in RIGHT_ASSOCIATIVE else priority)
            left = ("binary", value, left, right)

    def simple(self) -> tuple:
        kind, value = self.tokens[self.at]
        if kind in ("number", "string"):
            self.at += 1
            return ("const", value)
        for word, constant in (("nil", None), ("true", True), ("false", False)):
            if self.accept(word):
                return ("const", constant)
        if self.accept("..."):
            return ("vararg",)
        if self.accept("function"):
            return self.function(False)
        if self.peek("{"):
            return self.table()
        return self.suffixed()

    def table(self) -> tuple:
        self.expect("{")
        fields: list[tuple] = []
        while not self.peek("}"):
            if self.accept("["):
                key = self.expression()
                self.expect("]")
                self.expect("=")
                fields.append(("pair", key, self.expression()))
            elif self.tokens[self.at][0] == "name" and self.tokens[self.at + 1] == ("op", "="):
                key = ("const", self.name())
                self.expect("=")
                fields.append(("pair", key, self.expression()))
            else:
                fields.append(("item", self.expression()))
            if not (self.accept(",") or self.accept(";")):
                break
        self.expect("}")
        return ("table", fields)

    def suffixed(self) -> tuple:
        if self.accept("("):
            node: tuple = ("paren", self.expression())
            self.expect(")")
        else:
            node = ("name", self.name())
        while True:
            if self.accept("."):
                node = ("index", node, ("const", self.name()))
            elif self.accept("["):
                key = self.expression()
                self.expect("]")
                node = ("index", node, key)
            elif self.accept(":"):
                method = self.name()
                node = ("method", node, method, self.arguments())
            elif self.peek("(") or self.peek("{") or self.tokens[self.at][0] == "string":
                node = ("invoke", node, self.arguments())
            else:
                return node

    def arguments(self) -> list[tuple]:
        if self.peek("{"):
            return [self.table()]
        if self.tokens[self.at][0] == "string":
            return [self.simple()]
        self.expect("(")
        values = [] if self.peek(")") else self.expressions()
        self.expect(")")
        return values


class _Scope:
    def __init__(self, parent: _Scope | None = None) -> None:
        self.names: dict[str, Any] = {}
        self.parent = parent

    def owner(self, name: str) -> _Scope | None:
        scope: _Scope | None = self
        while scope is not None and name not in scope.names:
            scope = scope.parent
        return scope


def _truthy(value: Any) -> bool:
    return value is not None and value is not False


def _text(value: Any) -> str:
    if value is None:
        return "nil"
    if value is True or value is False:
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _lua_pattern(pattern: str) -> str:
    """Traduit les motifs du mod : classes `%d` et `%x`, échappements, ensembles, `+` et `?`."""
    classes = {"d": r"\d", "x": "[0-9a-fA-F]", "s": r"\s", "a": "[A-Za-z]", "w": r"[A-Za-z0-9]"}
    out, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if char == "%":
            index += 1
            out.append(classes.get(pattern[index], re.escape(pattern[index])))
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _match(text: str, pattern: str, start: int = 1) -> list[Any]:
    anchored = pattern.startswith("^")
    regex = re.compile(_lua_pattern(pattern[1:] if anchored else pattern))
    found = regex.match(text, start - 1) if anchored else regex.search(text, start - 1)
    if found is None:
        return [None]
    return list(found.groups()) or [found.group()]


def _sub(text: str, first: int, last: int = -1) -> str:
    size = len(text)
    first = size + first + 1 if first < 0 else first
    last = size + last + 1 if last < 0 else last
    return text[max(first, 1) - 1 : max(last, 0)]


def _tonumber(value: Any, base: int = 10) -> Any:
    if isinstance(value, (int, float)):
        return value
    try:
        return int(value, base) if base != 10 else _number(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _type(value: Any) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Table):
        return "table"
    return "function" if callable(value) else "userdata"


class Lua:
    """Un état Lua : `globals` reçoit les fonctions et objets du jeu, avant `run`."""

    def __init__(self, host: dict[str, Any] | None = None) -> None:
        self.scope = _Scope()
        library = {
            "math": self._library(floor=lambda v: math.floor(v), huge=math.inf, random=lambda *_: 1),
            "string": self._library(
                char=lambda *codes: "".join(map(chr, codes)),
                format=lambda form, *values: form % values,
                match=_match,
                sub=_sub,
            ),
            "table": self._library(
                concat=lambda table, sep="": sep.join(
                    _text(table.get(i)) for i in range(1, table.length() + 1)
                ),
            ),
            "error": self._error,
            "ipairs": self._ipairs,
            "pairs": self._pairs,
            "pcall": self._pcall,
            "print": lambda *_: [],
            "tonumber": _tonumber,
            "tostring": _text,
            "type": _type,
        }
        self.scope.names.update(library)
        self.scope.names.update(host or {})
        self.scope.names["_G"] = self._library(**self.scope.names)

    @staticmethod
    def _library(**entries: Any) -> Table:
        table = Table()
        for key, value in entries.items():
            table.set(key, value)
        return table

    @staticmethod
    def _error(message: Any = None, *_: Any) -> None:
        raise LuaError(_text(message))

    @staticmethod
    def _pairs(table: Table) -> list[Any]:
        entries = iter(list(table.items.items()))

        def step(*_: Any) -> list[Any]:
            return list(next(entries, (None, None)))

        return [step, table, None]

    @staticmethod
    def _ipairs(table: Table) -> list[Any]:
        def step(_: Any, index: int) -> list[Any]:
            value = table.get(index + 1)
            return [None, None] if value is None else [index + 1, value]

        return [step, table, 0]

    def _pcall(self, function: Callable[..., Any], *args: Any) -> list[Any]:
        try:
            return [True, *self.call(function, list(args))]
        except (LuaError, AttributeError, TypeError, KeyError, ValueError) as exc:
            return [False, str(exc)]

    def run(self, source: str) -> list[Any]:
        """Exécute un fragment dans une portée fille ; `return` en tête rend ses valeurs."""
        try:
            self._block(_Parser(source).block(), _Scope(self.scope), [])
        except _Return as result:
            return result.values
        return []

    @staticmethod
    def call(function: Callable[..., Any], args: Sequence[Any]) -> list[Any]:
        if not callable(function):
            raise LuaError(f"appel d'une valeur non fonction : {_text(function)}")
        result = function(*args)
        if isinstance(result, list):
            return result
        return [] if result is None else [result]

    def _block(self, statements: list[tuple], scope: _Scope, varargs: list[Any]) -> None:
        for statement in statements:
            getattr(self, f"_do_{statement[0]}")(statement, scope, varargs)

    def _do_return(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        raise _Return(self._values(node[1], scope, varargs))

    def _do_break(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        raise _Break

    def _do_call(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        self._multi(node[1], scope, varargs)

    def _do_do(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        self._block(node[1], _Scope(scope), varargs)

    def _do_local(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        values = self._values(node[2], scope, varargs)
        for index, name in enumerate(node[1]):
            scope.names[name] = values[index] if index < len(values) else None

    def _do_localfunction(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        scope.names[node[1]] = None
        scope.names[node[1]] = self._eval(node[2], scope, varargs)

    def _do_assign(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        values = self._values(node[2], scope, varargs)
        for index, target in enumerate(node[1]):
            value = values[index] if index < len(values) else None
            if target[0] == "name":
                owner = scope.owner(target[1]) or self._root()
                owner.names[target[1]] = value
            else:
                base = self._eval(target[1], scope, varargs)
                key = self._eval(target[2], scope, varargs)
                if isinstance(base, Table):
                    base.set(key, value)
                else:
                    setattr(base, key, value)

    def _root(self) -> _Scope:
        return self.scope

    def _do_if(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        for test, body in node[1]:
            if _truthy(self._eval(test, scope, varargs)):
                self._block(body, _Scope(scope), varargs)
                return
        self._block(node[2], _Scope(scope), varargs)

    def _do_while(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        try:
            while _truthy(self._eval(node[1], scope, varargs)):
                self._block(node[2], _Scope(scope), varargs)
        except _Break:
            pass

    def _do_repeat(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        try:
            while True:
                inner = _Scope(scope)
                self._block(node[1], inner, varargs)
                if _truthy(self._eval(node[2], inner, varargs)):
                    return
        except _Break:
            pass

    def _do_fornum(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        _, name, start, stop, step, body = node
        value = self._eval(start, scope, varargs)
        last = self._eval(stop, scope, varargs)
        increment = self._eval(step, scope, varargs)
        try:
            while value <= last if increment > 0 else value >= last:
                inner = _Scope(scope)
                inner.names[name] = value
                self._block(body, inner, varargs)
                value += increment
        except _Break:
            pass

    def _do_forin(self, node: tuple, scope: _Scope, varargs: list[Any]) -> None:
        _, names, sources, body = node
        function, state, control = (self._values(sources, scope, varargs) + [None] * 3)[:3]
        try:
            while True:
                values = self.call(function, [state, control])
                if not values or values[0] is None:
                    return
                control = values[0]
                inner = _Scope(scope)
                for index, name in enumerate(names):
                    inner.names[name] = values[index] if index < len(values) else None
                self._block(body, inner, varargs)
        except _Break:
            pass

    def _values(self, nodes: list[tuple], scope: _Scope, varargs: list[Any]) -> list[Any]:
        values: list[Any] = []
        for index, node in enumerate(nodes):
            if index == len(nodes) - 1:
                values.extend(self._multi(node, scope, varargs))
            else:
                values.append(self._eval(node, scope, varargs))
        return values

    def _multi(self, node: tuple, scope: _Scope, varargs: list[Any]) -> list[Any]:
        if node[0] == "vararg":
            return list(varargs)
        if node[0] == "invoke":
            function = self._eval(node[1], scope, varargs)
            return self.call(function, self._values(node[2], scope, varargs))
        if node[0] == "method":
            base = self._eval(node[1], scope, varargs)
            args = self._values(node[3], scope, varargs)
            if isinstance(base, Table):
                return self.call(base.get(node[2]), [base, *args])
            return self.call(getattr(base, node[2]), args)
        return [self._eval(node, scope, varargs)]

    def _eval(self, node: tuple, scope: _Scope, varargs: list[Any]) -> Any:
        kind = node[0]
        if kind == "const":
            return node[1]
        if kind == "name":
            owner = scope.owner(node[1])
            return None if owner is None else owner.names[node[1]]
        if kind == "paren":
            return self._eval(node[1], scope, varargs)
        if kind == "index":
            base = self._eval(node[1], scope, varargs)
            key = self._eval(node[2], scope, varargs)
            return base.get(key) if isinstance(base, Table) else getattr(base, key, None)
        if kind in ("invoke", "method", "vararg"):
            values = self._multi(node, scope, varargs)
            return values[0] if values else None
        if kind == "function":
            return self._closure(node, scope)
        if kind == "table":
            return self._table(node, scope, varargs)
        if kind == "unary":
            return self._unary(node, scope, varargs)
        return self._binary(node, scope, varargs)

    def _closure(self, node: tuple, scope: _Scope) -> Callable[..., list[Any]]:
        _, names, vararg, body = node

        def function(*args: Any) -> list[Any]:
            inner = _Scope(scope)
            for index, name in enumerate(names):
                inner.names[name] = args[index] if index < len(args) else None
            try:
                self._block(body, inner, list(args[len(names) :]) if vararg else [])
            except _Return as result:
                return result.values
            return []

        return function

    def _table(self, node: tuple, scope: _Scope, varargs: list[Any]) -> Table:
        table = Table()
        position = 1
        for index, field in enumerate(node[1]):
            if field[0] == "pair":
                table.set(self._eval(field[1], scope, varargs), self._eval(field[2], scope, varargs))
                continue
            last = index == len(node[1]) - 1
            values = self._multi(field[1], scope, varargs) if last else [
                self._eval(field[1], scope, varargs)
            ]
            for value in values:
                table.set(position, value)
                position += 1
        return table

    def _unary(self, node: tuple, scope: _Scope, varargs: list[Any]) -> Any:
        value = self._eval(node[2], scope, varargs)
        if node[1] == "not":
            return not _truthy(value)
        if node[1] == "-":
            return -value
        return value.length() if isinstance(value, Table) else len(value)

    def _binary(self, node: tuple, scope: _Scope, varargs: list[Any]) -> Any:
        _, op, left_node, right_node = node
        left = self._eval(left_node, scope, varargs)
        if op == "and":
            return self._eval(right_node, scope, varargs) if _truthy(left) else left
        if op == "or":
            return left if _truthy(left) else self._eval(right_node, scope, varargs)
        right = self._eval(right_node, scope, varargs)
        if op == "==":
            return _equal(left, right)
        if op == "~=":
            return not _equal(left, right)
        if op == "..":
            return _text(left) + _text(right)
        return OPERATIONS[op](left, right)


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return left == right


OPERATIONS: dict[str, Callable[[Any, Any], Any]] = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b,
    "%": lambda a, b: a % b,
    "^": lambda a, b: a**b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
}
