"""Cluster health and data consistency, read from the live cluster."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ch
from .topology import Topology


@dataclass
class NodeStatus:
    name: str
    up: bool
    error: str = ""
    readonly_tables: int = 0
    max_queue: int = 0
    max_delay_s: int = 0
    rows: int | None = None
    keeper_connected: bool = False


@dataclass
class KeeperStatus:
    name: str
    up: bool
    state: str = "down"
    followers: int | None = None
    znodes: int | None = None


def node_statuses(topo: Topology) -> list[NodeStatus]:
    out = []
    for n in topo.nodes:
        st = NodeStatus(n.name, up=False)
        try:
            c = ch.connect(n.http_port, timeout=5)
            r = c.query(
                "SELECT countIf(is_readonly), max(queue_size), max(absolute_delay) "
                "FROM system.replicas"
            ).result_rows[0]
            st.up = True
            st.readonly_tables, st.max_queue, st.max_delay_s = (int(x or 0) for x in r)
            st.keeper_connected = bool(
                c.command("SELECT count() FROM system.zookeeper_connection WHERE NOT is_expired")
            )
            has_table = c.command("EXISTS TABLE igaming.bets_local")
            if int(has_table):
                st.rows = int(c.command("SELECT count() FROM igaming.bets_local"))
        except Exception as e:
            st.error = type(e).__name__
        out.append(st)
    return out


def keeper_statuses(topo: Topology) -> list[KeeperStatus]:
    out = []
    for k in topo.keeper_nodes:
        ks = KeeperStatus(k.name, up=False)
        try:
            if ch.keeper_command(k.client_port, "ruok").strip() == "imok":
                ks.up = True
                mntr = dict(
                    line.split("\t", 1)
                    for line in ch.keeper_command(k.client_port, "mntr").splitlines()
                    if "\t" in line
                )
                ks.state = mntr.get("zk_server_state", "?")
                ks.followers = int(mntr["zk_followers"]) if "zk_followers" in mntr else None
                ks.znodes = int(mntr.get("zk_znode_count", 0))
        except OSError:
            pass
        out.append(ks)
    return out


@dataclass
class Consistency:
    """Result of comparing what clients were told with what the cluster holds."""

    total: int
    unique: int
    per_node: dict[str, int]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_consistency(
    topo: Topology, expected_rows: int | None = None, max_skew: float = 0.10
) -> Consistency:
    """Total and unique row counts, replica equality inside each shard, shard balance."""
    problems = []
    c = ch.first_alive(topo)
    c.command("SYSTEM FLUSH DISTRIBUTED igaming.bets")
    total, unique = (
        int(v)
        for v in c.query("SELECT count(), uniqExact(bet_id) FROM igaming.bets").result_rows[0]
    )
    if expected_rows is not None and total != expected_rows:
        problems.append(f"expected {expected_rows} rows, cluster has {total}")
    if unique != total:
        problems.append(f"{total - unique} duplicate bet_id rows")

    per_node: dict[str, int] = {}
    for n in topo.nodes:
        try:
            nc = ch.connect(n.http_port, timeout=10)
            # Make sure a replica that was just revived has caught up before counting.
            nc.command("SYSTEM SYNC REPLICA igaming.bets_local")
            per_node[n.name] = int(nc.command("SELECT count() FROM igaming.bets_local"))
        except Exception as e:
            problems.append(f"{n.name} unreachable: {type(e).__name__}")
    shard_totals = []
    for s in range(1, topo.shards + 1):
        counts = {x.name: per_node[x.name] for x in topo.shard_nodes(s) if x.name in per_node}
        if len(set(counts.values())) > 1:
            problems.append(f"replicas of shard {s} disagree: {counts}")
        if counts:
            shard_totals.append(max(counts.values()))
    if len(shard_totals) > 1 and total > 10_000:
        mean = sum(shard_totals) / len(shard_totals)
        if max(abs(x - mean) / mean for x in shard_totals) > max_skew:
            problems.append(f"shards are skewed beyond {max_skew:.0%}: {shard_totals}")
    return Consistency(total, unique, per_node, problems)
