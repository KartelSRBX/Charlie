"""Schema migrations for a cluster: ordered SQL files, applied once, tracked in a replicated table.

A migration file is ``NNN_name.sql`` with one or more statements. DDL should use
``ON CLUSTER '{cluster}'`` so that it reaches every shard and replica. The applied versions and
their checksums are kept in ``charlie_meta.schema_migrations``, which exists on every node, so
tampering with an already-applied file is caught.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from clickhouse_connect.driver.client import Client

META_DB = "charlie_meta"
META_TABLE = f"{META_DB}.schema_migrations"
FILE_RE = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()[:16]

    @property
    def statements(self) -> list[str]:
        return split_statements(self.sql)


def split_statements(sql: str) -> list[str]:
    """Split on ``;`` outside of quotes, dropping ``--`` comments. Enough for DDL files."""
    out, buf, quote = [], [], None
    for line in sql.splitlines():
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                buf.append(ch)
                if ch == "\\" and i + 1 < len(line):
                    buf.append(line[i + 1])
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in "'\"`":
                quote = ch
                buf.append(ch)
            elif ch == "-" and line[i : i + 2] == "--":
                break
            elif ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    out.append(stmt)
                buf = []
            else:
                buf.append(ch)
            i += 1
        buf.append("\n")
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def discover(directory: Path) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        m = FILE_RE.match(path.name)
        if not m:
            raise MigrationError(f"{path.name}: expected NNN_snake_case_name.sql")
        found.append(Migration(int(m[1]), m[2], path.read_text()))
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions in {directory}: {sorted(versions)}")
    return sorted(found, key=lambda m: m.version)


def ensure_meta(client: Client) -> None:
    client.command(f"CREATE DATABASE IF NOT EXISTS {META_DB} ON CLUSTER '{{cluster}}'")
    # One table, replicated across *all* nodes (no {shard} in the path).
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {META_TABLE} ON CLUSTER '{{cluster}}'
        (
            version UInt32,
            name String,
            checksum String,
            applied_at DateTime DEFAULT now()
        )
        ENGINE = ReplicatedMergeTree(
            '/clickhouse/tables/{META_DB}/schema_migrations', '{{replica}}'
        )
        ORDER BY version
        """
    )


def applied(client: Client) -> dict[int, str]:
    client.command(f"SYSTEM SYNC REPLICA {META_TABLE}")
    rows = client.query(f"SELECT version, checksum FROM {META_TABLE} ORDER BY version").result_rows
    return {int(v): c for v, c in rows}


def plan(client: Client, migrations: list[Migration]) -> list[Migration]:
    """Migrations still to run. Raises if an applied file was edited after the fact."""
    done = applied(client)
    for m in migrations:
        if m.version in done and done[m.version] != m.checksum:
            raise MigrationError(
                f"migration {m.version:03d}_{m.name} was changed after it was applied "
                f"(checksum {done[m.version]} -> {m.checksum}); add a new migration instead"
            )
    missing = set(done) - {m.version for m in migrations}
    if missing:
        raise MigrationError(f"applied migrations missing from disk: {sorted(missing)}")
    pending = [m for m in migrations if m.version not in done]
    if done and pending and pending[0].version < max(done):
        raise MigrationError(f"migration {pending[0].version:03d} is older than the latest applied")
    return pending


def apply(client: Client, pending: list[Migration], log=print) -> None:
    for m in pending:
        log(f"applying {m.version:03d}_{m.name} ({len(m.statements)} statements)")
        for stmt in m.statements:
            client.command(stmt, settings={"distributed_ddl_task_timeout": 120})
        client.insert(
            META_TABLE,
            [[m.version, m.name, m.checksum]],
            column_names=["version", "name", "checksum"],
        )
