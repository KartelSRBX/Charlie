"""Benchmarks that answer a question, not ones that produce a big number.

Everything runs in a scratch database (``bench``) that is created and dropped here, so a
benchmark never touches the demo data. Each phase asks one thing:

* ingest: what does a bigger insert batch buy, and what does ``insert_quorum = 2`` cost?
* queries: how long do typical reporting queries take, and what does doubling the data over two
  shards cost? (On one machine the shards share the same cores, so this measures the overhead of
  distribution, not a speed-up. The report says so.)
* sharding key: how much does skipping the shards that cannot hold a player save?
* replication: how long do replicas need to converge after a burst of writes?
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clickhouse_connect.driver.client import Client

from . import ch
from .loadgen import COLUMNS, make_batch
from .topology import Topology

DB = "bench"


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile of an ascending list (p in 0..100)."""
    if not sorted_values:
        return float("nan")
    k = max(0, min(len(sorted_values) - 1, math.ceil(p / 100 * len(sorted_values)) - 1))
    return sorted_values[k]


@dataclass
class IngestResult:
    label: str
    batch_size: int
    seconds: float
    rows: int
    batches: int
    errors: int
    latencies_ms: list[float] = field(repr=False, default_factory=list)

    @property
    def rows_per_s(self) -> float:
        return self.rows / self.seconds if self.seconds else 0.0

    @property
    def p50(self) -> float:
        return percentile(sorted(self.latencies_ms), 50)

    @property
    def p95(self) -> float:
        return percentile(sorted(self.latencies_ms), 95)


@dataclass
class QueryResult:
    name: str
    sql: str
    runs_ms: list[float]
    rows_read: int

    @property
    def median(self) -> float:
        return statistics.median(self.runs_ms)

    @property
    def best(self) -> float:
        return min(self.runs_ms)

    @property
    def worst(self) -> float:
        return max(self.runs_ms)


@dataclass
class Results:
    env: dict[str, str] = field(default_factory=dict)
    ingest: list[IngestResult] = field(default_factory=list)
    queries: list[QueryResult] = field(default_factory=list)
    prune: list[QueryResult] = field(default_factory=list)
    converge_seconds: float = 0.0
    dataset_rows: int = 0


def environment(topo: Topology) -> dict[str, str]:
    cpu = "?"
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            cpu = line.split(":", 1)[1].strip()
            break
    mem_kb = next(
        int(ln.split()[1])
        for ln in Path("/proc/meminfo").read_text().splitlines()
        if ln.startswith("MemTotal")
    )
    import os

    version = str(ch.first_alive(topo).command("SELECT version()"))
    return {
        "cpu": cpu,
        "cores": str(os.cpu_count()),
        "memory": f"{mem_kb / 1024 / 1024:.0f} GB",
        "clickhouse": version,
        "cluster": f"{topo.shards} shards x {topo.replicas} replicas, {topo.keepers} Keepers",
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
    }


def setup(c: Client, topo: Topology) -> None:
    cl = topo.cluster
    c.command(f"DROP DATABASE IF EXISTS {DB} ON CLUSTER '{cl}' SYNC")
    c.command(f"CREATE DATABASE {DB} ON CLUSTER '{cl}'")
    c.command(
        f"""CREATE TABLE {DB}.bets_local ON CLUSTER '{cl}'
        (bet_id UInt64, ts DateTime64(3), player_id UInt64, brand LowCardinality(String),
         game LowCardinality(String), currency LowCardinality(String),
         stake Decimal(18, 2), payout Decimal(18, 2))
        ENGINE = ReplicatedMergeTree('/clickhouse/tables/{{shard}}/{DB}/bets_local', '{{replica}}')
        PARTITION BY toYYYYMMDD(ts) ORDER BY (brand, game, ts, bet_id)"""
    )
    c.command(
        f"CREATE TABLE {DB}.bets ON CLUSTER '{cl}' AS {DB}.bets_local "
        f"ENGINE = Distributed('{cl}', {DB}, bets_local, cityHash64(player_id))"
    )


def teardown(c: Client, topo: Topology) -> None:
    c.command(f"DROP DATABASE IF EXISTS {DB} ON CLUSTER '{topo.cluster}' SYNC")


def truncate(c: Client, topo: Topology) -> None:
    c.command(f"TRUNCATE TABLE {DB}.bets_local ON CLUSTER '{topo.cluster}'")


