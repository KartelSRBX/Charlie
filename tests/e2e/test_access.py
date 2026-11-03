"""Access control against the running stand: ``make up access``, then ``make e2e``.

These tests log in as real people (through LDAP) and as service accounts, and check what each of
them can and cannot do in ClickHouse and PostgreSQL, then change the directory and check that
access follows. Passwords are the demo ones from fixtures/directory/people.yaml.
"""

from __future__ import annotations

import os

import clickhouse_connect
import psycopg
import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from charlie import ch
from charlie.access import directory, model
from charlie.cli import main

from .conftest import ROOT

pytestmark = pytest.mark.e2e

SVC = {"SVC_LOADER_PASSWORD": "loader-pass", "SVC_REPORTING_PASSWORD": "report-pass"}


@pytest.fixture(scope="session", autouse=True)
def _service_passwords():
    os.environ.update(SVC)


@pytest.fixture(scope="session")
def m():
    return model.load(ROOT / "access" / "model.yaml")


@pytest.fixture(scope="session")
def ldap(topo):
    return directory.connect(topo)


def run(*args: str) -> int:
    """`charlie access ...` exactly as a person would type it."""
    return main(["access", *args])


@pytest.fixture(scope="session", autouse=True)
def stand(topo, m, ldap, schema, _service_passwords):
    """A directory with the demo people and the databases brought in line with the model."""
    directory.seed(ldap, topo.ldap.base_dn, m, ROOT / "fixtures" / "directory" / "people.yaml")
    assert run("apply", "--prune") in (0, 1)
    assert run("drift") == 0
    c = ch.first_alive(topo)
    c.command(
        "INSERT INTO igaming.bets (bet_id, ts, player_id, brand, game, currency, stake, payout) "
        "SELECT 1000000000000 + number, now(), number, "
        "arrayElement(['aurora','bluefin','cobalt'], 1 + number % 3), 'slots', 'EUR', 1, 0 "
        "FROM numbers(300)"
    )
    c.command("SYSTEM FLUSH DISTRIBUTED igaming.bets ON CLUSTER '{cluster}'")


def ch_as(topo, user: str, password: str, node: int = 0):
    port = ch.endpoints(topo)[node].port
    return clickhouse_connect.get_client(host=ch.HOST, port=port, username=user, password=password)


def pg_as(topo, user: str, password: str):
    return psycopg.connect(
        host="localhost",
        port=topo.postgres.port,
        dbname=topo.postgres.database,
        user=user,
        password=password,
        connect_timeout=5,
        autocommit=True,
    )


def ch_denied(client, sql: str) -> bool:
    try:
        client.query(sql)
    except DatabaseError as e:
        return "Not enough privileges" in str(e) or "ACCESS_DENIED" in str(e)
    return False


def pg_denied(topo, user, password, sql) -> bool:
    try:
        with pg_as(topo, user, password) as c:
            c.execute(sql)
    except psycopg.errors.InsufficientPrivilege:
        return True
    return False


# --- the model, applied ---------------------------------------------------------------------
def test_apply_is_idempotent_and_drift_is_clean(topo):
    assert run("apply") == 0
    assert run("drift") == 0


def test_roles_exist_on_every_replica_without_on_cluster(topo, m):
    for ep in ch.endpoints(topo):
        c = ch.connect(ep.port)
        have = {r[0] for r in c.query("SELECT name FROM system.roles").result_rows}
        assert set(m.role_names()) <= have, ep.name


def test_a_grant_made_by_hand_is_drift_and_only_prune_removes_it(topo):
    admin = ch.first_alive(topo)
    admin.command("GRANT SELECT ON finance.* TO igaming_ro")
    assert run("drift", "--engine", "clickhouse") == 1
    assert run("apply", "--engine", "clickhouse") == 1  # removal skipped
    assert run("drift", "--engine", "clickhouse") == 1
    assert run("apply", "--engine", "clickhouse", "--prune") == 0
    assert run("drift", "--engine", "clickhouse") == 0


def test_postgres_drift_is_detected_and_pruned_in_one_transaction(topo):
    with pg_as(topo, "postgres", os.environ.get("PG_ADMIN_PASSWORD", "charlie")) as c:
        c.execute("CREATE ROLE rogue LOGIN")
        c.execute("GRANT SELECT ON finance.ledger TO igaming_ro")
    assert run("drift", "--engine", "postgres") == 1
    assert run("apply", "--engine", "postgres", "--prune") == 0
    assert run("drift", "--engine", "postgres") == 0
    assert pg_denied(topo, "bob", "bob-pass", "SELECT 1 FROM finance.ledger") is False
    assert pg_denied(topo, "dave", "dave-pass", "SELECT 1 FROM finance.ledger") is True


# --- what people can do ---------------------------------------------------------------------
def test_a_brand_analyst_sees_only_her_brand_on_every_node_through_the_distributed_table(topo):
    admin = ch.first_alive(topo)
    truth = int(admin.command("SELECT count() FROM igaming.bets WHERE brand = 'aurora'"))
    assert truth > 0
    for node in range(len(topo.nodes)):
        alice = ch_as(topo, "alice", "alice-pass", node)
        rows = alice.query("SELECT brand, count() FROM igaming.bets GROUP BY brand").result_rows
        assert rows == [("aurora", truth)], node


def test_an_unrestricted_analyst_still_sees_every_brand(topo):
    bob = ch_as(topo, "bob", "bob-pass")
    brands = {r[0] for r in bob.query("SELECT DISTINCT brand FROM igaming.bets").result_rows}
    assert {"aurora", "bluefin", "cobalt"} <= brands


