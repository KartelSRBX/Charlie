"""Backups to object storage and the restore drill that proves they work.

A backup nobody has restored is a hope. Everything here goes through ClickHouse's own
``BACKUP`` / ``RESTORE`` with ``ON CLUSTER``: for replicated tables one replica per shard writes
the data, so the bucket holds each row once, not once per replica. Credentials are in the server
config (matched by URL prefix), so no statement and no log line carries a secret.

Access control is deliberately not backed up: it is rebuilt from ``access/model.yaml`` and the
directory with ``charlie access apply``, which is the only source that can be reviewed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from clickhouse_connect.driver.client import Client

from . import ch, status
from .loadgen import LoadGenerator
from .topology import Topology

DATABASES = ("igaming", "finance", "charlie_meta")


@dataclass
class Result:
    name: str
    seconds: float
    stored_bytes: int
    files: int
    base: str | None = None


@dataclass
class Fingerprint:
    """Cheap proof that two copies of the data are the same data."""

    bets: int
    bets_hash: int
    ledger: int
    ledger_hash: int
    migrations: int

    def __str__(self) -> str:
        return (
            f"bets={self.bets} hash={self.bets_hash:016x} ledger={self.ledger} "
            f"hash={self.ledger_hash:016x} migrations={self.migrations}"
        )


@dataclass
class Report:
    rows_full: int = 0
    rows_incremental: int = 0
    full: Result | None = None
    incremental: Result | None = None
    restore_seconds: float = 0.0
    restore_full_seconds: float = 0.0
    fingerprints: dict[str, Fingerprint] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def admin(topo: Topology) -> Client:
    """A client that waits long enough for a backup or restore to finish."""
    last: Exception | None = None
    for ep in ch.endpoints(topo):
        try:
            c = ch.connect(ep.port, timeout=1800)
            c.command("SELECT 1")
            return c
        except Exception as e:
            last = e
    raise RuntimeError(f"no ClickHouse node answers: {last}")


def url(topo: Topology, name: str) -> str:
    if topo.minio is None:
        raise RuntimeError("topology.yaml has no minio section: there is nowhere to back up to")
    return f"http://minio:9000/{topo.minio.bucket}/{name}"


def _dbs() -> str:
    return ", ".join(f"DATABASE {d}" for d in DATABASES)


def stored(client: Client, topo: Topology, name: str) -> tuple[int, int]:
    """(bytes, files) a backup occupies in the bucket."""
    n, size = client.query(
        f"SELECT count(), sum(_size) FROM s3('{url(topo, name)}/**', 'One')"
    ).result_rows[0]
    return int(size or 0), int(n)


def create(client: Client, topo: Topology, name: str, base: str | None = None) -> Result:
    """Back up every database, as a full backup or an increment on top of ``base``."""
    settings = f"id = '{name}'"
    if base:
        settings += f", base_backup = S3('{url(topo, base)}')"
    t0 = time.monotonic()
    row = client.query(
        f"BACKUP {_dbs()} ON CLUSTER '{topo.cluster}' TO S3('{url(topo, name)}') "
        f"SETTINGS {settings}"
    ).result_rows[0]
    seconds = time.monotonic() - t0
    if row[1] != "BACKUP_CREATED":
        raise RuntimeError(f"backup {name} ended as {row[1]}")
    size, files = stored(client, topo, name)
    return Result(name, seconds, size, files, base)


def list_backups(client: Client, topo: Topology) -> list[tuple[str, int, int]]:
    """(name, bytes, files) of every backup in the bucket, oldest name first."""
    rows = client.query(
        f"SELECT splitByChar('/', _path)[2] AS name, sum(_size), count() "
        f"FROM s3('http://minio:9000/{topo.minio.bucket}/**', 'One') GROUP BY name ORDER BY name"
    ).result_rows
    return [(n, int(b), int(f)) for n, b, f in rows]


def drop_everything(client: Client, topo: Topology) -> None:
    """The disaster: the databases, and with them their replicated metadata in Keeper."""
    for db in DATABASES:
        client.command(f"DROP DATABASE IF EXISTS {db} ON CLUSTER '{topo.cluster}' SYNC")


def restore(client: Client, topo: Topology, name: str) -> float:
    t0 = time.monotonic()
    row = client.query(
        f"RESTORE {_dbs()} ON CLUSTER '{topo.cluster}' FROM S3('{url(topo, name)}')"
    ).result_rows[0]
    if row[1] != "RESTORED":
        raise RuntimeError(f"restore of {name} ended as {row[1]}")
    return time.monotonic() - t0


def sync_replicas(topo: Topology) -> None:
    for n in topo.nodes:
        c = ch.connect(n.http_port, timeout=120)
        for tbl in ("igaming.bets_local", "finance.ledger_local"):
            c.command(f"SYSTEM SYNC REPLICA {tbl}")


def fingerprint(client: Client) -> Fingerprint:
    bets, h = client.query(
        "SELECT count(), sum(cityHash64(bet_id, ts, player_id, brand, game, currency, stake, "
        "payout)) FROM igaming.bets"
    ).result_rows[0]
    ledger, lh = client.query(
        "SELECT count(), sum(cityHash64(entry_id, brand, booked_at, amount)) FROM finance.ledger"
    ).result_rows[0]
    return Fingerprint(
        int(bets),
        int(h or 0),
        int(ledger),
        int(lh or 0),
        int(client.command("SELECT count() FROM charlie_meta.schema_migrations")),
    )


def _load(topo: Topology, rows: int) -> None:
    c = admin(topo)
    # A second database, so that "everything came back" covers more than one table.
    c.command(
        "INSERT INTO finance.ledger (entry_id, brand, booked_at, amount) "
        "SELECT (SELECT coalesce(max(entry_id), 0) FROM finance.ledger) + number + 1, "
        "arrayElement(['aurora','bluefin','cobalt'], 1 + number % 3), now(), number / 4 "
        f"FROM numbers({max(rows // 100, 1)})"
    )
    size = 5000
    top = int(c.command("SELECT coalesce(max(bet_id) + 1, 0) FROM igaming.bets"))
    gen = LoadGenerator(topo, batch_size=size, start=-(-top // size))
    stats = gen.run(batches=-(-rows // size), duration=None, workers=4)
    if stats.failed_batches:
        raise RuntimeError(f"load failed: {stats.failed_batches}")


def drill(topo: Topology, rows: int = 200_000, log=print) -> Report:
    """Load, back up (full), load more, back up (incremental), destroy, restore, compare.

    Then destroy again and restore the *full* backup, to show that restoring an older point in
    time gives exactly the older data.
    """
    rep = Report(rows_full=rows, rows_incremental=rows // 2)
    c = admin(topo)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    full_name, inc_name = f"full-{stamp}", f"incr-{stamp}"

    log(f"loading {rows} rows")
    _load(topo, rows)
    sync_replicas(topo)
    rep.fingerprints["full"] = fingerprint(c)
    rep.full = create(c, topo, full_name)
    log(f"full backup {full_name}: {rep.full.seconds:.1f}s, {rep.full.stored_bytes:,} bytes")

    log(f"loading {rep.rows_incremental} more rows")
    _load(topo, rep.rows_incremental)
    sync_replicas(topo)
    rep.fingerprints["incremental"] = fingerprint(c)
    rep.incremental = create(c, topo, inc_name, base=full_name)
    log(
        f"incremental backup {inc_name}: {rep.incremental.seconds:.1f}s, "
        f"{rep.incremental.stored_bytes:,} bytes"
    )

    for label, name, attr in (
        ("incremental", inc_name, "restore_seconds"),
        ("full", full_name, "restore_full_seconds"),
    ):
        log(f"destroying the databases, restoring {name}")
        drop_everything(c, topo)
        setattr(rep, attr, restore(c, topo, name))
        sync_replicas(topo)
        got = fingerprint(c)
        want = rep.fingerprints[label]
        log(f"  restored in {getattr(rep, attr):.1f}s: {got}")
        if got != want:
            rep.problems.append(f"{label}: restored {got}, expected {want}")
        res = status.check_consistency(topo, expected_rows=want.bets)
        rep.problems += [f"{label}: {p}" for p in res.problems]
    return rep
