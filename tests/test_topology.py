import pytest
from pydantic import ValidationError

from charlie.topology import Topology, load

BASE = {
    "cluster": "charlie",
    "images": {"server": "s:1", "keeper": "k:1"},
    "keepers": 3,
    "shards": 2,
    "replicas": 2,
}


def make(**over) -> Topology:
    return Topology.model_validate({**BASE, **over})


def test_repo_topology_loads():
    topo = load("topology.yaml")
    assert len(topo.nodes) == topo.shards * topo.replicas


def test_node_names_and_ports_follow_shard_and_replica():
    topo = make()
    assert [n.name for n in topo.nodes] == ["ch-s1-r1", "ch-s1-r2", "ch-s2-r1", "ch-s2-r2"]
    assert [n.http_port for n in topo.nodes] == [8123, 8124, 8125, 8126]
    assert [k.client_port for k in topo.keeper_nodes] == [9181, 9182, 9183]
    assert [n.name for n in topo.shard_nodes(2)] == ["ch-s2-r1", "ch-s2-r2"]


@pytest.mark.parametrize("n", [2, 4, 6])
def test_even_keeper_ensemble_is_rejected(n):
    with pytest.raises(ValidationError, match="must be odd"):
        make(keepers=n)


@pytest.mark.parametrize(("keepers", "quorum"), [(1, 1), (3, 2), (5, 3)])
def test_quorum_is_a_majority(keepers, quorum):
    assert make(keepers=keepers).quorum == quorum


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        make(shardz=3)


def test_host_port_collision_is_rejected():
    with pytest.raises(ValidationError, match="collide"):
        make(ports={"http_base": 9000, "native_base": 9000})
    with pytest.raises(ValidationError, match="collide"):
        make(ports={"prometheus": 8124})


def test_cluster_name_must_be_a_safe_identifier():
    with pytest.raises(ValidationError):
        make(cluster="my cluster")
