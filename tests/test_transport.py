"""AC2 à AC6 : lecture de `out`, à travers le double du mod."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service.protocol import parse_ack, parse_event
from service.transport import OutReader
from doubles.fake_mod import FakeMod

EFFECTS_PATH = Path(__file__).resolve().parent.parent / "service" / "effects.json"
CATALOG = json.loads(EFFECTS_PATH.read_text(encoding="utf-8"))
KILL = {"w": "Base.Axe", "x": 10500, "y": 9900, "origin": "world"}


@pytest.fixture
def mod(tmp_path):
    return FakeMod(tmp_path, "A")


@pytest.fixture
def reader(mod):
    return OutReader(mod.out_path, "A", CATALOG)


def kills(mod, count):
    return [mod.emit("zombie_kill", KILL) for _ in range(count)]


def seqs(result):
    return [(event["boot"], event["seq"]) for event in result.events]


def types(result):
    return [anomaly.type for anomaly in result.anomalies]


def test_partial_line_waits_for_newline(mod, reader):
    # Une ligne du contrat dépasse 40 octets : la coupe 25 + 15 de l'AC se vérifie donc
    # sur le découpage seul, et l'événement sur une ligne réelle coupée au 25e octet.
    framed = b"x" * 39 + b"\n"
    mod.write_raw(framed[:25])
    assert reader.poll() == ([], [])
    assert reader.offset == 0
    mod.write_raw(framed[25:])
    result = reader.poll()
    assert types(result) == ["bad_line"]
    assert reader.offset == 40

    line = mod.line("zombie_kill", KILL)
    mod.write_raw(line[:25])
    assert reader.poll() == ([], [])
    assert reader.offset == 40
    mod.write_raw(line[25:])
    result = reader.poll()
    assert seqs(result) == [(1, 1)]
    assert result.anomalies == []
    assert reader.offset == 40 + len(line)
    assert reader.poll() == ([], [])


def test_offset_counts_bytes_not_characters(mod, reader):
    data = {"w": "Hache émoussée é", "origin": "world"}
    first = mod.emit("zombie_kill", data)
    second = mod.line("zombie_kill", data)
    cut = second.index("é".encode()) + 1
    mod.write_raw(second[:cut])

    result = reader.poll()
    assert [event["data"]["w"] for event in result.events] == [data["w"]]
    assert reader.offset == len(first)

    mod.write_raw(second[cut:])
    result = reader.poll()
    assert seqs(result) == [(1, 2)]
    assert result.anomalies == []
    assert reader.offset == len(first) + len(second)


def test_shrink_resets_offset_and_dedups(mod, reader):
    lines = kills(mod, 5)
    assert len(reader.poll().events) == 5

    kept = b"".join(lines[:2])
    mod.out_path.write_bytes(kept)
    result = reader.poll()
    assert result.events == []
    assert types(result) == ["dup", "dup"]
    assert [anomaly.data["seq"] for anomaly in result.anomalies] == [1, 2]
    assert reader.offset == len(kept)

    mod.emit("zombie_kill", KILL)
    result = reader.poll()
    assert seqs(result) == [(1, 6)]
    assert result.anomalies == []


def test_missing_file_reads_as_empty(mod, reader):
    assert reader.poll() == ([], [])
    kills(mod, 1)
    assert len(reader.poll().events) == 1

    mod.out_path.unlink()
    assert reader.poll() == ([], [])
    assert reader.offset == 0


def test_new_boot_restarts_seq_without_loss(mod, reader):
    last_of_boot_1 = mod.emit("zombie_kill", KILL, seq=50)
    assert seqs(reader.poll()) == [(1, 50)]

    mod.restart()
    kills(mod, 3)
    result = reader.poll()
    assert seqs(result) == [(2, 1), (2, 2), (2, 3)]
    assert result.anomalies == []

    mod.write_raw(last_of_boot_1)
    result = reader.poll()
    assert result.events == []
    assert types(result) == ["dup"]
    assert result.anomalies[0].data == {"boot": 1, "seq": 50}


def test_boot_change_never_counts_as_gap(mod, reader):
    # Le `seq` du nouveau `boot` dépasse l'ancien de plus de 1 : sans le contrôle du
    # `boot`, ce saut passerait pour un trou.
    mod.emit("zombie_kill", KILL, seq=2)
    mod.restart()
    mod.emit("zombie_kill", KILL, seq=7)
    result = reader.poll()
    assert seqs(result) == [(1, 2), (2, 7)]
    assert result.anomalies == []


def test_gap_and_bad_line_are_visible(tmp_path, mod, reader):
    for seq in (1, 2, 5):
        mod.emit("zombie_kill", KILL, seq=seq)
    result = reader.poll()
    assert seqs(result) == [(1, 1), (1, 2), (1, 5)]
    assert types(result) == ["seq_gap"]
    assert result.anomalies[0].data == {"boot": 1, "after": 2, "seq": 5, "missing": 2}

    other = FakeMod(tmp_path, "B")
    other_reader = OutReader(other.out_path, "B", CATALOG)
    kills(other, 1)
    other.write_raw(b'{"v":1,"boot":\n')
    kills(other, 1)
    result = other_reader.poll()
    assert seqs(result) == [(1, 1), (1, 2)]
    assert types(result) == ["bad_line"]
    assert result.anomalies[0].side == "B"


def test_late_line_after_gap_is_not_reprocessed(mod, reader):
    mod.emit("zombie_kill", KILL, seq=1)
    mod.emit("zombie_kill", KILL, seq=5)
    mod.emit("zombie_kill", KILL, seq=3)
    result = reader.poll()
    assert seqs(result) == [(1, 1), (1, 5)]
    assert types(result) == ["seq_gap", "dup"]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not json", id="text"),
        pytest.param(b'{"v":1,"note":"\xff\xfe"}', id="invalid-utf8"),
        pytest.param(b"\xc3", id="truncated-utf8"),
        pytest.param(b"\x00\x00", id="nul"),
        pytest.param(b"[]", id="array"),
        pytest.param(b"null", id="null"),
        pytest.param(b'{"v":2}', id="version"),
        pytest.param(b'{"v":1}', id="missing-fields"),
        pytest.param(b"[" * 200000, id="deep-nesting"),
        pytest.param(b"1" * 10000, id="huge-int"),
        pytest.param(b'{"v":1,"boot":"1","seq":1,"t_ms":0,"src":"A","side":"A","kind":"beat","data":{}}', id="bad-boot"),
        pytest.param(b'{"v":1,"boot":1,"seq":1,"t_ms":0,"src":"A","side":"A","kind":"nope","data":{}}', id="unknown-kind"),
    ],
)
def test_poll_never_raises_on_a_line(mod, reader, line):
    kills(mod, 1)
    mod.write_raw(line + b"\n")
    kills(mod, 1)
    result = reader.poll()
    assert seqs(result) == [(1, 1), (1, 2)]
    assert types(result) == ["bad_line"]


@pytest.mark.parametrize(
    ("src", "side"),
    [
        pytest.param("B", "B", id="src-and-side"),
        pytest.param("B", None, id="src-side-null"),
        pytest.param("A", "B", id="side"),
        pytest.param("B", "A", id="src"),
        pytest.param("C", "C", id="unknown-src"),
    ],
)
def test_foreign_side_rejected(mod, reader, src, side):
    kills(mod, 1)
    # Le `seq` très haut ne doit pas déplacer le couple, ni créer de trou ensuite.
    mod.emit("zombie_kill", KILL, src=src, side=side, seq=99)
    mod.emit("zombie_kill", KILL, seq=2)
    result = reader.poll()
    assert seqs(result) == [(1, 1), (1, 2)]
    assert types(result) == ["side_mismatch"]


def test_foreign_side_checked_before_dedup(mod, reader):
    kills(mod, 3)
    mod.emit("zombie_kill", KILL, src="B", side="B", seq=1)
    result = reader.poll()
    assert types(result) == ["side_mismatch"]


def test_null_side_is_accepted(mod, reader):
    mod.emit("zombie_kill", KILL, side=None)
    result = reader.poll()
    assert result.events[0]["side"] is None
    assert result.anomalies == []


def test_fake_mod_speaks_the_contract(mod, reader):
    mod.caps()
    mod.beat()
    mod.beat(age=2.5, players=("durand",), hp=87, kills=41)
    mod.hook_seen()
    mod.cmd_result(7, spawned=2)
    mod.cmd_result(8, status="refused", reason="unknown_kind")
    result = reader.poll()
    assert [event["kind"] for event in result.events] == [
        "caps",
        "beat",
        "beat",
        "hook_seen",
        "cmd_result",
        "cmd_result",
    ]
    assert result.anomalies == []
    assert result.events[0]["seq"] == 1
    assert result.events[4]["data"] == {"seq": 7, "status": "applied", "spawned": 2}
    for line in mod.out_path.read_bytes().splitlines():
        parse_event(line, CATALOG)

    mod.ack(7)
    assert parse_ack(mod.ack_path.read_bytes()) == {"v": 1, "boot": 1, "seq": 7}


def test_fake_mod_adopts_session_boot(mod):
    mod.session_path.write_text('{"v":1,"match":"8f3a2c","side":"A","boot":2,"cmd_floor":17}\n')
    mod.caps()
    assert mod.read_session()["cmd_floor"] == 17
    assert (mod.boot, mod.seq) == (2, 0)
