"""Backups against the running stand: take them, destroy the data, restore, compare.

Destructive on purpose: the databases are dropped and brought back from object storage.
"""

from __future__ import annotations

import os
import time

import pytest

from charlie import backup, ch, migrate
from charlie.cli import MIGRATIONS, main

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def report(topo):
    # Access entities live in Keeper, not in the databases that get dropped; make sure there are
    # some, so that "still in sync afterwards" means something.
    os.environ.setdefault("SVC_LOADER_PASSWORD", "loader-pass")
    os.environ.setdefault("SVC_REPORTING_PASSWORD", "report-pass")
    assert main(["access", "apply", "--engine", "clickhouse", "--no-directory"]) in (0, 1)
    assert main(["access", "drift", "--engine", "clickhouse", "--no-directory"]) == 0
    return backup.drill(topo, rows=100_000)


def test_restore_brings_back_exactly_the_data_of_each_backup(report):
    assert report.ok, report.problems
    full, inc = report.fingerprints["full"], report.fingerprints["incremental"]
    assert inc.bets > full.bets and inc.ledger > full.ledger  # the two points in time differ


def test_an_increment_of_an_unchanged_cluster_is_only_metadata(topo, report):
    # Increments work per part: parts that were merged since the base are uploaded again, so the
    # size after a busy period says little. With nothing written in between, it must be tiny.
    c = backup.admin(topo)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    full = backup.create(c, topo, f"probe-full-{stamp}")
    inc = backup.create(c, topo, f"probe-incr-{stamp}", base=full.name)
    assert inc.base == full.name
    assert inc.stored_bytes < full.stored_bytes / 10, (inc.stored_bytes, full.stored_bytes)


def test_the_drill_took_an_increment_on_top_of_the_full_backup(report):
    assert report.incremental.base == report.full.name


def test_the_cluster_is_usable_after_a_restore(topo, report):
    c = ch.first_alive(topo)
    assert migrate.plan(c, migrate.discover(MIGRATIONS)) == []  # nothing is pending
    before = int(c.command("SELECT count() FROM igaming.bets"))
    c.command(
        "INSERT INTO igaming.bets (bet_id, ts, player_id, brand, game, currency, stake, payout) "
        "VALUES (9000000000000, now(), 1, 'aurora', 'slots', 'EUR', 1, 0)"
    )
    assert int(c.command("SELECT count() FROM igaming.bets")) == before + 1
    c.command(
        "ALTER TABLE igaming.bets_local ON CLUSTER '{cluster}' DELETE WHERE bet_id = 9000000000000"
    )


def test_access_is_rebuilt_from_the_model_not_from_the_backup(report):
    # The row policies sit on tables that were dropped and re-created; they must still match.
    assert main(["access", "drift", "--engine", "clickhouse", "--no-directory"]) == 0


def test_no_statement_carries_a_credential(topo, report):
    for n in topo.nodes:
        c = ch.connect(n.http_port)
        c.command("SYSTEM FLUSH LOGS")
        hits = c.command(
            "SELECT count() FROM system.query_log WHERE query ILIKE '%charlie-secret%' "
            "AND query NOT ILIKE '%system.query_log%'"
        )
        assert int(hits) == 0, n.name


def test_the_bucket_lists_the_backups(topo, report):
    names = {name for name, _, _ in backup.list_backups(ch.first_alive(topo), topo)}
    assert {report.full.name, report.incremental.name} <= names
