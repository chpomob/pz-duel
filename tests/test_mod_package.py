"""Contrôles statiques du paquet `duelcore`.

Aucun de ces tests ne prouve le comportement du Lua : ils lisent le texte du mod. Le
comportement est prouvé sur le vrai jeu, en intégration.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "mods" / "duelcore"
VERSION_DIR = MOD / "42.21"
LUA = VERSION_DIR / "media" / "lua" / "server" / "DuelCore.lua"
PRESETS = sorted((ROOT / "service" / "presets").glob("*.json"))
CATALOG = json.loads((ROOT / "service" / "effects.json").read_text(encoding="utf-8"))


def lua_source() -> str:
    return LUA.read_text(encoding="utf-8")


def lua_code() -> str:
    """Le source sans ses commentaires de ligne, pour ne juger que les appels."""
    return re.sub(r"--.*", "", lua_source())


def function_body(name: str) -> str:
    """Corps d'une fonction de premier niveau, jusqu'à son `end` en colonne 0."""
    match = re.search(
        rf"^(?:local function {re.escape(name)}\b|{re.escape(name)} = function\b).*?^end$",
        lua_code(),
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"fonction absente du mod : {name}"
    return match.group(0)


def table_body(name: str) -> str:
    match = re.search(rf"^local {name} = \{{.*?^\}}$", lua_source(), re.MULTILINE | re.DOTALL)
    assert match, f"table absente du mod : {name}"
    return match.group(0)


def test_layout_and_forbidden_calls() -> None:
    files = sorted(path.relative_to(MOD).as_posix() for path in MOD.rglob("*") if path.is_file())
    assert files == [
        "42.21/media/lua/server/DuelCore.lua",
        "42.21/mod.info",
        "mod.info",
    ]
    assert "versionMin=42.21.0" in (VERSION_DIR / "mod.info").read_text(encoding="utf-8").splitlines()
    for info in (MOD / "mod.info", VERSION_DIR / "mod.info"):
        assert "id=duelcore" in info.read_text(encoding="utf-8").splitlines()

    code = lua_code()
    assert not re.search(r"(?<![\w.:])(os|io)\s*\.", code)
    assert not re.search(r"\b(require|dofile|loadfile)\b", code)


def test_no_balance_and_effects_covered() -> None:
    source = lua_source()
    forbidden: set[str] = set()
    for path in PRESETS:
        preset = json.loads(path.read_text(encoding="utf-8"))
        forbidden |= {rule["id"] for rule in preset["rules"]}
        forbidden |= set(preset["budget"]) | set(preset["score"])
    assert forbidden
    found = {name for name in forbidden if re.search(rf"\b{re.escape(name)}\b", source)}
    assert not found

    for kind in CATALOG["effects"]:
        assert re.search(rf"^handlers\.{kind} = ", source, re.MULTILINE), kind
        assert re.search(rf"^\s+{kind} = \"", table_body("EFFECT_FUNCTIONS"), re.MULTILINE), kind
    for atom in CATALOG["atoms"]:
        assert f'emit("{atom}"' in source, atom
        assert f'addAtom("{atom}"' in source, atom
    assert 'emit("player_death"' in source


def test_supply_content_not_in_mod() -> None:
    source = lua_source()
    assert "Base." not in source
    for item in CATALOG["supply_items"]:
        assert item not in source
        assert item.split(".", 1)[1] not in source

    body = function_body("handlers.supply_drop")
    assert "cmd.items" in body
    assert "unknown_item" in body
    assert body.index("instanceItem") < body.index("AddWorldInventoryItem")
    assert body.index("unknown_item") < body.index("AddWorldInventoryItem")


def test_command_reader_guards() -> None:
    reader = function_body("readCommandLines")
    assert ":read()" in reader
    assert re.search(r"==\s*10\b", reader)
    assert re.search(r"<\s*0\b", reader)
    assert "readLine" not in lua_source()

    code = lua_code()
    assert code.count("IN_FILE") == 2, "seule readCommandLines ouvre `in`"
    assert "getFileReader(IN_FILE" in reader

    accept = function_body("acceptCommandLine")
    assert re.search(r"seq\s*<=\s*lastCmdSeq", accept)
    assert re.search(r"seq\s*<=\s*session\.cmd_floor", accept)
    assert re.search(r"cmd\.v\s*~=\s*PROTOCOL", accept)
    guard = accept.index("session.cmd_floor")
    advance = accept.index("lastCmdSeq = cmd.seq")
    enqueue = accept.index("pending[#pending + 1]")
    assert guard < advance < enqueue


def test_hook_proof_is_a_wound_on_the_player() -> None:
    """`hook_seen` d'une blessure suit son `damage_taken`, jamais une réception quelconque."""
    code = lua_code()
    assert re.search(r'register\("OnPlayerGetDamage", onPlayerGetDamage, true\)', code)
    assert re.search(r'register\("OnBeingHitByZombie", onBeingHitByZombie, true\)', code)
    assert len(re.findall(r'^register\(.*, true\)$', code, re.MULTILINE)) == 2

    wrapper = function_body("register")
    assert re.search(r"if not selfProved then noteHook\(name\) end", wrapper)
    assert wrapper.index("noteHook(name)") < wrapper.index("handler(...)")

    assert "isPlayer(character) and WOUND_TYPES[damageType]" in function_body("onPlayerGetDamage")
    handler = function_body("reportWound")
    assert handler.index('emit("damage_taken"') < handler.index("noteHook(hook)")


