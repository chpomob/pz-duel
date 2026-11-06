"""Tests du vrai jeu (`pz_integration`) et preuve de leur `skip` quand PZ est absent.

Ces tests écrivent dans le vrai `~/Zomboid` : le mod `duelcore`, les réglages de `duelA` et
`duelB`, et les snapshots, dont le premier capture les mondes déjà présents. Ils ne prouvent
rien de ce qui exige un joueur : AC19 se déroule en jeu, selon le README, et
`test_live_journal_checklist` ne fait qu'en relire les traces.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from service.engine import FIRST_BOOT, MatchSecrets, RealMatch, load_presets
from service.journal import read_journal
from service.preflight import (
    ABSENT,
    ENV_VAR,
    MOD_SRC,
    SCRIPT,
    check_ports,
    detect_pz,
    install_mod,
    run_preflight,
)
from service.protocol import (
    PROTOCOL_VERSION,
    SIDES,
    ProtocolError,
    dump_line,
    parse_ack,
    parse_event,
)
from service.rules import load_effects, load_runtime
from service.servers import INI_MODE, STOP_RCON, ServerProcess, gen_secret, server_name
from service.state import MatchState
from service.transport import CommandChannel, OutReader

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_BUILD = "42.21.0"
PRESET_ID = "duel_rapide"
PLAYER_PASSWORD = "integration"
HOST_PASSWORD = "integration-hote"
GAME_SERVER_CLASS = b"zombie/network/GameServer"
POLL_S = 0.1

# Le mod lit `in` chaque seconde et réécrit `ack` toutes les 5 s : ce délai couvre plusieurs
# lectures et au moins une réécriture, qui atteste que le mod tournait pendant le silence.
QUIET_S = 7
READER_FLOOR = 2
READER_WARN_S = 3
READER_SIDE = "A"

ENV_LIVE_JOURNAL = "PZ_LIVE_JOURNAL"
ENV_LIVE_STATE = "PZ_LIVE_STATE"
DAMAGE_ATOM = "damage_taken"
KILL_ATOM = "zombie_kill"
SPAWN_KIND = "spawn_pop"
REFUSAL_REASONS = ("asleep", "in_vehicle")


@pytest.fixture
def pz_dir() -> Path:
    """Dossier du jeu détecté ; sans lui, le test est en `skip` avec le motif de la détection."""
    detection = detect_pz(None, os.environ, Path.home())
    if detection.path is None:
        pytest.skip(detection.message)
    return detection.path


def game_server_pids() -> list[int]:
    """Processus de `duelA` et `duelB` sur la machine, script ou JVM : d'autres `java` tournent."""
    names = [server_name(side).encode() for side in SIDES]
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(GAME_SERVER_CLASS in item for item in arguments) and any(
            name in arguments for name in names
        ):
            pids.append(int(entry.name))
    return pids


@pytest.fixture
def match_secrets() -> MatchSecrets:
    return MatchSecrets(gen_secret(), gen_secret())


@pytest.fixture
def real_match(pz_dir, tmp_path, match_secrets) -> Iterator[RealMatch]:
    """Un `RealMatch` sur le vrai `~/Zomboid`, préflight passé, serveurs non démarrés.

    Il tourne comme dans le moteur : mêmes secrets tirés, même `prepare`, même arrêt.
    """
    runtime, effects = load_runtime(), load_effects()
    zomboid_dir = Path.home() / "Zomboid"
    table = next(preset for preset in load_presets(runtime, effects) if preset["id"] == PRESET_ID)
    install_mod(MOD_SRC, zomboid_dir)
    preflight = run_preflight(table, runtime=runtime, effects=effects, pz_dir=pz_dir)
    assert preflight.ok, preflight.detail
    assert not game_server_pids()

    state = MatchState()
    match = RealMatch(
        {
            "password": PLAYER_PASSWORD,
            "host_password": HOST_PASSWORD,
            "day_length": table["day_length"],
            "duration_s": table["duration_s"],
        },
        table,
        secrets=match_secrets,
        runtime=runtime,
        effects=effects,
        zomboid_dir=zomboid_dir,
        pz_dir=pz_dir,
        matches_dir=tmp_path / "matches",
        clock=time.time,
        stop=threading.Event(),
        server_factory=ServerProcess,
        on_status=lambda side, status, detail: None,
        on_state=lambda change: change(state),
    )
    try:
        yield match
    finally:
        match.shutdown()
        match.close()


