# Benchmarks

Measured 2026-11-03 on Intel(R) Xeon(R) Processor @ 2.80GHz, 4 cores shared by every container, 16 GB RAM. ClickHouse 24.8.14.39, 2 shards x 2 replicas, 3 Keepers, all in Docker on this one machine. Reproduce with `charlie bench`.

These are numbers for a laptop-class single host. Nodes, Keepers, Prometheus and the load generator compete for the same cores and the same disk, so absolute values mean little elsewhere; the **ratios** between rows of one table are the result.

## Ingest

Four concurrent writers, synchronous Distributed inserts, ~15 s per row.

| Case | rows/s | batch p50 (ms) | batch p95 (ms) | errors |
| --- | ---: | ---: | ---: | ---: |
| batch 1,000 | 30,420 | 103 | 205 | 0 |
| batch 5,000 | 59,455 | 184 | 383 | 0 |
| batch 20,000 | 72,129 | 470 | 702 | 0 |
| batch 100,000 | 69,920 | 1,615 | 1,819 | 0 |
| quorum off | 55,012 | 193 | 368 | 0 |
| quorum 2 | 47,581 | 266 | 539 | 0 |

After a burst of writes the replicas needed 0.29 s to converge (`SYSTEM SYNC REPLICA` on every node).

## Queries over 10,000,000 rows

Warm cache, median of the runs. "One shard" is the same query on a single `bets_local`, which holds half of the rows; the Distributed query reads both halves.

| Query | Distributed (ms) | One shard (ms) | Ratio |
| --- | ---: | ---: | ---: |
| GGR per brand per day (scans everything) | 340 | 176 | 1.93x |
| top 10 games, last day (partition pruning) | 26 | 18 | 1.45x |
| distinct players per brand, exact (uniqExact) | 609 | 178 | 3.43x |
| distinct players per brand, approximate (uniq) | 317 | 126 | 2.52x |

## Sharding key

| Lookup | median (ms) | rows read |
| --- | ---: | ---: |
| player lookup, all shards | 66 | 10,000,000 |
| player lookup, shard pruned | 41 | 5,003,500 |
