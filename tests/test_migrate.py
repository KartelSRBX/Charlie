from pathlib import Path
from types import SimpleNamespace

import pytest

from charlie import migrate
from charlie.migrate import Migration, MigrationError, discover, split_statements

ROOT = Path(__file__).resolve().parents[1]


def test_split_handles_comments_quotes_and_trailing_statement():
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE a (x String DEFAULT ';') ENGINE = Memory;
    CREATE TABLE b (y Int8) ENGINE = Memory  -- no terminator on the last one
    """
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert "DEFAULT ';'" in stmts[0]
    assert stmts[1].startswith("CREATE TABLE b")
    assert "no terminator" not in stmts[1]


def test_repo_migrations_are_valid_and_ordered():
    found = discover(ROOT / "schema" / "migrations")
    assert found, "there must be at least one migration"
    assert [m.version for m in found] == sorted(m.version for m in found)
    for m in found:
        assert m.statements
        # Every DDL must reach all nodes, or the cluster ends up with a split schema.
        for stmt in m.statements:
            if stmt.upper().startswith(("CREATE", "ALTER", "DROP")):
                assert "ON CLUSTER" in stmt, f"{m.version:03d}_{m.name}: {stmt[:60]}"


def test_bad_file_name_is_rejected(tmp_path):
    (tmp_path / "add_stuff.sql").write_text("SELECT 1")
    with pytest.raises(MigrationError, match="expected NNN"):
        discover(tmp_path)


def test_duplicate_versions_are_rejected(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1")
    (tmp_path / "001_b.sql").write_text("SELECT 2")
    with pytest.raises(MigrationError, match="duplicate"):
        discover(tmp_path)


def test_checksum_changes_with_content():
    assert Migration(1, "a", "SELECT 1").checksum != Migration(1, "a", "SELECT 2").checksum
    assert Migration(1, "a", "SELECT 1").checksum == Migration(1, "b", "SELECT 1").checksum


class FakeClient:
    def __init__(self, applied: dict[int, str]):
        self._applied = applied

    def command(self, *_a, **_k):
        return None

    def query(self, *_a, **_k):
        return SimpleNamespace(result_rows=list(self._applied.items()))


def test_plan_returns_only_unapplied_migrations():
    m1, m2 = Migration(1, "a", "SELECT 1"), Migration(2, "b", "SELECT 2")
    assert migrate.plan(FakeClient({1: m1.checksum}), [m1, m2]) == [m2]
    assert migrate.plan(FakeClient({1: m1.checksum, 2: m2.checksum}), [m1, m2]) == []


def test_plan_refuses_an_edited_applied_migration():
    m1 = Migration(1, "a", "SELECT 1")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate.plan(FakeClient({1: "deadbeef"}), [m1])


def test_plan_refuses_a_migration_older_than_the_latest_applied():
    m1, m2, m3 = (Migration(i, "x", f"SELECT {i}") for i in (1, 2, 3))
    with pytest.raises(MigrationError, match="older than the latest"):
        migrate.plan(FakeClient({1: m1.checksum, 3: m3.checksum}), [m1, m2, m3])


def test_plan_refuses_when_an_applied_file_disappeared():
    m1 = Migration(1, "a", "SELECT 1")
    with pytest.raises(MigrationError, match="missing from disk"):
        migrate.plan(FakeClient({1: m1.checksum, 2: "x"}), [m1])
