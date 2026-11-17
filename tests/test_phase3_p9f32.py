"""P9F32 : l'anti-triche des coups journalise au lieu de bannir, et le service l'écrit.

`zombie/network/anticheats/AntiCheat$Policy` : Ban = 1, Kick = 2, Log = 3, Disabled = 4. Le
service écrit `AntiCheatHit=3` dans les ini générés, le contrat refuse toute autre valeur, et
une détection `action="Log"` du serveur est reprise une fois par match et par connexion dans le
journal du service. Chaque test cite la mutation qui le fait échouer.
"""

from __future__ import annotations

from functools import partial

from test_contract import (  # noqa: F401
    RCON_SECRET,
    Lobby,
    alter_ini,
    check,
    conforming,
    table,
)
from test_preflight import zomboid_home  # noqa: F401
from test_snapshots import ENGINE_READY, engine_rig  # noqa: F401

from service.engine import anticheat_message
from service.servers import AntiCheatNotice, parse_anticheat, read_ini

PRESET = "demo"
LOG = "3"
BAN = "1"


def hit_line(connection: str, distance: str, counter: str, action: str = "Log") -> str:
    """Une ligne de `server-console.txt`, à la forme lue le 17/11, sur une seule ligne."""
    return (
        f'Anti-cheat="HitWeapon" is triggered for connection="{connection}" '
        'server-option="AntiCheatHit" reason="PlayerHitZombiePacket: '
        f'distance={distance} is greater than weapon range" counter={counter} '
        f'action="{action}" ping=0'
    )


def test_generated_inis_carry_anticheat_hit_log(engine_rig):
    """Les ini que le service écrit au lancement portent `AntiCheatHit=3` (Log), jamais Ban.

    Mutation : retirer `"AntiCheatHit": "3"` de `managed_ini` (`service/runtime.json`) laisse la
    clé absente des ini écrits — le serveur reprend alors son défaut, 1, c'est-à-dire Ban.
    """
    Lobby(engine_rig, PRESET)
    server_dir = engine_rig.zomboid / "Server"

    for side in ("A", "B"):
        assert read_ini(server_dir / f"duel{side}.ini").get("AntiCheatHit") == LOG


def test_contract_refuses_anticheat_hit_ban(zomboid_home, runtime, table, conforming):
    """Une ini à `AntiCheatHit=1` est refusée, la clé et la valeur trouvée nommées.

    Mutation : retirer `AntiCheatHit` des clés attendues de `_check_ini_contract`
    (`service/servers.py`) rend le contrat muet sur cette ini.
    """
    assert check(zomboid_home, table, runtime, conforming) == []

    alter_ini(zomboid_home, "B", AntiCheatHit=BAN)
    failures = check(zomboid_home, table, runtime, conforming)

    assert len(failures) == 1
    assert "AntiCheatHit sur B" in failures[0]
    assert repr(BAN) in failures[0]
    assert repr(LOG) in failures[0]
    assert RCON_SECRET not in failures[0]


def test_parse_reads_log_lines_only():
    """Seule `action="Log"` est une détection reprise ; la distance est lue telle quelle."""
    assert parse_anticheat(hit_line("chpo", "6.687416", "1/2")) == AntiCheatNotice(
        "HitWeapon", "chpo", 6.687416
    )
    assert parse_anticheat(hit_line("chpo", "6.687416", "1/2", action="Ban")) is None
    assert parse_anticheat("server is listening on port 17261") is None
    message = anticheat_message("A", AntiCheatNotice("HitWeapon", "chpo", 6.687416))
    assert "désynchronisation détectée sur chpo, distance 6,7" in message


def test_log_line_relayed_once_per_connection(engine_rig, monkeypatch):
    """Une détection `action="Log"` du serveur paraît une seule fois dans le journal du service.

    Les deux serveurs doubles impriment chacun trois détections pour `chpo` et une pour
    `lordryoo` : le journal en porte une par connexion, pas une par coup ni par serveur.

    Mutations : retirer le test `notice.connection in self._anticheat_seen` de
    `Match._relay_anticheat` (`service/engine.py`) répète la ligne de `chpo` ; retirer l'appel à
    `_relay_anticheat` de `_poll_out`, ou la lecture dans `ServerProcess._scan`, n'en écrit aucune.
    """
    lines = [
        *ENGINE_READY,
        hit_line("chpo", "6.687416", "1/2"),
        hit_line("chpo", "7.100000", "2/2"),
        hit_line("lordryoo", "9.355252", "1/2"),
        hit_line("chpo", "6.319267", "3/3"),
    ]
    monkeypatch.setattr(engine_rig, "build", partial(engine_rig.build, lines=lines))
    lobby = Lobby(engine_rig, PRESET)

    def relayed(state: dict, connection: str) -> list[str]:
        needle = f"désynchronisation détectée sur {connection},"
        return [entry["msg"] for entry in state["logs"] if needle in entry["msg"]]

    lobby.wait_for(
        lambda s: relayed(s, "chpo") and relayed(s, "lordryoo"),
        expected="détections de chpo et de lordryoo reprises",
    )
    lobby.sync_pump()
    lobby.sync_pump()
    state = lobby.state()

    [chpo] = relayed(state, "chpo")
    [lordryoo] = relayed(state, "lordryoo")
    assert "distance 6,7" in chpo
    assert "distance 9,4" in lordryoo
    assert all(
        entry["level"] == "warn" for entry in state["logs"] if "anti-triche" in entry["msg"]
    )