def boot(match: RealMatch) -> None:
    assert match.launch(), match.pair.failure


def lua_dir(match: RealMatch) -> Path:
    return match.zomboid_dir / "Lua"


@pytest.mark.pz_integration
def test_detection_finds_install(pz_dir):
    assert os.access(pz_dir / SCRIPT, os.X_OK)


def test_detection_reports_absent(tmp_path):
    home = tmp_path / "home"
    empty_install = tmp_path / "install"
    home.mkdir()
    empty_install.mkdir()

    for install_dir in ("", str(empty_install)):
        env = {**os.environ, "HOME": str(home), ENV_VAR: install_dir, ENV_LIVE_JOURNAL: ""}
        assert detect_pz(None, env, home).status == ABSENT

        # `-m pz_integration` écarte ce test-ci : l'enfant ne se relance pas lui-même.
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-m",
                "pz_integration",
                "-rs",
                "-p",
                "no:cacheprovider",
                "tests/test_integration_pz.py",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        assert "PZ absent" in child.stdout
        assert "skipped" in child.stdout
        assert " passed" not in child.stdout


@pytest.mark.pz_integration
def test_caps_from_real_mod(real_match):
    real_match.prepare()
    boot(real_match)
    caps = real_match.wait_caps()

    for side in SIDES:
        assert caps[side] is not None, real_match.pair.servers[side].log_tail()
        out = lua_dir(real_match) / f"duel_{side}_out.txt"
        first_line = out.read_bytes().split(b"\n")[0]
        event = parse_event(first_line, real_match.effects)
        assert event["kind"] == "caps"
        assert event["src"] == side
        assert event["data"]["build"] == EXPECTED_BUILD


@pytest.mark.pz_integration
def test_real_servers_boot_contract_stop(real_match, match_secrets):
    real_match.prepare()
    boot(real_match)
    assert len(game_server_pids()) >= len(SIDES)

    caps = real_match.wait_caps()
    assert real_match.verify(caps) == []

    assert real_match.shutdown()
    assert game_server_pids() == []
    assert check_ports(real_match.runtime["ports"])[1] == []

    for side, server in real_match.pair.servers.items():
        # Le `RCONPassword` du `.ini` a bien servi : l'arrêt n'a pas eu à se replier.
        assert server.stop_stage == STOP_RCON
        ini = real_match.zomboid_dir / "Server" / f"{server_name(side)}.ini"
        assert stat.S_IMODE(ini.stat().st_mode) == INI_MODE
    for path in real_match.match_dir.iterdir():
        content = path.read_bytes()
        for secret in match_secrets:
            assert secret.encode() not in content, path.name


