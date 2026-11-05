"""AC2 à AC8 : `out`, `in`, `ack` et `session`, à travers le double du mod."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from service.protocol import parse_ack, parse_command, parse_event
from service.transport import CommandChannel, OutReader
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
        pytest.param(b'{"v":1,"boot":1,"seq":2,"t_ms":0,"src":"A","side":"A","kind":"zombie_kill","data":{"extra":1e999}}', id="infinite-preserved-field"),
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


@pytest.fixture
def channel(tmp_path):
    channel = CommandChannel("A", tmp_path)
    channel.write_session("8f3a2c", 1, 0)
    return channel


def command(seq):
    return {
        "v": 1,
        "seq": seq,
        "kind": "spawn_pop",
        "n": 2,
        "distance": [30, 60],
        "warn_s": 6,
        "origin": {"rule": "kill_baseline", "boot": 1, "event_seq": 42},
    }


def written(channel, *seqs):
    for seq in seqs:
        channel.write(command(seq))


def in_seqs(mod):
    return [parse_command(line, CATALOG)["seq"] for line in mod.in_path.read_bytes().splitlines()]


def test_write_is_one_write_then_fsync(channel, mod, monkeypatch):
    calls = []
    real_write, real_fsync = os.write, os.fsync
    monkeypatch.setattr(os, "write", lambda fd, data: calls.append(data) or real_write(fd, data))
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append("fsync") or real_fsync(fd))
    channel.write(command(1))
    assert len(calls) == 2
    assert calls[0].endswith(b"\n") and calls[0].count(b"\n") == 1
    assert calls[1] == "fsync"
    assert mod.in_path.read_bytes() == calls[0]


def test_seq_must_grow_above_floor(channel, mod):
    written(channel, 3)
    for seq in (3, 2):
        with pytest.raises(ValueError):
            channel.write(command(seq))
    assert in_seqs(mod) == [3]


def test_truncate_only_on_full_ack(channel, mod):
    written(channel, 1, 2, 3, 4, 5, 6, 7)
    before = mod.in_path.read_bytes()
    inode = mod.in_path.stat().st_ino

    mod.ack(6)
    assert channel.poll_ack() == []
    assert mod.in_path.read_bytes() == before

    mod.ack(9)
    (ahead,) = channel.poll_ack()
    assert (ahead.type, ahead.side) == ("ack_ahead", "A")
    assert ahead.data == {"boot": 1, "seq": 9, "last_seq": 7}
    assert mod.in_path.read_bytes() == before

    mod.ack(7, boot=2)
    (foreign,) = channel.poll_ack()
    assert foreign.type == "ack_foreign_boot"
    assert foreign.data == {"boot": 2, "seq": 7, "current_boot": 1}
    assert mod.in_path.read_bytes() == before

    mod.ack(7)
    assert channel.poll_ack() == []
    assert mod.in_path.read_bytes() == b""
    assert mod.in_path.stat().st_ino == inode


def test_write_after_read_ack_keeps_new_line(channel, mod):
    written(channel, 1)
    mod.ack(1)
    written(channel, 2)
    assert channel.poll_ack() == []
    assert in_seqs(mod) == [1, 2]


def test_abnormal_ack_reported_once_while_it_stays(channel, mod):
    written(channel, 1)
    mod.ack(9)
    assert [a.type for a in channel.poll_ack()] == ["ack_ahead"]
    assert channel.poll_ack() == []
    mod.ack(0)
    assert channel.poll_ack() == []
    mod.ack(9)
    assert [a.type for a in channel.poll_ack()] == ["ack_ahead"]


@pytest.mark.parametrize(
    "content",
    [None, b"", b'{"v":1,"boot":1,"se', b"\xff\xfe\n", b'{"v":2,"boot":1,"seq":1}\n', b'{"v":1,"boot":1}\n'],
)
def test_empty_or_unreadable_ack_is_no_ack(channel, mod, content):
    written(channel, 1)
    if content is not None:
        mod.ack_path.write_bytes(content)
    assert channel.poll_ack() == []
    assert in_seqs(mod) == [1]


def test_ack_timeout_is_reported_once_and_never_reissues(tmp_path, mod, runtime):
    now = [0.0]
    limit = runtime["ack_timeout_s"]
    channel = CommandChannel("A", tmp_path, clock=lambda: now[0])
    channel.write_session("8f3a2c", 1, 0)
    written(channel, 1)
    now[0] = limit / 2
    written(channel, 2)
    before = mod.in_path.read_bytes()

    assert channel.check_ack_timeout(limit) == []
    now[0] = limit
    (late,) = channel.check_ack_timeout(limit)
    assert (late.type, late.data) == ("ack_timeout", {"seq": 1})
    assert channel.check_ack_timeout(limit) == []
    assert mod.in_path.read_bytes() == before

    # L'ack arrivé après l'échéance vide encore `in`, et couvre la commande suivante.
    mod.ack(2)
    assert channel.poll_ack() == []
    assert mod.in_path.read_bytes() == b""
    now[0] = limit * 2
    assert channel.check_ack_timeout(limit) == []


def test_write_session_replaces_in_one_block(channel, mod):
    channel.write_session("8f3a2c", 2, 17)
    assert mod.read_session() == {"v": 1, "match": "8f3a2c", "side": "A", "boot": 2, "cmd_floor": 17}
    assert [path.name for path in mod.session_path.parent.iterdir()] == [mod.session_path.name]
    with pytest.raises(ValueError):
        channel.write_session("8f3a2c", 0, 17)
    assert mod.read_session()["boot"] == 2


def test_restart_is_at_most_once(channel, mod):
    reader = OutReader(mod.out_path, "A", CATALOG)
    written(channel, 1, 2, 3)
    mod.cmd_result(1)
    resolved = {event["data"]["seq"] for event in reader.poll().events}
    unresolved = [seq for seq in (1, 2, 3) if seq not in resolved]
    inode = mod.in_path.stat().st_ino

    lost = channel.prepare_restart(unresolved)

    assert [(a.type, a.side, a.data) for a in lost] == [
        ("lost_on_restart", "A", {"seq": 2}),
        ("lost_on_restart", "A", {"seq": 3}),
    ]
    assert mod.in_path.read_bytes() == b""
    assert mod.in_path.stat().st_ino == inode
    session = mod.read_session()
    assert (session["boot"], session["cmd_floor"]) == (2, 3)

    # Aucune réémission : le nouveau `boot` ne voit dans `in` que les commandes neuves.
    mod.ack(3)
    assert channel.poll_ack() == []
    assert mod.in_path.read_bytes() == b""
    written(channel, 4)
    assert in_seqs(mod) == [4]
    with pytest.raises(ValueError):
        channel.write(command(3))


def test_restart_forgets_pending_acks_and_rejects_old_boot(tmp_path, mod, runtime):
    now = [0.0]
    channel = CommandChannel("A", tmp_path, clock=lambda: now[0])
    channel.write_session("8f3a2c", 1, 0)
    written(channel, 1)
    mod.ack(1)
    channel.prepare_restart([1])
    now[0] = runtime["ack_timeout_s"]
    assert channel.check_ack_timeout(runtime["ack_timeout_s"]) == []
    assert [a.type for a in channel.poll_ack()] == ["ack_foreign_boot"]
    assert channel.boot == 2 and channel.last_seq == 1


def test_restart_without_session_is_refused(tmp_path):
    with pytest.raises(ValueError):
        CommandChannel("A", tmp_path).prepare_restart([])


def test_reset_empties_files_and_keeps_session(channel, mod):
    written(channel, 1, 2)
    mod.ack(2)
    mod.write_raw(b"reste\n")
    channel.reset()
    assert [path.read_bytes() for path in (mod.out_path, mod.in_path, mod.ack_path)] == [b"", b"", b""]
    assert mod.session_path.exists()
    assert channel.boot == 1 and channel.last_seq == 0
    written(channel, 1)
    assert in_seqs(mod) == [1]


def test_reset_creates_missing_files(tmp_path, mod):
    CommandChannel("A", tmp_path).reset()
    assert [path.stat().st_size for path in (mod.out_path, mod.in_path, mod.ack_path)] == [0, 0, 0]
