from datetime import datetime

from charlie.loadgen import COLUMNS, make_batch

TS = datetime(2026, 1, 1)


def test_same_batch_index_gives_identical_rows_so_a_retry_is_deduplicated():
    assert make_batch(7, 100, TS) == make_batch(7, 100, TS)


def test_different_batches_differ():
    assert make_batch(7, 100, TS) != make_batch(8, 100, TS)


def test_bet_ids_are_unique_across_batches():
    ids = [r[0] for i in range(5) for r in make_batch(i, 100, TS)]
    assert len(ids) == len(set(ids)) == 500
    assert ids == list(range(500))


def test_rows_match_the_table_columns():
    row = make_batch(0, 10, TS)[0]
    assert len(row) == len(COLUMNS)
    assert row[COLUMNS.index("stake")] > 0
    assert row[COLUMNS.index("payout")] >= 0