class ModWatch:
    """Observe `out` et `ack` d'un camp, sans jamais toucher à `in`."""

    def __init__(self, channel: CommandChannel, effects: Mapping[str, Any]) -> None:
        self._channel = channel
        self._reader = OutReader(channel.out_path, channel.side, effects)
        self.results: dict[int, list[Mapping[str, Any]]] = {}
        self.anomalies: list[Any] = []

    def _poll(self) -> None:
        events, anomalies = self._reader.poll()
        self.anomalies += anomalies
        for event in events:
            if event["kind"] == "cmd_result":
                self.results.setdefault(event["data"]["seq"], []).append(event["data"])

    def ack(self) -> int | None:
        """Le `seq` de `ack`. Le mod le réécrit sans atomicité : illisible vaut `None`."""
        try:
            return parse_ack(self._channel.ack_path.read_bytes())["seq"]
        except (OSError, ProtocolError):
            return None

    def _ack_stamp(self) -> int:
        return self._channel.ack_path.stat().st_mtime_ns

    def wait(self, condition: Callable[[], bool], timeout_s: float, what: str) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            self._poll()
            if condition():
                return
            assert time.monotonic() < deadline, f"{what} : rien après {timeout_s} s"
            time.sleep(POLL_S)

    def stay_quiet(self, acked: int) -> None:
        """Garde `QUIET_S`, sans autre résultat que ceux connus ni `ack` autre que `acked`."""
        known = {seq: len(items) for seq, items in self.results.items()}
        stamp = self._ack_stamp()
        deadline = time.monotonic() + QUIET_S
        while time.monotonic() < deadline:
            self._poll()
            assert {seq: len(items) for seq, items in self.results.items()} == known
            assert self.ack() in (acked, None)
            time.sleep(POLL_S)
        assert self._ack_stamp() != stamp, "`ack` non réécrit : le mod ne tourne pas"


