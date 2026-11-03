from pathlib import Path

from charlie import upgrade
from charlie.topology import load

ROOT = Path(__file__).resolve().parents[1]


def test_every_server_and_keeper_has_a_target_image():
    topo = load(ROOT / "topology.yaml")
    want = upgrade.targets(topo)
    assert len(want) == topo.keepers + topo.shards * topo.replicas
    assert {want[k.name] for k in topo.keeper_nodes} == {topo.images.keeper}
    assert {want[n.name] for n in topo.nodes} == {topo.images.server}


def test_servers_are_upgraded_replica_by_replica_across_shards(monkeypatch):
    topo = load(ROOT / "topology.yaml")
    monkeypatch.setattr(upgrade.status, "keeper_statuses", lambda t: [])
    names = upgrade.order(topo)
    keepers, servers = names[: topo.keepers], names[topo.keepers :]
    assert all(n.startswith("keeper-") for n in keepers)
    # all second replicas before any first replica: every shard keeps a fully caught-up copy
    assert servers == ["ch-s1-r2", "ch-s2-r2", "ch-s1-r1", "ch-s2-r1"]


def test_the_keeper_leader_is_upgraded_last(monkeypatch):
    topo = load(ROOT / "topology.yaml")
    fake = [
        upgrade.status.KeeperStatus("keeper-1", True, "leader"),
        upgrade.status.KeeperStatus("keeper-2", True, "follower"),
        upgrade.status.KeeperStatus("keeper-3", True, "follower"),
    ]
    monkeypatch.setattr(upgrade.status, "keeper_statuses", lambda t: fake)
    assert upgrade.order(topo)[: topo.keepers] == ["keeper-2", "keeper-3", "keeper-1"]
