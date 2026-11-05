"""R6 : séquence, `fsync` de phase, dernière ligne coupée et champs interdits."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import service.journal as journal_module
from service.journal import Journal, JournalError, read_journal
from service.protocol import parse_event
from service.rules import MatchCore, load_effects, load_runtime, load_table

T0_MS = 1793830000456
SECRET = "marqueur-secret"
PRESET_PATH = Path(journal_module.__file__).resolve().parent / "presets" / "duel_rapide.json"


class FakeClock:
    def __init__(self):
        self.now_ms = T0_MS

    def __call__(self):
        self.now_ms += 1
        return self.now_ms


@pytest.fixture
def journal(tmp_path):
    instance = Journal(tmp_path / "matches" / "m1", FakeClock())
    yield instance
    instance.close()


@pytest.fixture
def fsyncs(monkeypatch):
    """Compte les `fsync` du module sans dépendre du système de fichiers."""
    calls = []
    monkeypatch.setattr(journal_module.os, "fsync", calls.append)
    return calls


def raw_lines(journal):
    return journal.path.read_bytes().splitlines()


def test_sequenced_records(journal):
    """Match synthétique : un kill et un tick de fin, écrits comme `RealMatch` les écrira."""
    runtime = load_runtime()
    catalog = load_effects()
    raw = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    table = load_table(raw, catalog, runtime["limits"], PRESET_PATH.stem)
    duration_s = 5
    core = MatchCore(table, runtime, catalog, T0_MS, duration_s)
    kill = {
        "v": 1,
        "boot": 1,
        "seq": 1,
        "t_ms": T0_MS + 1000,
        "src": "A",
        "side": "A",
        "kind": "zombie_kill",
        "data": {"origin": "world"},
    }

    journal.write("match_start", {"preset": table, "duration_s": duration_s})
    journal.write("state", {"phase": "running", "t0_ms": T0_MS}, mt=0)
    core.submit_event(parse_event(kill, catalog))
    written = core.advance(T0_MS + duration_s * 1000)
    for record in written:
        journal.write(**record)

    records = read_journal(journal.path).records

    assert [record["n"] for record in records] == list(range(1, len(records) + 1))
    assert all(record["v"] == 1 and "type" in record for record in records)
    assert [record["ts_ms"] for record in records] == [
        T0_MS + i for i in range(1, len(records) + 1)
    ]
    assert {"match_start", "event", "rule_fired", "command", "match_end"} <= {
        record["type"] for record in records
    }


def test_line_format(journal):
    journal.write("match_start", {"match_id": "m1"})
    journal.write("event", {"kind": "é"}, side="A", mt=312.5)
    journal.write("state", {"phase": "running"}, mt=0)

    first, second, third = raw_lines(journal)

    assert first == (
        b'{"v":1,"n":1,"ts_ms":1793830000457,"mt":null,"type":"match_start",'
        b'"side":null,"data":{"match_id":"m1"}}'
    )
    assert json.loads(second)["mt"] == 312.5
    assert json.loads(second)["side"] == "A"
    assert json.loads(second)["data"] == {"kind": "é"}
    assert json.loads(third)["mt"] == 0


def test_write_returns_the_record(journal):
    record = journal.write("event", {"seq": 1}, side="B", mt=0.25)

    assert record == read_journal(journal.path).records[0]


def test_line_visible_before_close(journal):
    journal.write("event", {})

    assert len(raw_lines(journal)) == 1


def test_fsync_only_on_state(journal, fsyncs):
    journal.write("match_start", {})
    journal.write("event", {})
    assert fsyncs == []

    journal.write("state", {"phase": "running"})
    assert len(fsyncs) == 1

    journal.write("command", {})
    assert len(fsyncs) == 1


@pytest.mark.parametrize(
    "data",
    [
        {"password": SECRET},
        {"RCONPassword": SECRET},
        {"admin_password": SECRET},
        {"config": {"password": SECRET}},
        {"items": [{"nested": ({"password": SECRET},)}]},
    ],
)
def test_forbidden_field_refused(journal, data):
    with pytest.raises(JournalError) as caught:
        journal.write("match_start", data)

    assert SECRET not in str(caught.value)
    assert raw_lines(journal) == []


def test_refused_write_leaves_no_gap(journal):
    journal.write("event", {})
    with pytest.raises(JournalError):
        journal.write("event", {"password": "x"})
    with pytest.raises(ValueError):
        journal.write("event", {"bad": float("nan")})
    journal.write("event", {})

    assert [record["n"] for record in read_journal(journal.path).records] == [1, 2]


def test_second_journal_refused(journal):
    with pytest.raises(FileExistsError):
        Journal(journal.path.parent, FakeClock())


def test_n_exact_across_threads(journal):
    threads_count, writes = 8, 200

    def writer(side):
        for _ in range(writes):
            journal.write("event", {"pad": "x" * 64}, side=side)

    threads = [threading.Thread(target=writer, args=(str(i),)) for i in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    content = read_journal(journal.path)

    assert content.incomplete is None
    assert [record["n"] for record in content.records] == list(
        range(1, threads_count * writes + 1)
    )


def test_read_complete_journal(journal):
    journal.write("event", {})

    content = read_journal(journal.path)

    assert len(content.records) == 1
    assert content.incomplete is None


def test_read_empty_journal(journal):
    assert read_journal(journal.path) == ([], None)


def test_truncated_last_line(journal):
    journal.write("event", {"seq": 1})
    journal.write("event", {"seq": 2})
    raw = journal.path.read_bytes()
    journal.path.write_bytes(raw[:-10])

    content = read_journal(journal.path)

    assert [record["n"] for record in content.records] == [1]
    assert content.incomplete == raw[:-10].split(b"\n")[-1]


def test_cut_inside_multibyte_character(journal):
    journal.write("event", {"seq": 1})
    journal.write("event", {"w": "é"})
    raw = journal.path.read_bytes()
    cut = raw[: raw.index("é".encode()) + 1]
    journal.path.write_bytes(cut)

    content = read_journal(journal.path)

    assert len(content.records) == 1
    assert content.incomplete is not None


def test_last_line_without_newline_is_incomplete(journal):
    journal.write("event", {"seq": 1})
    journal.write("event", {"seq": 2})
    journal.path.write_bytes(journal.path.read_bytes()[:-1])

    content = read_journal(journal.path)

    assert len(content.records) == 1
    assert json.loads(content.incomplete)["n"] == 2


def test_unreadable_terminated_last_line_is_separated(journal):
    journal.write("event", {"seq": 1})
    with open(journal.path, "ab") as handle:
        handle.write(b'{"v":1,"n"\n')

    content = read_journal(journal.path)

    assert len(content.records) == 1
    assert content.incomplete == b'{"v":1,"n"'


def test_corrupt_middle_line_raises(journal):
    journal.write("event", {"seq": 1})
    journal.write("event", {"seq": 2})
    first, second = raw_lines(journal)
    journal.path.write_bytes(b"\n".join([first, b"{oops", second]) + b"\n")

    with pytest.raises(JournalError, match="ligne 2"):
        read_journal(journal.path)


def test_line_separator_inside_string_stays_one_record(journal):
    journal.write("event", {"w": "a b\u0085c"})

    assert read_journal(journal.path).records[0]["data"]["w"] == "a b\u0085c"
