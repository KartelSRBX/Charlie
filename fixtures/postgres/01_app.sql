-- Demo data for the PostgreSQL side of the access model: the same two business domains as in
-- ClickHouse, as schemas of one database. Objects are owned by the superuser; roles and grants
-- are applied by `charlie access apply`, never here.

CREATE SCHEMA igaming;
CREATE SCHEMA finance;

CREATE TABLE igaming.players (
    player_id bigint PRIMARY KEY,
    brand     text NOT NULL,
    country   text NOT NULL,
    created   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE finance.ledger (
    entry_id  bigserial PRIMARY KEY,
    brand     text NOT NULL,
    amount    numeric(18, 2) NOT NULL,
    booked_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO igaming.players (player_id, brand, country)
SELECT i, (ARRAY['aurora','bluefin','cobalt','dune'])[1 + i % 4], (ARRAY['RS','DE','ES','MT'])[1 + i % 4]
FROM generate_series(1, 100) AS i;

INSERT INTO finance.ledger (brand, amount)
SELECT (ARRAY['aurora','bluefin','cobalt','dune'])[1 + i % 4], (i * 10.5)::numeric(18, 2)
FROM generate_series(1, 40) AS i;