def append_in(channel: CommandChannel, data: bytes) -> None:
    with open(channel.in_path, "ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


@pytest.mark.pz_integration
def test_command_reader_at_most_once(real_match):
    """R1 côté mod, sans joueur : toute commande prise finit `refused` après `warn_s`."""
    runtime = real_match.runtime
    ack_timeout_s, result_timeout_s = runtime["ack_timeout_s"], runtime["cmd_result_timeout_s"]

    real_match.prepare()
    channel = CommandChannel(READER_SIDE, lua_dir(real_match))
    channel.write_session(real_match.match_id, FIRST_BOOT, READER_FLOOR)
    boot(real_match)
    watch = ModWatch(channel, real_match.effects)
    watch.wait(lambda: watch.ack() == READER_FLOOR, runtime["contract_timeout_s"], "ack initial")

    def line(seq: int, warn_s: int = 0) -> bytes:
        probe = {**runtime["probe_spawn"], "warn_s": warn_s}
        return dump_line({"v": PROTOCOL_VERSION, "seq": seq, "kind": "probe_spawn", **probe})

    def acked(seq: int) -> Callable[[], bool]:
        return lambda: watch.ack() == seq

    def resolved(seq: int) -> Callable[[], bool]:
        return lambda: seq in watch.results

    # Sous le plancher : ignorées, sans résultat ni avance de `ack`.
    append_in(channel, line(1) + line(2))
    watch.stay_quiet(READER_FLOOR)

    # Prise en charge tout de suite, résultat après `warn_s`, malgré les relectures de `in`.
    third = line(3, READER_WARN_S)
    taken_at = time.monotonic()
    append_in(channel, third)
    watch.wait(acked(3), ack_timeout_s, "ack de 3")
    assert time.monotonic() - taken_at < READER_WARN_S
    assert 3 not in watch.results
    watch.wait(resolved(3), result_timeout_s, "résultat de 3")
    assert time.monotonic() - taken_at >= READER_WARN_S
    watch.stay_quiet(3)

    # Réécrite, la ligne 3 n'est pas reprise.
    append_in(channel, third)
    watch.stay_quiet(3)

    # Une ligne coupée n'existe pas tant que sa fin n'est pas là.
    fourth = line(4)
    half = len(fourth) // 2
    append_in(channel, fourth[:half])
    watch.stay_quiet(3)
    append_in(channel, fourth[half:])
    watch.wait(resolved(4), result_timeout_s, "résultat de 4")
    watch.wait(acked(4), ack_timeout_s, "ack de 4")

    # Le JSON entier ne suffit pas : seul le `\n` fait la ligne.
    fifth = line(5)
    append_in(channel, fifth[:-1])
    watch.stay_quiet(4)
    append_in(channel, fifth[-1:])
    watch.wait(resolved(5), result_timeout_s, "résultat de 5")
    watch.wait(acked(5), ack_timeout_s, "ack de 5")
    watch.stay_quiet(5)

    assert sorted(watch.results) == [3, 4, 5]
    for seq, items in watch.results.items():
        assert [item["status"] for item in items] == ["refused"], seq
        assert items[0]["reason"] == "no_player"
    assert watch.anomalies == []


def live_checklist_gaps(
    records: Sequence[Mapping[str, Any]], state: Mapping[str, Any]
) -> list[str]:
    """Ce qui manque à AC19 dans un journal et sa copie de `/api/state`, un message par écart.

    Le spawn visible par le client et le « toujours » du refus échappent à ces traces : ils
    restent constatés par le joueur.
    """
    start = next(record["data"] for record in records if record["type"] == "match_start")
    gaps = []
    if state["phase"] != "over":
        gaps.append(f"état pris en phase {state['phase']}, attendu over")
    if state["match_id"] != start["match_id"]:
        gaps.append(f"état du match {state['match_id']}, journal du match {start['match_id']}")

    events = [record["data"] for record in records if record["type"] == "event"]
    results = {
        (record["data"]["src"], record["data"]["data"]["seq"]): record["data"]["data"]
        for record in records
        if record["type"] == "cmd_result"
    }
    if not any(event["kind"] == KILL_ATOM for event in events):
        gaps.append(f"aucun event {KILL_ATOM}")
    low, high = start["runtime"]["probe_spawn"]["distance"]
    spawns = [
        results.get((record["side"], command["seq"]), {})
        for record in records
        if record["type"] == "command"
        for command in [record["data"]["command"]]
        if command["kind"] == SPAWN_KIND
        and low <= command["distance"][0]
        and command["distance"][1] <= high
    ]
    if not any(item.get("status") == "applied" and item.get("spawned") for item in spawns):
        gaps.append(f"aucune commande {SPAWN_KIND} à {low}-{high} cases appliquée")
    if not any(
        item["status"] == "refused" and item.get("reason") in REFUSAL_REASONS
        for item in results.values()
    ):
        gaps.append(f"aucun cmd_result refused pour {' ou '.join(REFUSAL_REASONS)}")

    capabilities = state["capabilities"]
    active, published = set(capabilities["active_atoms"]), set(capabilities["withdrawn"])
    journaled = {record["data"]["atom"] for record in records if record["type"] == "atom_withdrawn"}
    if published != journaled:
        gaps.append(
            f"retrait contradictoire : état {sorted(published)}, journal {sorted(journaled)}"
        )
    if DAMAGE_ATOM in published | journaled:
        if DAMAGE_ATOM in active:
            gaps.append(f"retrait contradictoire : {DAMAGE_ATOM} retiré et actif")
    elif DAMAGE_ATOM not in active:
        gaps.append(f"{DAMAGE_ATOM} ni actif ni retiré")
    elif not any(event["kind"] == DAMAGE_ATOM for event in events):
        gaps.append(f"aucun event {DAMAGE_ATOM}")
    return gaps


@pytest.mark.pz_integration
def test_live_journal_checklist():
    """AC19 : relit les traces d'une partie jouée. Un `skip` laisse le critère ouvert."""
    journal, state = os.environ.get(ENV_LIVE_JOURNAL), os.environ.get(ENV_LIVE_STATE)
    if not journal or not state:
        pytest.skip(
            f"AC19 reste ouvert : {ENV_LIVE_JOURNAL} et {ENV_LIVE_STATE} sont requises, "
            "après une partie jouée selon le README"
        )
    records = read_journal(journal).records
    assert live_checklist_gaps(records, json.loads(Path(state).read_text("utf-8"))) == []


def live_traces(withdrawn: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Traces conformes à AC19, avec `damage_taken` actif ou retiré."""
    atoms = [KILL_ATOM] if withdrawn else [KILL_ATOM, DAMAGE_ATOM]
    runtime = load_runtime()

    def record(kind: str, data: Mapping[str, Any], side: str | None = None) -> dict[str, Any]:
        return {"type": kind, "side": side, "data": data}

    def event(kind: str) -> dict[str, Any]:
        return record("event", {"kind": kind, "src": "A", "data": {}}, "A")

    def result(seq: int, **data: Any) -> dict[str, Any]:
        return record("cmd_result", {"src": "B", "data": {"seq": seq, **data}}, "B")

    spawn = {"seq": 1, "kind": SPAWN_KIND, "distance": runtime["probe_spawn"]["distance"]}
    other = {"seq": 2, "kind": "noise", "distance": runtime["probe_spawn"]["distance"]}
    records = [
        record("match_start", {"match_id": "m", "preset": {"atoms": atoms}, "runtime": runtime}),
        event(KILL_ATOM),
        record("command", {"command": spawn}, "B"),
        result(1, status="applied", spawned=2),
        record("command", {"command": other}, "B"),
        result(2, status="refused", reason=REFUSAL_REASONS[0]),
    ]
    if withdrawn:
        records.insert(1, record("atom_withdrawn", {"atom": DAMAGE_ATOM}))
    else:
        records.append(event(DAMAGE_ATOM))
    capabilities = {"active_atoms": atoms, "withdrawn": [DAMAGE_ATOM] if withdrawn else []}
    return records, {"phase": "over", "match_id": "m", "capabilities": capabilities}


def without(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [item for item in records if item["data"].get("kind") != kind]


@pytest.mark.parametrize("withdrawn", [False, True])
def test_live_checklist_accepts_complete_traces(withdrawn):
    assert live_checklist_gaps(*live_traces(withdrawn)) == []


def test_live_checklist_names_each_missing_fact():
    no_spawn = "aucune commande spawn_pop à 30-60 cases appliquée"
    records, state = live_traces()
    assert live_checklist_gaps(without(records, KILL_ATOM), state) == [f"aucun event {KILL_ATOM}"]
    assert live_checklist_gaps(without(records, DAMAGE_ATOM), state) == [
        f"aucun event {DAMAGE_ATOM}"
    ]

    records, state = live_traces()
    records[3]["data"]["data"].update(status="failed", spawned=0)
    assert no_spawn in live_checklist_gaps(records, state)

    records, state = live_traces()
    records[2]["data"]["command"]["distance"] = [25, 40]
    assert no_spawn in live_checklist_gaps(records, state)

    records, state = live_traces()
    records[5]["data"]["data"]["reason"] = "no_player"
    assert live_checklist_gaps(records, state) == [
        "aucun cmd_result refused pour asleep ou in_vehicle"
    ]


def test_live_checklist_rejects_foreign_state():
    records, state = live_traces()
    state.update(phase="running", match_id="autre")
    assert live_checklist_gaps(records, state) == [
        "état pris en phase running, attendu over",
        "état du match autre, journal du match m",
    ]


def test_live_checklist_fails_on_withdrawal_contradiction():
    records, state = live_traces(withdrawn=True)
    state["capabilities"]["withdrawn"] = []
    assert live_checklist_gaps(records, state) == [
        f"retrait contradictoire : état [], journal ['{DAMAGE_ATOM}']"
    ]

    records, state = live_traces()
    state["capabilities"]["withdrawn"] = [DAMAGE_ATOM]
    assert f"retrait contradictoire : état ['{DAMAGE_ATOM}'], journal []" in live_checklist_gaps(
        records, state
    )
    assert f"retrait contradictoire : {DAMAGE_ATOM} retiré et actif" in live_checklist_gaps(
        records, state
    )


def test_live_checklist_accepts_withdrawal_of_table_atom():
    """Un atome retiré n'empêche plus le match : sa blessure n'est alors pas exigée."""
    records, state = live_traces(withdrawn=True)
    records[0]["data"]["preset"]["atoms"] = [KILL_ATOM, DAMAGE_ATOM]
    assert live_checklist_gaps(records, state) == []


def test_live_checklist_requires_a_decision_on_the_atom():
    records, state = live_traces()
    state["capabilities"]["active_atoms"] = [KILL_ATOM]
    assert live_checklist_gaps(records, state) == [f"{DAMAGE_ATOM} ni actif ni retiré"]
