"""Spike S1b : le VRAI jeu, le mod B ne parlant à l'arbitre que par le relais TCP.

Deux vrais serveurs Project Zomboid sont lancés par le vrai `RealMatch`, chacun sur son propre
dossier cache (`-cachedir`), hors du vrai `~/Zomboid`. Les quatre fichiers de transport du camp B
vivent dans le dossier de B et n'atteignent l'arbitre que par un `Bridge` (socket TCP, latence
injectée). Le camp A reste local : il sert de témoin.

Sans joueur, le spike prouve ce que `pytest -m pz_integration` prouve déjà en local :
`caps`, contrat vérifié, session, puis des commandes prises « au plus une fois » (elles
finissent `refused no_player`). Il mesure le délai commande → `ack` et commande → résultat,
camp local contre camp relayé.

Ce qui n'est PAS séparé : `Server/`, `Saves/`, `db/` et `mods/` de B sont des liens vers ceux de
l'hôte. Dans la vraie version, l'agent de l'invité les écrirait chez lui sur ordre de l'hôte.

    python -m spikes.chacun_son_serveur.real_game --work /chemin/de/travail --delay-ms 80
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

from service import servers as servers_module
from service import transport
from service.engine import MatchSecrets, RealMatch, load_presets
from service.preflight import MOD_SRC, detect_pz, install_mod
from service.proofs import PROOFS_NAME, HookProofs
from service.protocol import PROTOCOL_VERSION, ProtocolError, parse_ack
from service.rules import load_effects, load_runtime
from service.servers import ServerProcess, gen_secret
from service.state import MatchState
from service.transport import OutReader

from spikes.chacun_son_serveur.relay import Bridge

REMOTE = "B"
SHARED = ("Server", "Saves", "db", "mods")
PRESET_ID = "duel_rapide"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def lay_out(work: Path) -> tuple[Path, Path]:
    host, guest = work / "zomboid-hote", work / "zomboid-invite"
    for name in (*SHARED, "Lua"):
        (host / name).mkdir(parents=True, exist_ok=True)
    (host / "Saves" / "Multiplayer").mkdir(exist_ok=True)
    (guest / "Lua").mkdir(parents=True, exist_ok=True)
    for name in SHARED:
        link = guest / name
        if not link.exists():
            link.symlink_to(host / name, target_is_directory=True)
    return host, guest


class Watch:
    """Suit `out` et `ack` d'un camp, côté arbitre, et date ce qu'il voit."""

    def __init__(self, lua: Path, side: str, effects) -> None:
        self.side = side
        self._reader = OutReader(lua / f"duel_{side}_out.txt", side, effects)
        self._ack = lua / f"duel_{side}_ack.txt"
        self.results: dict[int, tuple[float, dict]] = {}
        self.acked: dict[int, float] = {}
        self.anomalies: list = []
        self.kinds: dict[str, int] = {}

    def poll(self) -> None:
        now = time.monotonic()
        events, anomalies = self._reader.poll()
        self.anomalies += anomalies
        for event in events:
            self.kinds[event["kind"]] = self.kinds.get(event["kind"], 0) + 1
            if event["kind"] == "cmd_result":
                self.results.setdefault(event["data"]["seq"], (now, event["data"]))
        try:
            seq = parse_ack(self._ack.read_bytes())["seq"]
        except (OSError, ProtocolError):
            return
        for known in range(1, seq + 1):
            self.acked.setdefault(known, now)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--delay-ms", type=float, default=80)
    parser.add_argument("--jitter-ms", type=float, default=40)
    parser.add_argument("--commands", type=int, default=8)
    args = parser.parse_args()

    runtime, effects = load_runtime(), load_effects()
    detection = detect_pz(None, __import__("os").environ, Path.home())
    if detection.path is None:
        log(f"jeu introuvable : {detection.message}")
        return 2
    pz_dir = detection.path
    host, guest = lay_out(args.work)
    install_mod(MOD_SRC, host)
    cache = {"A": host, REMOTE: guest}

    # Chaque serveur reçoit son dossier cache : rien ne touche au vrai `~/Zomboid`.
    launch = servers_module.launch_command

    def launch_in_its_folder(side, cfg, platform=None):
        return [*launch(side, cfg, platform), f"-cachedir={cache[side]}"]

    servers_module.launch_command = launch_in_its_folder

    bridge = Bridge(
        host / "Lua", guest / "Lua", REMOTE,
        delay_s=args.delay_ms / 1000, jitter_s=args.jitter_ms / 1000,
    ).start()
    reset = transport.CommandChannel.reset

    def relayed_reset(self):
        reset(self)
        if self.side == REMOTE:
            assert bridge.host.sync_reset(), "remise à zéro non acquittée"

    transport.CommandChannel.reset = relayed_reset

    table = next(p for p in load_presets(runtime, effects) if p["id"] == PRESET_ID)
    state = MatchState()
    match = RealMatch(
        {
            "password": "spike-invite",
            "host_password": "spike-hote",
            "day_length": table["day_length"],
            "duration_s": table["duration_s"],
        },
        table,
        secrets=MatchSecrets(gen_secret(), gen_secret()),
        runtime=runtime,
        effects=effects,
        zomboid_dir=host,
        pz_dir=pz_dir,
        matches_dir=args.work / "matches",
        proofs=HookProofs(args.work / PROOFS_NAME, print),
        clock=time.time,
        stop=threading.Event(),
        server_factory=ServerProcess,
        on_status=lambda side, status, detail: log(f"serveur {side} : {status} {detail[:120]}"),
        on_state=lambda change: change(state),
    )
    report: dict = {"delay_ms": args.delay_ms, "jitter_ms": args.jitter_ms}
    code = 1
    try:
        began = time.monotonic()
        log("préparation des mondes (passe de création au premier passage)…")
        match.prepare()
        report["prepare_s"] = round(time.monotonic() - began, 1)
        began = time.monotonic()
        assert match.launch(), match.pair.failure
        report["launch_s"] = round(time.monotonic() - began, 1)
        log("serveurs prêts, attente des caps…")
        began = time.monotonic()
        caps = match.wait_caps()
        report["caps_s"] = round(time.monotonic() - began, 1)
        report["caps"] = {side: bool(caps[side]) for side in caps}
        failures = match.verify(caps)
        report["contract_failures"] = failures
        log(f"caps : {report['caps']} ; contrat : {failures or 'vérifié'}")
        assert all(caps.values()) and not failures

        watches = {side: Watch(host / "Lua", side, effects) for side in ("A", REMOTE)}
        written: dict[str, dict[int, float]] = {"A": {}, REMOTE: {}}
        probe = {**runtime["probe_spawn"], "warn_s": 0}
        for index in range(args.commands):
            for side in ("A", REMOTE):
                channel = match._channels[side]
                seq = channel.last_seq + 1
                channel.write({"v": PROTOCOL_VERSION, "seq": seq, "kind": "probe_spawn", **probe})
                written[side][seq] = time.monotonic()
            deadline = time.monotonic() + 2.3
            while time.monotonic() < deadline:
                for watch in watches.values():
                    watch.poll()
                # Comme `pump` : l'arbitre relit `ack` et vide `in` quand tout est couvert.
                for side in ("A", REMOTE):
                    match._channels[side].poll_ack()
                time.sleep(0.02)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not all(
            set(written[side]) <= set(watches[side].results) for side in written
        ):
            for watch in watches.values():
                watch.poll()
            for side in ("A", REMOTE):
                match._channels[side].poll_ack()
            time.sleep(0.02)
        time.sleep(7)  # une réécriture d'`ack` de plus, et plus aucun résultat ne doit venir
        for watch in watches.values():
            watch.poll()

        for side, watch in watches.items():
            results = {seq: watch.results.get(seq) for seq in written[side]}
            delays = [results[seq][0] - at for seq, at in written[side].items() if results[seq]]
            acks = [watch.acked[seq] - at for seq, at in written[side].items() if seq in watch.acked]
            statuses = sorted(
                {f"{item[1]['status']} {item[1].get('reason', '')}".strip() for item in results.values() if item}
            )
            report[side] = {
                "written": len(written[side]),
                "resolved": sum(1 for item in results.values() if item),
                "statuses": statuses,
                "result_delay_s": _summary(delays),
                "ack_delay_s": _summary(acks),
                "anomalies": [(a.type, dict(a.data)) for a in watch.anomalies],
                "kinds": watch.kinds,
            }
        for name in ("out", "in", "ack", "session"):
            a = (host / "Lua" / f"duel_{REMOTE}_{name}.txt")
            b = (guest / "Lua" / f"duel_{REMOTE}_{name}.txt")
            report.setdefault("mirror_equal", {})[name] = (
                a.exists() and b.exists() and a.read_bytes() == b.read_bytes()
            )
        clock = bridge.host.clock
        report["clock"] = {
            "samples": clock.count,
            "offset_ms": None if clock.offset_ms is None else round(clock.offset_ms, 2),
            "bound_ms": None if clock.bound_ms is None else round(clock.bound_ms, 2),
        }
        report["link"] = {
            "host_to_guest_bytes": bridge.host.link.sent_bytes,
            "guest_to_host_bytes": bridge.guest.link.sent_bytes,
            "stale_dropped": bridge.host.stale_dropped,
            "resets": bridge.host.resets,
        }
        code = 0
    except BaseException as exc:  # le spike rend ce qu'il a, puis arrête les serveurs
        report["failure"] = f"{type(exc).__name__}: {exc}"
        log(f"ÉCHEC : {report['failure']}")
    finally:
        log("arrêt des serveurs…")
        report["stopped"] = match.shutdown()
        match.close()
        bridge.stop()
        (args.work / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return code


def _summary(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "n": len(values),
        "min": round(min(values), 2),
        "median": round(statistics.median(values), 2),
        "max": round(max(values), 2),
    }


if __name__ == "__main__":
    sys.exit(main())
