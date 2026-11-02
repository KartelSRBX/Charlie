-- Bets of a white-label casino: the fact table of the demo workload.
--
-- bets_local  : one copy per shard, replicated between that shard's replicas through Keeper.
-- bets        : the Distributed table everybody queries and inserts into. Rows are routed to a
--               shard by a hash of player_id, so one player always lives on one shard and
--               per-player queries touch only that shard's data.

CREATE DATABASE IF NOT EXISTS igaming ON CLUSTER '{cluster}';

CREATE TABLE igaming.bets_local ON CLUSTER '{cluster}'
(
    bet_id    UInt64,
    ts        DateTime64(3),
    player_id UInt64,
    brand     LowCardinality(String),
    game      LowCardinality(String),
    currency  LowCardinality(String),
    stake     Decimal(18, 2),
    payout    Decimal(18, 2)
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/igaming/bets_local', '{replica}')
PARTITION BY toYYYYMMDD(ts)
ORDER BY (brand, game, ts, bet_id);

CREATE TABLE igaming.bets ON CLUSTER '{cluster}' AS igaming.bets_local
ENGINE = Distributed('{cluster}', igaming, bets_local, cityHash64(player_id));