def test_domains_are_walls_in_both_engines(topo):
    dave = ch_as(topo, "dave", "dave-pass")
    assert ch_denied(dave, "SELECT count() FROM finance.ledger")
    assert pg_denied(topo, "dave", "dave-pass", "SELECT 1 FROM finance.ledger")
    bob = ch_as(topo, "bob", "bob-pass")
    bob.query("SELECT count() FROM finance.ledger")  # bob holds finance_ro: allowed


def test_levels_are_enforced_read_only_cannot_write_and_admin_can(topo):
    bob = ch_as(topo, "bob", "bob-pass")
    assert ch_denied(
        bob,
        "INSERT INTO igaming.bets_local (bet_id, ts, player_id, brand, game, currency, stake, "
        "payout) VALUES (1, now(), 1, 'x', 'x', 'EUR', 1, 1)",
    )
    assert pg_denied(topo, "bob", "bob-pass", "DELETE FROM igaming.players WHERE player_id = 0")
    with pg_as(topo, "carol", "carol-pass") as c:  # igaming_admin
        c.execute("DELETE FROM igaming.players WHERE player_id = -1")


def test_a_writer_can_insert_into_postgres_including_the_serial_column(topo):
    with pg_as(topo, "dave", "dave-pass") as c:
        c.execute("INSERT INTO igaming.players (player_id, brand, country) VALUES (-5, 'x', 'RS')")
    with pg_as(topo, "carol", "carol-pass") as c:
        c.execute("DELETE FROM igaming.players WHERE player_id = -5")


def test_profile_limits_are_applied_and_cannot_be_raised(topo):
    bob = ch_as(topo, "bob", "bob-pass")
    assert bob.command("SELECT getSetting('max_threads')") == 4
    with pytest.raises(DatabaseError, match=r"(?i)constraint|readonly|cannot be changed"):
        bob.query("SELECT 1", settings={"max_memory_usage": 9_000_000_000})


def test_the_analyst_quota_exists_with_its_limits(topo):
    c = ch.first_alive(topo)
    row = c.query(
        "SELECT max_queries FROM system.quota_limits WHERE quota_name = 'analyst_daily'"
    ).result_rows
    assert row == [(5000,)]


def test_service_accounts_have_their_own_passwords_and_narrow_roles(topo):
    loader = ch_as(topo, "svc_loader", "loader-pass")
    assert ch_denied(loader, "SELECT count() FROM finance.ledger")
    reporting = ch_as(topo, "svc_reporting", "report-pass")
    reporting.query("SELECT count() FROM finance.ledger")
    with pg_as(topo, "svc_reporting", "report-pass") as c:
        assert c.execute("SELECT count(*) FROM finance.ledger").fetchone()[0] > 0
    with pytest.raises(DatabaseError):
        ch_as(topo, "svc_loader", "nope").query("SELECT 1")
    with pytest.raises(psycopg.OperationalError):
        pg_as(topo, "svc_loader", "nope")


def test_wrong_directory_password_is_refused_by_both_engines(topo):
    with pytest.raises(DatabaseError):
        ch_as(topo, "bob", "wrong").query("SELECT 1")
    with pytest.raises(psycopg.OperationalError):
        pg_as(topo, "bob", "wrong")


# --- joiners and leavers --------------------------------------------------------------------
@pytest.fixture
def joiner(topo, ldap):
    base = topo.ldap.base_dn
    directory.add_person(ldap, base, "zed", "Zed Quill", "zed-pass")
    yield "zed"
    directory.remove_person(ldap, base, "zed")
    run("apply")


def test_a_joiner_gets_access_from_the_group_and_a_leaver_loses_it_everywhere(topo, ldap, joiner):
    base = topo.ldap.base_dn
    directory.add_member(ldap, base, joiner, "igaming_ro")
    assert run("apply") == 0

    zed = ch_as(topo, "zed", "zed-pass")
    assert zed.command("SELECT count() FROM igaming.bets") > 0
    with pg_as(topo, "zed", "zed-pass") as c:
        assert c.execute("SELECT count(*) FROM igaming.players").fetchone()[0] > 0

    # Removed from the group only: the account lives on, the access does not. No --prune needed,
    # because the directory is the authority on people.
    directory.remove_member(ldap, base, joiner, "igaming_ro")
    assert run("apply") == 0
    assert run("drift") == 0
    # Holding no role, he has no reason to exist in either database any more.
    with pytest.raises(DatabaseError):
        ch_as(topo, "zed", "zed-pass").query("SELECT 1")
    with pytest.raises(psycopg.OperationalError):
        pg_as(topo, "zed", "zed-pass")


def test_a_leaver_who_is_deleted_from_the_directory_is_dropped_from_both_databases(
    topo, ldap, joiner
):
    base = topo.ldap.base_dn
    directory.add_member(ldap, base, joiner, "igaming_rw")
    assert run("apply") == 0
    keep_open = pg_as(topo, "zed", "zed-pass")
    keep_open.execute("SELECT 1")

    directory.remove_person(ldap, base, joiner)
    assert run("apply") == 0

    admin = ch.first_alive(topo)
    assert admin.command("SELECT count() FROM system.users WHERE name = 'zed'") == 0
    with pg_as(topo, "postgres", os.environ.get("PG_ADMIN_PASSWORD", "charlie")) as c:
        assert c.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'zed'").fetchone()[0] == 0
    with pytest.raises(psycopg.OperationalError):
        keep_open.execute("SELECT 1")  # the open session was ended, not just the login