def ingest(
    topo: Topology,
    label: str,
    batch_size: int,
    seconds: float,
    phase: int,
    workers: int = 4,
    **settings,
) -> IngestResult:
    """Write for ``seconds`` through the Distributed table; every batch is distinct."""
    eps = ch.endpoints(topo)
    lock = threading.Lock()
    counter = [0]
    lat: list[float] = []
    totals = {"rows": 0, "errors": 0}
    # A different timestamp base per phase, so a block never equals one from an earlier phase
    # (an identical block would be silently deduplicated and inflate the numbers).
    base = datetime(2026, 1, 1) + timedelta(days=phase)
    deadline = time.monotonic() + seconds

    def worker(w: int) -> None:
        client = ch.connect(
            eps[w % len(eps)].port, timeout=60, insert_distributed_sync=1, **settings
        )
        while time.monotonic() < deadline:
            with lock:
                i = counter[0]
                counter[0] += 1
            rows = make_batch(phase * 1_000_000 + i, batch_size, base + timedelta(seconds=i))
            t0 = time.perf_counter()
            try:
                client.insert(f"{DB}.bets", rows, column_names=COLUMNS)
            except Exception:
                with lock:
                    totals["errors"] += 1
                continue
            ms = (time.perf_counter() - t0) * 1000
            with lock:
                lat.append(ms)
                totals["rows"] += len(rows)

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return IngestResult(
        label, batch_size, time.monotonic() - t0, totals["rows"], len(lat), totals["errors"], lat
    )


def converge(topo: Topology) -> float:
    """Seconds until every replica has everything its shard has committed."""
    t0 = time.monotonic()
    for n in topo.nodes:
        ch.connect(n.http_port, timeout=300).command(f"SYSTEM SYNC REPLICA {DB}.bets_local")
    return time.monotonic() - t0


def generate(c: Client, topo: Topology, rows: int, chunk: int = 5_000_000) -> None:
    """A month of bets, generated by the server: 1M players, 4 brands, 25 games."""
    done = 0
    while done < rows:
        n = min(chunk, rows - done)
        c.command(
            f"INSERT INTO {DB}.bets SELECT n AS bet_id, "
            f"toDateTime64('2026-10-01 00:00:00', 3) + (cityHash64(n, 0) % 2592000) AS ts, "
            f"cityHash64(n, 1) % 1000000 AS player_id, "
            f"['aurora','bluefin','cobalt','dune'][1 + cityHash64(n, 2) % 4] AS brand, "
            f"concat('game_', toString(cityHash64(n, 3) % 25)) AS game, 'EUR' AS currency, "
            f"toDecimal64((1 + cityHash64(n, 4) % 5000) / 100, 2) AS stake, "
            f"toDecimal64(if(cityHash64(n, 5) % 100 < 46, (cityHash64(n, 6) % 20000) / 100, 0), 2) "
            f"AS payout FROM (SELECT {done} + number AS n FROM numbers({n}))",
            settings={"insert_distributed_sync": 1, "max_insert_threads": 4},
        )
        done += n
    c.command(f"SYSTEM FLUSH DISTRIBUTED {DB}.bets")


QUERIES = {
    "GGR per brand per day (scans everything)": (
        f"SELECT brand, toDate(ts) AS d, sum(stake) - sum(payout) AS ggr FROM {DB}.bets "
        f"GROUP BY brand, d ORDER BY brand, d"
    ),
    "top 10 games, last day (partition pruning)": (
        f"SELECT game, sum(stake) AS s FROM {DB}.bets "
        f"WHERE ts >= toDateTime64('2026-10-30 00:00:00', 3) GROUP BY game ORDER BY s DESC LIMIT 10"
    ),
    "distinct players per brand, exact (uniqExact)": (
        f"SELECT brand, uniqExact(player_id) FROM {DB}.bets GROUP BY brand ORDER BY brand"
    ),
    "distinct players per brand, approximate (uniq)": (
        f"SELECT brand, uniq(player_id) FROM {DB}.bets GROUP BY brand ORDER BY brand"
    ),
}
PLAYER_QUERY = (
    f"SELECT bet_id, ts, stake, payout FROM {DB}.bets WHERE player_id = 424242 "
    f"ORDER BY ts DESC LIMIT 20"
)


def timed(c: Client, sql: str, runs: int, **settings) -> tuple[list[float], int]:
    """Milliseconds per run after one warm-up, and the rows the server says it read."""
    c.query(sql, settings=settings)
    out = []
    for _ in range(runs):
        t0 = time.perf_counter()
        r = c.query(sql, settings=settings)
        out.append((time.perf_counter() - t0) * 1000)
    read = int(r.summary.get("read_rows", 0)) if getattr(r, "summary", None) else 0
    return out, read