def test_caps_publish_registration_not_proof() -> None:
    caps = function_body("emitCaps")
    assert "hooks = hooks" in caps
    # Le relevé de santé tourne dans `OnTick` : l'atome ne dépend pas d'un hook à prouver.
    assert 'addAtom("damage_taken", hooks.OnTick)' in caps
    assert "seenHooks" not in caps
    assert "seenHooks" not in function_body("register")


def test_last_attacker_is_purged() -> None:
    """Un zombie touché qui ne meurt jamais ne reste pas dans la table."""
    dead = function_body("onZombieDead")
    assert "lastAttacker[zombie] = nil" in dead
    assert "freshAuthor(hit, zombie" in dead

    hit = function_body("onHitZombie")
    assert re.search(r"at = getTimestampMs\(\)", hit)

    purge = function_body("purgeAttackers")
    assert re.search(r"now - hit\.at < ATTACKER_TTL_MS", purge)
    assert "hit.at >= ATTACKER_TTL_MS" in function_body("freshAuthor")
    assert re.search(r"lastAttacker = kept", purge)

    step = function_body("step")
    assert "purgeAttackers(now)" in step
    assert step.index("purgeAttackers(now)") < step.index("if not session then")
    # Kahlua n'honore pas `__mode` : la purge ne repose pas sur une table faible.
    assert "__mode" not in lua_code()


def test_command_seq_is_bounded_before_any_advance() -> None:
    """`1e999` décode en infini : ni `lastCmdSeq` ni `cmd_floor` ne doivent le prendre."""
    integer = function_body("isInteger")
    assert re.search(r"value > -math\.huge and value < math\.huge", integer)
    assert integer.index("math.huge") < integer.index("math.floor")

    bounded = function_body("isCmdSeq")
    assert "isInteger(value)" in bounded
    assert "value >= lowest" in bounded
    assert "value <= MAX_CMD_SEQ" in bounded
    limit = re.search(r"^local MAX_CMD_SEQ = (\d+)$", lua_code(), re.MULTILINE)
    assert limit and int(limit.group(1)) < 9e15, "un `seq` fini immense bloquerait les suivants"

    accept = function_body("acceptCommandLine")
    check = accept.index("isCmdSeq(cmd.seq, 1)")
    refusal = accept.index("logError(")
    assert check < refusal < accept.index("lastCmdSeq = cmd.seq")
    assert "error(" not in accept.replace("logError(", "")

    session = function_body("openSession")
    check = session.index("isCmdSeq(loaded.cmd_floor, 0)")
    assert check < session.index("logError(") < session.index("session = loaded")


def test_number_decoder_follows_json_grammar() -> None:
    """Un `seq` hors grammaire ne doit pas atteindre `tonumber`, qui l'accepterait."""
    body = function_body("decodeNumber")
    assert '"^%d+"' in body
    assert re.search(r"#whole\s*>\s*1", body), "zéro de tête non rejeté"
    assert '"^%.%d+"' in body, "un point exige des chiffres"
    assert '"^[eE][+-]?%d+"' in body, "un exposant exige des chiffres"
    assert "%.?" not in body
    assert "[eE]?" not in body
