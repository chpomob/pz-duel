"""Spike S1a : rejoue les tests du moteur avec le camp B derrière le relais TCP.

Greffon pytest. Il ne change ni le moteur ni un seul test : il déplace les quatre fichiers du
mod B dans un second dossier (`LuaGuest`), comme s'ils étaient sur le PC de l'invité, et pose un
`Bridge` entre ce dossier et celui que lit l'arbitre. Chaque octet échangé avec le mod B passe
alors par un vrai socket TCP, avec la latence demandée.

    SPIKE_DELAY_MS=40 SPIKE_JITTER_MS=20 python -m pytest \
        -p spikes.chacun_son_serveur.relayed_suite tests/test_match.py tests/test_contract.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from spikes.chacun_son_serveur.relay import Bridge
from spikes.chacun_son_serveur.rendezvous import Rendezvous, join_pair

REMOTE_SIDE = "B"
GUEST_DIR = "LuaGuest"
DELAY_S = float(os.environ.get("SPIKE_DELAY_MS", "40")) / 1000
JITTER_S = float(os.environ.get("SPIKE_JITTER_MS", "20")) / 1000
# 1 : le double du mod attend la livraison avant de rendre la main (voir `delivered`).
SYNC_TESTS = os.environ.get("SPIKE_SYNC_TESTS", "1") == "1"
LOCAL_OUT = 'out="{zomboid}/Lua/duel_${{side}}_out.txt"'
RELAYED_OUT = (
    'if [ "$side" = B ]; then out="{zomboid}/LuaGuest/duel_B_out.txt"; '
    'else out="{zomboid}/Lua/duel_${{side}}_out.txt"; fi'
)
STATS = {"rigs": 0, "messages": 0, "bytes": 0, "stale": 0, "codes": 0}
# SPIKE_RENDEZVOUS=1 : le lien passe par un relais de rendez-vous, deux connexions sortantes.
RENDEZVOUS = Rendezvous() if os.environ.get("SPIKE_RENDEZVOUS") == "1" else None


@pytest.fixture(autouse=True)
def _side_b_behind_the_relay(monkeypatch):
    import test_snapshots
    from doubles import fake_mod

    assert LOCAL_OUT in test_snapshots.ENGINE_SCRIPT
    monkeypatch.setattr(
        test_snapshots, "ENGINE_SCRIPT", test_snapshots.ENGINE_SCRIPT.replace(LOCAL_OUT, RELAYED_OUT)
    )

    mod_init = fake_mod.FakeMod.__init__

    def relayed_mod(self, lua_dir, side, *args, **kwargs):
        lua_dir = Path(lua_dir)
        guest = lua_dir.parent / GUEST_DIR
        if side == REMOTE_SIDE and lua_dir.name == "Lua" and guest.is_dir():
            lua_dir = guest
        mod_init(self, lua_dir, side, *args, **kwargs)

    monkeypatch.setattr(fake_mod.FakeMod, "__init__", relayed_mod)

    # Les tests figent une horloge simulée et l'avancent d'un coup : un octet encore en vol au
    # moment du saut arrive « après son tick », et aucun tick ne suit. Une vraie horloge avance
    # toujours. Le double attend donc que le lien ait livré avant de rendre la main au test :
    # chaque octet traverse bien TCP avec sa latence, seul l'artefact d'horloge disparaît.
    def delivered(mod, name: str) -> None:
        guest = getattr(mod, f"{name}_path")
        if guest.parent.name != GUEST_DIR:
            return
        host = guest.parent.parent / "Lua" / guest.name
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                same = host.read_bytes() == guest.read_bytes()
            except OSError:
                same = False
            if same:
                return
            time.sleep(0.005)

    write_raw, write_ack = fake_mod.FakeMod.write_raw, fake_mod.FakeMod.ack

    def relayed_write(self, data):
        write_raw(self, data)
        if SYNC_TESTS:
            delivered(self, "out")

    def relayed_ack(self, *args, **kwargs):
        write_ack(self, *args, **kwargs)
        if SYNC_TESTS:
            delivered(self, "ack")

    monkeypatch.setattr(fake_mod.FakeMod, "write_raw", relayed_write)
    monkeypatch.setattr(fake_mod.FakeMod, "ack", relayed_ack)

    rig_init = test_snapshots.EngineRig.__init__
    rig_cleanup = test_snapshots.EngineRig.cleanup

    def relayed_rig(self, tmp_path, zomboid, runtime):
        guest = Path(zomboid) / GUEST_DIR
        guest.mkdir(parents=True, exist_ok=True)
        links = None
        if RENDEZVOUS is not None:
            # Variante « zéro port » : les deux bouts sortent vers le relais de rendez-vous.
            STATS["codes"] += 1
            links = join_pair(RENDEZVOUS.address, f"duel-{STATS['codes']}", DELAY_S, JITTER_S)
        self.bridge = Bridge(
            Path(zomboid) / "Lua", guest, REMOTE_SIDE, delay_s=DELAY_S, jitter_s=JITTER_S,
            links=links,
        ).start()
        bridges[Path(zomboid) / "Lua"] = self.bridge
        rig_init(self, tmp_path, zomboid, runtime)

    def cleanup(self):
        try:
            rig_cleanup(self)
        finally:
            STATS["rigs"] += 1
            STATS["stale"] += self.bridge.host.stale_dropped
            for end in (self.bridge.host, self.bridge.guest):
                STATS["messages"] += end.link.sent_messages
                STATS["bytes"] += end.link.sent_bytes
            self.bridge.stop()

    # Sur un disque partagé, `reset` vide `out` d'un coup. Sur un lien, c'est un ordre acquitté :
    # dans la vraie version il voyagerait sur le même lien ordonné que « démarre le serveur ».
    from service import transport

    channel_reset = transport.CommandChannel.reset
    bridges: dict[Path, Bridge] = {}

    def relayed_reset(self):
        channel_reset(self)
        bridge = bridges.get(self.out_path.parent)
        if self.side == REMOTE_SIDE and bridge is not None:
            assert bridge.host.sync_reset(), "remise à zéro non acquittée par l'invité"

    monkeypatch.setattr(transport.CommandChannel, "reset", relayed_reset)

    monkeypatch.setattr(test_snapshots.EngineRig, "__init__", relayed_rig)
    monkeypatch.setattr(test_snapshots.EngineRig, "cleanup", cleanup)


def pytest_terminal_summary(terminalreporter):
    if RENDEZVOUS is not None:
        terminalreporter.write_line(
            f"rendez-vous : {RENDEZVOUS.pairs} paires appariées, {RENDEZVOUS.relayed_bytes} octets recopiés"
        )
    terminalreporter.write_line(
        f"relais : {STATS['rigs']} matchs relayés, {STATS['messages']} messages, "
        f"{STATS['bytes']} octets, {STATS['stale']} messages périmés jetés, latence {DELAY_S * 1000:.0f} ms + gigue {JITTER_S * 1000:.0f} ms par sens"
    )
