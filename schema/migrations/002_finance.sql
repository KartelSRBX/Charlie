-- A second business domain, so that access control has something to keep people out of.

CREATE DATABASE IF NOT EXISTS finance ON CLUSTER '{cluster}';

CREATE TABLE finance.ledger_local ON CLUSTER '{cluster}'
(
    entry_id  UInt64,
    brand     LowCardinality(String),
    booked_at DateTime,
    amount    Decimal(18, 2)
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/finance/ledger_local', '{replica}')
ORDER BY (brand, booked_at, entry_id);

CREATE TABLE finance.ledger ON CLUSTER '{cluster}' AS finance.ledger_local
ENGINE = Distributed('{cluster}', finance, ledger_local, cityHash64(entry_id));
