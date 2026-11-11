"""R8 : verdict sur journal (AC54, AC56, AC58, AC66).

Les journaux sont écrits à la main, ligne par ligne, avec les formes de `Journal` et de
`MatchCore` : `Log` ne calcule aucun compteur, chaque test vérifie des valeurs littérales.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from service import verdict
from service.journal import Journal, read_journal
from service.verdict import (
    attribution,
    combined_spawn,
    fallback,
    publish_capabilities,
    spawn_status,
)

ROOT = Path(__file__).resolve().parent.parent
MIN_KILLS = 100
PROBE_TIMEOUT_S = 30
T0_MS = 1_793_830_000_000
HOOK = "OnPlayerGetDamage"
CAPS = {
    "build": "42.21.0",
    "mod": "0.1.0",
    "protocol": 1,
    "hooks": {"OnZombieDead": True, HOOK: True, "OnEnterVehicle": False},
    "functions": {"addZombiesInOutfit": True},
    "atoms": ["zombie_kill"],
    "effects": ["spawn_pop"],
}


class Log:
    """Enregistrements d'un journal ; `seq` et `boot` suivent chaque serveur à part."""

    def __init__(self):
        self.records = []
        self.seqs = {"A": 0, "B": 0}
        self.boots = {"A": 1, "B": 1}
        self.ts_ms = T0_MS

    def add(self, type, data, side=None, mt=None):
        record = {
            "v": 1,
            "n": len(self.records) + 1,
            "ts_ms": self.ts_ms,
            "mt": mt,
            "type": type,
            "side": side,
            "data": data,
        }
        self.records.append(record)
        return record

    def event(self, kind, src, data, side="src", mt=1.0, type="event"):
        self.seqs[src] += 1
        side = src if side == "src" else side
        line = {
            "v": 1,
            "boot": self.boots[src],
            "seq": self.seqs[src],
            "t_ms": self.ts_ms,
            "src": src,
            "side": side,
            "kind": kind,
            "data": data,
        }
        self.add(type, line, side, mt)

    def beat(self, src, kills, mt=1.0):
        data = {"age": 1.0, "players": ["durand"], "hp": 87, "kills": kills}
        self.event("beat", src, data, mt=mt)

    def kills(self, src, count, side="src", mt=1.0):
        for _ in range(count):
            self.event("zombie_kill", src, {"origin": "world"}, side, mt)

    def restart(self, src):
        self.boots[src] += 1
        self.seqs[src] = 0

    def probe(self, side, seq):
        command = {"v": 1, "seq": seq, "kind": "probe_spawn", "n": 1}
        self.add("command", {"command": command, "latency_ms": None}, side)

    def result(self, side, seq, status="applied", **fields):
        data = {"seq": seq, "status": status, **fields}
        self.event("cmd_result", side, data, mt=None, type="cmd_result")

    def after(self, seconds):
        self.ts_ms += seconds * 1000


def measured(reference, attributed):
    """Un camp dont `kills` monte de `reference`, avec `attributed` kills attribués."""
    log = Log()
    log.beat("A", 0)
    log.kills("A", attributed)
    log.beat("A", reference)
    return attribution(log.records, MIN_KILLS)


def test_attribution_thresholds():
    assert measured(100, 100)["status"] == "ok"
    assert measured(100, 99)["status"] == "ko"
    assert measured(60, 50)["status"] == "insufficient"
    # Le seuil porte sur `référence`, même quand les deux compteurs concordent.
    assert measured(99, 99)["status"] == "insufficient"
    assert measured(100, 101)["status"] == "ko"


