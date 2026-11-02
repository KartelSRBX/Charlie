"""Failure drills against the live cluster.

Every drill ends with the same question: is every acknowledged row present exactly once, and do
all replicas of a shard hold identical data? The load generator retries failed batches with
identical content, so ClickHouse's insert deduplication makes that provable.
"""

from __future__ import annotations

import threading

import pytest

from charlie import ch, status

from .conftest import compose, wait_for

pytestmark = pytest.mark.e2e


def assert_consistent(topo, expected: int) -> None:
    res = status.check_consistency(topo, expected_rows=expected, max_skew=0.15)
    assert res.ok, res.problems


def test_baseline_spreads_rows_over_shards_and_replicas(topo, make_gen, table_rows, healthy):
    before = table_rows()
    stats = make_gen().run(batches=20, workers=4)
    assert not stats.failed_batches
    assert_consistent(topo, before + stats.rows_ok)


def test_replica_down_writes_and_reads_continue(topo, make_gen, table_rows, healthy):
    before = table_rows()
    compose("stop", "ch-s1-r1")

    stats = make_gen().run(batches=20, workers=4)
    assert not stats.failed_batches, "inserts must fail over to the surviving replica"
    # Reading through the Distributed table of another node sees everything that was acknowledged.
    other = ch.connect(topo.nodes[2].http_port)
    assert int(other.command("SELECT count() FROM igaming.bets")) == before + stats.rows_ok

    compose("start", "ch-s1-r1")
    wait_for(
        lambda: (
            ch.connect(topo.nodes[0].http_port).command("SELECT count() FROM igaming.bets_local")
            == ch.connect(topo.nodes[1].http_port).command("SELECT count() FROM igaming.bets_local")
        ),
        what="ch-s1-r1 to catch up with its replica",
    )
    assert_consistent(topo, before + stats.rows_ok)


def test_one_keeper_down_cluster_keeps_quorum(topo, make_gen, table_rows, healthy):
    before = table_rows()
    compose("stop", "keeper-1")
    stats = make_gen().run(batches=20, workers=4)
    assert not stats.failed_batches, "2 of 3 keepers is still a quorum"
    ks = status.keeper_statuses(topo)
    assert sum(k.up for k in ks) == 2
    compose("start", "keeper-1")
    assert_consistent(topo, before + stats.rows_ok)


def test_keeper_quorum_lost_reads_survive_writes_stop_and_recover(
    topo, make_gen, table_rows, healthy
):
    before = table_rows()
    seed = make_gen().run(batches=10, workers=2)
    assert not seed.failed_batches
    acked = before + seed.rows_ok

    compose("stop", "keeper-2", "keeper-3")
    # Replicated tables notice once their Keeper session expires and turn read-only.
    wait_for(
        lambda: (
            ch.connect(topo.nodes[0].http_port).command(
                "SELECT countIf(is_readonly) FROM system.replicas"
            )
            > 0
        ),
        timeout=60,
        what="replicas to go read-only",
    )

    # Reads keep working from local data: a Keeper outage is a write outage, not a read outage.
    assert (
        int(ch.connect(topo.nodes[0].http_port).command("SELECT count() FROM igaming.bets"))
        == acked
    )

    # Writes are refused, and no half-written batch is acknowledged.
    gen = make_gen(attempts=2)
    gen.run(batches=3, workers=1)
    assert len(gen.stats.failed_batches) == 3
    assert gen.stats.rows_ok == 0

    compose("start", "keeper-2", "keeper-3")
    wait_for(
        lambda: (
            ch.connect(topo.nodes[0].http_port).command(
                "SELECT countIf(is_readonly) FROM system.replicas"
            )
            == 0
        ),
        timeout=90,
        what="replicas to leave read-only mode",
    )
    # The same batches, resent after recovery, are accepted exactly once even if one shard had
    # already taken its half before the outage hit.
    for i in gen.stats.failed_batches:
        assert gen.send(i)
    assert_consistent(topo, acked + gen.stats.rows_ok)


def test_whole_shard_down_fails_loudly_unless_told_to_skip(topo, make_gen, table_rows, healthy):
    make_gen().run(batches=10, workers=2)
    total = table_rows()
    compose("stop", "ch-s1-r1", "ch-s1-r2")
    survivor = ch.connect(topo.nodes[2].http_port)

    with pytest.raises(Exception, match=r"(?i)connect|timeout|network|all connection"):
        survivor.command("SELECT count() FROM igaming.bets")

    partial = int(
        survivor.command("SELECT count() FROM igaming.bets SETTINGS skip_unavailable_shards = 1")
    )
    assert 0 < partial < total, "skip_unavailable_shards returns a silently partial answer"


def test_rolling_restart_under_load_loses_nothing(topo, make_gen, table_rows, healthy):
    before = table_rows()
    gen = make_gen(attempts=8)
    t = threading.Thread(target=lambda: gen.run(duration=100, workers=4))
    t.start()
    try:
        for node in topo.nodes:  # one at a time; the other replica of the shard serves meanwhile
            compose("restart", node.name)
            wait_for(
                lambda n=node: ch.connect(n.http_port).command("SELECT 1") == 1,
                what=f"{node.name} to answer",
            )
            wait_for(
                lambda n=node: (
                    ch.connect(n.http_port).command("SELECT max(queue_size) FROM system.replicas")
                    == 0
                ),
                what=f"{node.name} to drain its replication queue",
            )
    finally:
        t.join()
    assert not gen.stats.failed_batches
    assert gen.stats.rows_ok > 0
    assert_consistent(topo, before + gen.stats.rows_ok)