def run(topo: Topology, rows: int = 10_000_000, seconds: float = 15, runs: int = 7, log=print):
    res = Results(env=environment(topo), dataset_rows=rows)
    c = ch.first_alive(topo)
    c = ch.connect(ch.endpoints(topo)[0].port, timeout=1800)
    setup(c, topo)
    try:
        log("ingest: batch size")
        phase = 0
        for size in (1_000, 5_000, 20_000, 100_000):
            truncate(c, topo)
            r = ingest(topo, f"batch {size:,}", size, seconds, phase)
            phase += 1
            res.ingest.append(r)
            log(
                f"  {r.label:<14} {r.rows_per_s:>10,.0f} rows/s  "
                f"p50 {r.p50:6.0f} ms  p95 {r.p95:6.0f} ms"
            )
        log("ingest: the price of insert_quorum = 2")
        for label, extra in (("quorum off", {}), ("quorum 2", {"insert_quorum": 2})):
            truncate(c, topo)
            r = ingest(topo, label, 5_000, seconds, phase, **extra)
            phase += 1
            res.ingest.append(r)
            log(
                f"  {r.label:<14} {r.rows_per_s:>10,.0f} rows/s  p50 {r.p50:6.0f} ms  "
                f"p95 {r.p95:6.0f} ms  errors {r.errors}"
            )
        truncate(c, topo)
        r = ingest(topo, "burst 20k", 20_000, seconds, phase)
        res.converge_seconds = converge(topo)
        log(f"replicas converged {res.converge_seconds:.2f}s after a {r.rows:,}-row burst")

        log(f"generating {rows:,} rows")
        truncate(c, topo)
        generate(c, topo, rows)
        converge(topo)
        total = int(c.command(f"SELECT count() FROM {DB}.bets"))
        res.dataset_rows = total
        log(f"  {total:,} rows")

        local = {n: f"{DB}.bets_local" for n in ("one shard",)}
        for name, sql in QUERIES.items():
            dist, read = timed(c, sql, runs)
            one_shard = sql.replace(f"{DB}.bets", local["one shard"])
            single, _ = timed(c, one_shard, runs)
            res.queries.append(QueryResult(name, sql, dist, read))
            res.queries.append(QueryResult(name + " [one shard]", one_shard, single, 0))
            log(
                f"  {name}: distributed {statistics.median(dist):.0f} ms, "
                f"one shard {statistics.median(single):.0f} ms"
            )
        for label, setting in (
            ("player lookup, all shards", {"optimize_skip_unused_shards": 0}),
            ("player lookup, shard pruned", {"optimize_skip_unused_shards": 1}),
        ):
            ms, read = timed(c, PLAYER_QUERY, runs, **setting)
            res.prune.append(QueryResult(label, PLAYER_QUERY, ms, read))
            log(f"  {label}: {statistics.median(ms):.0f} ms, read {read:,} rows")
    finally:
        teardown(c, topo)
    return res


def markdown(res: Results) -> str:
    e = res.env
    out = [
        "# Benchmarks",
        "",
        f"Measured {e['date']} on {e['cpu']}, {e['cores']} cores shared by every container, "
        f"{e['memory']} RAM. ClickHouse {e['clickhouse']}, {e['cluster']}, all in Docker on this "
        "one machine. Reproduce with `charlie bench`.",
        "",
        "These are numbers for a laptop-class single host. Nodes, Keepers, Prometheus and the "
        "load generator compete for the same cores and the same disk, so absolute values mean "
        "little elsewhere; the **ratios** between rows of one table are the result.",
        "",
        "## Ingest",
        "",
        "Four concurrent writers, synchronous Distributed inserts, ~15 s per row.",
        "",
        "| Case | rows/s | batch p50 (ms) | batch p95 (ms) | errors |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in res.ingest:
        out.append(
            f"| {r.label} | {r.rows_per_s:,.0f} | {r.p50:,.0f} | {r.p95:,.0f} | {r.errors} |"
        )
    out += [
        "",
        f"After a burst of writes the replicas needed {res.converge_seconds:.2f} s to converge "
        "(`SYSTEM SYNC REPLICA` on every node).",
        "",
        f"## Queries over {res.dataset_rows:,} rows",
        "",
        'Warm cache, median of the runs. "One shard" is the same query on a single '
        "`bets_local`, which holds half of the rows; the Distributed query reads both halves.",
        "",
        "| Query | Distributed (ms) | One shard (ms) | Ratio |",
        "| --- | ---: | ---: | ---: |",
    ]
    pairs = list(zip(res.queries[::2], res.queries[1::2], strict=True))
    for d, s in pairs:
        out.append(f"| {d.name} | {d.median:,.0f} | {s.median:,.0f} | {d.median / s.median:.2f}x |")
    out += [
        "",
        "## Sharding key",
        "",
        "| Lookup | median (ms) | rows read |",
        "| --- | ---: | ---: |",
    ]
    for p in res.prune:
        out.append(f"| {p.name} | {p.median:,.0f} | {p.rows_read:,} |")
    return "\n".join(out) + "\n"