def test_attribution_interval():
    log = Log()
    log.beat("A", 10, mt=None)  # lobby : `kills` non nul, mais hors du match
    log.kills("A", 10, mt=None)
    log.kills("A", 10)
    log.beat("A", 7)
    log.kills("A", 100)
    log.beat("A", 107)
    log.kills("A", 5)
    result = attribution(log.records, MIN_KILLS)
    assert (result["reference"], result["attributed"], result["status"]) == (100, 100, "ok")

    # Le même `seq` tombe dans l'intervalle de B et avant le H0 tardif de A.
    log = Log()
    log.beat("B", 0)  # B : H0 à seq 1
    log.kills("A", 30)  # A : seq 1 à 30, avant son H0
    log.kills("B", 40)  # B : seq 2 à 41
    log.beat("A", 0)  # A : H0 à seq 31
    log.kills("A", 60)
    log.beat("A", 60)  # A : Hk à seq 92
    log.kills("B", 40)
    log.beat("B", 80)  # B : Hk à seq 82
    log.kills("B", 3)
    result = attribution(log.records, MIN_KILLS)
    assert result["by_side"] == {
        "A": {"reference": 60, "attributed": 60},
        "B": {"reference": 80, "attributed": 80},
    }
    assert (result["reference"], result["attributed"], result["status"]) == (140, 140, "ok")

    log = Log()
    log.beat("A", 0)
    log.kills("A", 99)
    log.kills("A", 1, side=None)
    log.beat("A", 100)
    result = attribution(log.records, MIN_KILLS)
    assert (result["reference"], result["attributed"], result["status"]) == (100, 99, "ko")


def test_attribution_bounds_are_strict_and_cross_boots():
    log = Log()
    log.beat("A", 50)
    log.kills("A", 40)
    log.seqs["A"] = 500
    log.beat("A", 90)
    log.restart("A")  # `seq` repart de 1, `kills` aussi : une baisse compte 0
    log.kills("A", 10)
    log.beat("A", 10)
    log.kills("A", 3)
    result = attribution(log.records, 1)
    assert (result["reference"], result["attributed"]) == (40, 50)

    # Une ligne qui porte le couple de H0 ou de Hk n'est pas dans l'intervalle.
    log = Log()
    log.beat("A", 0)
    log.beat("A", 2)
    h0, hk = log.records[0]["data"], log.records[1]["data"]
    for bound in (h0, hk):
        log.add("event", {**bound, "kind": "zombie_kill", "data": {}}, "A", 1.0)
    assert attribution(log.records, 1)["attributed"] == 0


def test_attribution_retains_only_match_beats_with_kills():
    log = Log()
    log.beat("A", 0, mt=0.0)  # `mt` 0 et `kills` 0 sont des valeurs : H0 est ici
    log.kills("A", 1)
    log.beat("A", None)
    log.kills("A", 1)
    log.beat("A", 2)
    log.add("match_end", {"result": {}, "score": {}}, mt=2.0)
    log.kills("A", 8)
    log.beat("A", 10)  # après `match_end`, même avec un `mt`
    result = attribution(log.records, 1)
    assert (result["reference"], result["attributed"]) == (2, 2)


def test_attribution_orders_beats_by_boot_and_seq():
    """Un tick trie par `t_ms` : le journal peut inverser deux lignes d'un même serveur."""
    log = Log()
    log.beat("A", 0)
    log.kills("A", 3)
    log.beat("A", 3)
    log.records[0], log.records[-1] = log.records[-1], log.records[0]
    result = attribution(log.records, 1)
    assert (result["reference"], result["attributed"]) == (3, 3)


def test_attribution_camp_below_two_beats_adds_nothing():
    log = Log()
    log.beat("A", 5)
    log.kills("A", 5)
    log.kills("B", 5)
    log.beat("B", 0)
    log.kills("B", 2)
    log.beat("B", 2)
    result = attribution(log.records, 1)
    assert result["by_side"]["A"] == {"reference": 0, "attributed": 0}
    assert (result["reference"], result["attributed"]) == (2, 2)
    assert attribution([], 1)["status"] == "insufficient"


