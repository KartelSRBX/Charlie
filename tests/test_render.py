import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from charlie import render
from charlie.topology import Topology, load

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def topo() -> Topology:
    return load(ROOT / "topology.yaml")


def test_render_is_deterministic(topo):
    assert render.render(topo) == render.render(topo)


def test_committed_deploy_matches_topology(topo):
    """The same check CI runs with `charlie check`: nobody hand-edits deploy/."""
    assert render.drift(topo, ROOT / "deploy") == []


def test_every_generated_xml_is_well_formed(topo):
    for rel, content in render.render(topo).items():
        if rel.endswith(".xml"):
            ET.fromstring(content)  # raises on malformed XML


def test_macros_identify_each_replica_uniquely(topo):
    seen = set()
    for node in topo.nodes:
        xml = ET.fromstring(render.render(topo)[f"config/{node.name}/macros.xml"])
        macros = {c.tag: c.text for c in xml.find("macros")}
        assert macros == {"cluster": topo.cluster, "shard": str(node.shard), "replica": node.name}
        seen.add((macros["shard"], macros["replica"]))
    assert len(seen) == len(topo.nodes)


def test_remote_servers_lists_every_replica_in_its_shard(topo):
    xml = ET.fromstring(render.render(topo)["config/cluster.xml"])
    shards = xml.find(f"remote_servers/{topo.cluster}").findall("shard")
    assert len(shards) == topo.shards
    for i, shard in enumerate(shards, start=1):
        hosts = [r.findtext("host") for r in shard.findall("replica")]
        assert hosts == [n.name for n in topo.shard_nodes(i)]
        assert shard.findtext("internal_replication") == "true"


def test_every_server_points_at_every_keeper(topo):
    xml = ET.fromstring(render.render(topo)["config/cluster.xml"])
    hosts = [n.findtext("host") for n in xml.find("zookeeper").findall("node")]
    assert hosts == [k.name for k in topo.keeper_nodes]


def test_keepers_agree_on_the_raft_membership_and_have_own_ids(topo):
    members = []
    for k in topo.keeper_nodes:
        xml = ET.fromstring(render.render(topo)[f"config/{k.name}/keeper.xml"])
        ks = xml.find("keeper_server")
        assert ks.findtext("server_id") == str(k.id)
        members.append([(s.findtext("id"), s.findtext("hostname")) for s in ks.iter("server")])
    assert all(m == members[0] for m in members)
    assert len(members[0]) == topo.keepers


def test_compose_is_valid_yaml_with_a_service_per_node(topo):
    compose = yaml.safe_load(render.render(topo)["docker-compose.yml"])
    expected = {n.name for n in topo.nodes} | {k.name for k in topo.keeper_nodes}
    assert expected <= set(compose["services"])
    for n in topo.nodes:
        assert set(compose["services"][n.name]["depends_on"]) == {k.name for k in topo.keeper_nodes}
    stateful = {f"{name}-data" for name in expected} | {"postgres-data", "ldap-data", "ldap-config"}
    assert set(compose["volumes"]) == stateful


def test_no_secret_is_baked_into_the_rendered_files(topo):
    blob = "\n".join(render.render(topo).values())
    assert "from_env" in blob
    assert not re.search(r"<password>[^<]+</password>", blob)


def test_changing_the_topology_changes_the_output(topo):
    bigger = topo.model_copy(update={"shards": 3})
    a, b = render.render(topo), render.render(bigger)
    assert "config/ch-s3-r1/macros.xml" in b and "config/ch-s3-r1/macros.xml" not in a
    assert a["config/cluster.xml"] != b["config/cluster.xml"]


def test_write_removes_files_of_nodes_that_left_the_topology(topo, tmp_path):
    render.write(topo.model_copy(update={"shards": 3}), tmp_path)
    assert (tmp_path / "config/ch-s3-r2/macros.xml").exists()
    changed = render.write(topo, tmp_path)
    assert not (tmp_path / "config/ch-s3-r2").exists()
    assert any("removed" in c for c in changed)
    assert render.drift(topo, tmp_path) == []


def test_postgres_logins_go_to_the_directory_except_service_accounts(topo):
    hba = render.render(topo)["postgres/02_hba.sh"]
    assert "ldap ldapserver=ldap" in hba
    assert f'ldapsuffix=",ou=people,{topo.ldap.base_dn}"' in hba
    # the service-account rule must come before the catch-all LDAP rule, or it is never reached
    assert hba.index("/^svc_.*") < hba.index("ldapserver=ldap")


def test_ldap_acl_lets_signed_in_users_read_groups_only(topo):
    ldif = render.render(topo)["ldap/10-acl.ldif"]
    assert f'ou=groups,{topo.ldap.base_dn}" by users read by * none' in ldif


def test_clickhouse_has_no_ldap_user_directory(topo):
    # An LDAP *user directory* breaks Distributed queries: see the comment in cluster.xml.
    xml = ET.fromstring(render.render(topo)["config/cluster.xml"])
    assert xml.find("user_directories/ldap") is None
    assert xml.find("ldap_servers/directory/host").text == "ldap"
