import math

from charlie import bench


def test_percentile_is_nearest_rank_and_handles_edges():
    xs = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert bench.percentile(xs, 50) == 30.0
    assert bench.percentile(xs, 95) == 50.0
    assert bench.percentile(xs, 0) == 10.0
    assert math.isnan(bench.percentile([], 50))


def test_ingest_result_derives_rate_and_latency_percentiles():
    r = bench.IngestResult(
        "x",
        1000,
        seconds=2.0,
        rows=10_000,
        batches=10,
        errors=0,
        latencies_ms=[5.0, 1.0, 3.0, 2.0, 4.0, 6.0, 7.0, 8.0, 9.0, 10.0],
    )
    assert r.rows_per_s == 5000
    assert r.p50 == 5.0 and r.p95 == 10.0


def test_report_pairs_every_distributed_query_with_its_one_shard_twin():
    res = bench.Results(
        env={
            "date": "d",
            "cpu": "c",
            "cores": "4",
            "memory": "16 GB",
            "clickhouse": "24.8",
            "cluster": "2x2",
        },
        queries=[
            bench.QueryResult("q", "s", [20.0, 22.0, 24.0], 0),
            bench.QueryResult("q [one shard]", "s", [10.0, 11.0, 12.0], 0),
        ],
        prune=[bench.QueryResult("lookup", "s", [5.0], 100)],
        dataset_rows=1000,
    )
    text = bench.markdown(res)
    assert "| q | 22 | 11 | 2.00x |" in text
    assert "| lookup | 5 | 100 |" in text
    assert "ratios" in text  # the report says what to take from it


def test_generated_queries_stay_inside_the_scratch_database():
    for sql in [*bench.QUERIES.values(), bench.PLAYER_QUERY]:
        assert bench.DB + ".bets" in sql and "igaming" not in sql
