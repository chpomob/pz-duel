"""Contrat statique : chaque écart, relu sur le disque ou dans `caps`, est refusé et nommé."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service.rules import load_effects, load_table
from service.servers import verify_contract, write_ini, write_sandbox

PRESET = Path(__file__).resolve().parent.parent / "service" / "presets" / "demo.json"
BUILD = "42.21.0"
RCON_SECRET = "rcon-marker"
SANDBOX = "SandboxVars = {\n    DayLength = 4,\n    StartYear = 3,\n    StartMonth = 1,\n" + (
    "    StartDay = 2,\n    StartTime = 5,\n}\n"
)


def caps_data(table: dict) -> dict:
    return {
        "build": BUILD,
        "mod": "0.1.0",
        "protocol": 1,
        "hooks": {},
        "functions": {},
        "atoms": list(table["atoms"]),
        "effects": [effect["kind"] for rule in table["rules"] for effect in rule["effects"]],
    }


@pytest.fixture
def table(runtime):
    raw = json.loads(PRESET.read_text(encoding="utf-8"))
    return load_table(raw, load_effects(), runtime["limits"], file_stem="demo")


@pytest.fixture
def conforming(zomboid_home, runtime, table):
    for side, ports in runtime["ports"].items():
        managed = {
            **runtime["managed_ini"],
            "Mods": "duelcore",
            "DefaultPort": ports["game"],
            "UDPPort": ports["udp"],
            "RCONPort": ports["rcon"],
            "RCONPassword": RCON_SECRET,
        }
        write_ini(zomboid_home / "Server" / f"duel{side}.ini", managed)
        sandbox = zomboid_home / "Server" / f"duel{side}_SandboxVars.lua"
        sandbox.write_text(SANDBOX, encoding="utf-8")
        write_sandbox(sandbox, table["day_length"], runtime["sandbox_start"])
    return {"A": caps_data(table), "B": caps_data(table)}


def check(zomboid_home, table, runtime, caps) -> list[str]:
    builds = {"A": BUILD, "B": BUILD}
    return verify_contract(zomboid_home, table, table["day_length"], runtime, caps, builds)


def alter_ini(zomboid: Path, side: str, **values) -> None:
    write_ini(zomboid / "Server" / f"duel{side}.ini", values)


def mods(zomboid, runtime, caps):
    alter_ini(zomboid, "B", Mods="autre")


def pause_empty(zomboid, runtime, caps):
    alter_ini(zomboid, "A", PauseEmpty="true")


def rcon_port(zomboid, runtime, caps):
    alter_ini(zomboid, "B", RCONPort=runtime["ports"]["A"]["rcon"])


def day_length(zomboid, runtime, caps):
    sandbox = zomboid / "Server" / "duelB_SandboxVars.lua"
    write_sandbox(sandbox, 7, runtime["sandbox_start"])


def caps_mod(zomboid, runtime, caps):
    caps["B"]["mod"] = "0.2.0"


def caps_effect(zomboid, runtime, caps):
    caps["A"]["effects"].remove("spawn_pop")


@pytest.mark.parametrize(
    "alter, key",
    [
        (mods, "Mods"),
        (pause_empty, "PauseEmpty"),
        (rcon_port, "RCONPort"),
        (day_length, "DayLength"),
        (caps_mod, "mod"),
        (caps_effect, "spawn_pop"),
    ],
)
def test_each_mismatch_refused(zomboid_home, runtime, table, conforming, alter, key):
    assert check(zomboid_home, table, runtime, conforming) == []

    alter(zomboid_home, runtime, conforming)
    failures = check(zomboid_home, table, runtime, conforming)

    assert len(failures) == 1
    assert key in failures[0]
    assert RCON_SECRET not in failures[0]


def test_empty_caps_refuses_both_camps(zomboid_home, table, runtime):
    """Sans entrée ni fichier, rien n'est certifié : l'absence se juge camp par camp."""
    failures = check(zomboid_home, table, runtime, {})

    for side in ("A", "B"):
        assert any(f"réglages de {side} illisibles" in failure for failure in failures)
        assert any(f"caps de {side} non reçu" in failure for failure in failures)


def test_absent_camp_is_still_checked(zomboid_home, table, runtime, conforming):
    alter_ini(zomboid_home, "B", Mods="autre")

    failures = check(zomboid_home, table, runtime, {"A": conforming["A"]})

    assert any("caps de B non reçu" in failure for failure in failures)
    assert any("Mods sur B" in failure for failure in failures)


def test_absent_camp_files_refused(zomboid_home, table, runtime, conforming):
    (zomboid_home / "Server" / "duelB.ini").unlink()

    failures = check(zomboid_home, table, runtime, {"A": conforming["A"], "B": conforming["B"]})

    assert len(failures) == 1
    assert "réglages de B illisibles" in failures[0]