def test_spawn_status():
    log = Log()
    assert spawn_status(log.records, PROBE_TIMEOUT_S) == {"A": "unknown", "B": "unknown"}
    log.probe("A", 1)
    log.probe("B", 1)  # chaque camp son canal : un résultat de A ne répond pas pour B
    log.result("A", 1, spawned=1)
    assert spawn_status(log.records, PROBE_TIMEOUT_S) == {"A": "ok", "B": "unknown"}
    assert spawn_status(log.records, PROBE_TIMEOUT_S, T0_MS + 29_999)["B"] == "unknown"
    assert spawn_status(log.records, PROBE_TIMEOUT_S, T0_MS + 30_000)["B"] == "ko"


@pytest.mark.parametrize(
    "status, fields, delay_s, expected",
    [
        ("applied", {"spawned": 2}, 0, "ok"),
        ("applied", {"spawned": 1}, PROBE_TIMEOUT_S, "ok"),
        ("applied", {"spawned": 0}, 0, "ko"),
        ("applied", {}, 0, "ko"),
        ("refused", {"reason": "sleeping"}, 0, "unknown"),
        ("failed", {"reason": "no_square"}, 0, "unknown"),
        ("failed", {"reason": "no_spawn", "spawned": 0}, 0, "ko"),
        ("failed", {"spawned": 1}, 0, "ko"),
        ("applied", {"spawned": 1}, PROBE_TIMEOUT_S + 1, "ko"),
        # P9F8 : le mod rend aussi `spread` et `targeted` ; la sonde ne juge que `spawned`.
        ("applied", {"spawned": 1, "spread": 1, "targeted": 0}, 0, "ok"),
        ("applied", {"spawned": 2, "spread": 1, "targeted": 2}, 0, "ok"),
        ("applied", {"spawned": 0, "spread": 0, "targeted": 0}, 0, "ko"),
    ],
)
def test_spawn_result(status, fields, delay_s, expected):
    log = Log()
    log.probe("A", 1)
    log.after(delay_s)
    log.result("A", 1, status, **fields)
    assert spawn_status(log.records, PROBE_TIMEOUT_S)["A"] == expected


def test_spawn_retry_counts_from_the_first_attempt():
    log = Log()
    log.probe("A", 1)
    log.result("A", 1, "refused", reason="asleep")
    log.after(5)
    log.probe("A", 2)
    log.result("A", 2, "failed", reason="no_square")
    assert spawn_status(log.records, PROBE_TIMEOUT_S)["A"] == "unknown"
    assert spawn_status(log.records, PROBE_TIMEOUT_S, T0_MS + 30_000)["A"] == "ko"

    log.after(5)
    log.probe("A", 3)
    log.result("A", 3, spawned=1)
    assert spawn_status(log.records, PROBE_TIMEOUT_S, T0_MS + 30_000)["A"] == "ok"


def test_spawn_ignores_results_of_other_commands():
    log = Log()
    log.probe("A", 1)
    log.result("A", 2, "failed")
    assert spawn_status(log.records, PROBE_TIMEOUT_S)["A"] == "unknown"


@pytest.mark.parametrize(
    "by_side, expected",
    [
        ({"A": "ok", "B": "ok"}, "ok"),
        ({"A": "ok", "B": "unknown"}, "unknown"),
        ({"A": "unknown", "B": "ko"}, "ko"),
    ],
)
def test_combined_spawn(by_side, expected):
    assert combined_spawn(by_side) == expected


def test_fallback_table():
    for spawn in ("ok", "unknown"):
        assert fallback(spawn, "ok") is None
        assert fallback(spawn, "insufficient") is None
        assert fallback(spawn, "ko") == "java_agent"
    for status in ("ok", "ko", "insufficient"):
        assert fallback("ko", status) == "solo_ipc"
    with pytest.raises(ValueError):
        fallback("okay", "ok")
    with pytest.raises(ValueError):
        fallback("ok", "unknown")


def test_hook_seen_distinct_from_registered():
    published = publish_capabilities(CAPS, [])
    assert published["hooks"][HOOK] == {"registered": True, "seen": False}

    published = publish_capabilities(CAPS, [HOOK])
    assert published["hooks"][HOOK] == {"registered": True, "seen": True}
    assert published["hooks"]["OnZombieDead"] == {"registered": True, "seen": False}
    assert published["hooks"]["OnEnterVehicle"] == {"registered": False, "seen": False}

    published = publish_capabilities(CAPS, ["OnZombieDead"])
    assert published["hooks"][HOOK] == {"registered": True, "seen": False}
    assert published["functions"] == {"addZombiesInOutfit": True}

    assert publish_capabilities(None, []) == {"hooks": {}, "functions": {}}


def match_log():
    log = Log()
    runtime = {"attribution_min_kills": 3, "probe_timeout_s": PROBE_TIMEOUT_S}
    log.add("match_start", {"runtime": runtime})
    for side in ("A", "B"):
        log.event("caps", side, CAPS, mt=None)
        log.probe(side, 1)
        log.result(side, 1, spawned=1)
    log.event("hook_seen", "A", {"hook": HOOK}, mt=None)
    log.beat("A", 0, mt=0.0)
    log.kills("A", 2)
    log.kills("A", 1, side=None)
    log.beat("A", 3)
    return log


def test_verdict_of_a_whole_journal():
    log = match_log()
    log.restart("A")
    log.event("caps", "A", {**CAPS, "hooks": {HOOK: False}}, mt=2.0)
    result = verdict.verdict(log.records)
    assert result["attribution"]["status"] == "ko"
    assert result["spawn"] == {"A": "ok", "B": "ok"}
    assert result["fallback"] == "java_agent"
    # Le dernier `caps` fait foi ; un hook vu reste vu après le redémarrage.
    assert result["A"]["hooks"] == {HOOK: {"registered": False, "seen": True}}
    assert result["B"]["hooks"][HOOK] == {"registered": True, "seen": False}


def test_verdict_threshold_comes_from_match_start():
    log = match_log()
    log.records[0]["data"]["runtime"]["attribution_min_kills"] = 4
    assert verdict.verdict(log.records)["attribution"]["status"] == "insufficient"


def write_journal(directory, records):
    journal = Journal(directory, lambda: T0_MS)
    for record in records:
        journal.write(record["type"], record["data"], record["side"], record["mt"])
    journal.close()
    return journal.path


def run_cli(path):
    return subprocess.run(
        [sys.executable, "-m", "service.verdict", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_cli_prints_json_and_exits_0(tmp_path):
    path = write_journal(tmp_path, match_log().records)
    assert len(read_journal(path).records) == len(match_log().records)
    done = run_cli(path)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["attribution"]["status"] == "ko"
    assert (result["attribution"]["reference"], result["attribution"]["attributed"]) == (3, 2)
    assert result["fallback"] == "java_agent"


def test_cli_ignores_cut_last_line(tmp_path):
    path = write_journal(tmp_path, match_log().records)
    with open(path, "ab") as fh:
        fh.write(b'{"v":1,"n":')
    done = run_cli(path)
    assert done.returncode == 0
    assert json.loads(done.stdout)["fallback"] == "java_agent"
    assert "incomplète" in done.stderr


@pytest.mark.parametrize(
    "content, named",
    [
        (b'pas du json\n{"n":2}\n', "ligne 1"),
        (b"", "match_start"),
        (b'{"type":"match_start","data":{}}\n', "runtime"),
        (b'{"type":"match_start","data":{"runtime":{"probe_timeout_s":30}}}\n', "min_kills"),
        (b'{"n":1}\n', "illisible"),
    ],
)
def test_cli_unreadable_journal_exits_1(tmp_path, content, named):
    path = tmp_path / "journal.jsonl"
    path.write_bytes(content)
    done = run_cli(path)
    assert done.returncode == 1
    assert done.stdout == ""
    assert named in done.stderr


def test_cli_missing_file_exits_1(tmp_path):
    done = run_cli(tmp_path / "absent.jsonl")
    assert done.returncode == 1
    assert "illisible" in done.stderr
